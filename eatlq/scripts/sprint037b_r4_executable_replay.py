#!/usr/bin/env python3
"""Sprint037B-R4 — Quoteability-first Executable Replay
========================================================

Architectural shift vs R2/R3
------------------------------
R2/R3 sampled candidates randomly and discovered ~70% were unquoteable
after spending RPC budget. R4 inverts the pipeline:

  Phase 1 (CH)          : all active 5m candidates via ClickHouse features
  Phase 2 (entry probe) : QuoterV2 at entry_block for EVERY candidate
                          → executable if amountOut > 0 for MICRO notional
  Phase 3 (slippage)    : TINY + STRESS probes on executable candidates only
                          → pool depth / concentration diagnostics
  Phase 4 (exit label)  : exit QuoterV2 at (entry + 1800s) block,
                          concentration-capped (max MAX_EXIT_PER_POOL per pool)
  Phase 5 (readout)     : economic analysis only on the executable labeled set

Three concepts separated clearly:
  - CH feature candidate   : had ≥5 swaps in 5m window
  - executable candidate   : entry QuoterV2 succeeds (MICRO notional)
  - labeled candidate      : exit quote succeeds + net_return computed

Regime roles:
  - Feb-02 : primary positive candidate
  - Dec-29 : secondary
  - Dec-01 : negative control / artifact confirmation

Decision flags (frozen, never override from CLI):
  TRAINING_ALLOWED=false
  LIVE_ALLOWED=false
  MODEL_PROMOTION_ALLOWED=false
  H12_ABLATION_ALLOWED=false
  PHASE6_ALLOWED=false
  FAILED_NO_7D_ALPHA_ACTIVE=true
  QUOTEABILITY_FIRST_REPLAY_ALLOWED=true
  RPC_BACKED_LABEL_VALIDATION_ALLOWED=true
  NO_ALPHA_CLAIM=true

Usage
-----
  # Dry run: CH features only, no RPC
  python scripts/sprint037b_r4_executable_replay.py --dry-run

  # Full run
  LABEL_RPC_URL=https://... python scripts/sprint037b_r4_executable_replay.py --run

  # Single regime
  LABEL_RPC_URL=https://... python scripts/sprint037b_r4_executable_replay.py --run --regime 2026-02-02

Environment
-----------
  CLICKHOUSE_URL       (default: http://172.18.0.6:8123/)
  CLICKHOUSE_USER      (default: default)
  CLICKHOUSE_PASSWORD
  LABEL_RPC_URL or EVM_HTTP_URL — archive-capable Base RPC
  LABEL_WORKERS        (default: 20)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from urllib.request import Request, urlopen

# ── Frozen constants (label-semantics-freeze.md) ─────────────────────────────
WETH                   = "0x4200000000000000000000000000000000000006"
CHAIN_ID               = 8453
ROUND_TRIP_COST        = 0.006
LABEL_EXIT_HORIZON_SEC = 1800
FEATURE_WINDOW_SEC     = 300
MIN_ACTIVITY_HEAT      = 5
QUOTER_V2_BASE         = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
MAX_ABS_NET_RETURN     = 10.0

# ── Multi-notional probe sizes ────────────────────────────────────────────────
# MICRO is the authoritative gate and label notional (label-semantics-freeze.md).
# TINY gives reference price at zero-impact (slippage baseline).
# STRESS shows pool depth at 10× micro trade size.
PROBE_TINY_WEI   = int(1e13)       # 0.00001  ETH ≈ $0.025  — near-zero impact
PROBE_MICRO_WEI  = int(5e14)       # 0.0005   ETH ≈ $1.25   — authoritative gate
PROBE_STRESS_WEI = int(5e15)       # 0.005    ETH ≈ $12.50  — depth diagnostic

# ── Concentration cap ─────────────────────────────────────────────────────────
# Limit exit labels per pool to avoid single-pool dominance in the labeled set.
MAX_EXIT_PER_POOL = 50

# ── RPC / worker config ───────────────────────────────────────────────────────
RPC_WORKER_THREADS = 20

# ── Meme pool exclusions ──────────────────────────────────────────────────────
_STABLES = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})
_EXCLUDED = frozenset({
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})

# ── Regimes ───────────────────────────────────────────────────────────────────
REGIMES = [
    {"week_start": "2025-12-01", "week_end": "2025-12-08",
     "label": "Dec-01 (negative control / artifact confirmation)"},
    {"week_start": "2025-12-29", "week_end": "2026-01-05",
     "label": "Dec-29 (secondary)"},
    {"week_start": "2026-02-02", "week_end": "2026-02-09",
     "label": "Feb-02 (primary positive candidate)"},
]

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parents[1]
OUT_DIR     = REPO / "mvp/audits/sprint037b_r4"
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037b_r4_summary.json"
OUT_REPORT  = OUT_DIR / "sprint037b_r4_report.md"

DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED":                 False,
    "LIVE_ALLOWED":                     False,
    "MODEL_PROMOTION_ALLOWED":          False,
    "H12_ABLATION_ALLOWED":             False,
    "PHASE6_ALLOWED":                   False,
    "FAILED_NO_7D_ALPHA_ACTIVE":        True,
    "QUOTEABILITY_FIRST_REPLAY_ALLOWED":True,
    "RPC_BACKED_LABEL_VALIDATION_ALLOWED":True,
    "NO_ALPHA_CLAIM":                   True,
    "CLICKHOUSE_READ_ONLY":             True,
}


def _log(msg: str) -> None:
    print(f"[r4 {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r}")


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 600.0) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ── SQL builders ──────────────────────────────────────────────────────────────
def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))

def _excluded_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_EXCLUDED))

def _meme_pools_cte() -> str:
    return f"""meme_pools AS (
  SELECT
    lower(pool_address)   AS pa,
    lower(token0_address) AS token0_addr,
    lower(token1_address) AS token1_addr,
    toUInt32(fee_bps)     AS fee_bps,
    has_weth_leg,
    multiIf(lower(token0_address) = '{WETH}', 1, 0) AS weth_is_token0
  FROM eatlq.discovered_pools AS dp
  WHERE dp.chain_id = {CHAIN_ID}
    AND lower(dp.pool_type) = 'v3'
    AND dp.has_weth_leg = 1
    AND dp.fee_bps IN (100, 500, 3000, 10000)
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
    AND lower(dp.pool_address) NOT IN ({_excluded_sql()})
)"""

def _ts_expr(alias: str = "s") -> str:
    return f"coalesce({alias}.block_time, {alias}.received_at_ms)"


def build_feature_query(start_dt: str, end_dt: str) -> str:
    """Feature aggregation with R3 semantics + last_swap_sec + liquidity."""
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                                                         AS pool_address,
  mp.fee_bps,
  mp.token0_addr,
  mp.token1_addr,
  mp.weth_is_token0,
  toUnixTimestamp(toStartOfFiveMinute({te}))                                   AS bucket_5m_sec,
  toUnixTimestamp(toStartOfFiveMinute({te})) + {FEATURE_WINDOW_SEC}           AS entry_time_sec,
  count()                                                                        AS activity_heat,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_meme_count,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) <  '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) >= '8')
  )                                                                              AS sell_meme_count,
  sum(if(mp.weth_is_token0 = 0,
        -toFloat64(eatlq_hex_to_int256(s.amount0_raw)),
         toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                        AS net_signed_flow,
  sum(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                       AS abs_flow_sum,
  sumIf(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
        (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
        (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_flow_sum,
  sum(pow(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))), 2))               AS abs_flow_sq_sum,
  avg(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS avg_liquidity,
  min(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS min_liquidity,
  max(toUnixTimestamp({te}))                                                    AS last_swap_sec,
  uniqCombinedIf(lower(s.sender),
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS unique_buyers,
  uniqCombined(lower(s.sender))                                                 AS unique_senders,
  argMax(toInt32(eatlq_hex_to_int256(s.tick_raw)), {te})                        AS tick_last,
  min(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                 AS tick_min,
  max(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                 AS tick_max
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
GROUP BY
  pool_address, mp.fee_bps, mp.token0_addr, mp.token1_addr, mp.weth_is_token0,
  bucket_5m_sec, entry_time_sec
HAVING activity_heat >= {MIN_ACTIVITY_HEAT}
ORDER BY pool_address, bucket_5m_sec
"""


