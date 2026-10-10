#!/usr/bin/env python3
"""
Run-004 Patch G3-RPC: V2 Microstructure Feature Fetch Pilot.

Fetches on-chain microstructure features for the run004 canonical V2-only dataset
via archive RPC. Default: PILOT mode (200 rows). Full run is NOT implemented here —
it requires a separate approval gate.

Environment:
  LABEL_RPC_URL / LIVE_BASE_RPC_URL / EVM_HTTP_URL   (archive RPC, required)
  RUN004_G3_RPC_PILOT_ROWS=200        rows to fetch in pilot (default 200)
  RUN004_G3_RPC_FULL=0                full run gate (must stay 0 here)
  RUN004_G3_RPC_RESUME=1              skip already-fetched rows
  RUN004_G3_RPC_ABORT_ON_429=1        abort immediately on HTTP 429
  RUN004_G3_RPC_DELAY_MS=55           ms between RPC calls (default 55)
  LABEL_NOTIONAL_WEI=100000000000000000   notional trade size in wei (0.1 ETH)

Outputs:
  logs/run004_patch_g3_rpc_features_pilot.csv
  mvp/diagnostics/run004_patch_g3_rpc_feature_pilot_report.json
  mvp/diagnostics/run004_patch_g3_rpc_feature_cache_summary.json
  mvp/runs/run-004-patch-g3-rpc-feature-pilot.md
  cache/run004_g3/  (local cache files, gitignored)
"""

import csv
import json
import math
import os
import random
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not found. Install with: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CANONICAL_CSV = ROOT / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"
OUT_CSV = ROOT / "logs" / "run004_patch_g3_rpc_features_pilot.csv"
OUT_REPORT_JSON = ROOT / "mvp" / "diagnostics" / "run004_patch_g3_rpc_feature_pilot_report.json"
OUT_CACHE_SUMMARY_JSON = ROOT / "mvp" / "diagnostics" / "run004_patch_g3_rpc_feature_cache_summary.json"
OUT_MD = ROOT / "mvp" / "runs" / "run-004-patch-g3-rpc-feature-pilot.md"
CACHE_DIR = ROOT / "cache" / "run004_g3"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WETH_BASE = "0x4200000000000000000000000000000000000006"
# Default Uniswap V2 factory on Base (from RPC plan); overridable via env
FACTORY_DEFAULT = os.environ.get("RUN004_V2_FACTORY", "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6")

# Uniswap V2 PairCreated(address indexed token0, address indexed token1, address, uint256)
PAIR_CREATED_TOPIC0 = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# ABI function selectors (keccak256 of signature, first 4 bytes)
SEL_GET_RESERVES = "0x0902f1ac"  # getReserves()
SEL_TOKEN0 = "0x0dfe1681"        # token0()
SEL_TOKEN1 = "0xd21220a7"        # token1()
SEL_FACTORY = "0xc45a0155"       # factory()

# Holdout split from Patch G2
HOLDOUT_LO_MS = int(os.environ.get("RUN004_G2_HOLDOUT_LO_MS", "1776520584003"))

# Feature columns written to the output CSV
RPC_FEATURE_COLS = [
    # Group A — reserves
    "reserve_weth_at_entry",
    "reserve_token_at_entry",
    "log_reserve_weth",
    "reserve_ratio",
    "swap_size_vs_reserve",
    "liquidity_to_trade_ratio",
    "price_impact_estimate_at_notional",
    # Group B — block context
    "block_gas_used",
    "block_gas_limit",
    "block_gas_fill_ratio",
    "block_tx_count",
    # Group C — pool age
    "pair_created_block",
    "pool_age_blocks_at_entry",
    "pool_age_sec_at_entry",
    "is_new_pool_7d",
    "is_new_pool_30d",
    # Metadata
    "weth_is_token0",
    "pool_has_weth",
    "fetch_ok_group_a",
    "fetch_ok_group_b",
    "fetch_ok_group_c",
    "fetch_error",
]

CHECKPOINT_EVERY = 50  # write caches to disk every N rows


# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------
def _get_rpc_url() -> str:
    for key in ("LABEL_RPC_URL", "LIVE_BASE_RPC_URL", "EVM_HTTP_URL"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def _config() -> dict:
    return {
        "pilot_rows": int(os.environ.get("RUN004_G3_RPC_PILOT_ROWS", "200")),
        "full_run": os.environ.get("RUN004_G3_RPC_FULL", "0").strip() == "1",
        "resume": os.environ.get("RUN004_G3_RPC_RESUME", "1").strip() != "0",
        "abort_on_429": os.environ.get("RUN004_G3_RPC_ABORT_ON_429", "1").strip() != "0",
        "delay_ms": int(os.environ.get("RUN004_G3_RPC_DELAY_MS", "55")),
        "notional_wei": int(os.environ.get("LABEL_NOTIONAL_WEI", str(10 ** 17))),
        "max_pilot_calls": int(os.environ.get("RUN004_G3_RPC_MAX_PILOT_CALLS", "1800")),
        "min_success_rate": float(os.environ.get("RUN004_G3_RPC_MIN_SUCCESS_RATE", "0.90")),
        "rpc_url": _get_rpc_url(),
        "seed": int(os.environ.get("RUN004_G3_SEED", "42")),
    }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class RateLimitAbort(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class ScriptGuardError(Exception):
    pass


# ---------------------------------------------------------------------------
# Cache management (load/save JSON dicts atomically)
# ---------------------------------------------------------------------------
class JsonCache:
    """Persistent key-value cache backed by a JSON file."""

    def __init__(self, path: Path, name: str):
        self.path = path
        self.name = name
        self._data: dict = {}
        self._hits = 0
        self._misses = 0
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def get(self, key: str):
        val = self._data.get(key)
        if val is not None:
            self._hits += 1
            return val
        self._misses += 1
        return None

    def set(self, key: str, value):
        self._data[key] = value

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._data, f)
        tmp.replace(self.path)

    def stats(self) -> dict:
        return {
            "size": len(self._data),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 4),
        }


