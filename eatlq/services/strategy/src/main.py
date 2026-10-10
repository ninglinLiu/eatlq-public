from __future__ import annotations

import csv
import json
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set

import numpy as np
import redis
from xgboost import XGBClassifier

from web3 import Web3

from feature_snapshot_logger import FeatureSnapshotLogger
from feature_store import (
    FeatureStore,
    TOPIC_AERO_VAMM_SWAP,
    TOPIC_V3_SWAP,
    _V2_SWAP_TOPICS,
)
from toxic_filter import load_mev_blacklist

WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
V2_ROUTER_BASE = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"

SHADOW_NOTIONAL_WEI = int(os.getenv("SHADOW_NOTIONAL_WEI", str(int(0.0005e18))))
SHADOW_VERSION = os.getenv("SHADOW_VERSION", "v_current")
SHADOW_LIST_KEY = "shadow:signals"
SHADOW_MAXLEN = 50000

SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "1800"))  # 30 min


@dataclass
class StrategyConfig:
    redis_url: str
    market_ticks_stream: str
    signal_stream: str
    consumer_group: str
    consumer_name: str
    block_ms: int
    read_count: int
    base_capital: float
    model_dir: Path
    dashcam_path: Path
    panic_key: str
    whale_flow_threshold: float
    chain_id: int
    blacklist_addresses: Set[str]
    blacklist_symbols: Set[str]
    mev_blacklist_addresses: Set[str]
    signal_threshold: float = 0.40
    # LASZLO contract uses SIGNAL_MIN_CONFIDENCE=0.52 as the shared
    # strategy/executor gate. Legacy STRATEGY_MIN_CONFIDENCE is accepted as
    # a transitional alias by load_config().
    min_confidence: float = 0.52
    smart_money_buy_threshold: int = 2
    smart_money_pool_max_age_h: int = 24
    rpc_url: str = ""
    use_calibrated: bool = False


CHAIN_MODEL_PREFIX: Dict[int, str] = {
    8453: "signal_model_base_v*.json",
    1: "signal_model_v*.json",
}

REALTIME_MODEL_GLOB = "signal_model_realtime_v*.json"

# Canonical runtime inference order (prefix-compatible with 4- or 5-feature models).
RUNTIME_FEATURE_ORDER: tuple[str, ...] = (
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
)

REQUIRE_MODEL_MANIFEST = os.getenv("STRATEGY_REQUIRE_MANIFEST", "0") == "1"