def build_price_series_query(start_dt: str, end_dt: str) -> str:
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                       AS pool_address,
  toUnixTimestamp({te})                       AS ts_sec,
  toInt32(eatlq_hex_to_int256(s.tick_raw))    AS tick_i32
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
ORDER BY pool_address, ts_sec
"""


# ── Feature computation (R2/R3 aligned) ──────────────────────────────────────
def compute_features(row: dict[str, Any]) -> dict[str, Any]:
    activity_heat     = int(row.get("activity_heat") or 0)
    buy_meme_count    = int(row.get("buy_meme_count") or 0)
    sell_meme_count   = int(row.get("sell_meme_count") or 0)
    net_signed_flow   = float(row.get("net_signed_flow") or 0.0)
    abs_flow_sum      = float(row.get("abs_flow_sum") or 0.0)
    buy_flow_sum      = float(row.get("buy_flow_sum") or 0.0)
    abs_flow_sq_sum   = float(row.get("abs_flow_sq_sum") or 0.0)
    unique_buyers     = int(row.get("unique_buyers") or 0)
    avg_liquidity     = float(row.get("avg_liquidity") or 0.0)
    min_liquidity     = float(row.get("min_liquidity") or 0.0)
    last_swap_sec     = int(row.get("last_swap_sec") or 0)
    tick_min          = int(row.get("tick_min") or 0)
    tick_max          = int(row.get("tick_max") or 0)

    total_count = max(buy_meme_count + sell_meme_count, 1)
    price_velocity_5m = (
        max(-1.0, min(1.0, net_signed_flow / abs_flow_sum)) if abs_flow_sum > 0 else 0.0
    )
    buy_pressure_ratio = (
        max(0.0, min(1.0, buy_flow_sum / abs_flow_sum)) if abs_flow_sum > 0 else 0.5
    )
    if activity_heat > 0 and abs_flow_sum > 0:
        mean_abs = abs_flow_sum / activity_heat
        variance = max(0.0, abs_flow_sq_sum / activity_heat - mean_abs ** 2)
        token_volatility_5m = min(2.0, max(0.0, math.sqrt(variance) / mean_abs))
    else:
        token_volatility_5m = 0.0

    return {
        "price_velocity_5m":       round(price_velocity_5m, 6),
        "buy_pressure_ratio":      round(buy_pressure_ratio, 6),
        "token_volatility_5m":     round(token_volatility_5m, 6),
        "activity_heat":           activity_heat,
        "unique_buyer_entropy_5m": round(math.log1p(unique_buyers), 6),  # EATLQ-additive
        "buy_meme_count":          buy_meme_count,
        "sell_meme_count":         sell_meme_count,
        "abs_flow_sum":            round(abs_flow_sum, 2),
        "avg_liquidity":           avg_liquidity,
        "min_liquidity":           min_liquidity,
        "last_swap_sec":           last_swap_sec,
        "freshness_lag_sec":       0,  # filled later
        "tick_range":              abs(tick_max - tick_min),
    }


# ── Price index for exit-activity check ───────────────────────────────────────
class PoolPriceIndex:
    def __init__(self) -> None:
        self._ts:   dict[str, list[int]] = defaultdict(list)
        self._tick: dict[str, list[int]] = defaultdict(list)

    def ingest(self, pool: str, ts: int, tick: int) -> None:
        if pool and ts > 0:
            self._ts[pool].append(ts)
            self._tick[pool].append(tick)

    def has_ticks_in_range(self, pool: str, ts_start: int, ts_end: int) -> bool:
        s = self._ts.get(pool, [])
        return bisect.bisect_left(s, ts_end) > bisect.bisect_left(s, ts_start)

    def pools(self) -> set[str]:
        return set(self._ts.keys())


# ── RPC infrastructure ────────────────────────────────────────────────────────
class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 6):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._lock = Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _post(self, body: bytes) -> Any:
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                status = getattr(e, "code", None)
                if status == 429 or (status and 500 <= status < 600) or status is None:
                    time.sleep(min(0.5 * (2 ** attempt), 10.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc retries exhausted")

    def call(self, method: str, params: list) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params, "id": self._next_id()}).encode()
        return self._post(body)

    def batch(self, calls: list) -> list:
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i}
                   for i, (m, p) in enumerate(calls)]
        body = json.dumps(payload).encode()
        out  = self._post(body)
        idx  = {r.get("id"): r for r in out}
        return [idx.get(i) for i in range(len(calls))]


# ── Per-regime ts→block grid ──────────────────────────────────────────────────
def build_ts_grid_for_regime(
    rpc: RPC, ts_start: int, ts_end: int, step: int = 50
) -> tuple[list[int], list[int]]:
    """Efficient ts→block grid for just the regime window (~5K blocks)."""
    r = rpc.call("eth_getBlockByNumber", ["latest", False])
    latest_num = int(r["result"]["number"], 16)
    latest_ts  = int(r["result"]["timestamp"], 16)
    MARGIN = 5000

    lo = max(0, int(latest_num - (latest_ts - ts_start) / 2.0) - MARGIN)
    hi = min(latest_num, int(latest_num - (latest_ts - ts_end) / 2.0) + MARGIN)
    block_nums = list(range(lo, hi + 1, step))
    results: dict[int, int] = {}
    t0 = time.time()
    for i in range(0, len(block_nums), 500):
        sub = block_nums[i:i + 500]
        for bn, res in zip(sub, rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in sub])):
            if res and res.get("result"):
                results[bn] = int(res["result"]["timestamp"], 16)
        _log(f"    ts-grid: {min(i+500, len(block_nums))}/{len(block_nums)} [{time.time()-t0:.1f}s]")

    sorted_bn = sorted(results.keys())
    sorted_ts = [results[b] for b in sorted_bn]
    _log(f"    ts-grid: {len(sorted_bn)} samples, "
         f"span [{sorted_ts[0] if sorted_ts else -1}..{sorted_ts[-1] if sorted_ts else -1}]")
    return sorted_bn, sorted_ts


def ts_to_block(bn_arr: list[int], ts_arr: list[int], target_ts: int) -> int:
    idx = bisect.bisect_left(ts_arr, target_ts)
    if idx == 0:
        return bn_arr[0]
    if idx >= len(bn_arr):
        return bn_arr[-1]
    b_lo, t_lo = bn_arr[idx - 1], ts_arr[idx - 1]
    b_hi, t_hi = bn_arr[idx],     ts_arr[idx]
    if t_hi == t_lo:
        return b_hi
    return b_lo + int((target_ts - t_lo) / (t_hi - t_lo) * (b_hi - b_lo))


# ── QuoterV2 (verbatim from label_dashcam_executable.py) ─────────────────────
_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(h: str) -> str: return h.zfill(64)
def _addr_pad(addr: str) -> str:
    a = addr[2:] if addr.startswith("0x") else addr
    return _pad32(a.lower())
def _uint_pad(n: int) -> str: return _pad32(hex(n)[2:])
def _strip_0x(h: str) -> str: return h[2:] if h.startswith("0x") else h


def _eth_call(rpc: RPC, to: str, data: str, block_num: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block_num)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_quoter_v3(rpc: RPC, token_in: str, token_out: str,
                   amount_in: int, fee: int, block: int) -> int:
    """QuoterV2.quoteExactInputSingle; returns amountOut, 0 on any failure."""
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in)
        + _addr_pad(token_out)
        + _uint_pad(amount_in)
        + _uint_pad(fee)
        + _uint_pad(0)
    )
    try:
        res = _eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception:
        return 0
    raw = _strip_0x(res)
    return int(raw[0:64], 16) if len(raw) >= 64 else 0


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET_RETURN or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET_RETURN, net)), True
    return net, False


# ── Phase 2: Entry probe for a single candidate ───────────────────────────────
def run_entry_probe(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """Probe entry quoteability (MICRO) in-place on candidate dict.

    Sets: entry_block, entry_quote_ok, entry_amount_out_micro, entry_fail_reason.
    out_of_range_suspected is set True when MICRO returns 0 (primary failure mode).
    """
    weth = WETH.lower()
    t0   = c["token0_addr"].lower()
    t1   = c["token1_addr"].lower()
    if weth not in (t0, t1):
        c.update(entry_quote_ok=False, entry_fail_reason="fail_no_weth_leg",
                 entry_block=0, entry_amount_out_micro=0,
                 out_of_range_suspected=False)
        return

    meme = t1 if t0 == weth else t0
    entry_block = ts_to_block(bn_arr, ts_arr, c["entry_time_sec"])
    c["entry_block"] = entry_block

    if entry_block <= 0:
        c.update(entry_quote_ok=False, entry_fail_reason="fail_block_lookup",
                 entry_amount_out_micro=0, out_of_range_suspected=False)
        return

    micro_out = call_quoter_v3(rpc, weth, meme, PROBE_MICRO_WEI, c["fee_bps"], entry_block)
    if micro_out <= 0:
        c.update(entry_quote_ok=False, entry_fail_reason="fail_v3_entry_zero",
                 entry_amount_out_micro=0, out_of_range_suspected=True)
        return

    c.update(
        entry_quote_ok=True,
        entry_fail_reason=None,
        entry_amount_out_micro=micro_out,
        out_of_range_suspected=False,
    )
    c["_meme_addr"] = meme  # cached for exit label


# ── Phase 3: Slippage diagnostics (TINY + STRESS) ────────────────────────────
def run_slippage_diagnostics(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """Run TINY and STRESS probes and compute slippage proxy.

    Only called for entry-quoteable candidates.
    Sets: entry_amount_out_tiny, entry_amount_out_stress, entry_slippage_proxy.
    slippage_proxy < 0 means MICRO has worse price than TINY (expected);
    very negative (< -0.5) indicates thin pool.
    """
    weth = WETH.lower()
    meme = c.get("_meme_addr") or (
        c["token1_addr"].lower() if c["token0_addr"].lower() == weth
        else c["token0_addr"].lower()
    )
    fee   = c["fee_bps"]
    block = c["entry_block"]

    tiny_out   = call_quoter_v3(rpc, weth, meme, PROBE_TINY_WEI,   fee, block)
    stress_out = call_quoter_v3(rpc, weth, meme, PROBE_STRESS_WEI, fee, block)

    # slippage_proxy: how much worse is MICRO price vs TINY price
    price_tiny  = tiny_out   / PROBE_TINY_WEI   if tiny_out   > 0 else 0.0
    price_micro = c["entry_amount_out_micro"] / PROBE_MICRO_WEI

    slippage = (price_micro / price_tiny - 1.0) if price_tiny > 0 else None

    c["entry_amount_out_tiny"]   = tiny_out
    c["entry_amount_out_stress"] = stress_out
    c["entry_slippage_proxy"]    = round(slippage, 6) if slippage is not None else None


# ── Phase 4: Exit label ───────────────────────────────────────────────────────
def run_exit_label(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """Compute exit label for an executable candidate.

    Uses the exact amountOut from MICRO entry probe as input to exit quote,
    matching label-semantics-freeze.md exactly.
    Sets: exit_block, rpc_label, net_return_rpc, exit_fail_reason, exit_quote_ok.
    """
    weth = WETH.lower()
    meme = c.get("_meme_addr") or (
        c["token1_addr"].lower() if c["token0_addr"].lower() == weth
        else c["token0_addr"].lower()
    )
    fee        = c["fee_bps"]
    exit_block = ts_to_block(bn_arr, ts_arr, c["entry_time_sec"] + LABEL_EXIT_HORIZON_SEC)
    c["exit_block"] = exit_block

    tokens_received = c.get("entry_amount_out_micro", 0)
    if tokens_received <= 0:
        c.update(rpc_label=None, net_return_rpc=None,
                 exit_fail_reason="fail_no_entry_amount", exit_quote_ok=False)
        return

    weth_back = call_quoter_v3(rpc, meme, weth, tokens_received, fee, exit_block)
    if weth_back <= 0:
        c.update(rpc_label=0, net_return_rpc=-1.0,
                 exit_fail_reason="executable_exit_drained", exit_quote_ok=False)
        return

    net, bogus = _sanity_clip(weth_back / PROBE_MICRO_WEI - 1.0 - ROUND_TRIP_COST)
    if bogus:
        c.update(rpc_label=None, net_return_rpc=None,
                 exit_fail_reason="fail_sanity_clip", exit_quote_ok=False)
        return

    c.update(
        rpc_label=1 if net > 0 else 0,
        net_return_rpc=round(net, 6),
        exit_fail_reason=None,
        exit_quote_ok=True,
    )


# ── Economic readout helpers ──────────────────────────────────────────────────
def _stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {}
    rs  = sorted(returns)
    n   = len(rs)
    pos = sum(x > 0 for x in rs)
    k   = max(1, n // 10)
    trimmed = rs[k:-k] if n > 2 * k else rs
    return {
        "n":           n,
        "pos":         pos,
        "pos_share":   round(pos / n, 4),
        "mean":        round(sum(rs) / n, 6),
        "median":      round(rs[n // 2], 6),
        "trimmed_mean":round(sum(trimmed) / max(len(trimmed), 1), 6),
        "p10":         round(rs[max(0, n // 10)], 6),
        "p90":         round(rs[min(n - 1, 9 * n // 10)], 6),
    }


def compute_funnel(
    all_cands: list[dict],
    exe_cands: list[dict],
    labeled: list[dict],
) -> dict[str, Any]:
    """Candidate funnel: CH active → executable → labeled."""
    n_all    = len(all_cands)
    n_exe    = len(exe_cands)
    n_labeled= len(labeled)
    n_fail0  = sum(1 for c in all_cands if c.get("entry_fail_reason") == "fail_v3_entry_zero")
    n_other  = n_all - n_exe - n_fail0

    return {
        "ch_candidates":         n_all,
        "entry_quoteable":       n_exe,
        "entry_quoteable_rate":  round(n_exe / max(n_all, 1), 4),
        "fail_v3_entry_zero":    n_fail0,
        "fail_v3_entry_zero_rate": round(n_fail0 / max(n_all, 1), 4),
        "fail_other":            n_other,
        "labeled":               n_labeled,
        "label_funnel_rate":     round(n_labeled / max(n_all, 1), 4),
    }


def compute_feature_bucket_analysis(
    candidates: list[dict],
    features: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Per-feature quartile: entry_success_rate + label_1_share.

    Useful to check if CH features predict either quoteability or economic outcome.
    """
    if features is None:
        features = ["price_velocity_5m", "buy_pressure_ratio",
                    "activity_heat", "token_volatility_5m"]
    result: dict[str, Any] = {}
    for feat in features:
        vals = sorted(
            (float(c[feat]), i, c) for i, c in enumerate(candidates) if c.get(feat) is not None
        )
        if not vals:
            continue
        n = len(vals)
        q_bounds = [vals[n // 4][0], vals[n // 2][0], vals[3 * n // 4][0]]
        buckets: dict[str, list] = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}
        for v, _i, c in vals:
            b = ("Q1" if v <= q_bounds[0] else
                 "Q2" if v <= q_bounds[1] else
                 "Q3" if v <= q_bounds[2] else "Q4")
            buckets[b].append(c)

        result[feat] = {
            "quartile_bounds": [round(x, 6) for x in q_bounds],
            "per_quartile": {
                bname: {
                    "n":                   len(rows),
                    "entry_success_rate":  round(
                        sum(1 for r in rows if r.get("entry_quote_ok")) / max(len(rows), 1), 4
                    ),
                    "label_1_share":       round(
                        sum(1 for r in rows if r.get("rpc_label") == 1) /
                        max(sum(1 for r in rows if r.get("rpc_label") is not None), 1), 4
                    ),
                    "labeled_n":           sum(1 for r in rows if r.get("rpc_label") is not None),
                }
                for bname, rows in buckets.items()
            },
        }
    return result


def compute_pool_readout(labeled: list[dict]) -> list[dict]:
    """Per-pool economic summary, sorted by n."""
    by_pool: dict[str, list[float]] = defaultdict(list)
    for c in labeled:
        if c.get("net_return_rpc") is not None:
            by_pool[c["pool_address"]].append(c["net_return_rpc"])
    rows = []
    for pool, rets in sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)[:20]:
        s = _stats(rets)
        rows.append({"pool": pool, **s})
    return rows