def _make_caches() -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "pool_metadata": JsonCache(CACHE_DIR / "pool_metadata.json", "pool_metadata"),
        "block_headers": JsonCache(CACHE_DIR / "block_headers.json", "block_headers"),
        "reserves": JsonCache(CACHE_DIR / "reserves.json", "reserves"),
        "pair_created": JsonCache(CACHE_DIR / "pair_created.json", "pair_created"),
    }


def _save_all_caches(caches: dict):
    for c in caches.values():
        c.save()


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
_rpc_counts: Counter = Counter()
_consecutive_429s = 0


def _rpc_post(session: requests.Session, rpc_url: str, payload: dict,
              abort_on_429: bool, delay_ms: int) -> dict | None:
    global _consecutive_429s
    time.sleep(delay_ms / 1000.0)
    try:
        resp = session.post(rpc_url, json=payload, timeout=25)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"RPC network error: {exc}") from exc

    if resp.status_code == 429:
        _consecutive_429s += 1
        if abort_on_429 or _consecutive_429s >= 3:
            raise RateLimitAbort(f"HTTP 429 after {_consecutive_429s} consecutive attempts")
        return None

    _consecutive_429s = 0
    if resp.status_code != 200:
        raise RuntimeError(f"RPC HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if "error" in data and data["error"]:
        # Some nodes return JSONRPC error for unsupported historical calls
        raise RuntimeError(f"JSONRPC error: {data['error']}")
    return data


def _eth_call(session: requests.Session, rpc_url: str, to: str, data_hex: str,
              block_hex: str, abort_on_429: bool, delay_ms: int) -> str:
    """eth_call at a specific block. Returns hex result string."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data_hex}, block_hex],
    }
    _rpc_counts["eth_call"] += 1
    result = _rpc_post(session, rpc_url, payload, abort_on_429, delay_ms)
    if result is None:
        raise RuntimeError("eth_call returned None (rate limited)")
    return result.get("result", "0x")


def _eth_get_block(session: requests.Session, rpc_url: str, block_hex: str,
                   abort_on_429: bool, delay_ms: int) -> dict:
    """eth_getBlockByNumber(block_hex, false). Returns block dict."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getBlockByNumber",
        "params": [block_hex, False],
    }
    _rpc_counts["eth_getBlockByNumber"] += 1
    result = _rpc_post(session, rpc_url, payload, abort_on_429, delay_ms)
    if result is None:
        raise RuntimeError("eth_getBlockByNumber returned None (rate limited)")
    block = result.get("result")
    if not block:
        raise RuntimeError(f"Block {block_hex} not found")
    return block


def _eth_get_logs(session: requests.Session, rpc_url: str, address: str,
                  topics: list, from_block: str, to_block: str,
                  abort_on_429: bool, delay_ms: int) -> list:
    """eth_getLogs with at most a 10,000-block range. Returns list of log objects."""
    from_int = int(from_block, 16) if from_block.startswith("0x") else int(from_block)
    to_int = int(to_block, 16) if to_block.startswith("0x") else int(to_block)
    if to_int - from_int > 9999:
        raise RuntimeError(
            f"eth_getLogs range {to_int - from_int} exceeds 10,000. "
            "Narrow the range before calling."
        )
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getLogs",
        "params": [{
            "address": address,
            "topics": topics,
            "fromBlock": hex(from_int),
            "toBlock": hex(to_int),
        }],
    }
    _rpc_counts["eth_getLogs"] += 1
    result = _rpc_post(session, rpc_url, payload, abort_on_429, delay_ms)
    if result is None:
        raise RuntimeError("eth_getLogs returned None (rate limited)")
    return result.get("result", [])


