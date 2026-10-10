"""
EATLQ dashboard API server.

Serves a multi-route cockpit frontend, aggregates Redis runtime state, and
exposes a very small set of gated control actions.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import msgpack
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# Unified default ETH price across dashboard / executor / backtests (LASZLO).
ETH_PRICE_USD = float(os.environ.get("EXECUTOR_ETH_PRICE_USD", "3500"))
CHAIN_ID = int(os.environ.get("CHAIN_ID", "8453"))
# Opportunistic live ETH price refresh (60s TTL). See _maybe_refresh_eth_price.
ETH_PRICE_REFRESH_SEC = float(os.environ.get("DASHBOARD_ETH_PRICE_REFRESH_SEC", "60"))
ETH_PRICE_RPC_URL = os.environ.get("EXECUTOR_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
_eth_price_last_ts: float = 0.0
STATIC_DIR = Path(__file__).parent / "static"

DASHBOARD_BIND = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
DASHBOARD_BASIC_USER = os.environ.get("DASHBOARD_BASIC_USER", "").strip()
DASHBOARD_BASIC_PASS = os.environ.get("DASHBOARD_BASIC_PASS", "").strip()
DASHBOARD_ACTION_TOKEN = os.environ.get("DASHBOARD_ACTION_TOKEN", "").strip()
DASHBOARD_ALLOW_PANIC_ACTIONS = os.environ.get("DASHBOARD_ALLOW_PANIC_ACTIONS", "0").strip() == "1"
DASHBOARD_READONLY = os.environ.get("DASHBOARD_READONLY", "0").strip() == "1"
MAX_SIGNALS = int(os.environ.get("DASHBOARD_MAX_SIGNALS", "60"))
MAX_SHADOW = int(os.environ.get("DASHBOARD_MAX_SHADOW", "120"))
MAX_MARKET_TICKS = int(os.environ.get("DASHBOARD_MAX_MARKET_TICKS", "1200"))
MAX_MARKET_CANDLES = int(os.environ.get("DASHBOARD_MAX_MARKET_CANDLES", "180"))
MAX_CONCURRENT = int(os.environ.get("EXECUTOR_MAX_CONCURRENT_POSITIONS", "3"))
# Sprint078B — read-only Alpha Factory / forward race artifacts (compose volume).
ALPHA_ARTIFACT_ROOT = os.environ.get("ALPHA_ARTIFACT_ROOT", "/artifacts/audits").strip()
_ALPHA_ROOT = Path(ALPHA_ARTIFACT_ROOT)
# Sprint state dirs (compose mounts ../../logs/sprint07x → /artifacts/logs/sprint07x)
_SPRINT075_DIR = _ALPHA_ROOT.parent / "logs" / "sprint075"
_SPRINT077_DIR = _ALPHA_ROOT.parent / "logs" / "sprint077"
# Alpha factory artifact paths
_AF_DIR = _ALPHA_ROOT / "sprint074_alpha_factory"
_AF_ROLLING_DASH = _AF_DIR / "alpha_factory_rolling_dashboard.json"
_AF_WATCHLIST = _AF_DIR / "alpha_factory_dryrun_watchlist.json"
_AF_SCOREBOARD = _AF_DIR / "alpha_factory_scoreboard.json"
_AF076_LEADERBOARD = _ALPHA_ROOT / "sprint076_forward_race" / "sprint076_forward_leaderboard.json"
_AF077_SCOREBOARD = _ALPHA_ROOT / "sprint077_forward_expansion" / "scoreboard.json"
_AF077_EXPANSION = _ALPHA_ROOT / "sprint077_forward_expansion" / "sprint077_expansion_plan.json"

TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_AERO_VAMM_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WETH_BASE = "0x4200000000000000000000000000000000000006"
SHADOW_LIST_KEY = "shadow:signals"
SIGNALS_STREAM = os.environ.get("SIGNALS_STREAM", "signals")
MARKET_TICKS_STREAM = os.environ.get("MARKET_TICKS_STREAM", "market_ticks")
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123").strip()
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "eatlq").strip()
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "default").strip()
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "").strip()
DASHBOARD_INGEST_STALE_SEC = int(os.environ.get("DASHBOARD_INGEST_STALE_SEC", "600"))
DASHBOARD_ARCHIVE_STALE_SEC = int(os.environ.get("DASHBOARD_ARCHIVE_STALE_SEC", "1800"))

r_text = redis.from_url(REDIS_URL, decode_responses=True)
r_bin = redis.from_url(REDIS_URL, decode_responses=False)


def _utc_day_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _bool_to_status(flag: bool) -> str:
    return "enabled" if flag else "disabled"


def _maybe_refresh_eth_price() -> None:
    """Opportunistically refresh ``ETH_PRICE_USD`` from the executor's published
    value every 60s. The executor writes its live price to Redis key
    ``exec:eth_price_usd`` on each monitor tick, so this avoids duplicate RPC
    calls from the dashboard and keeps both services on the same number.
    Falls back silently on any error — the env default remains authoritative.
    """
    global ETH_PRICE_USD, _eth_price_last_ts
    now = time.time()
    if now - _eth_price_last_ts < ETH_PRICE_REFRESH_SEC:
        return
    _eth_price_last_ts = now
    try:
        raw = r_text.get("exec:eth_price_usd")
        if raw:
            price = float(raw)
            if 100.0 < price < 100_000.0:
                ETH_PRICE_USD = price
    except Exception:
        pass


def _wei_to_usd(wei: int) -> float:
    _maybe_refresh_eth_price()
    return (wei / 1e18) * ETH_PRICE_USD


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_epoch_seconds(value: Any) -> int:
    ts = _safe_int(value)
    if ts <= 0:
        return 0
    if ts >= 10**12:
        return ts // 1000
    return ts


def _json_load(raw: Any) -> Optional[Dict[str, Any]]:
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return json.loads(raw)
    except Exception:
        return None


def _topic_to_address(topic_hex: Any) -> str:
    cleaned = str(topic_hex or "")
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lower()
    if len(cleaned) < 40:
        return ""
    return "0x" + cleaned[-40:]


def _hex_to_uint256(hex64: str) -> int:
    return int(hex64 or "0", 16)


def _hex_to_int256(hex64: str) -> int:
    val = int(hex64 or "0", 16)
    threshold = 1 << 255
    modulus = 1 << 256
    return val - modulus if val >= threshold else val


def _normalize_msgpack_payload(payload_bytes: bytes) -> Dict[str, Any]:
    unpacked = msgpack.unpackb(payload_bytes, raw=False)
    if isinstance(unpacked, dict):
        return unpacked
    if isinstance(unpacked, list):
        # Alternating [key, value, key, value, ...] (legacy compatibility).
        if len(unpacked) % 2 == 0 and all(isinstance(unpacked[i], str) for i in range(0, len(unpacked), 2)):
            return {unpacked[i]: unpacked[i + 1] for i in range(0, len(unpacked), 2)}
        # List of 2-tuples (legacy compatibility).
        if all(isinstance(item, (list, tuple)) and len(item) == 2 for item in unpacked):
            return {str(item[0]): item[1] for item in unpacked}
    raise ValueError("unsupported msgpack payload")


def _load_pool_meta(pool_address: str) -> Dict[str, str]:
    if not pool_address:
        return {}
    try:
        return r_text.hgetall(f"pool:discovered:{pool_address.lower()}") or {}
    except Exception:
        return {}


def _short_addr(addr: str, head: int = 6, tail: int = 4) -> str:
    raw = (addr or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) < head + tail:
        return f"0x{raw}" if raw else ""
    return f"0x{raw[:head]}…{raw[-tail:]}"


def _symbol_or_addr(sym: str, token_addr: str) -> str:
    label = (sym or "").strip()
    if label and label.upper() != "UNKNOWN":
        return label
    if token_addr:
        return _short_addr(token_addr)
    return "?"


def _non_weth_token_meta(pool_meta: Dict[str, str]) -> Dict[str, Any]:
    if not pool_meta:
        return {"symbol": "UNKNOWN", "decimals": 18, "address": ""}
    t0 = pool_meta.get("token0_address", "").lower()
    if t0 == WETH_BASE:
        return {
            "address": pool_meta.get("token1_address", ""),
            "symbol": pool_meta.get("token1_symbol", "UNKNOWN"),
            "decimals": _safe_int(pool_meta.get("token1_decimals"), 18),
        }
    return {
        "address": pool_meta.get("token0_address", ""),
        "symbol": pool_meta.get("token0_symbol", "UNKNOWN"),
        "decimals": _safe_int(pool_meta.get("token0_decimals"), 18),
    }


def _pool_label(pool_address: str, pool_meta: Dict[str, str]) -> str:
    pool_bits = _short_addr(pool_address) if pool_address else ""
    if pool_meta:
        t0_sym = _symbol_or_addr(pool_meta.get("token0_symbol", ""), pool_meta.get("token0_address", ""))
        t1_sym = _symbol_or_addr(pool_meta.get("token1_symbol", ""), pool_meta.get("token1_address", ""))
        pair = f"{t0_sym}/{t1_sym}"
        pool_type = pool_meta.get("pool_type", "")
        fee_bps = pool_meta.get("fee_bps", "")
        fee_suffix = f" · {fee_bps}bps" if fee_bps else ""
        type_suffix = f" · {pool_type}" if pool_type else ""
        suffix_pool = f" · {pool_bits}" if pool_bits else ""
        return f"{pair}{type_suffix}{fee_suffix}{suffix_pool}"
    if pool_bits:
        return f"pool {pool_bits}"
    return "Unknown Pool"


def _decorate_pool_item(pool_address: str) -> Dict[str, Any]:
    meta = _load_pool_meta(pool_address)
    token_meta = _non_weth_token_meta(meta)
    return {
        "pool": pool_address,
        "label": _pool_label(pool_address, meta),
        "pool_type": meta.get("pool_type", ""),
        "fee_bps": _safe_int(meta.get("fee_bps"), 0),
        "has_weth_leg": meta.get("has_weth_leg", "0") == "1",
        "token_symbol": token_meta["symbol"],
        "token_address": token_meta["address"],
        "token_decimals": token_meta["decimals"],
        "pool_meta": meta,
    }


def _get_open_positions() -> List[Dict[str, Any]]:
    # LASZLO key scheme: ``positions:open_pools`` SET + ``positions:{pool}`` Hash.
    # Fall back to legacy ``exec:*`` keys for operators mid-migration.
    pools = r_text.smembers("positions:open_pools") or r_text.smembers("exec:open_pools") or set()
    now_ms = int(time.time() * 1000)
    out: List[Dict[str, Any]] = []
    for pool in sorted(pools):
        pos = r_text.hgetall(f"positions:{pool}")
        if not pos:
            raw = r_text.get(f"exec:pos:{pool}")
            pos = _json_load(raw)
        if not pos:
            continue
        pool_meta = _load_pool_meta(str(pos.get("pool_address", pool)))
        token_meta = _non_weth_token_meta(pool_meta)
        opened_at_ms = _safe_int(pos.get("opened_at_ms"))
        age_min = (now_ms - opened_at_ms) / 60000.0 if opened_at_ms else 0.0
        entry_wei = _safe_int(pos.get("entry_eth_wei"))
        out.append(
            {
                "pool": pos.get("pool_address", pool),
                "label": _pool_label(str(pos.get("pool_address", pool)), pool_meta),
                "pool_type": pos.get("pool_type", pool_meta.get("pool_type", "")),
                "token": pos.get("token_address", token_meta["address"]),
                "token_symbol": token_meta["symbol"],
                "token_decimals": _safe_int(pos.get("token_decimals"), token_meta["decimals"]),
                "token_amount": str(pos.get("token_amount", "0")),
                "entry_wei": str(entry_wei),
                "entry_usd": round(_wei_to_usd(entry_wei), 4),
                "age_min": round(age_min, 1),
                "buy_tx": pos.get("buy_tx_hash", ""),
                "opened_at_ms": opened_at_ms,
            }
        )
    return sorted(out, key=lambda item: item["opened_at_ms"], reverse=True)


def _get_trades() -> List[Dict[str, Any]]:
    raw_list = r_text.lrange("exec:trades", 0, 199) or []
    trades: List[Dict[str, Any]] = []
    for raw in raw_list:
        trade = _json_load(raw)
        if not trade:
            continue
        pool = str(trade.get("pool", "")).lower()
        pool_meta = _load_pool_meta(pool)
        trade["pool"] = pool
        trade["label"] = _pool_label(pool, pool_meta)
        trade["signal_source"] = trade.get("signal_source", "executor")
        trade["ts"] = _safe_int(trade.get("ts"))
        trade["pnl_usd"] = round(_safe_float(trade.get("pnl_usd")), 4)
        trades.append(trade)
    return trades


def _get_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    sells = [t for t in trades if t.get("action") == "sell" and t.get("success")]
    wins = [t for t in sells if _safe_float(t.get("pnl_usd")) > 0]
    losses = [t for t in sells if _safe_float(t.get("pnl_usd")) <= 0]
    total_trades = len(sells)
    gross_profit = sum(_safe_float(t.get("pnl_usd")) for t in wins)
    gross_loss = abs(sum(_safe_float(t.get("pnl_usd")) for t in losses))
    net_pnl = gross_profit - gross_loss
    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = sum(_safe_float(t.get("pnl_usd")) for t in losses) / len(losses) if losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win_usd": round(avg_win, 4),
        "avg_loss_usd": round(avg_loss, 4),
        "profit_factor": round(min(profit_factor, 99.0), 2),
        "gross_profit_usd": round(gross_profit, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "net_pnl_usd": round(net_pnl, 4),
    }


def _get_pnl_curve(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sells = sorted(
        [t for t in trades if t.get("action") == "sell" and t.get("success")],
        key=lambda t: _safe_int(t.get("ts")),
    )
    curve: List[Dict[str, Any]] = []
    cumulative = 0.0
    for trade in sells:
        cumulative += _safe_float(trade.get("pnl_usd"))
        ts = _safe_int(trade.get("ts"))
        curve.append({"ts": ts, "time": ts, "value": round(cumulative, 4)})
    return curve


def _build_equity_curve(base_equity_usd: float, pnl_curve: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not pnl_curve:
        return []
    return [
        {
            "ts": point["ts"],
            "time": point["time"],
            "value": round(base_equity_usd + _safe_float(point.get("value")), 4),
        }
        for point in pnl_curve
    ]


def _compute_drawdown(curve: List[Dict[str, Any]]) -> Dict[str, Any]:
    peak = 0.0
    max_drawdown = 0.0
    worst_value = 0.0
    for point in curve:
        value = _safe_float(point.get("value"))
        if value > peak:
            peak = value
        drawdown = peak - value
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            worst_value = value
    drawdown_pct = (max_drawdown / peak * 100.0) if peak > 0 else 0.0
    return {
        "drawdown_usd": round(max_drawdown, 4),
        "drawdown_pct": round(drawdown_pct, 2),
        "worst_value_usd": round(worst_value, 4),
    }


def _get_signal_entries() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        entries = r_text.xrevrange(SIGNALS_STREAM, count=MAX_SIGNALS) or []
    except Exception:
        return items
    for stream_id, fields in entries:
        signal = _json_load(fields.get("payload") or fields.get("signal_json"))
        if not signal:
            continue
        pool = str(signal.get("pool_address", "")).lower()
        pool_meta = _load_pool_meta(pool)
        signal["id"] = stream_id
        signal["pool"] = pool
        signal["label"] = _pool_label(pool, pool_meta)
        signal["timestamp"] = _normalize_epoch_seconds(signal.get("timestamp"))
        signal["position_size_usd"] = round(_safe_float(signal.get("position_size_usd")), 4)
        review_raw = r_text.get(f"dashboard:signal_review:{stream_id}")
        signal["review"] = _json_load(review_raw)
        items.append(signal)
    return items


def _get_shadow_records() -> List[Dict[str, Any]]:
    rows = r_text.lrange(SHADOW_LIST_KEY, 0, MAX_SHADOW - 1) or []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        rec = _json_load(raw)
        if not rec:
            continue
        pool = str(rec.get("pool_address", "")).lower()
        pool_meta = _load_pool_meta(pool)
        rec["pool"] = pool
        rec["label"] = _pool_label(pool, pool_meta)
        rec["signal_ts_ms"] = _safe_int(rec.get("signal_ts_ms"))
        rec["closed_at_ms"] = _safe_int(rec.get("closed_at_ms"))
        rec["confidence"] = round(_safe_float(rec.get("confidence")), 4)
        rec["net_return_pct"] = round(_safe_float(rec.get("net_return_pct")), 4)
        rec["hold_minutes"] = round(
            max(0.0, (rec["closed_at_ms"] - rec["signal_ts_ms"]) / 60000.0),
            1,
        ) if rec.get("closed_at_ms") else None
        out.append(rec)
    return out


def _get_daily_evals() -> List[Dict[str, Any]]:
    keys = sorted(r_text.scan_iter(match="exec:daily_eval:*"), reverse=True)
    evals: List[Dict[str, Any]] = []
    for key in keys[:14]:
        value = _json_load(r_text.get(key))
        if not value:
            continue
        evals.append(value)
    return evals


_MSGPACK_PAYLOAD_FIELDS = (
    b"data",
    b"payload_msgpack",
    b"payload",
    b"tick",
)


def _decode_market_tick(entry_fields: Dict[bytes, bytes]) -> Optional[Dict[str, Any]]:
    # Field order matches strategy `parse_stream_payload` /
    # ``docs/market-ticks-stream-contract.md`` (canonical ``data`` first).
    payload_bytes = None
    payload_field_key: Optional[bytes] = None
    for candidate in _MSGPACK_PAYLOAD_FIELDS:
        if candidate in entry_fields and entry_fields[candidate]:
            payload_bytes = entry_fields[candidate]
            payload_field_key = candidate
            break
    if not payload_bytes:
        return None
    try:
        payload = _normalize_msgpack_payload(payload_bytes)
    except Exception:
        return None
    extra_fields: Dict[str, Any] = {}
    for key, value in entry_fields.items():
        if key == payload_field_key:
            continue
        decoded_key = key.decode("utf-8", errors="ignore")
        try:
            extra_fields[decoded_key] = value.decode("utf-8", errors="ignore")
        except Exception:
            extra_fields[decoded_key] = ""
    payload.update({k: v for k, v in extra_fields.items() if k not in payload})
    topic0 = str(payload.get("topic0", "")).lower()
    pool = str(payload.get("address") or payload.get("token_address") or "").lower()
    timestamp_ms = _safe_int(payload.get("timestamp_ms") or payload.get("received_at_ms"))
    data = str(payload.get("data", ""))
    if data.startswith("0x"):
        data = data[2:]
    return {
        "pool": pool,
        "topic0": topic0,
        "timestamp_ms": timestamp_ms,
        "data": data.lower(),
    }


def _decode_v2_price_usd(tick: Dict[str, Any], pool_meta: Dict[str, str]) -> Tuple[Optional[float], float]:
    data = tick.get("data", "")
    if len(data) < 256:
        return None, 0.0
    amount0_in = _hex_to_uint256(data[0:64])
    amount1_in = _hex_to_uint256(data[64:128])
    amount0_out = _hex_to_uint256(data[128:192])
    amount1_out = _hex_to_uint256(data[192:256])
    token_meta = _non_weth_token_meta(pool_meta)
    token_decimals = token_meta["decimals"]
    weth_is_token0 = pool_meta.get("token0_address", "").lower() == WETH_BASE
    token_amount = 0.0
    weth_amount = 0.0
    if weth_is_token0:
        if amount0_in and amount1_out:
            weth_amount = amount0_in / 1e18
            token_amount = amount1_out / (10 ** token_decimals)
        elif amount1_in and amount0_out:
            weth_amount = amount0_out / 1e18
            token_amount = amount1_in / (10 ** token_decimals)
    else:
        if amount1_in and amount0_out:
            weth_amount = amount1_in / 1e18
            token_amount = amount0_out / (10 ** token_decimals)
        elif amount0_in and amount1_out:
            weth_amount = amount1_out / 1e18
            token_amount = amount0_in / (10 ** token_decimals)
    if token_amount <= 0 or weth_amount <= 0:
        return None, 0.0
    return (weth_amount / token_amount) * ETH_PRICE_USD, weth_amount * ETH_PRICE_USD


def _decode_v3_price_usd(tick: Dict[str, Any], pool_meta: Dict[str, str]) -> Tuple[Optional[float], float]:
    data = tick.get("data", "")
    if len(data) < 128:
        return None, 0.0
    amount0 = _hex_to_int256(data[0:64])
    amount1 = _hex_to_int256(data[64:128])
    token_meta = _non_weth_token_meta(pool_meta)
    token_decimals = token_meta["decimals"]
    weth_is_token0 = pool_meta.get("token0_address", "").lower() == WETH_BASE
    weth_amount = abs(amount0) / 1e18 if weth_is_token0 else abs(amount1) / 1e18
    token_amount = abs(amount1) / (10 ** token_decimals) if weth_is_token0 else abs(amount0) / (10 ** token_decimals)
    if token_amount <= 0 or weth_amount <= 0:
        return None, 0.0
    return (weth_amount / token_amount) * ETH_PRICE_USD, weth_amount * ETH_PRICE_USD


def _build_candles(samples: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    buckets: Dict[int, Dict[str, float]] = {}
    for sample in samples:
        ts_ms = _safe_int(sample.get("timestamp_ms"))
        price = _safe_float(sample.get("price_usd"))
        volume = _safe_float(sample.get("volume_usd"))
        if ts_ms <= 0 or price <= 0:
            continue
        bucket = (ts_ms // 60000) * 60
        current = buckets.get(bucket)
        if current is None:
            buckets[bucket] = {
                "time": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
            }
            continue
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        current["volume"] += volume
    ordered = [buckets[key] for key in sorted(buckets.keys())][-MAX_MARKET_CANDLES:]
    candles = [
        {
            "time": int(item["time"]),
            "open": round(item["open"], 6),
            "high": round(item["high"], 6),
            "low": round(item["low"], 6),
            "close": round(item["close"], 6),
        }
        for item in ordered
    ]
    volumes = [
        {
            "time": int(item["time"]),
            "value": round(item["volume"], 4),
        }
        for item in ordered
    ]
    return candles, volumes


def _get_market_watch(
    open_positions: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    shadow_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    shadow_records = shadow_records or []
    candidate_pools = {
        str(row.get("pool", ""))
        for row in [*open_positions, *trades, *signals, *shadow_records]
        if row.get("pool")
    }
    samples_by_pool: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    pool_activity: Dict[str, Dict[str, float]] = defaultdict(lambda: {"latest_ts": 0, "volume_usd": 0.0, "samples": 0})
    try:
        entries = r_bin.xrevrange(MARKET_TICKS_STREAM, count=max(MAX_MARKET_TICKS * 10, 12000)) or []
    except Exception:
        entries = []
    for _, entry_fields in entries:
        tick = _decode_market_tick(entry_fields)
        if not tick:
            continue
        pool_meta = _load_pool_meta(tick["pool"])
        if pool_meta.get("has_weth_leg", "0") != "1":
            continue
        if tick["topic0"] in (TOPIC_V2_SWAP, TOPIC_AERO_VAMM_SWAP):
            price_usd, volume_usd = _decode_v2_price_usd(tick, pool_meta)
        elif tick["topic0"] == TOPIC_V3_SWAP:
            price_usd, volume_usd = _decode_v3_price_usd(tick, pool_meta)
        else:
            continue
        if not price_usd:
            continue
        tick["price_usd"] = price_usd
        tick["volume_usd"] = volume_usd
        samples_by_pool[tick["pool"]].append(tick)
        activity = pool_activity[tick["pool"]]
        activity["latest_ts"] = max(activity["latest_ts"], _safe_int(tick.get("timestamp_ms")))
        activity["volume_usd"] += volume_usd
        activity["samples"] += 1

    signal_pools = {str(row.get("pool", "")) for row in signals if row.get("pool")}
    position_pools = {str(row.get("pool", "")) for row in open_positions if row.get("pool")}
    trade_pools = {str(row.get("pool", "")) for row in trades if row.get("pool")}

    def _pool_rank(pool: str) -> Tuple[int, int, float]:
        meta = _load_pool_meta(pool)
        token_meta = _non_weth_token_meta(meta)
        labeled = 1 if token_meta["symbol"] != "UNKNOWN" else 0
        priority = 3 if pool in position_pools else 2 if pool in signal_pools else 1 if pool in trade_pools else 0
        activity = pool_activity.get(pool, {})
        return (
            labeled,
            priority,
            activity.get("latest_ts", 0),
        )

    ordered_unique = sorted((set(samples_by_pool.keys()) | candidate_pools), key=_pool_rank, reverse=True)
    prioritized: List[str] = []
    seen_rank: set[str] = set()
    # Replay/strategy pools should stay selectable even when symbols are missing from Redis.
    for pool in sorted(candidate_pools, key=_pool_rank, reverse=True):
        if pool and pool not in seen_rank:
            prioritized.append(pool)
            seen_rank.add(pool)
    for pool in ordered_unique:
        if pool not in seen_rank:
            prioritized.append(pool)
            seen_rank.add(pool)
    ordered_unique = prioritized[:18]
    if not ordered_unique:
        return {"active_pool": None, "pools": [], "pool_snapshots": {}, "candles": [], "volumes": [], "markers": []}

    pool_options: List[Dict[str, Any]] = []
    pool_snapshots: Dict[str, Any] = {}
    for pool in ordered_unique:
        decorated = _decorate_pool_item(pool)
        candles, volumes = _build_candles(samples_by_pool.get(pool, []))
        markers: List[Dict[str, Any]] = []
        for signal in signals:
            if signal.get("pool") != pool:
                continue
            ts = _normalize_epoch_seconds(signal.get("timestamp")) // 60 * 60
            if ts <= 0:
                continue
            markers.append(
                {
                    "time": ts,
                    "position": "aboveBar",
                    "color": "#6b8cff",
                    "shape": "arrowUp",
                    "text": signal.get("signal_source", "signal"),
                }
            )
        for trade in trades:
            if trade.get("pool") != pool:
                continue
            ts = _normalize_epoch_seconds(trade.get("ts")) // 60 * 60
            if ts <= 0:
                continue
            is_buy = trade.get("action") == "buy"
            markers.append(
                {
                    "time": ts,
                    "position": "belowBar" if is_buy else "aboveBar",
                    "color": "#3ddc97" if is_buy else "#ff6b6b",
                    "shape": "circle",
                    "text": trade.get("action", ""),
                }
            )
        activity = pool_activity.get(pool, {})
        decorated["latest_ts"] = activity.get("latest_ts", 0)
        decorated["last_price_usd"] = round(_safe_float(samples_by_pool[pool][-1].get("price_usd")), 6) if samples_by_pool.get(pool) else 0.0
        decorated["recent_volume_usd"] = round(activity.get("volume_usd", 0.0), 2)
        decorated["sample_count"] = int(activity.get("samples", 0))
        pool_options.append(decorated)
        pool_snapshots[pool] = {
            "meta": decorated,
            "candles": candles,
            "volumes": volumes,
            "markers": markers,
        }

    if not pool_options:
        return {"active_pool": None, "pools": [], "pool_snapshots": {}, "candles": [], "volumes": [], "markers": []}

    active_pool = next((item["pool"] for item in pool_options if pool_snapshots[item["pool"]]["candles"]), pool_options[0]["pool"])
    active_meta = pool_snapshots[active_pool]["meta"]
    return {
        "active_pool": active_meta,
        "pools": pool_options,
        "pool_snapshots": pool_snapshots,
        "candles": pool_snapshots[active_pool]["candles"],
        "volumes": pool_snapshots[active_pool]["volumes"],
        "markers": pool_snapshots[active_pool]["markers"],
    }


def _build_shadow_summary(shadow_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [row for row in shadow_records if row.get("status") == "closed"]
    grouped: Dict[str, List[float]] = defaultdict(list)
    for row in closed:
        grouped[str(row.get("signal_source", "unknown"))].append(_safe_float(row.get("net_return_pct")))
    by_source = []
    for source, returns in sorted(grouped.items()):
        count = len(returns)
        mean_return = sum(returns) / count if count else 0.0
        win_rate = len([value for value in returns if value > 0]) / count if count else 0.0
        by_source.append(
            {
                "source": source,
                "count": count,
                "mean_return_pct": round(mean_return, 3),
                "win_rate": round(win_rate * 100.0, 1),
            }
        )
    aggregate_mean = sum(_safe_float(row.get("net_return_pct")) for row in closed) / len(closed) if closed else 0.0
    return {
        "open_count": len([row for row in shadow_records if row.get("status") != "closed"]),
        "closed_count": len(closed),
        "aggregate_mean_return_pct": round(aggregate_mean, 3),
        "by_source": by_source,
    }


def _build_trade_breakdown(trades: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    groups: Dict[str, List[float]] = defaultdict(list)
    for trade in trades:
        if trade.get("action") != "sell":
            continue
        key = str(trade.get(field) or "unknown")
        groups[key].append(_safe_float(trade.get("pnl_usd")))
    rows = []
    for key, pnls in sorted(groups.items()):
        count = len(pnls)
        wins = len([p for p in pnls if p > 0])
        rows.append(
            {
                "label": key,
                "count": count,
                "total_pnl_usd": round(sum(pnls), 4),
                "avg_pnl_usd": round(sum(pnls) / count, 4) if count else 0.0,
                "win_rate": round((wins / count * 100.0), 1) if count else 0.0,
            }
        )
    return rows


def _build_strategy_flow(signals: List[Dict[str, Any]], shadow_summary: Dict[str, Any], panic_reason: str) -> List[Dict[str, Any]]:
    recent_signals = len([row for row in signals if _safe_int(row.get("timestamp")) >= int(time.time()) - 3600])
    return [
        {
            "step": "Market ingest",
            "status": "healthy",
            "detail": "market_ticks -> feature snapshot",
            "metric": f"{recent_signals} signals / hour",
        },
        {
            "step": "Signal scoring",
            "status": "healthy" if shadow_summary["closed_count"] else "watch",
            "detail": "smart_money + pool_score candidates",
            "metric": f"{shadow_summary['closed_count']} shadow closes",
        },
        {
            "step": "Risk gate",
            "status": "critical" if panic_reason else "healthy",
            "detail": "panic gate + drawdown guard",
            "metric": panic_reason or "SYSTEM_PANIC clear",
        },
        {
            "step": "Execution",
            "status": "healthy",
            "detail": "executor consumes signals stream",
            "metric": f"{len([row for row in signals if row.get('signal_source') == 'smart_money'])} smart-money prompts",
        },
        {
            "step": "Replay",
            "status": "healthy" if shadow_summary["closed_count"] else "watch",
            "detail": "shadow closer + cohort attribution",
            "metric": f"{shadow_summary['aggregate_mean_return_pct']:+.2f}% mean return",
        },
    ]


def _latest_timestamp(signals: List[Dict[str, Any]], trades: List[Dict[str, Any]], shadow_records: List[Dict[str, Any]]) -> Dict[str, int]:
    latest_signal = max((_safe_int(row.get("timestamp")) for row in signals), default=0)
    latest_trade = max((_safe_int(row.get("ts")) for row in trades), default=0)
    latest_shadow = max((_safe_int(row.get("signal_ts_ms")) // 1000 for row in shadow_records), default=0)
    return {"signal_ts": latest_signal, "trade_ts": latest_trade, "shadow_ts": latest_shadow}


def _clickhouse_max_block_time() -> Dict[str, Any]:
    """Best-effort max(block_time) from ClickHouse; fail-soft to unknown."""
    sql = (
        "SELECT max(block_time) AS max_bt "
        f"FROM {CLICKHOUSE_DB}.uniswap_v3_swaps "
        "WHERE chain_id = 8453 FORMAT JSON"
    )
    params = {"query": sql}
    if CLICKHOUSE_USER:
        params["user"] = CLICKHOUSE_USER
    if CLICKHOUSE_PASSWORD:
        params["password"] = CLICKHOUSE_PASSWORD
    url = f"{CLICKHOUSE_URL.rstrip('/')}/?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        rows = payload.get("data") or []
        if not rows:
            return {"status": "unknown", "max_block_time": None, "age_sec": None}
        raw = rows[0].get("max_bt")
        if not raw:
            return {"status": "empty", "max_block_time": None, "age_sec": None}
        # ClickHouse returns ISO timestamp string.
        from datetime import datetime

        if isinstance(raw, str):
            ts = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            max_ts = int(dt.timestamp())
        else:
            max_ts = _normalize_epoch_seconds(raw)
        age = max(0, int(time.time()) - max_ts) if max_ts else None
        return {
            "status": "ok",
            "max_block_time": raw,
            "max_block_ts": max_ts,
            "age_sec": age,
        }
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "max_block_time": None, "age_sec": None}


def _market_ticks_freshness() -> Dict[str, Any]:
    try:
        entries = r_bin.xrevrange(MARKET_TICKS_STREAM, count=1) or []
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "age_sec": None}
    if not entries:
        return {"status": "empty", "age_sec": None, "last_stream_id": None}
    stream_id, fields = entries[0]
    sid = stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
    tick = _decode_market_tick(fields if isinstance(fields, dict) else {})
    ts_ms = _safe_int(tick.get("timestamp_ms")) if tick else 0
    if ts_ms <= 0:
        # Fall back to Redis stream ID ms prefix when payload lacks timestamp.
        try:
            ts_ms = int(str(sid).split("-")[0])
        except (TypeError, ValueError, IndexError):
            ts_ms = 0
    age = max(0, int(time.time()) - ts_ms // 1000) if ts_ms else None
    return {
        "status": "ok" if age is not None else "unknown",
        "age_sec": age,
        "last_stream_id": sid,
        "last_timestamp_ms": ts_ms,
    }


def _ingestor_pool_health() -> Dict[str, Any]:
    try:
        raw = r_text.hgetall("ingestor:pool_health") or {}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if not raw:
        return {"status": "missing"}
    return {"status": "ok", "fields": dict(raw)}


def _pipeline_freshness() -> Dict[str, Any]:
    ingest = _market_ticks_freshness()
    archive = _clickhouse_max_block_time()
    pool = _ingestor_pool_health()
    now = int(time.time())

    ingest_age = ingest.get("age_sec")
    archive_age = archive.get("age_sec")
    ingest_stale = ingest_age is None or ingest_age > DASHBOARD_INGEST_STALE_SEC
    archive_stale = archive_age is None or archive_age > DASHBOARD_ARCHIVE_STALE_SEC

    if ingest_stale and archive_stale:
        overall = "critical"
    elif ingest_stale or archive_stale:
        overall = "warning"
    else:
        overall = "healthy"

    return {
        "overall": overall,
        "checked_at": now,
        "ingest": ingest,
        "archive": archive,
        "ingestor_pool": pool,
        "thresholds": {
            "ingest_stale_sec": DASHBOARD_INGEST_STALE_SEC,
            "archive_stale_sec": DASHBOARD_ARCHIVE_STALE_SEC,
        },
    }


def _build_alerts(
    panic_reason: str,
    stats: Dict[str, Any],
    drawdown: Dict[str, Any],
    open_positions: List[Dict[str, Any]],
    latest_ts: Dict[str, int],
    shadow_summary: Dict[str, Any],
    pipeline: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    alerts: List[Dict[str, Any]] = []
    pipeline = pipeline or {}
    ingest = pipeline.get("ingest") or {}
    archive = pipeline.get("archive") or {}
    ingest_age = ingest.get("age_sec")
    archive_age = archive.get("age_sec")
    if ingest_age is None:
        alerts.append(
            {
                "id": "ingest-stale-critical",
                "severity": "critical",
                "title": "Market ticks ingestion appears dead",
                "body": "No recent market_ticks entries in Redis.",
                "action": "check ingestor WsPool / swap-poller / EATLQ_INGESTION_MODE",
            }
        )
    elif ingest_age > DASHBOARD_INGEST_STALE_SEC:
        alerts.append(
            {
                "id": "ingest-stale-critical",
                "severity": "critical",
                "title": "Market ticks stream is stale",
                "body": f"Last tick age {ingest_age}s (threshold {DASHBOARD_INGEST_STALE_SEC}s).",
                "action": "inspect ingestor logs and ingestor:pool_health rotation",
            }
        )
    archive_status = archive.get("status")
    if archive_status in {"empty", "error", "unknown"} or archive_age is None:
        alerts.append(
            {
                "id": "archive-stale-critical",
                "severity": "critical",
                "title": "ClickHouse archive freshness unknown or empty",
                "body": f"CH max(block_time) unavailable (status={archive_status}).",
                "action": "verify archiver container and ClickHouse connectivity",
            }
        )
    elif archive_age > DASHBOARD_ARCHIVE_STALE_SEC:
        alerts.append(
            {
                "id": "archive-stale-critical",
                "severity": "critical",
                "title": "ClickHouse archive is stale",
                "body": f"max(block_time) age {archive_age}s (threshold {DASHBOARD_ARCHIVE_STALE_SEC}s).",
                "action": "archiver may be dead — this caused the 6-08 silent data gap",
            }
        )
    if panic_reason:
        alerts.append(
            {
                "id": "panic-active",
                "severity": "critical",
                "title": "SYSTEM_PANIC is active",
                "body": panic_reason,
                "action": "clear panic only after executor state is verified",
            }
        )
    if len(open_positions) >= MAX_CONCURRENT:
        alerts.append(
            {
                "id": "max-concurrent",
                "severity": "warning",
                "title": "Open positions at concurrency cap",
                "body": f"{len(open_positions)} / {MAX_CONCURRENT} positions are live.",
                "action": "watch exit queue before approving new signals",
            }
        )
    signal_age = now_ts - latest_ts["signal_ts"] if latest_ts["signal_ts"] else None
    if signal_age is None or signal_age > 900:
        alerts.append(
            {
                "id": "signal-stale",
                "severity": "warning",
                "title": "Signals stream looks quiet",
                "body": "No recent signal in the last 15 minutes.",
                "action": "check strategy, pool discovery, and market tick freshness",
            }
        )
    if stats["total_trades"] >= 5 and stats["win_rate"] < 0.4:
        alerts.append(
            {
                "id": "low-win-rate",
                "severity": "warning",
                "title": "Realized win rate is soft",
                "body": f"Win rate is {stats['win_rate'] * 100:.1f}% across {stats['total_trades']} closed trades.",
                "action": "review signal source attribution before enabling more write actions",
            }
        )
    if drawdown["drawdown_pct"] > 20:
        alerts.append(
            {
                "id": "drawdown-elevated",
                "severity": "critical",
                "title": "Drawdown is elevated",
                "body": f"Peak-to-trough drawdown is {drawdown['drawdown_pct']:.1f}%.",
                "action": "freeze discretionary actions and inspect replay outcomes",
            }
        )
    if shadow_summary["closed_count"] >= 10 and shadow_summary["aggregate_mean_return_pct"] < 0:
        alerts.append(
            {
                "id": "shadow-negative",
                "severity": "warning",
                "title": "Shadow cohort mean return is negative",
                "body": f"Average closed shadow return is {shadow_summary['aggregate_mean_return_pct']:+.2f}%.",
                "action": "stay in review mode until cohort quality recovers",
            }
        )
    for alert in alerts:
        ack = _json_load(r_text.get(f"dashboard:alert_ack:{alert['id']}"))
        alert["acked"] = ack
    return alerts


TIMELINE_EVENT_LIMIT = 100


def _build_timeline(
    signals: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    shadow_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge signals, trades, and shadow rows on one clock — do **not** truncate each
    stream before merging (that destroys chronological ordering).
    """
    items: List[Dict[str, Any]] = []
    for signal in signals:
        ts = _normalize_epoch_seconds(signal.get("timestamp"))
        if ts <= 0:
            continue
        pool = str(signal.get("pool") or "").lower()
        items.append(
            {
                "ts": ts,
                "kind": "signal",
                "pool": pool,
                "label": signal.get("label", "signal"),
                "subtitle": str(signal.get("signal_source", "unknown")),
                "detail": (
                    f"score {float(signal.get('final_score', 0)):.2f} · "
                    f"conf {_safe_float(signal.get('confidence')):.2f}"
                ),
            }
        )
    for trade in trades:
        ts = _safe_int(trade.get("ts"))
        if ts <= 0:
            continue
        pool = str(trade.get("pool") or "").lower()
        dry_run = bool(trade.get("dry_run"))
        lane = str(trade.get("signal_source", "executor"))
        if dry_run:
            lane = f"{lane} · dry_run"
        items.append(
            {
                "ts": ts,
                "kind": "trade",
                "pool": pool,
                "label": trade.get("label", "trade"),
                "subtitle": lane,
                "detail": f"{str(trade.get('action', '')).upper()} · {float(trade.get('pnl_usd', 0)):+.4f} USD",
            }
        )
    for shadow in shadow_records:
        ts_ms = _safe_int(shadow.get("signal_ts_ms"))
        ts = ts_ms // 1000 if ts_ms else 0
        if ts <= 0:
            continue
        pool = str(shadow.get("pool") or "").lower()
        src = str(shadow.get("signal_source", "shadow"))
        rej = str(shadow.get("reject_reason") or "").strip()
        subtitle = f"{src}" + (f" · {rej}" if rej else "")
        status = str(shadow.get("status", "") or "")
        conf = _safe_float(shadow.get("confidence"))
        ret = _safe_float(shadow.get("net_return_pct"))
        ptype = str(shadow.get("pool_type") or "").strip()
        parts = [f"conf {conf:.2f}"]
        if ptype:
            parts.append(ptype)
        if status:
            parts.append(status)
        if status == "closed":
            parts.append(f"net {ret:+.2f}%")
        elif rej:
            parts.append("shadow trace")
        detail = " · ".join(parts)
        items.append(
            {
                "ts": ts,
                "kind": "shadow",
                "pool": pool,
                "label": shadow.get("label", "shadow"),
                "subtitle": subtitle,
                "detail": detail,
            }
        )
    items.sort(key=lambda row: row["ts"], reverse=True)
    return items[:TIMELINE_EVENT_LIMIT]