def compute_regime_readout(labeled: list[dict]) -> dict[str, Any]:
    by_regime: dict[str, list[float]] = defaultdict(list)
    for c in labeled:
        if c.get("net_return_rpc") is not None:
            by_regime[c.get("regime_week", "?")].append(c["net_return_rpc"])
    return {k: _stats(v) for k, v in by_regime.items()}


def compute_failure_breakdown(candidates: list[dict]) -> dict[str, int]:
    counts: Counter = Counter()
    for c in candidates:
        reason = c.get("entry_fail_reason") or c.get("exit_fail_reason") or "ok"
        counts[reason] += 1
    return dict(counts)


def compute_concentration_audit(labeled: list[dict]) -> dict[str, Any]:
    """Top-5 pools vs tail — checks if edge concentrates in few pools."""
    by_pool: dict[str, list] = defaultdict(list)
    for c in labeled:
        if c.get("rpc_label") is not None:
            by_pool[c["pool_address"]].append(c)

    sorted_pools = sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)
    total_labeled = len(labeled)
    total_pos     = sum(1 for c in labeled if c.get("rpc_label") == 1)

    top5 = sorted_pools[:5]
    tail = sorted_pools[5:]

    def _pool_stat(pool_items):
        items = [c for _, cs in pool_items for c in cs]
        if not items:
            return {"n": 0, "pos_share": None, "mean_return": None}
        pos = sum(1 for c in items if c.get("rpc_label") == 1)
        rets = [c["net_return_rpc"] for c in items if c.get("net_return_rpc") is not None]
        return {
            "n":          len(items),
            "pos_share":  round(pos / max(len(items), 1), 4),
            "mean_return":round(sum(rets) / max(len(rets), 1), 6) if rets else None,
        }

    return {
        "total_labeled":          total_labeled,
        "total_positive":         total_pos,
        "total_positive_share":   round(total_pos / max(total_labeled, 1), 4),
        "unique_pools_labeled":   len(by_pool),
        "top5_pools":             [{"pool": p, **_pool_stat([(p, cs)])} for p, cs in top5],
        "top5_aggregate":         _pool_stat(top5),
        "tail_aggregate":         _pool_stat(tail),
    }