def _eth_get_code(session: requests.Session, rpc_url: str, address: str,
                  block_hex: str, abort_on_429: bool, delay_ms: int) -> str:
    """eth_getCode at a specific block. Returns hex bytecode string."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getCode",
        "params": [address, block_hex],
    }
    _rpc_counts["eth_getCode"] += 1
    result = _rpc_post(session, rpc_url, payload, abort_on_429, delay_ms)
    if result is None:
        raise RuntimeError("eth_getCode returned None (rate limited)")
    return result.get("result", "0x")


# ---------------------------------------------------------------------------
# ABI decoding helpers
# ---------------------------------------------------------------------------
def _decode_address(hex_str: str) -> str:
    """Decode an ABI-encoded address (32 bytes, last 20 bytes = addr)."""
    raw = hex_str.lstrip("0x").lstrip("0")
    if len(raw) < 40:
        raw = raw.zfill(40)
    return "0x" + raw[-40:].lower()


def _decode_uint256(hex_str: str, offset: int = 0) -> int:
    """Decode a uint256 ABI word. Strip ONLY the 0x prefix, preserve leading zeros."""
    clean = hex_str[2:] if hex_str.startswith(("0x", "0X")) else hex_str
    pad = (64 - len(clean) % 64) % 64
    clean = "0" * pad + clean
    start = offset * 64
    return int(clean[start: start + 64] or "0", 16)


def _pad_address_topic(addr: str) -> str:
    """Pad an address to a 32-byte topic (0x + 24 zeros + 40-char address)."""
    clean = addr.lower().lstrip("0x").zfill(40)
    return "0x" + "0" * 24 + clean


# ---------------------------------------------------------------------------
# Feature fetchers
# ---------------------------------------------------------------------------
def fetch_pool_metadata(pool: str, session: requests.Session, rpc_url: str,
                        caches: dict, cfg: dict) -> dict:
    """Fetch token0, token1, weth_idx, factory for a V2 pool. Cached per pool."""
    key = pool.lower()
    cached = caches["pool_metadata"].get(key)
    if cached:
        return cached

    abort = cfg["abort_on_429"]
    delay = cfg["delay_ms"]

    # token0
    r0 = _eth_call(session, rpc_url, pool, SEL_TOKEN0, "latest", abort, delay)
    token0 = _decode_address(r0) if r0 and r0 != "0x" else ""

    # token1
    r1 = _eth_call(session, rpc_url, pool, SEL_TOKEN1, "latest", abort, delay)
    token1 = _decode_address(r1) if r1 and r1 != "0x" else ""

    # factory
    rf = _eth_call(session, rpc_url, pool, SEL_FACTORY, "latest", abort, delay)
    factory = _decode_address(rf) if rf and rf != "0x" else FACTORY_DEFAULT.lower()

    weth = WETH_BASE.lower()
    if token0 == weth:
        weth_idx = 0
    elif token1 == weth:
        weth_idx = 1
    else:
        weth_idx = -1  # non-WETH pair

    meta = {
        "token0": token0,
        "token1": token1,
        "factory": factory,
        "weth_idx": weth_idx,
    }
    caches["pool_metadata"].set(key, meta)
    return meta


def fetch_reserves_at_block(pool: str, block_num: int, session: requests.Session,
                            rpc_url: str, caches: dict, cfg: dict) -> dict:
    """Fetch getReserves() at a specific block. Cached per (pool, block)."""
    key = f"{pool.lower()}_{block_num}"
    cached = caches["reserves"].get(key)
    if cached:
        return cached

    block_hex = hex(block_num)
    abort = cfg["abort_on_429"]
    delay = cfg["delay_ms"]

    raw = _eth_call(session, rpc_url, pool, SEL_GET_RESERVES, block_hex, abort, delay)
    if not raw or raw == "0x" or len(raw) < 194:
        raise RuntimeError(f"getReserves returned short/empty result at block {block_num}")

    reserve0 = _decode_uint256(raw, 0)
    reserve1 = _decode_uint256(raw, 1)

    result = {"reserve0": reserve0, "reserve1": reserve1}
    caches["reserves"].set(key, result)
    return result


def fetch_block_header(block_num: int, session: requests.Session, rpc_url: str,
                       caches: dict, cfg: dict) -> dict:
    """Fetch gasUsed, gasLimit, txCount for a block. Cached per block."""
    key = str(block_num)
    cached = caches["block_headers"].get(key)
    if cached:
        return cached

    block_hex = hex(block_num)
    abort = cfg["abort_on_429"]
    delay = cfg["delay_ms"]

    block = _eth_get_block(session, rpc_url, block_hex, abort, delay)

    gas_used = int(block.get("gasUsed", "0x0"), 16)
    gas_limit = int(block.get("gasLimit", "0x1"), 16)
    txs = block.get("transactions", [])
    tx_count = len(txs) if isinstance(txs, list) else 0

    result = {
        "gas_used": gas_used,
        "gas_limit": gas_limit,
        "tx_count": tx_count,
    }
    caches["block_headers"].set(key, result)
    return result


def fetch_pair_creation(pool: str, meta: dict, entry_block: int,
                        session: requests.Session, rpc_url: str,
                        caches: dict, cfg: dict) -> dict:
    """Fetch PairCreated event within the last 9,999 blocks of entry_block.
    Returns {creation_block, available, is_new_pool_7d, is_new_pool_30d}.
    For pools created earlier, uses eth_getCode probes for the age flags."""
    key = pool.lower()
    cached = caches["pair_created"].get(key)
    if cached and cached.get("probed"):
        return cached

    token0 = meta.get("token0", "")
    token1 = meta.get("token1", "")
    factory = meta.get("factory", FACTORY_DEFAULT.lower())

    abort = cfg["abort_on_429"]
    delay = cfg["delay_ms"]

    # --- Narrow getLogs: last 9,999 blocks (catches recently-created pools) ---
    creation_block = None
    getLogs_reason = "not_attempted"
    if token0 and token1 and factory:
        topics = [
            PAIR_CREATED_TOPIC0,
            _pad_address_topic(token0),
            _pad_address_topic(token1),
        ]
        from_blk = max(1, entry_block - 9999)
        try:
            logs = _eth_get_logs(
                session, rpc_url, factory,
                topics, hex(from_blk), hex(entry_block),
                abort, delay
            )
            for log in logs:
                log_data = log.get("data", "0x").lstrip("0x")
                if len(log_data) >= 64:
                    pair_in_data = "0x" + log_data[24:64].lower()
                    if pair_in_data == pool.lower():
                        creation_block = int(log.get("blockNumber", "0x0"), 16)
                        break
            if creation_block is None and len(logs) == 1:
                # Fallback: single result, trust it
                creation_block = int(logs[0].get("blockNumber", "0x0"), 16)
            getLogs_reason = "ok" if creation_block is not None else "not_in_last_9999_blocks"
        except RuntimeError as exc:
            getLogs_reason = str(exc)[:120]

    # --- eth_getCode probes for age flags (per unique pool, cached) ---
    # Base block time ≈ 2s; 7d = 302,400 blocks; 30d = 1,296,000 blocks
    BLOCKS_7D = 302_400
    BLOCKS_30D = 1_296_000

    is_new_pool_7d = False
    is_new_pool_30d = False

    if creation_block is not None:
        # Exact answer from getLogs
        age_blocks = entry_block - creation_block
        is_new_pool_7d = age_blocks < BLOCKS_7D
        is_new_pool_30d = age_blocks < BLOCKS_30D
    else:
        # Probe with eth_getCode at (entry_block - BLOCKS_7D)
        probe_7d_block = max(1, entry_block - BLOCKS_7D)
        try:
            code_7d = _eth_get_code(session, rpc_url, pool, hex(probe_7d_block), abort, delay)
            pool_existed_7d_ago = (code_7d not in ("0x", "0x0", "") and len(code_7d) > 10)
        except RuntimeError:
            pool_existed_7d_ago = True  # conservative: assume old pool

        if not pool_existed_7d_ago:
            is_new_pool_7d = True
            is_new_pool_30d = True
        else:
            is_new_pool_7d = False
            # Probe at (entry_block - BLOCKS_30D) only if >7d
            probe_30d_block = max(1, entry_block - BLOCKS_30D)
            try:
                code_30d = _eth_get_code(session, rpc_url, pool, hex(probe_30d_block), abort, delay)
                pool_existed_30d_ago = (code_30d not in ("0x", "0x0", "") and len(code_30d) > 10)
            except RuntimeError:
                pool_existed_30d_ago = True

            is_new_pool_30d = not pool_existed_30d_ago

    result = {
        "creation_block": creation_block,
        "available": creation_block is not None,
        "reason": getLogs_reason,
        "is_new_pool_7d": is_new_pool_7d,
        "is_new_pool_30d": is_new_pool_30d,
        "probed": True,
    }
    caches["pair_created"].set(key, result)
    return result


# ---------------------------------------------------------------------------
# Derived feature computation
# ---------------------------------------------------------------------------
def compute_features(row: dict, meta: dict, reserves: dict | None,
                     block_hdr: dict | None, creation: dict | None,
                     notional_wei: int) -> dict:
    """Compute all RPC-derived features for one row."""
    f: dict = {col: "" for col in RPC_FEATURE_COLS}

    entry_block = int(row.get("entry_block", 0) or 0)
    weth_idx = meta.get("weth_idx", -1)
    f["weth_is_token0"] = str(int(weth_idx == 0))
    f["pool_has_weth"] = str(int(weth_idx >= 0))

    # --- Group A: Reserves ---
    group_a_ok = False
    if reserves is not None:
        r0 = reserves.get("reserve0", 0) or 0
        r1 = reserves.get("reserve1", 0) or 0

        if weth_idx == 0:
            reserve_weth = r0
            reserve_token = r1
        elif weth_idx == 1:
            reserve_weth = r1
            reserve_token = r0
        else:
            # non-WETH pair: treat reserve0 as "main" for derived features
            reserve_weth = r0
            reserve_token = r1

        if reserve_weth > 0 or reserve_token > 0:
            f["reserve_weth_at_entry"] = str(reserve_weth)
            f["reserve_token_at_entry"] = str(reserve_token)
            f["log_reserve_weth"] = str(round(math.log1p(reserve_weth / 1e18), 8))

            denom_ratio = max(reserve_token, 1)
            f["reserve_ratio"] = str(round(math.log(reserve_weth / denom_ratio + 1e-9), 8))

            denom_res = max(reserve_weth, 1)
            f["swap_size_vs_reserve"] = str(round(notional_wei / denom_res, 8))
            f["liquidity_to_trade_ratio"] = str(round(reserve_weth / max(notional_wei, 1), 8))
            f["price_impact_estimate_at_notional"] = str(round(notional_wei / denom_res, 8))
            group_a_ok = True

    f["fetch_ok_group_a"] = str(int(group_a_ok))

    # --- Group B: Block context ---
    group_b_ok = False
    if block_hdr is not None:
        gas_used = block_hdr.get("gas_used", 0) or 0
        gas_limit = block_hdr.get("gas_limit", 0) or 0
        tx_count = block_hdr.get("tx_count", 0) or 0

        if gas_limit > 0:
            f["block_gas_used"] = str(gas_used)
            f["block_gas_limit"] = str(gas_limit)
            f["block_gas_fill_ratio"] = str(round(gas_used / gas_limit, 8))
            f["block_tx_count"] = str(tx_count)
            group_b_ok = True

    f["fetch_ok_group_b"] = str(int(group_b_ok))

    # --- Group C: Pool age ---
    # creation dict always has is_new_pool_7d / is_new_pool_30d from eth_getCode probes,
    # even if exact creation_block is unknown (getLogs too far back).
    group_c_ok = False
    if creation is not None and creation.get("probed"):
        is_7d = creation.get("is_new_pool_7d", False)
        is_30d = creation.get("is_new_pool_30d", False)
        f["is_new_pool_7d"] = str(int(is_7d))
        f["is_new_pool_30d"] = str(int(is_30d))
        group_c_ok = True

        cb = creation.get("creation_block") or 0
        if cb > 0 and entry_block > 0:
            age_blocks = max(entry_block - cb, 0)
            age_sec = age_blocks * 2  # Base ~2s block time
            f["pair_created_block"] = str(cb)
            f["pool_age_blocks_at_entry"] = str(age_blocks)
            f["pool_age_sec_at_entry"] = str(age_sec)

    f["fetch_ok_group_c"] = str(int(group_c_ok))

    return f


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def sample_pilot_rows(rows: list, n_pilot: int, holdout_lo_ms: int, seed: int) -> tuple:
    """Sample n_pilot rows with train/holdout balance and pool diversity.

    Strategy:
    - Split into train (ts < holdout_lo_ms) and holdout.
    - Assign each row a pool group. Cap at max_per_pool = ceil(n_pilot/2 / n_pools).
    - Sample n_half from train, n_half from holdout.
    - Record strategy metadata.
    """
    rng = random.Random(seed)

    train_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) < holdout_lo_ms]
    hold_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) >= holdout_lo_ms]

    n_half = n_pilot // 2

    def _stratified_sample(subset: list, n: int) -> list:
        if not subset:
            return []
        pools = list({r["pool_address"] for r in subset})
        n_pools = len(pools)
        max_per_pool = max(2, math.ceil(n / max(n_pools, 1)))

        by_pool: dict = defaultdict(list)
        for r in subset:
            by_pool[r["pool_address"]].append(r)

        candidates = []
        for pool in rng.sample(pools, min(n_pools, n_pools)):
            pool_rows = by_pool[pool]
            rng.shuffle(pool_rows)
            candidates.extend(pool_rows[:max_per_pool])

        rng.shuffle(candidates)
        return candidates[:n]

    train_sample = _stratified_sample(train_rows, n_half)
    hold_sample = _stratified_sample(hold_rows, n_half)

    # If either side has fewer than n_half, compensate from the other
    deficit_train = n_half - len(train_sample)
    deficit_hold = n_half - len(hold_sample)
    if deficit_train > 0:
        extra = _stratified_sample(hold_rows, deficit_train + len(hold_sample))
        extra_needed = [r for r in extra if r not in hold_sample][:deficit_train]
        hold_sample.extend(extra_needed)
    if deficit_hold > 0:
        extra = _stratified_sample(train_rows, deficit_hold + len(train_sample))
        extra_needed = [r for r in extra if r not in train_sample][:deficit_hold]
        train_sample.extend(extra_needed)

    sample = train_sample + hold_sample
    rng.shuffle(sample)

    strategy = {
        "total_canonical_rows": len(rows),
        "train_pool_count": len({r["pool_address"] for r in train_rows}),
        "holdout_pool_count": len({r["pool_address"] for r in hold_rows}),
        "train_rows_available": len(train_rows),
        "holdout_rows_available": len(hold_rows),
        "sampled_from_train": len(train_sample),
        "sampled_from_holdout": len(hold_sample),
        "unique_pools_in_sample": len({r["pool_address"] for r in sample}),
        "seed": seed,
    }
    return sample, strategy


# ---------------------------------------------------------------------------
# Resume: load already-fetched rows from output CSV
# ---------------------------------------------------------------------------
def _load_already_done(path: Path) -> set:
    """Return set of (pool_address, entry_block) already in output CSV."""
    done = set()
    if not path.exists():
        return done
    try:
        with open(path) as f:
            for r in csv.DictReader(f):
                pool = r.get("pool_address", "")
                blk = r.get("entry_block", "")
                if pool and blk:
                    done.add((pool.lower(), blk))
    except Exception:
        pass
    return done


# ---------------------------------------------------------------------------
# CSV writer helpers
# ---------------------------------------------------------------------------
_PASSTHROUGH_COLS = [
    "timestamp", "pool_address", "token_address", "log_index",
    "final_score", "label", "net_return", "entry_block",
    "tx_hash", "source_row_id",
]


def _write_rows(path: Path, rows_data: list, mode: str = "w"):
    if not rows_data:
        return
    fieldnames = _PASSTHROUGH_COLS + RPC_FEATURE_COLS
    with open(path, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows_data)


# ---------------------------------------------------------------------------
# Null-rate analysis helper
# ---------------------------------------------------------------------------
def _null_rate(completed: list, col: str) -> float:
    if not completed:
        return 1.0
    nulls = sum(1 for r in completed if not r.get(col, "").strip())
    return round(nulls / len(completed), 4)


# ---------------------------------------------------------------------------
# Main pilot execution
# ---------------------------------------------------------------------------
def run_pilot(cfg: dict) -> dict:
    """Run the 200-row pilot. Returns report dict."""
    rpc_url = cfg["rpc_url"]
    if not rpc_url:
        raise ScriptGuardError(
            "No RPC URL found. Set LABEL_RPC_URL, LIVE_BASE_RPC_URL, or EVM_HTTP_URL."
        )

    if cfg["full_run"]:
        raise ScriptGuardError(
            "RUN004_G3_RPC_FULL=1 is set. Full run is NOT implemented in this script. "
            "Run the pilot first; get approval; then use the dedicated full-fetch script."
        )

    n_pilot = cfg["pilot_rows"]
    if n_pilot > 200:
        print(f"[WARNING] Capping pilot to 200 rows (requested {n_pilot}).")
        n_pilot = 200

    # Load canonical
    print(f"[INFO] Loading {CANONICAL_CSV} ...")
    rows = []
    with open(CANONICAL_CSV) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    print(f"[INFO] Loaded {len(rows)} canonical rows.")

    # Sample
    sample, strategy = sample_pilot_rows(rows, n_pilot, HOLDOUT_LO_MS, cfg["seed"])
    print(f"[INFO] Sample: {len(sample)} rows | "
          f"train={strategy['sampled_from_train']} holdout={strategy['sampled_from_holdout']} "
          f"pools={strategy['unique_pools_in_sample']}")

    # Resume: skip already-done rows
    already_done = set()
    if cfg["resume"] and OUT_CSV.exists():
        already_done = _load_already_done(OUT_CSV)
        print(f"[INFO] Resume: {len(already_done)} rows already in output CSV.")

    caches = _make_caches()
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})

    completed: list = []
    n_ok = 0
    n_failed = 0
    abort_reason: str | None = None
    errors: list = []

    # Reload already-done into completed list
    if already_done and OUT_CSV.exists():
        try:
            with open(OUT_CSV) as f:
                for r in csv.DictReader(f):
                    key = (r.get("pool_address", "").lower(), r.get("entry_block", ""))
                    if key in already_done:
                        completed.append(r)
                        n_ok += 1
        except Exception:
            pass

    # Open output CSV (append if resuming, else create)
    write_mode = "a" if already_done else "w"
    out_file = open(OUT_CSV, write_mode, newline="")
    fieldnames = _PASSTHROUGH_COLS + RPC_FEATURE_COLS
    writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
    if write_mode == "w":
        writer.writeheader()

    try:
        for idx, row in enumerate(sample):
            pool = row["pool_address"]
            entry_block = int(row.get("entry_block", 0) or 0)
            resume_key = (pool.lower(), str(entry_block))

            if resume_key in already_done:
                continue

            # Budget check
            total_calls = sum(_rpc_counts.values())
            if total_calls > cfg["max_pilot_calls"]:
                abort_reason = f"HOLD_DUE_TO_RPC_COST: total calls {total_calls} > {cfg['max_pilot_calls']}"
                print(f"[ABORT] {abort_reason}")
                break

            if idx % 10 == 0:
                print(f"[INFO] Row {idx+1}/{len(sample)} | calls={total_calls} | "
                      f"ok={n_ok} fail={n_failed}")

            out_row = {col: row.get(col, "") for col in _PASSTHROUGH_COLS}
            out_row.update({col: "" for col in RPC_FEATURE_COLS})
            out_row["fetch_error"] = ""

            try:
                # --- Pool metadata (token0/token1) ---
                meta = fetch_pool_metadata(pool, session, rpc_url, caches, cfg)

                # --- Group A: Reserves at entry block ---
                try:
                    reserves = fetch_reserves_at_block(pool, entry_block, session, rpc_url, caches, cfg)
                except Exception as e:
                    reserves = None
                    errors.append(f"reserves [{pool[:10]}@{entry_block}]: {e}")

                # --- Group B: Block header ---
                try:
                    block_hdr = fetch_block_header(entry_block, session, rpc_url, caches, cfg)
                except Exception as e:
                    block_hdr = None
                    errors.append(f"block_header [{entry_block}]: {e}")

                # --- Group C: Pool creation (best-effort) ---
                try:
                    creation = fetch_pair_creation(pool, meta, entry_block, session, rpc_url, caches, cfg)
                except Exception as e:
                    creation = {"creation_block": None, "available": False, "reason": str(e)[:80]}
                    errors.append(f"pair_created [{pool[:10]}]: {e}")

                # Compute derived features
                features = compute_features(row, meta, reserves, block_hdr, creation, cfg["notional_wei"])
                out_row.update(features)

                row_ok = (
                    features.get("fetch_ok_group_a") == "1"
                    or features.get("fetch_ok_group_b") == "1"
                )
                if row_ok:
                    n_ok += 1
                else:
                    n_failed += 1
                    out_row["fetch_error"] = "no_group_ab_data"

            except RateLimitAbort as e:
                abort_reason = f"HOLD_DUE_TO_RPC_LIMIT: {e}"
                out_row["fetch_error"] = abort_reason
                out_row["fetch_ok_group_a"] = "0"
                out_row["fetch_ok_group_b"] = "0"
                out_row["fetch_ok_group_c"] = "0"
                writer.writerow(out_row)
                out_file.flush()
                completed.append(out_row)
                n_failed += 1
                print(f"[ABORT] {abort_reason}")
                break

            except Exception as e:
                n_failed += 1
                out_row["fetch_error"] = str(e)[:200]
                out_row["fetch_ok_group_a"] = "0"
                out_row["fetch_ok_group_b"] = "0"
                out_row["fetch_ok_group_c"] = "0"
                errors.append(f"row {idx}: {e}")
                tb = traceback.format_exc()
                print(f"[ERROR] Row {idx}: {e}")
                if "RateLimitAbort" in tb:
                    abort_reason = f"HOLD_DUE_TO_RPC_LIMIT: {e}"
                    break

            writer.writerow(out_row)
            out_file.flush()
            completed.append(out_row)

            # Checkpoint caches
            if (idx + 1) % CHECKPOINT_EVERY == 0:
                _save_all_caches(caches)
                print(f"[CHECKPOINT] Saved caches at row {idx+1}. Calls so far: {sum(_rpc_counts.values())}")

    finally:
        out_file.close()
        _save_all_caches(caches)

    # ---------------------------------------------------------------------------
    # Build report
    # ---------------------------------------------------------------------------
    total_calls = sum(_rpc_counts.values())
    n_processed = n_ok + n_failed
    success_rate = n_ok / max(n_processed, 1)

    null_rates = {col: _null_rate(completed, col) for col in RPC_FEATURE_COLS if not col.startswith("fetch_")}

    # Determine conclusion
    if abort_reason:
        if "RPC_LIMIT" in abort_reason:
            conclusion = "HOLD_DUE_TO_RPC_LIMIT"
        elif "RPC_COST" in abort_reason:
            conclusion = "HOLD_DUE_TO_RPC_COST"
        else:
            conclusion = "HOLD_DUE_TO_SCRIPT_FAILURE"
    elif total_calls > cfg["max_pilot_calls"]:
        conclusion = "HOLD_DUE_TO_RPC_COST"
    elif success_rate < cfg["min_success_rate"]:
        conclusion = "HOLD_DUE_TO_RPC_FEATURE_FETCH_FAILURE"
    else:
        # Check for schema mismatch (all reserve features null despite successful calls)
        group_a_ok_count = sum(1 for r in completed if r.get("fetch_ok_group_a") == "1")
        if group_a_ok_count == 0 and n_processed > 0:
            conclusion = "HOLD_DUE_TO_SCHEMA_MISMATCH"
        else:
            conclusion = "PATCH_G3_RPC_PILOT_GO_TO_FULL_FETCH_APPROVAL"

    calls_per_row = round(total_calls / max(n_ok, 1), 2)

    # Full-run readiness estimation
    full_run_est = _estimate_full_run(
        total_calls=total_calls,
        n_ok=n_ok,
        n_pilot=n_processed,
        full_rows=len(rows),
        caches=caches,
    )

    report = {
        "patch": "G3-RPC-Pilot",
        "conclusion": conclusion,
        "abort_reason": abort_reason,
        "pilot": {
            "requested_rows": n_pilot,
            "processed_rows": n_processed,
            "success_rows": n_ok,
            "failed_rows": n_failed,
            "success_rate": round(success_rate, 4),
            "sample_strategy": strategy,
        },
        "rpc_calls": {
            "rpc_call_count_by_method": dict(_rpc_counts),
            "total_calls": total_calls,
            "estimated_rpc_calls_per_success_row": calls_per_row,
            "pilot_budget_limit": cfg["max_pilot_calls"],
            "within_budget": total_calls <= cfg["max_pilot_calls"],
        },
        "cache_stats": {
            name: c.stats() for name, c in caches.items()
        },
        "null_rates": null_rates,
        "group_success": {
            "group_a_ok_rate": round(
                sum(1 for r in completed if r.get("fetch_ok_group_a") == "1") / max(len(completed), 1), 4
            ),
            "group_b_ok_rate": round(
                sum(1 for r in completed if r.get("fetch_ok_group_b") == "1") / max(len(completed), 1), 4
            ),
            "group_c_ok_rate": round(
                sum(1 for r in completed if r.get("fetch_ok_group_c") == "1") / max(len(completed), 1), 4
            ),
        },
        "full_run_readiness": full_run_est,
        "top_errors": errors[:20],
    }

    return report


def _estimate_full_run(total_calls: int, n_ok: int, n_pilot: int,
                       full_rows: int, caches: dict) -> dict:
    if n_ok == 0:
        return {"available": False, "reason": "no_successful_pilot_rows"}

    calls_per_row = total_calls / max(n_ok, 1)

    # Cache hit rates observed in pilot
    block_cache = caches["block_headers"]
    reserves_cache = caches["reserves"]
    pool_cache = caches["pool_metadata"]
    creation_cache = caches["pair_created"]

    # Unique blocks / pools in full dataset will be roughly proportional
    # Pilot covered ~200 rows; full = 16644. Scale up.
    scale = full_rows / max(n_pilot, 1)

    # For reserves: cache hit rate depends on repeated (pool, block) pairs
    # Since each event has a unique entry_block almost always, cache helps mostly for pools
    est_reserve_calls = int(full_rows * (1 - reserves_cache.stats()["hit_rate"] * 0.5))
    est_block_calls = int(full_rows * (1 - block_cache.stats()["hit_rate"] * 0.7))
    est_pool_meta_calls = int(
        (pool_cache.stats()["misses"] / max(n_pilot, 1)) * full_rows
        * (1 - pool_cache.stats()["hit_rate"])
    )
    est_creation_calls = int(
        (creation_cache.stats()["misses"] / max(n_pilot, 1)) * full_rows
        * (1 - creation_cache.stats()["hit_rate"])
    )

    est_total = est_reserve_calls + est_block_calls + est_pool_meta_calls * 3 + est_creation_calls
    # More realistic: use calls_per_row scaled
    est_total_v2 = int(calls_per_row * full_rows * 0.6)  # 0.6 cache factor

    est_time_20rps = round(max(est_total, est_total_v2) / 20, 1)

    return {
        "available": True,
        "estimated_full_rows": full_rows,
        "estimated_full_rpc_calls_low": est_total_v2,
        "estimated_full_rpc_calls_high": int(calls_per_row * full_rows),
        "estimated_full_time_seconds_at_20rps": est_time_20rps,
        "expected_block_cache_hit_rate": round(min(block_cache.stats()["hit_rate"] + 0.2, 0.95), 3),
        "expected_reserves_cache_hit_rate": round(min(reserves_cache.stats()["hit_rate"] + 0.05, 0.30), 3),
        "expected_pool_meta_cache_hit_rate": round(min(pool_cache.stats()["hit_rate"] + 0.50, 0.99), 3),
        "pilot_calls_per_success_row": round(calls_per_row, 2),
        "fields_ready_for_merge": [
            col for col in RPC_FEATURE_COLS
            if not col.startswith("fetch_") and not col.startswith("weth_") and not col.startswith("pool_has")
        ],
        "full_fetch_recommended": True,
    }


# ---------------------------------------------------------------------------
# Cache summary output
# ---------------------------------------------------------------------------
def _write_cache_summary(caches: dict):
    summary = {
        "cache_dir": str(CACHE_DIR),
        "caches": {
            name: {
                **c.stats(),
                "path": str(c.path),
            }
            for name, c in caches.items()
        },
    }
    OUT_CACHE_SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CACHE_SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _write_md(report: dict):
    c = report["pilot"]
    r = report["rpc_calls"]
    g = report["group_success"]
    fr = report["full_run_readiness"]
    conclusion = report["conclusion"]
    go = conclusion == "PATCH_G3_RPC_PILOT_GO_TO_FULL_FETCH_APPROVAL"

    null_block = "\n".join(
        f"| `{k}` | {v:.1%} |"
        for k, v in sorted(report["null_rates"].items(), key=lambda x: x[1])
        if not k.startswith("weth_") and not k.startswith("pool_has")
    )

    errors_block = ""
    if report.get("top_errors"):
        errors_block = "\n".join(f"- `{e}`" for e in report["top_errors"][:10])
    else:
        errors_block = "_No errors._"

    full_block = ""
    if fr.get("available"):
        full_block = f"""