def _build_replay_summary(
    shadow_summary: Dict[str, Any],
    stats: Dict[str, Any],
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    sells = [t for t in trades if str(t.get("action", "")).lower() == "sell" and t.get("success")]
    dry_sells = [t for t in sells if t.get("dry_run")]
    live_sells = [t for t in sells if not t.get("dry_run")]
    return {
        "shadow_closed_mean_pct": shadow_summary["aggregate_mean_return_pct"],
        "shadow_closed_n": shadow_summary["closed_count"],
        "shadow_open_n": shadow_summary["open_count"],
        "live_net_pnl_usd": stats["net_pnl_usd"],
        "live_closed_n": len(live_sells),
        "dry_run_closed_n": len(dry_sells),
        "live_win_rate_pct": round(stats["win_rate"] * 100.0, 1) if stats.get("total_trades", 0) > 0 else 0.0,
    }


def _safe_read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_read_json_list(path: Path) -> Optional[List[Dict[str, Any]]]:
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else None
    except Exception:
        return None


def _live_trading_mode_display() -> str:
    raw = os.environ.get("LIVE_TRADING_MODE", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "ON"
    return "OFF"


def _redis_signals_health(latest_ts: Dict[str, int]) -> Dict[str, Any]:
    now_ts = int(time.time())
    sig_ts = latest_ts.get("signal_ts") or 0
    age = (now_ts - sig_ts) if sig_ts else None
    threshold = 900
    if age is None:
        status = "unknown"
        detail = "no signal timestamps loaded"
    elif age > threshold:
        status = "stale"
        detail = f"latest signal stream age {age}s (threshold {threshold}s)"
    else:
        status = "live"
        detail = f"latest signal stream age {age}s"
    return {"status": status, "signal_age_sec": age, "detail": detail, "threshold_stale_sec": threshold}


def _effective_artifact_root() -> Path:
    p = Path(ALPHA_ARTIFACT_ROOT)
    if p.is_dir():
        return p
    fallback = Path(__file__).resolve().parents[2] / "mvp" / "audits"
    return fallback if fallback.is_dir() else p


def _build_research_panel(latest_ts: Dict[str, int]) -> Dict[str, Any]:
    """Sprint078B — filesystem artifacts only; forward vs historical kept separate."""
    root = _effective_artifact_root()
    errors: List[str] = []
    out: Dict[str, Any] = {
        "artifact_root": str(root),
        "artifact_root_exists": root.is_dir(),
        "live_trading_mode": _live_trading_mode_display(),
        "redis_signals": _redis_signals_health(latest_ts),
        "server_built_at": datetime.now(tz=timezone.utc).isoformat(),
        "sprint076_generated_at_utc": None,
        "strategy_count": None,
        "expansion_plan_count": None,
        "forward_leaderboard": [],
        "forward_top": [],
        "family_scoreboard": [],
        "aggregates": {},
        "risk_flags": [],
        "historical_note": (
            "Historical columns come from factory label backtests — NOT forward proof. "
            "Use forward_only rows for dry-run race; never blend into a single alpha conclusion."
        ),
        "errors": errors,
    }

    lb_path = root / "sprint076_forward_race" / "sprint076_forward_leaderboard.json"
    lb = _safe_read_json_file(lb_path)
    if not isinstance(lb, dict):
        errors.append(f"missing_or_invalid:{lb_path}")
        return out

    out["sprint076_generated_at_utc"] = lb.get("generated_at_utc")
    out["strategy_count"] = lb.get("strategy_count")
    out["expansion_plan_count"] = lb.get("expansion_plan_count")

    fwd = lb.get("views", {}).get("forward_only") or []
    if not isinstance(fwd, list):
        fwd = []

    sb77_path = root / "sprint077_forward_expansion" / "scoreboard.json"
    sb77_list = _safe_read_json_list(sb77_path)
    sb77_map: Dict[str, Dict[str, Any]] = {}
    if sb77_list:
        sb77_map = {str(r.get("strategy_id")): r for r in sb77_list if isinstance(r, dict) and r.get("strategy_id")}

    enriched: List[Dict[str, Any]] = []
    for row in fwd:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("strategy_id") or "")
        extra = sb77_map.get(sid, {})
        merged = dict(row)
        merged["hard_exit_forced_count"] = extra.get("hard_exit_forced_count")
        merged["fwd_quote_fail_proxy_077"] = extra.get("fwd_quote_fail_proxy")
        enriched.append(merged)

    out["forward_leaderboard"] = enriched
    top_sorted = sorted(
        enriched,
        key=lambda r: (
            _safe_int(r.get("forward_n")),
            float(r.get("ev") or -1e18),
        ),
        reverse=True,
    )
    out["forward_top"] = top_sorted[:8]

    stuck = sum(_safe_int(r.get("open_position_stuck_count")) for r in enriched)
    qvals = [float(r.get("quote_fail_rate") or 0) for r in enriched]
    qmax = max(qvals) if qvals else 0.0
    hsum77 = sum(_safe_int(r.get("hard_exit_forced_count")) for r in sb77_map.values())
    out["aggregates"] = {
        "total_stuck_gate_counts": stuck,
        "max_quote_fail_rate_forward076": round(qmax, 6),
        "sum_hard_exit_forced_sprint077": hsum77,
    }

    fam_path = root / "sprint076_forward_race" / "sprint076_family_scoreboard.json"
    fam = _safe_read_json_list(fam_path)
    if fam:
        out["family_scoreboard"] = fam
    else:
        errors.append(f"missing_or_invalid:{fam_path}")

    flags076 = _safe_read_json_file(root / "sprint076_forward_race" / "sprint076_decision_flags.json")
    flags077 = _safe_read_json_file(root / "sprint077_forward_expansion" / "sprint077_decision_flags.json")
    risk: List[str] = []
    if isinstance(flags076, dict):
        for k, v in flags076.items():
            if v is True:
                risk.append(f"sprint076:{k}")
    if isinstance(flags077, dict):
        for k, v in flags077.items():
            if v is True:
                risk.append(f"sprint077:{k}")
        blk = flags077.get("still_blocked_strategy_ids")
        if isinstance(blk, list) and blk:
            risk.append(f"sprint077:still_blocked_hard_exit_count={len(blk)}")
    out["risk_flags"] = risk[:48]

    res077 = _safe_read_json_file(root / "sprint077_forward_expansion" / "sprint077_resource_snapshot.json")
    if isinstance(res077, dict):
        out["sprint077_resource"] = res077

    return out


def _load_harness_states() -> List[Dict[str, Any]]:
    """Read all *_state.json from sprint075 and sprint077 dirs."""
    states: List[Dict[str, Any]] = []
    for harness_tag, state_dir in [("sprint075", _SPRINT075_DIR), ("sprint077", _SPRINT077_DIR)]:
        try:
            if not state_dir.is_dir():
                continue
            for fp in sorted(state_dir.glob("*_state.json")):
                rec = _safe_read_json_file(fp)
                if isinstance(rec, dict):
                    rec["_harness"] = harness_tag
                    states.append(rec)
        except Exception:
            pass
    return states


def _build_shadow_funnel(shadow_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts by signal_source and reject_reason over the latest MAX_SHADOW records."""
    by_source: Dict[str, int] = defaultdict(int)
    by_reject: Dict[str, int] = defaultdict(int)
    closed = open_count = rejected = accepted = 0
    for rec in shadow_records:
        src = str(rec.get("signal_source") or "unknown")
        rej = str(rec.get("reject_reason") or "").strip()
        status = str(rec.get("status") or "")
        by_source[src] += 1
        if rej:
            by_reject[rej] += 1
            rejected += 1
        else:
            accepted += 1
        if status == "closed":
            closed += 1
        else:
            open_count += 1
    total = len(shadow_records)
    return {
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "closed": closed,
        "open": open_count,
        "by_source": [{"source": k, "count": v} for k, v in sorted(by_source.items(), key=lambda x: -x[1])],
        "by_reject_reason": [{"reason": k, "count": v} for k, v in sorted(by_reject.items(), key=lambda x: -x[1])],
    }


def _build_alpha_panel() -> Dict[str, Any]:
    """Aggregate Alpha Factory + forward harness state into a single structured panel."""
    states = _load_harness_states()

    # ---- per-strategy aggregation ----
    strategy_rows: List[Dict[str, Any]] = []
    # Build scoreboard map from sprint077 scoreboard for extra forward metrics
    sb077_list = _safe_read_json_list(_AF077_SCOREBOARD) or []
    sb077_map: Dict[str, Dict[str, Any]] = {str(r.get("strategy_id")): r for r in sb077_list if isinstance(r, dict)}

    for state in states:
        if not isinstance(state, dict):
            continue
        sid = str(state.get("strategy_id") or "")
        stats = state.get("stats") or {}
        fwd_n = _safe_int(stats.get("forward_n"))
        wins = _safe_int(stats.get("wins"))
        losses = _safe_int(stats.get("losses"))
        total_gain = _safe_float(stats.get("total_gain"))
        total_loss = _safe_float(stats.get("total_loss_abs"))
        equity = _safe_float(state.get("equity"), 100.0)
        roi_pct = round(equity - 100.0, 4)
        open_pos = state.get("open_positions") or {}
        trades_list = state.get("trades") or []
        wr = round(wins / fwd_n, 4) if fwd_n > 0 else 0.0
        pf = min(_safe_float(total_gain / total_loss, 0.0) if total_loss > 0 else (99.0 if total_gain > 0 else 0.0), 99.0)
        # payoff ratio: avg_win / avg_loss (paper money)
        payoff = round((total_gain / wins) / (total_loss / losses), 3) if wins > 0 and losses > 0 else None
        # pull extra forward fields from sprint077 scoreboard if available
        sb77 = sb077_map.get(sid, {})
        strategy_rows.append({
            "strategy_id": sid,
            "harness": state.get("_harness", ""),
            "family": str(state.get("family") or sb77.get("family") or ""),
            "exit_horizon": str(state.get("exit_horizon") or sb77.get("exit_horizon") or ""),
            "entry_desc": str(state.get("entry_description") or sb77.get("entry_description") or ""),
            "equity": round(equity, 4),
            "roi_pct": roi_pct,
            "fwd_n": fwd_n,
            "fwd_wr": wr,
            "fwd_pf": round(pf, 3),
            "fwd_ev": round(_safe_float(sb77.get("fwd_ev")), 6),
            "fwd_payoff_ratio": round(_safe_float(payoff or sb77.get("fwd_payoff_ratio")), 3) if (payoff or sb77.get("fwd_payoff_ratio")) else None,
            "fwd_max_drawdown": round(_safe_float(sb77.get("fwd_max_drawdown")), 4),
            "fwd_top_pool_share": round(_safe_float(sb77.get("fwd_top_pool_share")), 4),
            "total_gain": round(total_gain, 4),
            "total_loss": round(total_loss, 4),
            "open_count": len(open_pos) if isinstance(open_pos, dict) else 0,
            "trade_count": len(trades_list),
            "last_run": str(state.get("last_run") or ""),
            "run_count": _safe_int(state.get("run_count")),
            "hard_exit_required": bool(state.get("HARD_EXIT_REQUIRED") or sb77.get("HARD_EXIT_REQUIRED")),
            "hard_exit_count": _safe_int(stats.get("hard_exit_forced") or sb77.get("hard_exit_forced_count")),
        })

    strategy_rows.sort(key=lambda r: r["equity"], reverse=True)

    # ---- per-pool aggregation across all state trades ----
    pool_agg: Dict[str, Dict[str, Any]] = {}
    for state in states:
        sid = str(state.get("strategy_id") or "")
        for t in (state.get("trades") or []):
            pool = str(t.get("pool") or "").lower()
            if not pool:
                continue
            ps = pool_agg.setdefault(pool, {"pool": pool, "n": 0, "wins": 0, "pnl": 0.0, "strategies": set()})
            ps["n"] += 1
            ps["wins"] += int(bool(t.get("win")))
            ps["pnl"] += _safe_float(t.get("pnl"))
            ps["strategies"].add(sid)

    pool_rows: List[Dict[str, Any]] = []
    for pool, ps in pool_agg.items():
        meta = _load_pool_meta(pool)
        wr = round(ps["wins"] / ps["n"], 4) if ps["n"] > 0 else 0.0
        pool_rows.append({
            "pool": pool,
            "label": _pool_label(pool, meta),
            "n": ps["n"],
            "wins": ps["wins"],
            "wr": wr,
            "pnl": round(ps["pnl"], 4),
            "strategy_count": len(ps["strategies"]),
        })
    pool_rows.sort(key=lambda r: r["pnl"], reverse=True)

    # ---- aggregate KPIs ----
    total_fwd_n = sum(r["fwd_n"] for r in strategy_rows)
    total_fwd_wins = sum(r["fwd_n"] * r["fwd_wr"] for r in strategy_rows)
    avg_equity = round(sum(r["equity"] for r in strategy_rows) / len(strategy_rows), 4) if strategy_rows else 100.0
    n_positive = sum(1 for r in strategy_rows if r["roi_pct"] > 0)
    total_pnl = round(sum(r["pnl"] for r in pool_rows), 4)
    n_multi_pool = sum(1 for r in pool_rows if r["strategy_count"] > 1)
    unique_pools = len(pool_rows)

    # ---- rolling dashboard (alpha factory top5 from historical backtest) ----
    rolling = _safe_read_json_file(_AF_ROLLING_DASH) or {}
    gates_l073 = rolling.get("global_gates_L073_summary") or {}
    band_hist = rolling.get("factory_band_histogram") or {}
    top5_hist = rolling.get("top_5_by_composite") or []

    # ---- watchlist top dryrun candidates ----
    watchlist = _safe_read_json_file(_AF_WATCHLIST) or {}
    top_dryrun = watchlist.get("top_dryrun_candidates") or []

    return {
        "generated_ts": int(time.time()),
        "strategy_rows": strategy_rows,
        "pool_rows": pool_rows,
        "aggregate": {
            "total_fwd_trades": total_fwd_n,
            "avg_equity": avg_equity,
            "strategies_total": len(strategy_rows),
            "strategies_positive": n_positive,
            "total_paper_pnl": total_pnl,
            "unique_pools": unique_pools,
            "multi_pool_pools": n_multi_pool,
        },
        "gates_l073": gates_l073,
        "band_histogram": band_hist,
        "top5_historical": top5_hist,
        "top_dryrun_candidates": top_dryrun,
        "watchlist_generated_at": watchlist.get("generated_at_utc", ""),
        "rolling_generated_at": rolling.get("generated_at_utc", ""),
    }


def _get_shadow_funnel_from_redis() -> Dict[str, Any]:
    """Sample the most recent 500 shadow:signals entries for funnel stats."""
    try:
        rows = r_text.lrange(SHADOW_LIST_KEY, 0, 499) or []
    except Exception:
        return {}
    records = []
    for raw in rows:
        rec = _json_load(raw)
        if rec:
            records.append(rec)
    return _build_shadow_funnel(records)


def _build_canary_panel() -> Dict[str, Any]:
    """fee=100 live forward-test canary state + overall verdict (sprint097)."""
    def _j(key):
        try:
            raw = r_text.get(key)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    state = _j("canary:state")
    detector = _j("canary:detector")
    overall = _j("eatlq:overall")
    events = []
    try:
        for raw in (r_text.lrange("canary:events", 0, 19) or []):
            try:
                events.append(json.loads(raw))
            except Exception:
                pass
    except Exception:
        pass
    loop_alive = False
    hb = _safe_int(state.get("loop_alive_ts")) if state else 0
    if hb:
        loop_alive = (time.time() - hb) < 720
    return {
        "state": state,
        "detector": detector,
        "overall": overall,
        "events": events,
        "loop_alive": loop_alive,
    }


def get_api_data() -> Dict[str, Any]:
    panic_val = r_text.get("SYSTEM_PANIC") or ""
    day_key = _utc_day_key()
    pnl_wei = _safe_int(
        r_text.get(f"positions:realized_pnl:{day_key}") or r_text.get(f"exec:realized_pnl:{day_key}")
    )
    start_wei = _safe_int(
        r_text.get(f"positions:day_start_balance:{day_key}") or r_text.get(f"exec:day_start_balance:{day_key}")
    )
    pnl_usd = _wei_to_usd(pnl_wei)
    start_usd = _wei_to_usd(start_wei)
    pnl_pct = (pnl_usd / start_usd * 100.0) if start_usd > 0 else 0.0

    open_positions = _get_open_positions()
    trades = _get_trades()
    stats = _get_stats(trades)
    pnl_curve = _get_pnl_curve(trades)
    equity_curve = _build_equity_curve(start_usd, pnl_curve)
    drawdown = _compute_drawdown(equity_curve)
    signals = _get_signal_entries()
    shadow_records = _get_shadow_records()
    shadow_summary = _build_shadow_summary(shadow_records)
    daily_evals = _get_daily_evals()
    latest_ts = _latest_timestamp(signals, trades, shadow_records)
    pipeline = _pipeline_freshness()
    alerts = _build_alerts(panic_val, stats, drawdown, open_positions, latest_ts, shadow_summary, pipeline)
    market_watch = _get_market_watch(open_positions, trades, signals, shadow_records)
    timeline = _build_timeline(signals, trades, shadow_records)
    replay_summary = _build_replay_summary(shadow_summary, stats, trades)
    research = _build_research_panel(latest_ts)
    alpha_panel = _build_alpha_panel()
    shadow_funnel = _get_shadow_funnel_from_redis()

    capabilities = {
        "read_only": DASHBOARD_READONLY,
        "action_token_required": bool(DASHBOARD_ACTION_TOKEN),
        "panic_actions_enabled": DASHBOARD_ALLOW_PANIC_ACTIONS and not DASHBOARD_READONLY,
        "auth_enabled": bool(DASHBOARD_BASIC_USER and DASHBOARD_BASIC_PASS),
    }

    security = {
        "dashboard_bind": DASHBOARD_BIND,
        "basic_auth": _bool_to_status(capabilities["auth_enabled"]),
        "action_token": _bool_to_status(capabilities["action_token_required"]),
        "read_only": _bool_to_status(capabilities["read_only"]),
        "panic_actions": _bool_to_status(capabilities["panic_actions_enabled"]),
        "reverse_proxy_ready": True,
    }

    return {
        "status": "panic" if panic_val else "live",
        "panic_reason": panic_val,
        "pipeline": pipeline,
        "canary": _build_canary_panel(),
        "today_pnl_usd": round(pnl_usd, 4),
        "today_pnl_pct": round(pnl_pct, 2),
        "day_start_usd": round(start_usd, 4),
        "eth_price_usd": ETH_PRICE_USD,
        "open_positions": open_positions,
        "trades": trades[:80],
        "stats": stats,
        "pnl_curve": pnl_curve,
        "equity_curve": equity_curve,
        "server_ts": int(time.time()),
        "meta": {
            "chain_id": CHAIN_ID,
            "server_ts": int(time.time()),
            "dashboard_mode": "read-only" if DASHBOARD_READONLY else "controlled",
            "eth_price_usd": ETH_PRICE_USD,
            "security": security,
        },
        "overview": {
            "hero": [
                {"label": "Today realized PnL", "value": round(pnl_usd, 4), "suffix": "USD", "signed": True, "tone": "positive" if pnl_usd >= 0 else "negative"},
                {"label": "Win rate", "value": round(stats["win_rate"] * 100.0, 1), "suffix": "%", "signed": False, "tone": "positive" if stats["win_rate"] >= 0.5 else "negative"},
                {"label": "Profit factor", "value": stats["profit_factor"], "suffix": "", "signed": False, "tone": "positive" if stats["profit_factor"] >= 1 else "negative"},
                {"label": "Open positions", "value": len(open_positions), "suffix": "", "signed": False, "tone": "neutral"},
                {"label": "Shadow mean", "value": shadow_summary["aggregate_mean_return_pct"], "suffix": "%", "signed": True, "tone": "positive" if shadow_summary["aggregate_mean_return_pct"] >= 0 else "negative"},
                {"label": "Max drawdown", "value": drawdown["drawdown_pct"], "suffix": "%", "signed": False, "tone": "negative"},
            ],
            "alerts": alerts[:6],
            "recent_signals": signals[:8],
            "recent_trades": trades[:8],
        },
        "markets": {
            **market_watch,
            "recent_signals": signals[:20],
        },
        "execution": {
            "daily_evals": daily_evals,
            "by_source": _build_trade_breakdown(trades, "signal_source"),
            "by_reason": _build_trade_breakdown(trades, "reason"),
        },
        "strategies": {
            "flow": _build_strategy_flow(signals, shadow_summary, panic_val),
            "signals": signals[:20],
            "shadow_summary": shadow_summary,
            "pending_reviews": [row for row in signals[:20] if not row.get("review")],
        },
        "replay": {
            "timeline": timeline,
            "shadow_records": shadow_records[:40],
            "summary": replay_summary,
        },
        "risk": {
            "drawdown": drawdown,
            "cap_hit_count": _safe_int(r_text.get("exec:cap_hit_count")),
            "panic_reason": panic_val,
            "latest_timestamps": latest_ts,
            "alerts": alerts,
        },
        "settings": {
            "capabilities": capabilities,
            "security": security,
            "limits": {
                "max_concurrent_positions": MAX_CONCURRENT,
                "signals_loaded": len(signals),
                "shadow_loaded": len(shadow_records),
            },
        },
        "actions": capabilities,
        "research": research,
        "alpha": alpha_panel,
        "shadow_funnel": shadow_funnel,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read_json_body(self) -> Dict[str, Any]:
        length = _safe_int(self.headers.get("Content-Length"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_auth_required(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EATLQ Dashboard"')
        self.end_headers()

    def _authorized(self) -> bool:
        if not (DASHBOARD_BASIC_USER and DASHBOARD_BASIC_PASS):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self._send_auth_required()
            return False
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            self._send_auth_required()
            return False
        user, _, password = decoded.partition(":")
        if secrets.compare_digest(user, DASHBOARD_BASIC_USER) and secrets.compare_digest(password, DASHBOARD_BASIC_PASS):
            return True
        self._send_auth_required()
        return False

    def _action_allowed(self) -> Tuple[bool, str]:
        if DASHBOARD_READONLY:
            return False, "dashboard is in read-only mode"
        if not DASHBOARD_ACTION_TOKEN:
            return False, "dashboard action token is not configured"
        token = self.headers.get("X-Dashboard-Action-Token", "")
        if not token or not secrets.compare_digest(token, DASHBOARD_ACTION_TOKEN):
            return False, "missing or invalid dashboard action token"
        return True, ""

    def _json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", f"{mime_type or 'application/octet-stream'}; charset=utf-8" if path.suffix in {".html", ".css", ".js"} else (mime_type or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static_path(self, request_path: str) -> Path:
        clean = request_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / clean).resolve()
        static_root = STATIC_DIR.resolve()
        if static_root not in candidate.parents and candidate != static_root:
            raise FileNotFoundError("path escapes static root")
        return candidate

    def do_GET(self):
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            try:
                self._json(get_api_data())
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/research":
            try:
                signals = _get_signal_entries()
                trades = _get_trades()
                shadow_records = _get_shadow_records()
                latest_ts_r = _latest_timestamp(signals, trades, shadow_records)
                self._json(_build_research_panel(latest_ts_r))
            except Exception as exc:
                self._json({"error": str(exc), "artifact_root": ALPHA_ARTIFACT_ROOT}, 500)
            return
        if parsed.path == "/api/alpha":
            try:
                self._json({
                    **_build_alpha_panel(),
                    "shadow_funnel": _get_shadow_funnel_from_redis(),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/canary":
            try:
                self._json(_build_canary_panel())
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/health":
            self._json({"ok": True, "ts": int(time.time())})
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html")
            return
        if parsed.path in ("/canary", "/canary.html"):
            self._serve_file(STATIC_DIR / "canary.html")
            return
        try:
            self._serve_file(self._static_path(parsed.path))
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        payload = self._read_json_body()
        allowed, reason = self._action_allowed()
        if parsed.path == "/api/actions/ack-alert":
            if not allowed:
                self._json({"error": reason}, 403)
                return
            alert_id = str(payload.get("alert_id", "")).strip()
            if not alert_id:
                self._json({"error": "alert_id is required"}, 400)
                return
            record = {"alert_id": alert_id, "acked_at": int(time.time()), "acked_by": "dashboard"}
            r_text.set(f"dashboard:alert_ack:{alert_id}", json.dumps(record, ensure_ascii=True))
            self._json({"ok": True, "record": record})
            return
        if parsed.path == "/api/actions/review-signal":
            if not allowed:
                self._json({"error": reason}, 403)
                return
            signal_id = str(payload.get("signal_id", "")).strip()
            decision = str(payload.get("decision", "")).strip().lower()
            if not signal_id or decision not in {"approved", "rejected", "observed"}:
                self._json({"error": "signal_id and decision are required"}, 400)
                return
            record = {
                "signal_id": signal_id,
                "decision": decision,
                "comment": str(payload.get("comment", "")).strip(),
                "updated_at": int(time.time()),
            }
            r_text.set(f"dashboard:signal_review:{signal_id}", json.dumps(record, ensure_ascii=True))
            self._json({"ok": True, "record": record})
            return
        if parsed.path == "/api/actions/panic":
            if not allowed:
                self._json({"error": reason}, 403)
                return
            if not DASHBOARD_ALLOW_PANIC_ACTIONS:
                self._json({"error": "panic actions are disabled"}, 403)
                return
            mode = str(payload.get("mode", "")).strip().lower()
            if mode == "set":
                action_reason = str(payload.get("reason", "")).strip() or "manual dashboard panic"
                r_text.set("SYSTEM_PANIC", action_reason)
                self._json({"ok": True, "status": "panic", "reason": action_reason})
                return
            if mode == "clear":
                r_text.delete("SYSTEM_PANIC")
                self._json({"ok": True, "status": "live"})
                return
            self._json({"error": "mode must be set or clear"}, 400)
            return
        self._json({"error": "not found"}, 404)


def main():
    print(
        json.dumps(
            {
                "event": "dashboard_started",
                "bind": DASHBOARD_BIND,
                "port": DASHBOARD_PORT,
                "basic_auth": bool(DASHBOARD_BASIC_USER and DASHBOARD_BASIC_PASS),
                "action_token": bool(DASHBOARD_ACTION_TOKEN),
                "read_only": DASHBOARD_READONLY,
                "panic_actions": DASHBOARD_ALLOW_PANIC_ACTIONS,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    HTTPServer((DASHBOARD_BIND, DASHBOARD_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