# ── Main regime pipeline ──────────────────────────────────────────────────────
def run_regime_pipeline(
    regime: dict,
    ch_url: str, ch_user: str, ch_pwd: str,
    rpc: Optional["RPC"],
    n_workers: int,
    do_rpc: bool,
) -> dict[str, Any]:
    """Full pipeline for one regime. Returns summary dict."""
    week_start = regime["week_start"]
    week_end   = regime["week_end"]
    _log(f"\n{'='*65}")
    _log(f"REGIME {week_start}  ({regime['label']})")

    # ── Phase 1: CH features ─────────────────────────────────────────────────
    feat_sql = build_feature_query(week_start, week_end)
    _log(f"  CH features [{week_start}…{week_end}] …")
    t0 = time.time()
    try:
        feature_rows = _ch_query(ch_url, feat_sql, ch_user, ch_pwd, timeout=600.0)
    except Exception as exc:
        _log(f"  ERROR: {exc}")
        return {"error": str(exc), "regime_week": week_start}
    _log(f"  Feature rows: {len(feature_rows)} in {time.time()-t0:.1f}s")

    end_ext = (
        datetime.strptime(week_end, "%Y-%m-%d") + timedelta(seconds=LABEL_EXIT_HORIZON_SEC + 300)
    ).strftime("%Y-%m-%d %H:%M:%S")
    price_sql = build_price_series_query(week_start, end_ext)
    _log(f"  CH price series …")
    t0 = time.time()
    try:
        price_rows = _ch_query(ch_url, price_sql, ch_user, ch_pwd, timeout=600.0)
    except Exception as exc:
        _log(f"  WARNING price series failed: {exc}")
        price_rows = []
    _log(f"  Price rows: {len(price_rows)} in {time.time()-t0:.1f}s")

    price_index = PoolPriceIndex()
    for pr in price_rows:
        price_index.ingest(
            str(pr.get("pool_address") or "").lower(),
            int(pr.get("ts_sec") or 0),
            int(pr.get("tick_i32") or 0),
        )

    regime_start_sec = int(datetime.strptime(week_start, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())
    regime_end_sec   = int(datetime.strptime(week_end, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())

    candidates: list[dict[str, Any]] = []
    for row in feature_rows:
        pool       = str(row.get("pool_address") or "").lower()
        entry_time = int(row.get("entry_time_sec") or 0)
        w0         = int(row.get("weth_is_token0") or 0)
        if not pool or entry_time <= 0:
            continue
        if entry_time < regime_start_sec or entry_time > regime_end_sec:
            continue

        feats = compute_features(row)
        feats["freshness_lag_sec"] = entry_time - feats.get("last_swap_sec", 0)

        has_exit_short = price_index.has_ticks_in_range(
            pool, entry_time, entry_time + 120
        )
        has_exit_full  = price_index.has_ticks_in_range(
            pool, entry_time, entry_time + LABEL_EXIT_HORIZON_SEC
        )

        c: dict[str, Any] = {
            "regime_week":         week_start,
            "pool_address":        pool,
            "entry_time_sec":      entry_time,
            "entry_time_iso":      datetime.fromtimestamp(
                entry_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fee_bps":             int(row.get("fee_bps") or 0),
            "token0_addr":         str(row.get("token0_addr") or ""),
            "token1_addr":         str(row.get("token1_addr") or ""),
            "weth_is_token0":      w0,
            **feats,
            "has_exit_short":      has_exit_short,
            "has_exit_full":       has_exit_full,
            # Entry probe fields (filled in Phase 2)
            "entry_block":         0,
            "entry_quote_ok":      False,
            "entry_amount_out_micro": 0,
            "entry_amount_out_tiny":  0,
            "entry_amount_out_stress":0,
            "entry_slippage_proxy":   None,
            "entry_fail_reason":      None,
            "out_of_range_suspected": False,
            # Exit label fields (filled in Phase 4)
            "exit_block":          0,
            "selected_for_exit":   False,
            "rpc_label":           None,
            "net_return_rpc":      None,
            "exit_fail_reason":    None,
            "exit_quote_ok":       False,
        }
        candidates.append(c)

    _log(f"  CH candidates: {len(candidates)}")

    if not do_rpc or rpc is None or not candidates:
        _write_candidates(candidates, week_start)
        return _dry_run_summary(candidates, week_start)

    # ── Phase 2: ts→block grid ─────────────────────────────────────────────
    ts_vals  = [c["entry_time_sec"] for c in candidates]
    ts_start = min(ts_vals) - 300
    ts_end   = max(ts_vals) + LABEL_EXIT_HORIZON_SEC + 600
    _log(f"  Building ts-grid …")
    try:
        bn_arr, ts_arr = build_ts_grid_for_regime(rpc, ts_start, ts_end)
    except Exception as exc:
        _log(f"  ERROR ts-grid: {exc}")
        _write_candidates(candidates, week_start)
        return _dry_run_summary(candidates, week_start)

    # ── Phase 3: Entry probe ALL candidates ───────────────────────────────
    _log(f"  Entry probe on ALL {len(candidates)} candidates …")
    t0   = time.time()
    lock = Lock()
    done_count = [0]

    def probe_one(c: dict) -> None:
        run_entry_probe(rpc, c, bn_arr, ts_arr)
        with lock:
            done_count[0] += 1
            if done_count[0] % 200 == 0:
                ok = sum(1 for x in candidates[:done_count[0]] if x.get("entry_quote_ok"))
                _log(f"    entry probe: {done_count[0]}/{len(candidates)} "
                     f"({ok} ok so far) [{time.time()-t0:.0f}s]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(probe_one, candidates))

    n_exe = sum(1 for c in candidates if c.get("entry_quote_ok"))
    n_fail0 = sum(1 for c in candidates if c.get("entry_fail_reason") == "fail_v3_entry_zero")
    _log(f"  Entry probe done: {n_exe}/{len(candidates)} executable "
         f"({n_exe/max(len(candidates),1):.1%}) "
         f"fail_entry_zero={n_fail0} ({n_fail0/max(len(candidates),1):.1%})")

    executable = [c for c in candidates if c.get("entry_quote_ok")]

    # ── Phase 3b: Slippage diagnostics (TINY + STRESS) ────────────────────
    if executable:
        _log(f"  Slippage diagnostics (TINY + STRESS) on {len(executable)} executable …")
        t0 = time.time()
        done_count[0] = 0

        def diag_one(c: dict) -> None:
            run_slippage_diagnostics(rpc, c, bn_arr, ts_arr)
            with lock:
                done_count[0] += 1

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            list(pool.map(diag_one, executable))
        _log(f"  Slippage diagnostics done [{time.time()-t0:.0f}s]")

    # ── Phase 4: Concentration cap + exit labels ──────────────────────────
    pool_exit_count: Counter = Counter()
    selected: list[dict] = []
    for c in executable:
        if pool_exit_count[c["pool_address"]] < MAX_EXIT_PER_POOL:
            c["selected_for_exit"] = True
            pool_exit_count[c["pool_address"]] += 1
            selected.append(c)

    _log(f"  Selected for exit label: {len(selected)}/{len(executable)} "
         f"(cap={MAX_EXIT_PER_POOL}/pool, {len(pool_exit_count)} pools)")

    if selected:
        _log(f"  Exit labeling {len(selected)} candidates …")
        t0 = time.time()
        done_count[0] = 0

        def exit_one(c: dict) -> None:
            run_exit_label(rpc, c, bn_arr, ts_arr)
            with lock:
                done_count[0] += 1
                if done_count[0] % 100 == 0:
                    ok_exit = sum(1 for x in selected[:done_count[0]] if x.get("exit_quote_ok"))
                    _log(f"    exit: {done_count[0]}/{len(selected)} "
                         f"({ok_exit} ok) [{time.time()-t0:.0f}s]")

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            list(pool.map(exit_one, selected))

        n_exit_ok = sum(1 for c in selected if c.get("exit_quote_ok"))
        n_pos     = sum(1 for c in selected if c.get("rpc_label") == 1)
        _log(f"  Exit done: {n_exit_ok}/{len(selected)} ok, "
             f"label_1={n_pos}/{n_exit_ok} "
             f"({n_pos/max(n_exit_ok,1):.1%})")

    # ── Write CSV ─────────────────────────────────────────────────────────
    _write_candidates(candidates, week_start)

    # ── Compute regime stats ───────────────────────────────────────────────
    labeled   = [c for c in selected if c.get("rpc_label") is not None]
    ok_rets   = sorted(c["net_return_rpc"] for c in labeled if c.get("net_return_rpc") is not None)
    pos_n     = sum(1 for c in labeled if c.get("rpc_label") == 1)
    trim_n    = max(1, len(ok_rets) // 10)
    trimmed   = ok_rets[trim_n:-trim_n] if len(ok_rets) > 2 * trim_n else ok_rets

    slip_vals = [c.get("entry_slippage_proxy") for c in executable
                 if c.get("entry_slippage_proxy") is not None]
    slip_vals.sort()

    return {
        "regime_week":          week_start,
        "ch_candidates":        len(candidates),
        "entry_quoteable":      n_exe,
        "entry_quoteable_rate": round(n_exe / max(len(candidates), 1), 4),
        "fail_v3_entry_zero":   n_fail0,
        "fail_v3_entry_zero_rate": round(n_fail0 / max(len(candidates), 1), 4),
        "selected_for_exit":    len(selected),
        "labeled":              len(labeled),
        "label_1":              pos_n,
        "label_1_share":        round(pos_n / max(len(labeled), 1), 4) if labeled else None,
        "median_return":        round(ok_rets[len(ok_rets)//2], 6) if ok_rets else None,
        "mean_return":          round(sum(ok_rets)/len(ok_rets), 6) if ok_rets else None,
        "trimmed_mean_return":  round(sum(trimmed)/max(len(trimmed),1), 6) if trimmed else None,
        "p10_return":           round(ok_rets[max(0,len(ok_rets)//10)], 6) if ok_rets else None,
        "p90_return":           round(ok_rets[min(len(ok_rets)-1,9*len(ok_rets)//10)], 6) if ok_rets else None,
        "failure_breakdown":    compute_failure_breakdown(candidates),
        "slippage_proxy_p50":   round(slip_vals[len(slip_vals)//2], 6) if slip_vals else None,
        "slippage_proxy_p10":   round(slip_vals[max(0,len(slip_vals)//10)], 6) if slip_vals else None,
        "pool_exit_count":      dict(pool_exit_count.most_common(10)),
        "pool_readout":         compute_pool_readout(labeled),
        "concentration_audit":  compute_concentration_audit(labeled),
        "feature_bucket":       compute_feature_bucket_analysis(candidates),
    }


def _write_candidates(candidates: list[dict], week_start: str) -> None:
    if not candidates:
        return
    # drop internal cache key before writing
    rows = [{k: v for k, v in c.items() if k != "_meme_addr"} for c in candidates]
    out_csv = OUT_DIR / f"candidates_{week_start}.csv"
    fields  = list(rows[0].keys())
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    _log(f"  Written: {out_csv} ({len(rows)} rows)")


def _dry_run_summary(candidates: list[dict], week_start: str) -> dict[str, Any]:
    return {
        "regime_week":      week_start,
        "ch_candidates":    len(candidates),
        "rpc_not_run":      True,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sprint037B-R4: Quoteability-first executable replay"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run",     action="store_true", help="full run with RPC")
    mode.add_argument("--dry-run", action="store_true", help="CH features only, no RPC")
    parser.add_argument("--regime", help="restrict to specific week_start")
    args = parser.parse_args()

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    ch_url  = os.environ.get("CLICKHOUSE_URL",  "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    n_workers = int(os.environ.get("LABEL_WORKERS", str(RPC_WORKER_THREADS)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    do_rpc = args.run and bool(rpc_url)
    if args.run and not rpc_url:
        _log("WARNING: --run but no LABEL_RPC_URL / EVM_HTTP_URL; running dry")
        do_rpc = False

    regimes_to_run = [
        r for r in REGIMES
        if (not args.regime or r["week_start"] == args.regime)
    ]
    _log(f"Regimes: {[r['week_start'] for r in regimes_to_run]}")
    _log(f"RPC workers: {n_workers}  do_rpc: {do_rpc}")

    rpc: Optional[RPC] = None
    if do_rpc:
        rpc = RPC(rpc_url)
        try:
            chid = int(rpc.call("eth_chainId", [])["result"], 16)
            if chid != CHAIN_ID:
                _log(f"ERROR: chain_id={chid} != {CHAIN_ID}")
                do_rpc = False
            else:
                _log(f"  RPC ok, chain_id={chid}")
        except Exception as exc:
            _log(f"  ERROR RPC: {exc}")
            do_rpc = False

    all_regime_summaries: list[dict] = []
    all_candidates: list[dict] = []

    for regime in regimes_to_run:
        reg_summary = run_regime_pipeline(
            regime, ch_url, ch_user, ch_pwd, rpc, n_workers, do_rpc
        )
        all_regime_summaries.append(reg_summary)

    # Reload all candidates from CSVs for cross-regime analysis
    for regime in regimes_to_run:
        csv_path = OUT_DIR / f"candidates_{regime['week_start']}.csv"
        if csv_path.exists():
            with csv_path.open() as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                # coerce numeric booleans
                for key in ("entry_quote_ok", "exit_quote_ok", "selected_for_exit",
                            "out_of_range_suspected", "has_exit_short", "has_exit_full"):
                    row[key] = row.get(key) in ("True", "1", "true")
                for key in ("rpc_label",):
                    v = row.get(key)
                    row[key] = int(v) if v not in (None, "", "None") else None
                for key in ("net_return_rpc",):
                    v = row.get(key)
                    row[key] = float(v) if v not in (None, "", "None") else None
            all_candidates.extend(rows)

    # Cross-regime analysis
    labeled_all  = [c for c in all_candidates if c.get("rpc_label") is not None]
    exe_all      = [c for c in all_candidates if c.get("entry_quote_ok")]

    funnel_all   = compute_funnel(all_candidates, exe_all, labeled_all)
    concentration= compute_concentration_audit(labeled_all)
    feature_bkts = compute_feature_bucket_analysis(all_candidates)

    # ── Status ────────────────────────────────────────────────────────────────
    if not do_rpc:
        status = "DRY_RUN_CH_CANDIDATES_ONLY"
    else:
        total_labeled = len(labeled_all)
        total_pos     = sum(1 for c in labeled_all if c.get("rpc_label") == 1)
        total_l1      = total_pos / max(total_labeled, 1)
        exe_rate      = funnel_all.get("entry_quoteable_rate", 0)
        # Regime-specific metrics
        feb02_summary = next(
            (s for s in all_regime_summaries if s.get("regime_week") == "2026-02-02"), {}
        )
        feb02_l1 = feb02_summary.get("label_1_share") or 0

        if exe_rate >= 0.40 and feb02_l1 >= 0.15 and total_labeled >= 500:
            status = "EXECUTABLE_REPLAY_PROMISING_REVIEW_REQUIRED"
        elif exe_rate >= 0.30 and feb02_l1 >= 0.10:
            status = "FEB02_ONLY_SCOPED_EDGE_CANDIDATE"
        elif exe_rate >= 0.30 and total_l1 >= 0.08:
            status = "QUOTEABILITY_GATE_WORKS_ECON_WEAK"
        elif total_labeled < 200:
            status = "NEEDS_MORE_EXECUTABLE_LABELS"
        elif exe_rate < 0.20:
            status = "DEAD_POOL_ARTIFACT_CONFIRMED_NO_EDGE"
        else:
            status = "ROUTE_PIVOT_REQUIRED"

    sprint037c_decision = "not_evaluated" if not do_rpc else (
        "executable_replay_expansion" if status == "EXECUTABLE_REPLAY_PROMISING_REVIEW_REQUIRED" else
        "rpc_label_expansion"         if status in ("FEB02_ONLY_SCOPED_EDGE_CANDIDATE",
                                                      "QUOTEABILITY_GATE_WORKS_ECON_WEAK") else
        "route_pivot"                 if status in ("DEAD_POOL_ARTIFACT_CONFIRMED_NO_EDGE",
                                                     "ROUTE_PIVOT_REQUIRED") else
        "needs_more_data"
    )

    # ── Summary JSON ──────────────────────────────────────────────────────────
    summary: dict[str, Any] = {
        "kind":       "sprint037b_r4_executable_replay",
        "version":    "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":     status,
        "decision_flags": DECISION_FLAGS,
        "sprint037c_decision": sprint037c_decision,
        "probe_config": {
            "tiny_wei":   PROBE_TINY_WEI,
            "micro_wei":  PROBE_MICRO_WEI,
            "stress_wei": PROBE_STRESS_WEI,
            "max_exit_per_pool": MAX_EXIT_PER_POOL,
            "label_exit_horizon_sec": LABEL_EXIT_HORIZON_SEC,
        },
        "candidate_funnel":       funnel_all,
        "per_regime":             {s["regime_week"]: s for s in all_regime_summaries
                                   if "regime_week" in s},
        "concentration_audit":    concentration,
        "feature_bucket_analysis":feature_bkts,
        "r2_baseline": {
            "source": "mvp/diagnostics/sprint037b_r2_summary.json",
            "ungated_quote_success_rate": 0.2705,
            "ungated_fail_v3_entry_zero_rate": 0.7204,
            "ungated_label_1_share": 0.0625,
        },
        "r3_baseline": {
            "source": "mvp/diagnostics/sprint037b_r3_summary.json",
            "best_gate_success_rate": 0.306,
            "best_gate_fail_entry_zero_rate": 0.688,
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    _log(f"\nWritten: {OUT_SUMMARY}")

    _write_report(summary, all_regime_summaries)

    # ── Final log ─────────────────────────────────────────────────────────────
    _log(f"\n{'='*65}")
    _log(f"STATUS: {status}")
    _log(f"Sprint037C decision: {sprint037c_decision}")
    if do_rpc:
        f = funnel_all
        _log(f"  CH candidates:      {f.get('ch_candidates', 0)}")
        _log(f"  Entry-quoteable:    {f.get('entry_quoteable', 0)} "
             f"({f.get('entry_quoteable_rate', 0):.1%})")
        _log(f"  fail_v3_entry_zero: {f.get('fail_v3_entry_zero', 0)} "
             f"({f.get('fail_v3_entry_zero_rate', 0):.1%})")
        _log(f"  Labeled:            {f.get('labeled', 0)}")
        _log(f"  label_1_share:      {concentration.get('total_positive_share', 0):.1%}")


# ── Report writer ─────────────────────────────────────────────────────────────
def _write_report(summary: dict, regime_summaries: list[dict]) -> None:
    r2 = summary.get("r2_baseline", {})
    r3 = summary.get("r3_baseline", {})
    f  = summary.get("candidate_funnel", {})
    pc = summary.get("probe_config", {})

    lines = [
        "# Sprint037B-R4 — Quoteability-first Executable Replay",
        "",
        f"Generated: {summary.get('created_at')}  ",
        f"**Status: `{summary.get('status')}`**  ",
        f"Sprint037C decision: **{summary.get('sprint037c_decision')}**",
        "",
        "---",
        "",
        "## 1. Architecture Shift",
        "",
        "| Phase | R2/R3 approach | R4 (quoteability-first) |",
        "|-------|---------------|-------------------------|",
        "| Candidate selection | CH active pools, sampled | CH active pools, ALL probed |",
        "| Entry gate | None (random sample) | QuoterV2 MICRO probe on every candidate |",
        "| Exit label | ~27% of sample succeed | Only executable candidates labeled |",
        "| RPC efficiency | ~70% wasted on dead pools | Near-zero waste |",
        "",
        "---",
        "",
        "## 2. Probe Configuration",
        "",
        f"- TINY  = {pc.get('tiny_wei')} wei ({pc.get('tiny_wei',0)/1e18:.5f} ETH) — zero-impact reference",
        f"- MICRO = {pc.get('micro_wei')} wei ({pc.get('micro_wei',0)/1e18:.4f} ETH) — authoritative gate (label-semantics-freeze.md)",
        f"- STRESS= {pc.get('stress_wei')} wei ({pc.get('stress_wei',0)/1e18:.3f} ETH) — depth diagnostic",
        f"- Concentration cap: max {pc.get('max_exit_per_pool')} exit labels per pool per regime",
        f"- Exit horizon: {pc.get('label_exit_horizon_sec')}s",
        "",
        "---",
        "",
        "## 3. Baseline Comparison (R2/R3 → R4)",
        "",
        "| Metric | R2 (ungated sample) | R3 (best gate) | R4 (quoteability-first) |",
        "|--------|---------------------|----------------|------------------------|",
        f"| Quote success rate | {r2.get('ungated_quote_success_rate',0):.1%} "
        f"| {r3.get('best_gate_success_rate',0):.1%} "
        f"| {f.get('entry_quoteable_rate', 'N/A')} |",
        f"| fail_v3_entry_zero rate | {r2.get('ungated_fail_v3_entry_zero_rate',0):.1%} "
        f"| {r3.get('best_gate_fail_entry_zero_rate',0):.1%} "
        f"| {f.get('fail_v3_entry_zero_rate', 'N/A')} |",
        f"| label_1_share | {r2.get('ungated_label_1_share',0):.1%} | ~8% | (see below) |",
        "",
        "---",
        "",
        "## 4. Candidate Funnel",
        "",
        "| Stage | Count | Rate |",
        "|-------|-------|------|",
        f"| CH feature candidates | {f.get('ch_candidates', 'N/A')} | 100% |",
        f"| Entry-quoteable (MICRO) | {f.get('entry_quoteable', 'N/A')} | {f.get('entry_quoteable_rate', 0):.1%} |" if isinstance(f.get('entry_quoteable_rate'), float) else f"| Entry-quoteable (MICRO) | {f.get('entry_quoteable', 'N/A')} | N/A |",
        f"| fail_v3_entry_zero | {f.get('fail_v3_entry_zero', 'N/A')} | {f.get('fail_v3_entry_zero_rate', 0):.1%} |" if isinstance(f.get('fail_v3_entry_zero_rate'), float) else f"| fail_v3_entry_zero | {f.get('fail_v3_entry_zero', 'N/A')} | N/A |",
        f"| Exit labeled | {f.get('labeled', 'N/A')} | {f.get('label_funnel_rate', 0):.1%} |" if isinstance(f.get('label_funnel_rate'), float) else f"| Exit labeled | {f.get('labeled', 'N/A')} | N/A |",
        "",
        "---",
        "",
        "## 5. Per-regime Results",
        "",
        "| Regime | CH cands | Executable | Exec% | Labeled | label_1 | Median ret | TrimMean |",
        "|--------|----------|------------|-------|---------|---------|-----------|----------|",
    ]

    for rs in regime_summaries:
        rw   = rs.get("regime_week", "?")
        ch   = rs.get("ch_candidates", "N/A")
        exe  = rs.get("entry_quoteable", "N/A")
        ep   = f"{rs.get('entry_quoteable_rate', 0):.1%}" if isinstance(rs.get("entry_quoteable_rate"), float) else "N/A"
        lab  = rs.get("labeled", "N/A")
        l1   = f"{rs.get('label_1_share', 0):.1%}" if isinstance(rs.get("label_1_share"), float) else "N/A"
        med  = f"{rs.get('median_return', 0):.4f}" if isinstance(rs.get("median_return"), float) else "N/A"
        trim = f"{rs.get('trimmed_mean_return', 0):.4f}" if isinstance(rs.get("trimmed_mean_return"), float) else "N/A"
        lines.append(f"| {rw} | {ch} | {exe} | {ep} | {lab} | {l1} | {med} | {trim} |")

    conc = summary.get("concentration_audit", {})
    lines += [
        "",
        "---",
        "",
        "## 6. Concentration Audit",
        "",
        f"- Total labeled: {conc.get('total_labeled', 'N/A')}",
        f"- Total positive: {conc.get('total_positive', 'N/A')} "
        f"({conc.get('total_positive_share', 0):.1%})" if isinstance(conc.get("total_positive_share"), float) else f"- Total positive: {conc.get('total_positive', 'N/A')}",
        f"- Unique pools with labels: {conc.get('unique_pools_labeled', 'N/A')}",
        "",
        "| Pool | N labeled | Pos share | Mean return |",
        "|------|-----------|-----------|-------------|",
    ]
    for tp in (conc.get("top5_pools") or [])[:5]:
        lines.append(
            f"| {tp.get('pool','')[:12]}… | {tp.get('n', 'N/A')} "
            f"| {tp.get('pos_share', 0):.1%} | {tp.get('mean_return', 0):.4f} |"
            if isinstance(tp.get("pos_share"), float) else
            f"| {tp.get('pool','')[:12]}… | {tp.get('n', 'N/A')} | N/A | N/A |"
        )

    top5_agg = conc.get("top5_aggregate", {})
    tail_agg  = conc.get("tail_aggregate", {})
    lines += [
        "",
        f"- Top-5 pools: n={top5_agg.get('n','N/A')} "
        f"pos={top5_agg.get('pos_share',0):.1%} "
        f"mean={top5_agg.get('mean_return',0):.4f}" if isinstance(top5_agg.get("pos_share"), float) else "",
        f"- Tail pools: n={tail_agg.get('n','N/A')} "
        f"pos={tail_agg.get('pos_share',0):.1%} "
        f"mean={tail_agg.get('mean_return',0):.4f}" if isinstance(tail_agg.get("pos_share"), float) else "",
        "",
        "---",
        "",
        "## 7. Decision Flags",
        "",
        "| Flag | Value |",
        "|------|-------|",
    ]
    for k, v in DECISION_FLAGS.items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        f"**Sprint037C decision: `{summary.get('sprint037c_decision')}`**",
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037b_r4_executable_replay.py*",
    ]

    OUT_REPORT.write_text("\n".join(lines))
    _log(f"Written: {OUT_REPORT}")


if __name__ == "__main__":
    main()