### Full-Run Estimate

| Metric | Value |
|--------|-------|
| Full rows | {fr['estimated_full_rows']:,} |
| Estimated calls (low) | {fr['estimated_full_rpc_calls_low']:,} |
| Estimated calls (high) | {fr['estimated_full_rpc_calls_high']:,} |
| Estimated time @ 20 rps | {fr['estimated_full_time_seconds_at_20rps']}s |
| Pilot calls/success row | {fr['pilot_calls_per_success_row']} |
| Expected pool-meta cache hit | {fr['expected_pool_meta_cache_hit_rate']:.1%} |
| Expected block-header cache hit | {fr['expected_block_cache_hit_rate']:.1%} |
"""

    md = f"""# Run-004 Patch G3-RPC: Feature Fetch Pilot Report

**Conclusion**: `{conclusion}`  
**Go/No-Go**: {"✅ GO — recommend full-fetch approval" if go else "⛔ HOLD — see reason above"}  
{"**Abort reason**: " + report['abort_reason'] if report.get("abort_reason") else ""}

---

## Pilot Summary

| Metric | Value |
|--------|-------|
| Requested rows | {c['requested_rows']} |
| Processed rows | {c['processed_rows']} |
| Success rows | {c['success_rows']} |
| Failed rows | {c['failed_rows']} |
| Success rate | {c['success_rate']:.1%} |
| Sampled from train | {c['sample_strategy']['sampled_from_train']} |
| Sampled from holdout | {c['sample_strategy']['sampled_from_holdout']} |
| Unique pools in sample | {c['sample_strategy']['unique_pools_in_sample']} |