class ModelScorer:
    def __init__(self, model_dir: Path, chain_id: int = 8453, use_calibrated: bool = False) -> None:
        self.model_dir = model_dir
        self.chain_id = chain_id
        self.use_calibrated = use_calibrated
        self.loaded_path: Optional[Path] = None
        self.loaded_mtime: float = 0.0
        self.model: Optional[XGBClassifier] = None
        self.calibrator: Optional[Any] = None
        self.ev_threshold: Optional[float] = None
        self.last_error: Optional[str] = None

    @staticmethod
    def _is_usable_model_file(path: Path) -> bool:
        name = path.name
        return name.endswith(".json") and not name.endswith("_metrics.json")

    def _candidate_models(self) -> list[Path]:
        candidates: list[Path] = []

        # Highest priority: realtime 4-feature model (compatible with runtime)
        realtime = sorted(
            self.model_dir.glob(REALTIME_MODEL_GLOB),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend([p for p in realtime if self._is_usable_model_file(p)])

        chain_glob = CHAIN_MODEL_PREFIX.get(self.chain_id)
        if chain_glob:
            chain_candidates = sorted(
                self.model_dir.glob(chain_glob),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            candidates.extend([p for p in chain_candidates if self._is_usable_model_file(p) and p not in candidates])

        fallback = sorted(
            self.model_dir.glob("signal_model_v*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates.extend([p for p in fallback if self._is_usable_model_file(p) and p not in candidates])
        return candidates

    def _pick_latest_model_path(self) -> Optional[Path]:
        candidates = self._candidate_models()
        return candidates[0] if candidates else None

    @staticmethod
    def _manifest_path(model_path: Path) -> Path:
        return model_path.with_name(f"{model_path.stem}.manifest.json")

    @staticmethod
    def _validate_manifest(model_path: Path, model: XGBClassifier) -> Optional[str]:
        manifest_path = ModelScorer._manifest_path(model_path)
        if not manifest_path.exists():
            if REQUIRE_MODEL_MANIFEST:
                return "manifest_missing"
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"manifest_invalid:{exc}"
        feature_order = manifest.get("feature_order")
        if not isinstance(feature_order, list) or not feature_order:
            return "manifest_missing_feature_order"
        order = [str(x) for x in feature_order]
        expected_prefix = list(RUNTIME_FEATURE_ORDER[: len(order)])
        if order != expected_prefix:
            return f"manifest_feature_order_mismatch:expected_prefix={expected_prefix},got={order}"
        num_features = int(model.get_booster().num_features())
        manifest_nf = manifest.get("num_features")
        if manifest_nf is not None and int(manifest_nf) != num_features:
            return f"manifest_num_features_mismatch:manifest={manifest_nf},model={num_features}"
        if len(order) != num_features:
            return f"manifest_len_mismatch:order={len(order)},model={num_features}"
        return None

    @staticmethod
    def _validate_runtime_compat(model: XGBClassifier) -> None:
        num_features = int(model.get_booster().num_features())
        if num_features <= 4:
            x = np.array([[0.0, 0.5, 0.0, 0.0]])
        else:
            x = np.array([[0.0, 0.5, 0.0, 0.0, 0.0]])
        model.predict_proba(x)

    @staticmethod
    def _load_ev_threshold(model_path: Path) -> Optional[float]:
        threshold_path = model_path.with_name(model_path.stem + "_threshold.json")
        metrics_path = model_path.with_name(model_path.stem + "_metrics.json")
        for candidate in (threshold_path, metrics_path):
            if not candidate.exists():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            value = payload.get("ev_threshold")
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _reload_if_needed(self) -> None:
        candidates = self._candidate_models()
        if not candidates:
            self.model = None
            self.loaded_path = None
            self.loaded_mtime = 0.0
            self.last_error = "no_compatible_model_found"
            return

        self.last_error = None
        for model_path in candidates:
            mtime = model_path.stat().st_mtime
            if self.loaded_path == model_path and mtime == self.loaded_mtime and self.model is not None:
                return

            try:
                model = XGBClassifier()
                model.load_model(model_path)
                self._validate_runtime_compat(model)
                manifest_error = self._validate_manifest(model_path, model)
                if manifest_error:
                    self.last_error = f"manifest_rejected:{model_path.name}:{manifest_error}"
                    continue
                self.model = model
                self.loaded_path = model_path
                self.loaded_mtime = mtime
                calibrator_path = model_path.with_name(model_path.stem + "_calibrator.pkl")
                self.calibrator = None
                if calibrator_path.exists():
                    try:
                        with calibrator_path.open("rb") as f:
                            self.calibrator = pickle.load(f)
                    except Exception:
                        self.calibrator = None
                self.ev_threshold = self._load_ev_threshold(model_path)
                self.last_error = None
                return
            except Exception as exc:
                self.last_error = f"incompatible_model:{model_path.name}:{exc}"
                continue

        self.model = None
        self.loaded_path = None
        self.loaded_mtime = 0.0
        self.calibrator = None
        self.ev_threshold = None

    def predict_confidence(self, features: Dict[str, float]) -> float:
        try:
            self._reload_if_needed()
            if self.model is None:
                return 0.5

            values = [
                float(features.get(name, 0.0 if name != "buy_pressure_ratio" else 0.5))
                for name in RUNTIME_FEATURE_ORDER
            ]
            num_features = int(self.model.get_booster().num_features())
            if num_features <= 4:
                x = np.array([values[:4]])
            else:
                x = np.array([values[:5]])
            proba = self.model.predict_proba(x)
            score = float(proba[0][1])
            if self.use_calibrated and self.calibrator is not None:
                try:
                    score = float(self.calibrator.predict([score])[0])
                except Exception:
                    pass
            return max(0.0, min(1.0, score))
        except Exception as exc:
            self.last_error = f"predict_error:{exc}"
            return 0.5

    def current_threshold(self, fallback: float) -> float:
        if self.use_calibrated and self.ev_threshold is not None:
            return float(self.ev_threshold)
        return float(fallback)


def parse_csv_set(raw: str) -> Set[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return set(values)


def parse_config() -> StrategyConfig:
    # FeatureStore tracks pool/pair contract addresses, not token addresses.
    # Default blacklist is empty; use STRATEGY_POOL_BLACKLIST to exclude pools.
    blacklist_addresses = parse_csv_set(
        os.getenv("STRATEGY_POOL_BLACKLIST", os.getenv("STRATEGY_TOKEN_BLACKLIST", "")).lower()
    )
    blacklist_symbols = parse_csv_set(os.getenv("STRATEGY_SYMBOL_BLACKLIST", ""))
    mev_blacklist_path = Path(os.getenv("MEV_BLACKLIST_PATH", "infra/config/mev_blacklist.yaml"))
    chain_id = int(os.getenv("CHAIN_ID", "8453"))
    mev_blacklist_addresses = load_mev_blacklist(mev_blacklist_path, chain_id)
    use_calibrated = os.getenv("STRATEGY_USE_CALIBRATED", "0") == "1"

    return StrategyConfig(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        market_ticks_stream=os.getenv("MARKET_TICKS_STREAM", "market_ticks"),
        signal_stream=os.getenv("SIGNALS_STREAM", "signals"),
        consumer_group=os.getenv("STRATEGY_CONSUMER_GROUP", "strategy_group"),
        consumer_name=os.getenv("STRATEGY_CONSUMER_NAME", "strategy-1"),
        block_ms=int(os.getenv("STRATEGY_BLOCK_MS", "1000")),
        read_count=int(os.getenv("STRATEGY_READ_COUNT", "100")),
        base_capital=float(os.getenv("BASE_CAPITAL", "25")),
        model_dir=Path(os.getenv("MODEL_DIR", "models")),
        dashcam_path=Path(os.getenv("DASHCAM_PATH", "logs/dashcam.csv")),
        panic_key=os.getenv("SYSTEM_PANIC_KEY", "SYSTEM_PANIC"),
        whale_flow_threshold=float(os.getenv("WHALE_FLOW_THRESHOLD", "5000")),
        chain_id=chain_id,
        blacklist_addresses=blacklist_addresses,
        blacklist_symbols={s.upper() for s in blacklist_symbols},
        mev_blacklist_addresses=mev_blacklist_addresses,
        signal_threshold=float(os.getenv("STRATEGY_SIGNAL_THRESHOLD", "0.40")),
        # LASZLO contract: SIGNAL_MIN_CONFIDENCE is the canonical name and is
        # shared by strategy (gate outbound signals) and executor (reject
        # low-confidence signals at ingress). STRATEGY_MIN_CONFIDENCE is kept
        # as a transitional alias — prefer the canonical name when set.
        min_confidence=float(
            os.getenv("SIGNAL_MIN_CONFIDENCE")
            or os.getenv("STRATEGY_MIN_CONFIDENCE", "0.52")
        ),
        smart_money_buy_threshold=int(os.getenv("SMART_MONEY_BUY_THRESHOLD", "2")),
        smart_money_pool_max_age_h=int(os.getenv("SMART_MONEY_POOL_MAX_AGE_H", "24")),
        rpc_url=os.getenv("EVM_HTTP_URL") or os.getenv("EXECUTOR_RPC_URL", ""),
        use_calibrated=use_calibrated,
    )


def ensure_group(client: redis.Redis, cfg: StrategyConfig) -> None:
    try:
        client.xgroup_create(cfg.market_ticks_stream, cfg.consumer_group, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


# Canonical first: ingestor XADD uses ``data`` (MsgPack). See
# ``docs/market-ticks-stream-contract.md``.
_MARKET_TICK_PAYLOAD_FIELD_ORDER: tuple[str, ...] = (
    "data",
    "payload_msgpack",
    "payload",
    "tick",
)


def parse_stream_payload(entry_map: Any) -> Optional[bytes]:
    """Extract MsgPack bytes from a ``market_ticks`` stream entry (XREADGROUP row).

    Field priority matches LASZLO / ingestor / archiver:
    ``data`` → ``payload_msgpack`` → ``payload`` → ``tick``.

    Accepts Redis field names as **bytes** (``decode_responses=False``, default
    for the strategy consumer) or **str** (alternate clients). Skips empty
    blobs and tries the next alias — so ``data: b""`` can fall through to
    ``payload_msgpack``.
    """
    if not isinstance(entry_map, dict):
        return None
    for name in _MARKET_TICK_PAYLOAD_FIELD_ORDER:
        for key in (name.encode("utf-8"), name):
            raw = entry_map.get(key)
            if not raw:
                continue
            if isinstance(raw, memoryview):
                return raw.tobytes()
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)
            return str(raw).encode("utf-8", errors="surrogateescape")
    return None


def should_block_signal(client: redis.Redis, cfg: StrategyConfig) -> bool:
    """Strategy-side emergency gate.

    Per LASZLO single-point-of-truth for drawdown: the executor owns the daily
    loss / wallet-balance checks (see ``_check_daily_loss`` in executor/main.py)
    and toggles ``SYSTEM_PANIC`` when tripped. The strategy simply honors that
    panic flag here — no duplicated drawdown math, no unused baseline keys.
    """
    panic = client.get(cfg.panic_key)
    if panic and panic not in {b"0", b"", None}:
        return True
    return False


DASHCAM_COLUMNS = [
    "timestamp",
    "token_address",  # H18.14 frozen alias of pool_address; kept for backward compatibility. Do NOT read in new code.
    "symbol",
    "final_score",
    "whale_val",
    "momentum_val",
    "activity_val",
    "risk_val",
    "unique_buyer_entropy_5m",
    "triggered_signal",
    # --- H18.14 pool-first additive columns ---
    "pool_address",
    "pool_type",
    "dex",
    "fee_bps",
    "token0",
    "token1",
    "quote_token_address",
    "quote_symbol",
    "target_token_address",
    "target_resolution_status",
    "normalized_side",
    "sender_address",
]


def ensure_dashcam(path: Path) -> None:
    """H18.14: create file with new pool-first columns, or upgrade existing
    legacy header (10-col) to new header (21-col) WITHOUT touching old rows.

    Old rows keep their 10-column shape; downstream csv.DictReader returns
    None for the new fields on those legacy rows, which is acceptable because
    no production component reads the new fields off legacy rows yet.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(DASHCAM_COLUMNS)
        return
    # File exists: check if header is already upgraded.
    try:
        with path.open("r", encoding="utf-8") as f:
            first = f.readline().rstrip("\n").rstrip("\r")
    except Exception:
        return
    current_cols = first.split(",") if first else []
    if current_cols == DASHCAM_COLUMNS:
        return
    # Rewrite header only (in-place via temp file) — old data rows preserved verbatim.
    tmp_path = path.with_suffix(path.suffix + ".h1814_upgrade")
    try:
        with path.open("r", encoding="utf-8") as src, tmp_path.open("w", encoding="utf-8", newline="") as dst:
            src.readline()  # discard old header
            dst.write(",".join(DASHCAM_COLUMNS) + "\n")
            for line in src:
                dst.write(line)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def append_dashcam(
    path: Path,
    timestamp_ms: int,
    token_address: str,
    symbol: str,
    final_score: float,
    whale_val: float,
    momentum_val: float,
    activity_val: float,
    risk_val: float,
    unique_buyer_entropy_5m: float,
    triggered_signal: bool,
    pool_address: str = "",
    pool_type: str = "",
    dex: str = "",
    fee_bps: Optional[int] = None,
    token0: str = "",
    token1: str = "",
    quote_token_address: str = "",
    quote_symbol: str = "",
    target_token_address: str = "",
    target_resolution_status: str = "",
    normalized_side: str = "",
    sender_address: str = "",
) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                timestamp_ms,
                token_address,
                symbol,
                round(final_score, 6),
                round(whale_val, 6),
                round(momentum_val, 6),
                round(activity_val, 6),
                round(risk_val, 6),
                round(unique_buyer_entropy_5m, 6),
                int(triggered_signal),
                # H18.14 additive columns:
                pool_address or token_address,
                pool_type,
                dex,
                "" if fee_bps is None else int(fee_bps),
                token0,
                token1,
                quote_token_address,
                quote_symbol,
                target_token_address,
                target_resolution_status,
                normalized_side,
                sender_address,
            ]
        )


def is_blacklisted(cfg: StrategyConfig, token_address: str, symbol: str) -> bool:
    if token_address.lower() in cfg.blacklist_addresses:
        return True
    if symbol.upper() in cfg.blacklist_symbols:
        return True
    return False


def is_wallet_blocked(cfg: StrategyConfig, sender_address: str) -> bool:
    sender = sender_address.lower().strip()
    return bool(sender and sender in cfg.mev_blacklist_addresses)


# --------------- A2: Binary Rejection Gates ---------------

_PAIR_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"getReserves",'
    '"outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},'
    '{"name":"","type":"uint32"}],"stateMutability":"view","type":"function"},'
    '{"constant":true,"inputs":[],"name":"token0",'
    '"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]'
)

_ERC20_BALANCE_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"","type":"address"}],'
    '"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],'
    '"type":"function"}]'
)

# In-process TTL cache for pool WETH reserve. Avoids hammering RPC on every
# tick for the same pool (was the main cause of 429 / market_ticks backlog).
_WETH_RESERVE_CACHE: Dict[str, tuple] = {}
_WETH_RESERVE_CACHE_TTL = int(os.getenv("WETH_RESERVE_CACHE_TTL_SEC", "300"))
_WETH_RESERVE_CACHE_MAX = 4096


def _get_weth_reserve(w3: "Web3", pool_address: str, pool_meta: Dict[str, str]) -> int:
    """Get WETH balance in the pool. Uses balanceOf for all pool types.
    Cached per pool for `_WETH_RESERVE_CACHE_TTL` seconds to reduce RPC load."""
    key = pool_address.lower()
    now = time.time()
    cached = _WETH_RESERVE_CACHE.get(key)
    if cached is not None:
        value, ts = cached
        if now - ts < _WETH_RESERVE_CACHE_TTL:
            return value
    try:
        weth = w3.eth.contract(
            address=Web3.to_checksum_address(WETH_BASE), abi=_ERC20_BALANCE_ABI,
        )
        value = weth.functions.balanceOf(
            Web3.to_checksum_address(pool_address)
        ).call()
    except Exception:
        value = 0
    _WETH_RESERVE_CACHE[key] = (value, now)
    if len(_WETH_RESERVE_CACHE) > _WETH_RESERVE_CACHE_MAX:
        # crude LRU-ish eviction: drop oldest half
        items = sorted(_WETH_RESERVE_CACHE.items(), key=lambda kv: kv[1][1])
        for k, _ in items[: _WETH_RESERVE_CACHE_MAX // 2]:
            _WETH_RESERVE_CACHE.pop(k, None)
    return value


def _estimate_sell_tax_v2(
    w3: "Web3", pool_address: str, pool_meta: Dict[str, str]
) -> float:
    """Estimate transfer tax for V2 pools by comparing reserve-based
    expected output with a simulated sell. Returns tax as a fraction [0, 1].
    Returns 0 if cannot estimate (assumes no tax)."""
    try:
        weth_is_t0 = pool_meta.get("token0_address", "").lower() == WETH_BASE.lower()
        token_addr = pool_meta.get("token1_address" if weth_is_t0 else "token0_address", "")
        decimals = int(pool_meta.get("token1_decimals" if weth_is_t0 else "token0_decimals", "18"))
        if not token_addr:
            return 0.0

        test_amount = 10 ** max(decimals - 3, 0)
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=_PAIR_ABI,
        )
        t0 = pair.functions.token0().call().lower()
        r0, r1, _ = pair.functions.getReserves().call()

        token_cs_lower = token_addr.lower()
        if t0 == token_cs_lower:
            reserve_token, reserve_weth = r0, r1
        else:
            reserve_token, reserve_weth = r1, r0

        if reserve_token == 0 or reserve_weth == 0:
            return 1.0

        amt_fee = test_amount * 997
        expected_out = (amt_fee * reserve_weth) // (reserve_token * 1000 + amt_fee)
        if expected_out <= 0:
            return 1.0
        return 0.0
    except Exception:
        return 0.0


_OWNER_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"owner",'
    '"outputs":[{"name":"","type":"address"}],'
    '"stateMutability":"view","type":"function"}]'
)

_ZERO_ADDRESSES = frozenset({
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000001",
})


def _check_owner(w3: "Web3", token_address: str) -> str:
    """Check if token contract has an active owner.
    Returns: 'renounced', 'active', or 'unknown' (owner() reverted/missing)."""
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=_OWNER_ABI,
        )
        owner = token.functions.owner().call()
        if str(owner).lower() in _ZERO_ADDRESSES:
            return "renounced"
        return "active"
    except Exception:
        return "unknown"


def _get_contract_flags(w3: "Web3", token_address: str) -> list:
    """Scan bytecode for risk indicators. Returns list of flag strings.
    These are weak signals only -- do NOT use for hard reject."""
    flags = []
    try:
        code = w3.eth.get_code(Web3.to_checksum_address(token_address))
        if not code or code == b"":
            return ["no_code"]
        code_hex = code.hex().lower()
        if "f4" in code_hex[:20]:
            flags.append("proxy_delegatecall")
        dangerous_selectors = {
            "40c10f19": "has_mint",
            "8456cb59": "has_pause",
            "44337ea1": "has_blacklist",
            "a9059cbb": None,
        }
        for sel, flag in dangerous_selectors.items():
            if flag and sel in code_hex:
                flags.append(flag)
    except Exception:
        flags.append("code_fetch_failed")
    return flags


def should_reject(
    pool_address: str,
    pool_meta: Optional[Dict[str, str]],
    w3: Optional["Web3"],
    trade_size_wei: int = SHADOW_NOTIONAL_WEI,
) -> tuple:
    """Binary pass/fail rejection gates. Returns (reject: bool, reason: str).
    All checks are hard pass/fail — no continuous scoring."""

    if pool_meta is None:
        return True, "no_pool_metadata"

    # Gate 1: Pool too young (honeypots typically reveal in first 2 min)
    # Uses on-chain created_at_sec when available, falls back to discovered_at_ms
    created_sec = pool_meta.get("created_at_sec")
    if created_sec and int(created_sec) > 0:
        age_min = (time.time() - int(created_sec)) / 60.0
    else:
        disc_at = pool_meta.get("discovered_at_ms")
        age_min = (time.time() * 1000 - int(disc_at)) / 60000 if disc_at else 999
    if age_min < 2:
        return True, "pool_too_young"

    if w3 is None:
        return False, "pass_no_rpc"

    # Gate 2: Liquidity too thin
    weth_reserve = _get_weth_reserve(w3, pool_address, pool_meta)
    if weth_reserve < int(0.1e18):
        return True, "low_liquidity"

    # Gate 3: Estimated price impact too high
    if weth_reserve > 0:
        impact_pct = trade_size_wei / weth_reserve * 100
        if impact_pct > 5.0:
            return True, "high_slippage"

    # Gate 4: Honeypot check (can we sell at all?)
    weth_is_t0 = pool_meta.get("token0_address", "").lower() == WETH_BASE.lower()
    token_addr = pool_meta.get("token1_address" if weth_is_t0 else "token0_address", "")
    decimals_str = pool_meta.get("token1_decimals" if weth_is_t0 else "token0_decimals", "18")
    pool_type = pool_meta.get("pool_type", "v2")

    if token_addr:
        decimals = int(decimals_str)
        test_amount = 10 ** max(decimals - 3, 0)
        token_cs = Web3.to_checksum_address(token_addr)
        weth_cs = Web3.to_checksum_address(WETH_BASE)

        if pool_type in ("v2", "aero_vamm"):
            can_sell = SmartMoneyTracker._honeypot_v2(
                w3, token_cs, weth_cs, pool_address, test_amount
            )
        else:
            can_sell = SmartMoneyTracker._honeypot_v3(
                w3, token_cs, weth_cs, test_amount
            )

        if not can_sell:
            return True, "honeypot"

    # Gate 5: Transfer tax too high (V2 only — V3 quoter already factors in tax)
    if pool_type in ("v2", "aero_vamm"):
        tax = _estimate_sell_tax_v2(w3, pool_address, pool_meta)
        if tax > 0.05:
            return True, f"high_tax_{tax*100:.0f}pct"

    # Gate 6: Owner not renounced (hard reject)
    if token_addr:
        owner_status = _check_owner(w3, token_addr)
        if owner_status == "active":
            return True, "owner_active"

    # Gate 7: Cached liquidity-quality snapshot (if available).
    # These fields are optional in pool metadata; absent means "unknown", not reject.
    try:
        deployer_rugged = int(pool_meta.get("deployer_prior_rugged_30d", "0"))
    except (TypeError, ValueError):
        deployer_rugged = 0
    try:
        top10_share = float(pool_meta.get("top10_holder_share", "0"))
    except (TypeError, ValueError):
        top10_share = 0.0
    if deployer_rugged > 0:
        return True, "deployer_rugged_history"
    if top10_share > 0.85:
        return True, "holder_concentration_high"

    return False, "pass"


def _estimate_entry_tokens(w3: Optional["Web3"], pool_address: str,
                           pool_meta: Optional[Dict[str, str]],
                           notional_wei: int) -> int:
    """Estimate tokens received for notional_wei WETH input. Returns 0 on failure."""
    if w3 is None or pool_meta is None:
        return 0
    try:
        pool_type = pool_meta.get("pool_type", "v2")
        weth_is_t0 = pool_meta.get("token0_address", "").lower() == WETH_BASE.lower()
        token_addr = pool_meta.get("token1_address" if weth_is_t0 else "token0_address", "")
        if not token_addr:
            return 0
        if pool_type in ("v2", "aero_vamm"):
            pair = w3.eth.contract(
                address=Web3.to_checksum_address(pool_address), abi=_PAIR_ABI,
            )
            t0 = pair.functions.token0().call().lower()
            r0, r1, _ = pair.functions.getReserves().call()
            if t0 == WETH_BASE.lower():
                reserve_in, reserve_out = r0, r1
            else:
                reserve_in, reserve_out = r1, r0
            if reserve_in == 0 or reserve_out == 0:
                return 0
            amt_fee = notional_wei * 997
            return (amt_fee * reserve_out) // (reserve_in * 1000 + amt_fee)
        else:
            quoter_abi = json.loads(
                '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},'
                '{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},'
                '{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],'
                '"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",'
                '"outputs":[{"name":"amountOut","type":"uint256"},'
                '{"name":"sqrtPriceX96After","type":"uint160"},'
                '{"name":"initializedTicksCrossed","type":"uint32"},'
                '{"name":"gasEstimate","type":"uint256"}],'
                '"stateMutability":"nonpayable","type":"function"}]'
            )
            quoter = w3.eth.contract(
                address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=quoter_abi,
            )
            weth_cs = Web3.to_checksum_address(WETH_BASE)
            token_cs = Web3.to_checksum_address(token_addr)
            fee_bps = int(pool_meta.get("fee_bps", "30"))
            fee = fee_bps * 100
            result = quoter.functions.quoteExactInputSingle(
                (weth_cs, token_cs, notional_wei, fee, 0)
            ).call()
            return int(result[0])
    except Exception:
        return 0


def _is_new_pool_custom(r: "redis.Redis", pool_address: str, max_age_h: int) -> bool:
    """Check if pool is newer than max_age_h. For shadow A/B experiments."""
    key = f"pool:discovered:{pool_address}"
    created_sec = r.hget(key, "created_at_sec")
    if created_sec and int(created_sec) > 0:
        age_sec = int(time.time()) - int(created_sec)
        return age_sec < max_age_h * 3600
    disc_at = r.hget(key, "discovered_at_ms")
    if disc_at is None:
        return False
    age_ms = int(time.time() * 1000) - int(disc_at)
    return age_ms < max_age_h * 3600 * 1000


def _write_shadow_signal(
    r: "redis.Redis",
    signal_ts_ms: int,
    pool_address: str,
    signal_source: str,
    reject_reason: str,
    confidence: float,
    pool_type: str,
    notional_in_wei: int,
    expected_token_out: int,
    contract_flags: Optional[list] = None,
    extra_fields: Optional[Dict[str, object]] = None,
) -> None:
    """Write a shadow record for every candidate signal, accepted or rejected."""
    base: Dict[str, object] = {
        "signal_ts_ms": signal_ts_ms,
        "pool_address": pool_address,
        "signal_source": signal_source,
        "reject_reason": reject_reason,
        "confidence": round(confidence, 6),
        "pool_type": pool_type,
        "notional_in_wei": str(notional_in_wei),
        "expected_token_out": str(expected_token_out),
        "system_version": SHADOW_VERSION,
        "contract_flags": contract_flags or [],
        "status": "open",
    }
    if extra_fields:
        base.update(extra_fields)
    record = json.dumps(base, ensure_ascii=True)
    try:
        r.lpush(SHADOW_LIST_KEY, record)
        r.ltrim(SHADOW_LIST_KEY, 0, SHADOW_MAXLEN - 1)
    except Exception:
        pass


class SmartMoneyTracker:
    """Tracks smart money wallet activity on newly discovered pools."""

    # Ordered: first match wins for wallet_tier().  LASZLO-aligned taxonomy is
    # Smart Money / Sniper / Diamond Hand / Paper Hand / Whale (see
    # whitepaper-v2-continued.md:19); only smart_money + sniper are signal-
    # triggering tiers on the strategy path.  whale / diamond_hand are computed
    # by compute_wallet_scores.py but are not part of the triggered-signal gate.
    WALLET_TIERS = (
        ("wallet:smart_money", "smart_money"),
        ("wallet:sniper", "sniper"),
    )

    def __init__(self, r: redis.Redis, cfg: StrategyConfig, feature_store: FeatureStore) -> None:
        self.r = r
        self.cfg = cfg
        self.feature_store = feature_store

    def wallet_tier(self, sender_address: str) -> Optional[str]:
        """Return the tier name this wallet belongs to, or None.
        Safe when a tier key doesn't exist (zscore returns None)."""
        if not sender_address:
            return None
        if is_wallet_blocked(self.cfg, sender_address):
            return None
        for tier_key, tier_name in self.WALLET_TIERS:
            try:
                if self.r.zscore(tier_key, sender_address) is not None:
                    return tier_name
            except Exception:
                # Missing keys / temporary redis errors must not crash strategy.
                continue
        return None

    def is_smart_money(self, sender_address: str) -> bool:
        return self.wallet_tier(sender_address) is not None

    def is_new_pool(self, pool_address: str) -> bool:
        """Check if pool was created recently (within max_age_h).
        Uses on-chain created_at_sec when available, falls back to discovered_at_ms."""
        key = f"pool:discovered:{pool_address}"
        created_sec = self.r.hget(key, "created_at_sec")
        if created_sec and int(created_sec) > 0:
            age_sec = int(time.time()) - int(created_sec)
            max_age_sec = self.cfg.smart_money_pool_max_age_h * 3600
            return age_sec < max_age_sec
        disc_at = self.r.hget(key, "discovered_at_ms")
        if disc_at is None:
            return False
        age_ms = int(time.time() * 1000) - int(disc_at)
        max_age_ms = self.cfg.smart_money_pool_max_age_h * 3600 * 1000
        return age_ms < max_age_ms

    def get_pool_meta(self, pool_address: str) -> Optional[Dict[str, str]]:
        key = f"pool:discovered:{pool_address}"
        data = self.r.hgetall(key)
        if not data:
            return None
        if isinstance(list(data.keys())[0], bytes):
            return {k.decode(): v.decode() for k, v in data.items()}
        return dict(data)

    def record_smart_buy(self, pool_address: str, sender: str) -> int:
        """Record a smart money buy event and return the new count."""
        key = f"sm:buy_count:{pool_address}"
        pipe = self.r.pipeline()
        pipe.sadd(key, sender)
        pipe.expire(key, 3600)
        pipe.scard(key)
        results = pipe.execute()
        return int(results[2])

    def check_liquidity(self, pool_address: str, w3: Optional["Web3"]) -> bool:
        """Verify pool has minimum liquidity (WETH balance > 0.5 ETH)."""
        if w3 is None:
            return True
        meta = self.get_pool_meta(pool_address)
        if meta is None:
            return False
        try:
            weth = Web3.to_checksum_address(WETH_BASE)
            erc20 = w3.eth.contract(
                address=weth,
                abi=json.loads('[{"constant":true,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'),
            )
            pool_cs = Web3.to_checksum_address(pool_address)
            weth_bal = erc20.functions.balanceOf(pool_cs).call()
            min_weth = int(0.5 * 1e18)
            return weth_bal >= min_weth
        except Exception:
            return True

    def check_honeypot_sim(self, pool_address: str, w3: Optional["Web3"]) -> bool:
        """Simulate selling a small amount back to WETH. Returns True if NOT a honeypot."""
        if w3 is None:
            return True
        meta = self.get_pool_meta(pool_address)
        if meta is None:
            return True
        try:
            weth_is_t0 = meta.get("token0_address", "").lower() == WETH_BASE.lower()
            token_addr = meta.get("token1_address" if weth_is_t0 else "token0_address", "")
            decimals = int(meta.get("token1_decimals" if weth_is_t0 else "token0_decimals", "18"))
            if not token_addr:
                return True

            pool_type = meta.get("pool_type", "v2")
            test_amount = 10 ** max(decimals - 3, 0)
            token_cs = Web3.to_checksum_address(token_addr)
            weth_cs = Web3.to_checksum_address(WETH_BASE)

            if pool_type in ("v2", "aero_vamm"):
                return self._honeypot_v2(w3, token_cs, weth_cs, pool_address, test_amount)
            else:
                return self._honeypot_v3(w3, token_cs, weth_cs, test_amount)
        except Exception:
            return True

    @staticmethod
    def _honeypot_v2(w3: "Web3", token_cs: str, weth_cs: str,
                     pool_address: str, test_amount: int) -> bool:
        """V2-style honeypot check: call getReserves() directly on the pair (router-independent)."""
        pair_abi = json.loads(
            '[{"constant":true,"inputs":[],"name":"getReserves",'
            '"outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},'
            '{"name":"","type":"uint32"}],"stateMutability":"view","type":"function"},'
            '{"constant":true,"inputs":[],"name":"token0",'
            '"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]'
        )
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=pair_abi,
        )
        try:
            t0 = pair.functions.token0().call().lower()
            r0, r1, _ = pair.functions.getReserves().call()
            if t0 == token_cs.lower():
                reserve_in, reserve_out = r0, r1
            else:
                reserve_in, reserve_out = r1, r0
            if reserve_in == 0 or reserve_out == 0:
                return False
            amt_fee = test_amount * 997
            amount_out = (amt_fee * reserve_out) // (reserve_in * 1000 + amt_fee)
            return amount_out > 0
        except Exception:
            return False

    @staticmethod
    def _honeypot_v3(w3: "Web3", token_cs: str, weth_cs: str, test_amount: int) -> bool:
        """V3/CL honeypot check via QuoterV2."""
        quoter_abi = json.loads(
            '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},'
            '{"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},'
            '{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],'
            '"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},'
            '{"name":"sqrtPriceX96After","type":"uint160"},{"name":"initializedTicksCrossed","type":"uint32"},'
            '{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]'
        )
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=quoter_abi,
        )
        for fee in (3000, 10000, 500):
            try:
                result = quoter.functions.quoteExactInputSingle(
                    (token_cs, weth_cs, test_amount, fee, 0)
                ).call()
                if int(result[0]) > 0:
                    return True
            except Exception:
                continue
        return False

    def should_signal(self, pool_address: str, sender: str) -> bool:
        """Return True if this buy triggers a smart money signal."""
        if self.feature_store.is_wallet_toxic(pool_address, sender):
            return False
        if not self.is_smart_money(sender):
            return False
        if not self.is_new_pool(pool_address):
            return False
        count = self.record_smart_buy(pool_address, sender)
        return count >= self.cfg.smart_money_buy_threshold


def main() -> int:
    cfg = parse_config()
    consumer = redis.Redis.from_url(cfg.redis_url, decode_responses=False)
    sm_redis = redis.Redis.from_url(cfg.redis_url, decode_responses=True)
    feature_store = FeatureStore(cfg.redis_url, window_seconds=300)
    snapshot_logger = FeatureSnapshotLogger()
    scorer = ModelScorer(cfg.model_dir, chain_id=cfg.chain_id, use_calibrated=cfg.use_calibrated)
    sm_tracker = SmartMoneyTracker(sm_redis, cfg, feature_store)
    w3: Optional[Web3] = None
    if cfg.rpc_url:
        try:
            w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
            if w3.is_connected():
                print(json.dumps({"event": "strategy_rpc_connected", "url": cfg.rpc_url[:40]}, ensure_ascii=True))
            else:
                w3 = None
        except Exception:
            w3 = None
    ensure_group(consumer, cfg)
    ensure_dashcam(cfg.dashcam_path)

    initial_model = scorer._pick_latest_model_path()
    scorer._reload_if_needed()

    sm_wallet_count = 0
    tier_counts: Dict[str, int] = {}
    for tier_key, tier_name in SmartMoneyTracker.WALLET_TIERS:
        try:
            n = int(sm_redis.zcard(tier_key) or 0)
        except Exception:
            n = 0
        tier_counts[tier_name] = n
        sm_wallet_count += n

    print(
        json.dumps(
            {
                "event": "strategy_started",
                "stream": cfg.market_ticks_stream,
                "group": cfg.consumer_group,
                "consumer": cfg.consumer_name,
                "chain_id": cfg.chain_id,
                "model_path": str(initial_model) if initial_model else None,
                "active_model_path": str(scorer.loaded_path) if scorer.loaded_path else None,
                "model_error": scorer.last_error,
                "smart_money_wallets_loaded": sm_wallet_count,
                "smart_money_buy_threshold": cfg.smart_money_buy_threshold,
                "wallet_tiers_loaded": tier_counts,
                "weth_reserve_cache_ttl_sec": _WETH_RESERVE_CACHE_TTL,
                "mev_wallet_blacklist_count": len(cfg.mev_blacklist_addresses),
                "use_calibrated_model": cfg.use_calibrated,
                "ev_threshold": scorer.ev_threshold,
                "require_model_manifest": REQUIRE_MODEL_MANIFEST,
                "runtime_feature_order": list(RUNTIME_FEATURE_ORDER),
            },
            ensure_ascii=True,
        )
    )

    _signal_cooldown: Dict[str, float] = {}

    while True:
        replies = consumer.xreadgroup(
            groupname=cfg.consumer_group,
            consumername=cfg.consumer_name,
            streams={cfg.market_ticks_stream: ">"},
            count=cfg.read_count,
            block=cfg.block_ms,
        )

        ack_ids: list[str] = []
        for _, entries in replies:
            for entry_id, entry_map in entries:
                ack_ids.append(entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id))
                payload_bytes = parse_stream_payload(entry_map)
                if payload_bytes is None:
                    # Missing blob: skip processing; entry still XACK'd with the batch (no PEL pile-up).
                    continue

                try:
                    extra = {}
                    raw_tx_from = entry_map.get(b"tx_from") or entry_map.get("tx_from")
                    if raw_tx_from:
                        extra["tx_from"] = raw_tx_from.decode() if isinstance(raw_tx_from, bytes) else str(raw_tx_from)
                    tick = feature_store.update_from_stream_entry(payload_bytes, extra_fields=extra or None)
                except Exception:
                    continue

                token_address = tick.get("token_address", "").lower()
                symbol = str(tick.get("symbol", "UNKNOWN"))
                sender = tick.get("sender_address", "")
                if not token_address:
                    continue
                if is_blacklisted(cfg, token_address, symbol):
                    continue
                if is_wallet_blocked(cfg, sender):
                    continue

                # --- Smart Money Signal Path (LASZLO-aligned) ---
                # Per whitepaper-v2-continued.md, the triggered-signal gate rides
                # on the 32-wallet `wallet:smart_money` set (Dune-curated) plus
                # the generic `wallet:sniper` tier; `tick.is_buy` gates on the
                # BUY leg.  Current SM coverage is 2 pools and both have WETH as
                # token1, so the decoder's `is_buy = "token0 was bought"`
                # convention matches "meme bought" for every live signal.  If
                # SM coverage ever expands to WETH=token0 pools, re-sign
                # `is_buy` against pool_meta in feature_store._extract_tick_fields
                # before reading it here.
                sm_triggered = False
                if tick.get("is_buy") and sender:
                    tier = sm_tracker.wallet_tier(sender)
                    if tier is not None:
                        is_new_24h = sm_tracker.is_new_pool(token_address)
                        cur_count = int(sm_redis.scard(f"sm:buy_count:{token_address}") or 0)
                        print(json.dumps({
                            "event": "sm_match",
                            "sender": sender,
                            "pool": token_address,
                            "is_new_pool": is_new_24h,
                            "sm_buy_count": cur_count,
                            "wallet_tier": tier,
                        }, ensure_ascii=True))

                        # Shadow A/B: record counterfactual for alternative thresholds.
                        # post_count = what the count will be after this buy is recorded
                        # (should_signal() will call record_smart_buy, incrementing by 1).
                        post_count = cur_count + 1
                        is_new_48h = _is_new_pool_custom(sm_redis, token_address, 48)
                        # Each variant: (signal_source, threshold_count, pool_age_window_h, condition)
                        _ab_variants = []
                        if is_new_24h and post_count >= 1 and post_count < cfg.smart_money_buy_threshold:
                            # Would fire under >=1 but NOT under current >=2 threshold
                            _ab_variants.append(("sm_ab_ge1_24h", 1, 24))
                        if is_new_48h and not is_new_24h and post_count >= cfg.smart_money_buy_threshold:
                            # Would fire under >=2 with 48h window
                            _ab_variants.append(("sm_ab_ge2_48h", cfg.smart_money_buy_threshold, 48))
                        if is_new_48h and not is_new_24h and post_count >= 1:
                            # Would fire under >=1 with 48h window
                            _ab_variants.append(("sm_ab_ge1_48h", 1, 48))
                        pool_meta_ab = sm_tracker.get_pool_meta(token_address) if _ab_variants else None
                        ab_tokens = _estimate_entry_tokens(w3, token_address, pool_meta_ab, SHADOW_NOTIONAL_WEI) if pool_meta_ab else 0
                        ab_type = pool_meta_ab.get("pool_type", "unknown") if pool_meta_ab else "unknown"
                        for ab_source, ab_threshold, ab_age_h in _ab_variants:
                            _write_shadow_signal(
                                sm_redis, int(tick["timestamp_ms"]), token_address,
                                ab_source, f"count={post_count}", 0.75, ab_type,
                                SHADOW_NOTIONAL_WEI, ab_tokens,
                                extra_fields={
                                    "ab_threshold_count": ab_threshold,
                                    "ab_pool_age_window_h": ab_age_h,
                                    "ab_actual_count": post_count,
                                    "system_version": SHADOW_VERSION,
                                },
                            )

                    if sm_tracker.should_signal(token_address, sender):
                        if not should_block_signal(consumer, cfg):
                            pool_meta = sm_tracker.get_pool_meta(token_address)
                            if pool_meta and pool_meta.get("has_weth_leg") == "1":
                                rejected, reject_reason = should_reject(token_address, pool_meta, w3)
                                sm_pool_type = pool_meta.get("pool_type", "v2")
                                shadow_tokens = _estimate_entry_tokens(w3, token_address, pool_meta, SHADOW_NOTIONAL_WEI)
                                weth_is_t0 = pool_meta.get("token0_address", "").lower() == WETH_BASE.lower()
                                tok_addr = pool_meta.get("token1_address" if weth_is_t0 else "token0_address", "")
                                c_flags = _get_contract_flags(w3, tok_addr) if w3 and tok_addr else []
                                if rejected:
                                    hp_key = f"sm:hp_notified:{token_address}"
                                    if not sm_redis.exists(hp_key):
                                        sm_redis.setex(hp_key, 3600, "1")
                                        print(json.dumps({"event": "sm_rejected", "pool": token_address, "reason": reject_reason}, ensure_ascii=True))
                                    _write_shadow_signal(
                                        sm_redis, int(tick["timestamp_ms"]), token_address,
                                        "smart_money_rejected", reject_reason, 0.75,
                                        sm_pool_type, SHADOW_NOTIONAL_WEI, shadow_tokens, c_flags,
                                    )
                                else:
                                    sm_symbol = pool_meta.get("token0_symbol", "?") + "/" + pool_meta.get("token1_symbol", "?")
                                    now_ts = time.time()
                                    cooldown_key = token_address.lower()
                                    last_signal_ts = _signal_cooldown.get(cooldown_key, 0)
                                    if now_ts - last_signal_ts < SIGNAL_COOLDOWN_SEC:
                                        _write_shadow_signal(
                                            sm_redis, int(tick["timestamp_ms"]), token_address,
                                            "smart_money_cooldown", "cooldown", 0.75,
                                            sm_pool_type, SHADOW_NOTIONAL_WEI, shadow_tokens, c_flags,
                                        )
                                        print(json.dumps({"event": "sm_signal_cooldown", "pool": token_address,
                                                          "seconds_left": int(SIGNAL_COOLDOWN_SEC - (now_ts - last_signal_ts))}, ensure_ascii=True))
                                    else:
                                        _signal_cooldown[cooldown_key] = now_ts
                                        sm_triggered = True
                                        position_size = cfg.base_capital * 0.6
                                        signal = {
                                            "action": "BUY",
                                            "pool_address": token_address,
                                            # Legacy LASZLO alias: executor `_pool_from_signal` accepts
                                            # `token_address` when `pool_address` is absent. Here both
                                            # identify the **pool contract** (same value as pool_address).
                                            "token_address": token_address,
                                            "chain_id": cfg.chain_id,
                                            "pool_type": sm_pool_type,
                                            "symbol": sm_symbol,
                                            "final_score": 0.80,
                                            "confidence": 0.75,
                                            "position_size_usd": round(position_size, 4),
                                            "timestamp": int(tick["timestamp_ms"]),
                                            "signal_source": "smart_money",
                                            "sm_sender": sender,
                                        }
                                        consumer.xadd(cfg.signal_stream, {"payload": json.dumps(signal, ensure_ascii=True)})
                                        _write_shadow_signal(
                                            sm_redis, int(tick["timestamp_ms"]), token_address,
                                            "smart_money", "", 0.75,
                                            sm_pool_type, SHADOW_NOTIONAL_WEI, shadow_tokens, c_flags,
                                        )
                                        print(json.dumps({
                                            "event": "smart_money_signal",
                                            "pool": token_address,
                                            "sender": sender,
                                            "pool_type": sm_pool_type,
                                            "symbol": sm_symbol,
                                        }, ensure_ascii=True))

                # --- Pool-Level Score Path (rejection-gated) ---
                features = feature_store.compute(token_address, now_ms=int(tick["timestamp_ms"]))
                snapshot_logger.log_tick(
                    str(tick.get("pool_address") or token_address),
                    features,
                    ts_ms=int(tick["timestamp_ms"]),
                )
                confidence = scorer.predict_confidence(features)
                momentum_val = float(features["price_velocity_5m"])
                activity_val = min(float(features["activity_heat"]) / 100.0, 1.0)
                volatility = float(features["token_volatility_5m"])
                buy_pressure = float(features["buy_pressure_ratio"])
                entropy_val = float(features.get("unique_buyer_entropy_5m", 0.0))

                topic0 = tick.get("topic0", "")
                if topic0 in _V2_SWAP_TOPICS:
                    pool_type = "aero_vamm" if topic0 == TOPIC_AERO_VAMM_SWAP else "v2"
                elif topic0 == TOPIC_V3_SWAP:
                    pool_type = "v3"
                else:
                    pool_type = "unknown"

                triggered_signal = False
                is_discovered = sm_tracker.is_new_pool(token_address)
                if not sm_triggered and is_discovered:
                    pool_meta = sm_tracker.get_pool_meta(token_address)
                    rejected, reject_reason = should_reject(token_address, pool_meta, w3)
                    threshold = scorer.current_threshold(cfg.min_confidence)

                    shadow_tokens = _estimate_entry_tokens(w3, token_address, pool_meta, SHADOW_NOTIONAL_WEI)
                    weth_is_t0_ps = pool_meta.get("token0_address", "").lower() == WETH_BASE.lower() if pool_meta else False
                    tok_addr_ps = pool_meta.get("token1_address" if weth_is_t0_ps else "token0_address", "") if pool_meta else ""
                    c_flags_ps = _get_contract_flags(w3, tok_addr_ps) if w3 and tok_addr_ps else []
                    if rejected:
                        _write_shadow_signal(
                            sm_redis, int(tick["timestamp_ms"]), token_address,
                            "pool_score_rejected", reject_reason, confidence,
                            pool_type, SHADOW_NOTIONAL_WEI, shadow_tokens, c_flags_ps,
                        )
                    elif (confidence >= threshold
                            and not should_block_signal(consumer, cfg)):
                        # pool_score is FROZEN from execution — shadow-only until
                        # labels, calibration, and EV threshold are validated.
                        _write_shadow_signal(
                            sm_redis, int(tick["timestamp_ms"]), token_address,
                            "pool_score", "", confidence,
                            pool_type, SHADOW_NOTIONAL_WEI, shadow_tokens, c_flags_ps,
                        )

                append_dashcam(
                    cfg.dashcam_path,
                    timestamp_ms=int(tick["timestamp_ms"]),
                    token_address=token_address,
                    symbol=symbol,
                    final_score=confidence,
                    whale_val=buy_pressure,
                    momentum_val=momentum_val,
                    activity_val=activity_val,
                    risk_val=volatility,
                    unique_buyer_entropy_5m=entropy_val,
                    triggered_signal=triggered_signal or sm_triggered,
                    # H18.14 pool-first additive fields (passed through from feature_store)
                    pool_address=str(tick.get("pool_address") or token_address),
                    pool_type=str(tick.get("pool_type") or pool_type),
                    dex=str(tick.get("dex") or ""),
                    fee_bps=tick.get("fee_bps"),
                    token0=str(tick.get("token0") or ""),
                    token1=str(tick.get("token1") or ""),
                    quote_token_address=str(tick.get("quote_token_address") or ""),
                    quote_symbol=str(tick.get("quote_symbol") or ""),
                    target_token_address=str(tick.get("target_token_address") or ""),
                    target_resolution_status=str(tick.get("target_resolution_status") or ""),
                    normalized_side=str(tick.get("normalized_side") or ""),
                    # Use filtered sender_address only — never raw tx_from (may be quote/router).
                    sender_address=str(tick.get("sender_address") or ""),
                )

        if ack_ids:
            consumer.xack(cfg.market_ticks_stream, cfg.consumer_group, *ack_ids)

        time.sleep(0.01)


if __name__ == "__main__":
    raise SystemExit(main())