---

## RPC Call Counts

| Method | Calls |
|--------|-------|
{"".join(f"| `{k}` | {v} |" + chr(10) for k, v in r['rpc_call_count_by_method'].items())}| **Total** | **{r['total_calls']}** |
| Calls per success row | {r['estimated_rpc_calls_per_success_row']} |
| Budget limit | {r['pilot_budget_limit']} |
| Within budget | {'YES' if r['within_budget'] else 'NO'} |

---

## Group Fetch Success

| Group | OK Rate |
|-------|---------|
| Group A (reserves) | {g['group_a_ok_rate']:.1%} |
| Group B (block ctx) | {g['group_b_ok_rate']:.1%} |
| Group C (pool age) | {g['group_c_ok_rate']:.1%} |

---

## Null Rates (fetched features)

| Feature | Null Rate |
|---------|-----------|
{null_block}

---
{full_block}

## Top Errors (sample)

{errors_block}

---

## Conclusion

**`{conclusion}`**

{"Pilot passed all gates. Recommend user approve full fetch." if go else "Pilot did not pass gates. See abort_reason / group success rates."}

### Artifacts
- Pilot CSV: `logs/run004_patch_g3_rpc_features_pilot.csv`
- Pilot report: `mvp/diagnostics/run004_patch_g3_rpc_feature_pilot_report.json`
- Cache summary: `mvp/diagnostics/run004_patch_g3_rpc_feature_cache_summary.json`
- Cache dir: `cache/run004_g3/`

### Next Step
{"- Request explicit user approval for full fetch (G3-RPC-Full)" if go else "- Investigate failures before proceeding"}
{"- Full-run script: `scripts/fetch_run004_patch_g3_rpc_features_full.py` (to be built after approval)" if go else ""}
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write(md.strip() + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("Run-004 Patch G3-RPC: V2 Microstructure Feature Fetch Pilot")
    print("=" * 70)

    cfg = _config()

    print(f"[CONFIG] pilot_rows={cfg['pilot_rows']} | full_run={cfg['full_run']} | "
          f"resume={cfg['resume']} | abort_on_429={cfg['abort_on_429']} | "
          f"delay_ms={cfg['delay_ms']} | rpc_url_set={bool(cfg['rpc_url'])}")

    if not cfg["rpc_url"]:
        print("ERROR: No RPC URL set. Provide LABEL_RPC_URL, LIVE_BASE_RPC_URL, or EVM_HTTP_URL.")
        sys.exit(1)

    # Create output dirs
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)

    try:
        report = run_pilot(cfg)
    except ScriptGuardError as e:
        print(f"[GUARD] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INTERRUPT] User interrupted. Partial results may have been saved.")
        sys.exit(130)

    # Write report JSON
    with open(OUT_REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)

    # Write cache summary
    caches = _make_caches()  # reload to get current stats
    _write_cache_summary(caches)

    # Write markdown
    _write_md(report)

    # Summary to stdout
    print()
    print("=" * 70)
    print(f"CONCLUSION: {report['conclusion']}")
    print(f"Processed: {report['pilot']['processed_rows']} rows | "
          f"Success: {report['pilot']['success_rows']} | "
          f"Success rate: {report['pilot']['success_rate']:.1%}")
    print(f"Total RPC calls: {report['rpc_calls']['total_calls']} | "
          f"Calls/success row: {report['rpc_calls']['estimated_rpc_calls_per_success_row']}")
    print(f"Group A ok: {report['group_success']['group_a_ok_rate']:.1%} | "
          f"Group B ok: {report['group_success']['group_b_ok_rate']:.1%} | "
          f"Group C ok: {report['group_success']['group_c_ok_rate']:.1%}")
    print()
    print(f"Outputs:")
    print(f"  {OUT_CSV}")
    print(f"  {OUT_REPORT_JSON}")
    print(f"  {OUT_CACHE_SUMMARY_JSON}")
    print(f"  {OUT_MD}")
    print("=" * 70)


if __name__ == "__main__":
    main()
