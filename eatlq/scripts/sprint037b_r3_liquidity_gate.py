#!/usr/bin/env python3
"""Sprint037B-R3 — Liquidity-gated Replay + Batch RPC Validation
=================================================================

Root cause confirmed in Sprint037B-R2:
  72% of historical meme-pool candidates hit fail_v3_entry_zero.
  Tick movements came from pools that had already lost all tradeable
  liquidity (V3 in-range liquidity = 0).

This sprint adds entry-time liquidity / quoteability gates and measures
their effect on quote success rate, label_1_share, and net returns.

Gate tiers tested
-----------------
  ungated      — no filter (R2 baseline)
  exit_only    — pool must have ≥1 swap in [entry_time, +1800 s]
  liq_nonzero  — avg V3 in-range liquidity in feature window > 0
  liq_and_exit — liq_nonzero AND exit_only (primary gate)

New in R3 vs R2
---------------
  • CH feature query adds avg_liquidity / min_liquidity columns.
  • PoolPriceIndex.has_ticks_in_range() — O(log N) exit-window check.
  • Per-regime ts→block grid (scans ~3K blocks / regime, ~15× faster).
  • Full cross-tier comparison table in summary JSON and report.

Decision flags (frozen, never override from CLI)
-------------------------------------------------
  TRAINING_ALLOWED=false
  LIVE_ALLOWED=false
  MODEL_PROMOTION_ALLOWED=false
  H12_ABLATION_ALLOWED=false
  PHASE6_ALLOWED=false
  FAILED_NO_7D_ALPHA_ACTIVE=true
  HISTORICAL_REPLAY_REPAIR_ALLOWED=true
  RPC_BACKED_LABEL_VALIDATION_ALLOWED=true
  NO_ALPHA_CLAIM=true

Usage
-----
  # Dry run: builds candidates + gate flags, skips RPC
  python scripts/sprint037b_r3_liquidity_gate.py --dry-run

  # Full run (all 4 gate tiers × 3 regimes RPC-validated)
  LABEL_RPC_URL=https://... python scripts/sprint037b_r3_liquidity_gate.py --run

  # Single regime
  LABEL_RPC_URL=https://... python scripts/sprint037b_r3_liquidity_gate.py --run --regime 2025-12-01

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
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── Frozen constants (label-semantics-freeze.md) ─────────────────────────────
WETH                  = "0x4200000000000000000000000000000000000006"
CHAIN_ID              = 8453
ROUND_TRIP_COST       = 0.006
LABEL_EXIT_HORIZON_SEC= 1800
LABEL_NOTIONAL_WEI    = int(0.0005e18)
FEATURE_WINDOW_SEC    = 300
MIN_ACTIVITY_HEAT     = 5
QUOTER_V2_BASE        = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
MAX_ABS_NET_RETURN    = 10.0

# ── Gate tiers ────────────────────────────────────────────────────────────────
# Dry-run revealed liq_nonzero filters only 0.3% of candidates (swaps during
# the feature window imply non-zero in-range liquidity at that moment).
# The real failure mode: pools drain / go out-of-range AFTER the last observed
# swap in the feature window, BEFORE entry_time.
#
# Two targeted gates:
#   fresh        — last swap ≤ FRESHNESS_LAG_SEC before entry_time
#                  (pool was recently active, lower risk it drained before entry)
#   exit_short   — pool had ≥1 swap in [entry_time, entry_time + EXIT_SHORT_SEC]
#                  (pool was still being traded right after entry → had liquidity)
FRESHNESS_LAG_SEC = 120    # pool must have had a swap ≤120s before entry_time
EXIT_SHORT_SEC    = 120    # pool must have a swap within first 120s of exit window

GATE_TIERS = [
    # name, require_fresh, require_exit_short, require_exit_full
    {"name": "ungated",           "require_fresh": False, "require_exit_short": False, "require_exit_full": False},
    {"name": "exit_full",         "require_fresh": False, "require_exit_short": False, "require_exit_full": True},
    {"name": "fresh",             "require_fresh": True,  "require_exit_short": False, "require_exit_full": False},
    {"name": "fresh_and_exit_short", "require_fresh": True, "require_exit_short": True,  "require_exit_full": False},
]

STRATUM_SAMPLES_PER_TIER = 300
RPC_WORKER_THREADS        = 20

# ── Meme pool exclusions (aligned with Sprint037A/R2) ────────────────────────
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

# ── Regime windows ────────────────────────────────────────────────────────────
REGIMES = [
    {"week_start": "2025-12-01", "week_end": "2025-12-08",
     "label": "Dec-01 (strongest positive)"},
    {"week_start": "2025-12-29", "week_end": "2026-01-05",
     "label": "Dec-29 (holiday surge)"},
    {"week_start": "2026-02-02", "week_end": "2026-02-09",
     "label": "Feb-02 (highest activity)"},
]

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parents[1]
OUT_DIR     = REPO / "mvp/audits/sprint037b_r3"
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037b_r3_summary.json"
OUT_REPORT  = OUT_DIR / "sprint037b_r3_report.md"

DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "MODEL_PROMOTION_ALLOWED": False,
    "H12_ABLATION_ALLOWED": False,
    "PHASE6_ALLOWED": False,
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "HISTORICAL_REPLAY_REPAIR_ALLOWED": True,
    "RPC_BACKED_LABEL_VALIDATION_ALLOWED": True,
    "NO_ALPHA_CLAIM": True,
    "CLICKHOUSE_READ_ONLY": True,
}


def _log(msg: str) -> None:
    print(f"[r3 {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: unexpected statement: {sql[:60]!r}")
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


def build_feature_query_r3(start_dt: str, end_dt: str) -> str:
    """Feature aggregation query — same as R2 plus liquidity + freshness columns.

    New columns vs R2:
      avg_liquidity   — average V3 active in-range liquidity in feature window.
                        0 = pool had no in-range LPs for some swaps.
      min_liquidity   — minimum in-range liquidity in window.
      last_swap_sec   — Unix timestamp of the LAST swap in the feature window.
                        Used for the 'fresh' gate: entry_time - last_swap_sec
                        must be ≤ FRESHNESS_LAG_SEC (120s by default).
                        Pools whose last swap was >120s before entry_time were
                        already dying and likely out-of-range at entry.
    """
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
  -- WETH-leg corrected buy/sell classification
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_meme_count,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) <  '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) >= '8')
  )                                                                              AS sell_meme_count,
  -- Volume aggregates for flow-weighted features
  sum(if(mp.weth_is_token0 = 0,
        -toFloat64(eatlq_hex_to_int256(s.amount0_raw)),
         toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                        AS net_signed_flow,
  sum(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                       AS abs_flow_sum,
  sumIf(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
        (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
        (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_flow_sum,
  sum(pow(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))), 2))               AS abs_flow_sq_sum,
  -- V3 active in-range liquidity (diagnostic; root cause gate uses freshness instead)
  avg(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS avg_liquidity,
  min(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS min_liquidity,
  -- Freshness: last swap timestamp in feature window (key for fresh gate)
  max(toUnixTimestamp({te}))                                                    AS last_swap_sec,
  -- unique senders
  uniqCombinedIf(lower(s.sender),
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS unique_buyers,
  uniqCombined(lower(s.sender))                                                 AS unique_senders,
  -- tick aggregates for tick-proxy diagnostics
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
    """Tick series for tick-proxy + exit-activity checks."""
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                       AS pool_address,
  toUnixTimestamp({te})                       AS ts_sec,
  toInt32(eatlq_hex_to_int256(s.tick_raw))    AS tick_i32,
  mp.weth_is_token0
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
ORDER BY pool_address, ts_sec
"""


# ── Feature computation (identical to R2) ────────────────────────────────────
def compute_features(row: dict[str, Any]) -> dict[str, Any]:
    """FeatureStore-aligned feature vector (same semantics as R2)."""
    activity_heat     = int(row.get("activity_heat") or 0)
    buy_meme_count    = int(row.get("buy_meme_count") or 0)
    sell_meme_count   = int(row.get("sell_meme_count") or 0)
    net_signed_flow   = float(row.get("net_signed_flow") or 0.0)
    abs_flow_sum      = float(row.get("abs_flow_sum") or 0.0)
    buy_flow_sum      = float(row.get("buy_flow_sum") or 0.0)
    abs_flow_sq_sum   = float(row.get("abs_flow_sq_sum") or 0.0)
    unique_buyers     = int(row.get("unique_buyers") or 0)
    unique_senders    = int(row.get("unique_senders") or 0)
    tick_last         = int(row.get("tick_last") or 0)
    tick_min          = int(row.get("tick_min") or 0)
    tick_max          = int(row.get("tick_max") or 0)
    avg_liquidity     = float(row.get("avg_liquidity") or 0.0)
    min_liquidity     = float(row.get("min_liquidity") or 0.0)
    last_swap_sec     = int(row.get("last_swap_sec") or 0)

    total_count = max(buy_meme_count + sell_meme_count, 1)

    price_velocity_5m = (
        max(-1.0, min(1.0, net_signed_flow / abs_flow_sum))
        if abs_flow_sum > 0 else 0.0
    )
    buy_pressure_ratio = (
        max(0.0, min(1.0, buy_flow_sum / abs_flow_sum))
        if abs_flow_sum > 0 else 0.5
    )
    if activity_heat > 0 and abs_flow_sum > 0:
        mean_abs = abs_flow_sum / activity_heat
        variance = max(0.0, abs_flow_sq_sum / activity_heat - mean_abs ** 2)
        token_volatility_5m = min(2.0, max(0.0, math.sqrt(variance) / mean_abs))
    else:
        token_volatility_5m = 0.0

    unique_buyer_entropy_5m = math.log1p(unique_buyers)
    buy_pressure_ratio_count = max(0.0, min(1.0, buy_meme_count / total_count))

    return {
        "price_velocity_5m":      round(price_velocity_5m, 6),
        "buy_pressure_ratio":     round(buy_pressure_ratio, 6),
        "token_volatility_5m":    round(token_volatility_5m, 6),
        "activity_heat":          activity_heat,
        "unique_buyer_entropy_5m":round(unique_buyer_entropy_5m, 6),
        "buy_pressure_ratio_count":round(buy_pressure_ratio_count, 6),
        "buy_meme_count":         buy_meme_count,
        "sell_meme_count":        sell_meme_count,
        "unique_buyers":          unique_buyers,
        "unique_senders":         unique_senders,
        "abs_flow_sum":           round(abs_flow_sum, 2),
        "avg_liquidity":          avg_liquidity,
        "min_liquidity":          min_liquidity,
        "last_swap_sec":          last_swap_sec,
        "tick_last":              tick_last,
        "tick_range":             abs(tick_max - tick_min),
    }


# ── PoolPriceIndex with exit-activity support ─────────────────────────────────
class PoolPriceIndex:
    """Per-pool sorted tick series for O(log N) tick-proxy + exit checks."""

    def __init__(self) -> None:
        self._ts:   dict[str, list[int]] = defaultdict(list)
        self._tick: dict[str, list[int]] = defaultdict(list)

    def ingest(self, pool: str, ts: int, tick: int) -> None:
        if pool and ts > 0:
            self._ts[pool].append(ts)
            self._tick[pool].append(tick)

    def get_last_tick_before(self, pool: str, ts: int) -> Optional[int]:
        series = self._ts.get(pool)
        if not series:
            return None
        idx = bisect.bisect_left(series, ts)
        if idx == 0:
            return None
        return self._tick[pool][idx - 1]

    def has_ticks_in_range(self, pool: str, ts_start: int, ts_end: int) -> bool:
        """True if at least one tick observation exists in [ts_start, ts_end)."""
        series = self._ts.get(pool, [])
        lo = bisect.bisect_left(series, ts_start)
        hi = bisect.bisect_left(series, ts_end)
        return hi > lo

    def pools(self) -> set[str]:
        return set(self._ts.keys())


def compute_tick_proxy(price_index: PoolPriceIndex, pool: str,
                       entry_time: int, weth_is_token0: int) -> Optional[float]:
    """Tick-based 30m return proxy (diagnostic only, not authoritative)."""
    entry_tick = price_index.get_last_tick_before(pool, entry_time)
    if entry_tick is None:
        return None
    exit_tick = price_index.get_last_tick_before(pool, entry_time + LABEL_EXIT_HORIZON_SEC)
    if exit_tick is None:
        return None
    tick_delta = exit_tick - entry_tick
    if int(weth_is_token0) == 1:
        tick_delta = -tick_delta
    tick_delta_capped = max(-50000, min(50000, tick_delta))
    raw_return = math.expm1(tick_delta_capped * 9.9995000333297e-5)
    return max(-1.0, min(5.0, raw_return - ROUND_TRIP_COST))


# ── Gate flag computation ──────────────────────────────────────────────────────
def compute_gate_flags(
    candidate: dict[str, Any],
    price_index: PoolPriceIndex,
) -> dict[str, bool]:
    """Return gate flag booleans for all 4 tiers.

    Gates:
      ungated              — always True (baseline)
      exit_full            — pool has ≥1 swap in [entry_time, +1800s]
      fresh                — last feature-window swap ≤ FRESHNESS_LAG_SEC before entry
      fresh_and_exit_short — fresh AND pool has ≥1 swap in [entry_time, +EXIT_SHORT_SEC]

    Rationale:
      liq_nonzero (avg_liquidity > 0) filtered only 0.3% in dry-run — ineffective.
      The failure mode: pool drains BETWEEN last observed swap and entry_time (≤5 min).
      'fresh' addresses this directly: if the last swap was within 120s of entry_time,
      the pool was active right before entry, reducing the drain-window to ≤120s.
      'exit_short' adds: pool continued trading in the first 120s of exit window,
      proving it was actually liquid at entry (couldn't trade if liquidity were 0).
    """
    pool          = candidate["pool_address"]
    entry_time    = candidate["entry_time_sec"]
    last_swap_sec = int(candidate.get("last_swap_sec") or 0)

    freshness_lag = entry_time - last_swap_sec if last_swap_sec > 0 else 9999
    is_fresh      = freshness_lag <= FRESHNESS_LAG_SEC

    has_exit_full  = price_index.has_ticks_in_range(
        pool, entry_time, entry_time + LABEL_EXIT_HORIZON_SEC
    )
    has_exit_short = price_index.has_ticks_in_range(
        pool, entry_time, entry_time + EXIT_SHORT_SEC
    )

    return {
        "gate_ungated":              True,
        "gate_exit_full":            has_exit_full,
        "gate_fresh":                is_fresh,
        "gate_fresh_and_exit_short": is_fresh and has_exit_short,
        # diagnostic
        "freshness_lag_sec":         freshness_lag,
        "has_exit_short":            has_exit_short,
    }


# ── Stratified sampling ────────────────────────────────────────────────────────
def stratify_candidates(
    candidates: list[dict[str, Any]],
    target_n: int,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Stratified sample preserving fee tier, weth side, activity, proxy return."""
    import random
    rng = random.Random(seed)

    fat_pos = [c for c in candidates if (c.get("tick_proxy_return") or 0.0) >= 1.0]
    fat_neg = [c for c in candidates if (c.get("tick_proxy_return") or 0.0) <= -1.0]
    forced  = fat_pos[:50] + fat_neg[:50]
    forced_ids = {id(c) for c in forced}
    remainder  = [c for c in candidates if id(c) not in forced_ids]

    def _key(c: dict) -> tuple:
        fee  = c.get("fee_bps", 0)
        w0   = c.get("weth_is_token0", 0)
        ah   = c.get("activity_heat", 0)
        ab   = "low" if ah < 20 else ("mid" if ah < 100 else "high")
        tr   = c.get("tick_proxy_return") or 0.0
        rb   = ("loss" if tr < -0.1 else ("low" if tr < 0.1 else "high"))
        return (fee, w0, ab, rb)

    strata: dict[tuple, list] = defaultdict(list)
    for c in remainder:
        strata[_key(c)].append(c)

    budget     = max(0, target_n - len(forced))
    per_strat  = max(1, budget // max(len(strata), 1))
    sampled    = list(forced)
    for members in strata.values():
        sampled.extend(rng.sample(members, min(per_strat, len(members))))
        if len(sampled) >= target_n:
            break

    return sampled[:target_n]


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
        rid = self._next_id()
        body = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params, "id": rid}).encode()
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


# ── Per-regime ts→block grid (NEW in R3) ──────────────────────────────────────
def build_ts_grid_for_regime(
    rpc: RPC,
    ts_start: int,
    ts_end: int,
    step: int = 50,
) -> tuple[list[int], list[int]]:
    """Efficient ts→block grid covering only [ts_start, ts_end].

    R2 scanned from Dec-2025 to latest block (~136K samples at step=50).
    R3 estimates the relevant block range and scans only that window
    (~3K samples per regime, ~15× faster).
    """
    r = rpc.call("eth_getBlockByNumber", ["latest", False])
    latest_num = int(r["result"]["number"], 16)
    latest_ts  = int(r["result"]["timestamp"], 16)

    BASE_BLOCK_TIME = 2.0   # Base ~2s/block
    MARGIN = 5000           # ±5000 blocks margin

    lo_block = max(0, int(latest_num - (latest_ts - ts_start) / BASE_BLOCK_TIME) - MARGIN)
    hi_block = min(latest_num,
                   int(latest_num - (latest_ts - ts_end) / BASE_BLOCK_TIME) + MARGIN)

    block_nums = list(range(lo_block, hi_block + 1, step))
    batch_size = 500
    results_map: dict[int, int] = {}
    t0 = time.time()

    for i in range(0, len(block_nums), batch_size):
        sub   = block_nums[i:i + batch_size]
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in sub]
        for bn, res in zip(sub, rpc.batch(calls)):
            if res and res.get("result"):
                results_map[bn] = int(res["result"]["timestamp"], 16)
        _log(f"    ts-grid: {min(i + batch_size, len(block_nums))}/{len(block_nums)} "
             f"blocks [{time.time() - t0:.1f}s]")

    sorted_bn = sorted(results_map.keys())
    sorted_ts = [results_map[b] for b in sorted_bn]
    span = f"[{sorted_ts[0] if sorted_ts else -1}..{sorted_ts[-1] if sorted_ts else -1}]"
    _log(f"    ts-grid: {len(sorted_bn)} samples, ts span {span}")
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


# ── QuoterV2 encoding (verbatim from label_dashcam_executable.py) ─────────────
_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(h: str) -> str:
    return h.zfill(64)

def _addr_pad(addr: str) -> str:
    a = addr[2:] if addr.startswith("0x") else addr
    return _pad32(a.lower())

def _uint_pad(n: int) -> str:
    return _pad32(hex(n)[2:])

def _strip_0x(h: str) -> str:
    return h[2:] if h.startswith("0x") else h


def _eth_call(rpc: RPC, to: str, data: str, block_num: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block_num)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_quoter_v3(rpc: RPC, token_in: str, token_out: str,
                   amount_in: int, fee: int, block: int) -> int:
    """Returns amountOut from QuoterV2.quoteExactInputSingle; 0 on failure."""
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
    if len(raw) < 64:
        return 0
    return int(raw[0:64], 16)


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET_RETURN or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET_RETURN, net)), True
    return net, False


def label_v3_candidate(
    rpc: RPC,
    pool: str, token0: str, token1: str, fee_bps: int,
    entry_time: int,
    bn_arr: list[int], ts_arr: list[int],
) -> tuple[int, float, str]:
    """QuoterV2 label. Returns (label, net_return, label_type).
    label=-1 = quote failed (row excluded per label-semantics-freeze.md).
    """
    weth = WETH.lower()
    t0, t1 = token0.lower(), token1.lower()
    if weth not in (t0, t1):
        return -1, 0.0, "fail_no_weth_leg"
    meme = t1 if t0 == weth else t0

    entry_block = ts_to_block(bn_arr, ts_arr, entry_time)
    exit_block  = ts_to_block(bn_arr, ts_arr, entry_time + LABEL_EXIT_HORIZON_SEC)
    if entry_block <= 0 or exit_block <= 0:
        return -1, 0.0, "fail_block_lookup"

    try:
        tokens = call_quoter_v3(rpc, weth, meme, LABEL_NOTIONAL_WEI, fee_bps, entry_block)
    except Exception:
        return -1, 0.0, "fail_entry_rpc_error"
    if tokens <= 0:
        return -1, 0.0, "fail_v3_entry_zero"

    try:
        weth_back = call_quoter_v3(rpc, meme, weth, tokens, fee_bps, exit_block)
    except Exception:
        return -1, 0.0, "fail_exit_rpc_error"
    if weth_back <= 0:
        return 0, -1.0, "executable_exit_drained"

    net, bogus = _sanity_clip(weth_back / LABEL_NOTIONAL_WEI - 1.0 - ROUND_TRIP_COST)
    if bogus:
        return -1, 0.0, "fail_sanity_clip"
    return 1 if net > 0 else 0, net, "v3_quote_ok"


# ── RPC batch labeler for one gate tier ───────────────────────────────────────
def run_rpc_for_tier(
    rpc: RPC,
    bn_arr: list[int],
    ts_arr: list[int],
    candidates: list[dict],
    tier_name: str,
    n_workers: int,
) -> dict[str, Any]:
    """Stratified sample + RPC validation for a single gate tier.

    Returns per-regime stats dict.
    """
    regimes_present = sorted({c["regime_week"] for c in candidates})
    per_regime_stats: dict[str, dict] = {}

    for regime_week in regimes_present:
        regime_cands = [c for c in candidates if c["regime_week"] == regime_week]
        sample = stratify_candidates(regime_cands, STRATUM_SAMPLES_PER_TIER)
        _log(f"  [{tier_name}] {regime_week}: {len(sample)}/{len(regime_cands)} sampled for RPC")

        reasons: Counter = Counter()
        lock = Lock()

        # Use a thread-local label key per tier
        lbl_key = f"rpc_label_{tier_name}"
        ret_key = f"net_return_{tier_name}"
        typ_key = f"rpc_type_{tier_name}"

        def label_one(c: dict) -> None:
            lbl, net, lt = label_v3_candidate(
                rpc, c["pool_address"], c["token0_addr"], c["token1_addr"],
                c["fee_bps"], c["entry_time_sec"], bn_arr, ts_arr,
            )
            c[lbl_key] = lbl if lbl >= 0 else None
            c[ret_key] = net if lbl >= 0 else None
            c[typ_key] = lt
            with lock:
                reasons[lt] += 1

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = {pool.submit(label_one, c): c for c in sample}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    fut.result()
                except Exception as exc:
                    _log(f"    worker error: {exc}")
                if done % 50 == 0:
                    _log(f"    [{tier_name}] {regime_week}: {done}/{len(sample)} done")

        n_ok    = sum(1 for c in sample if c.get(lbl_key) is not None)
        n_total = len(sample)
        n_fail  = n_total - n_ok
        pos_n   = sum(1 for c in sample if c.get(lbl_key) == 1)
        ok_returns = sorted(c[ret_key] for c in sample if c.get(ret_key) is not None)
        trim_n  = max(1, n_ok // 10)
        trimmed = ok_returns[trim_n:-trim_n] if n_ok > 2 * trim_n else ok_returns

        per_regime_stats[regime_week] = {
            "n_sampled":           n_total,
            "n_ok":                n_ok,
            "n_fail":              n_fail,
            "quote_success_rate":  round(n_ok / max(n_total, 1), 4),
            "label_1_share":       round(pos_n / max(n_ok, 1), 4) if n_ok > 0 else None,
            "median_net_return":   round(ok_returns[n_ok // 2], 6) if ok_returns else None,
            "mean_net_return":     round(sum(ok_returns) / n_ok, 6) if ok_returns else None,
            "trimmed_mean":        round(sum(trimmed) / max(len(trimmed), 1), 6) if trimmed else None,
            "reason_counts":       dict(reasons),
        }
        _log(
            f"    [{tier_name}] {regime_week}: "
            f"ok={n_ok}/{n_total} ({per_regime_stats[regime_week]['quote_success_rate']:.1%}), "
            f"label_1={per_regime_stats[regime_week]['label_1_share']}, "
            f"fail_entry_zero={reasons.get('fail_v3_entry_zero', 0)}"
        )

    return per_regime_stats


# ── Cross-tier comparison builder ─────────────────────────────────────────────
def build_comparison_table(
    candidates_by_tier: dict[str, list[dict]],
    rpc_stats_by_tier: dict[str, dict],
) -> list[dict]:
    """Produce one summary row per gate tier for easy comparison."""
    rows = []
    for tier in GATE_TIERS:
        name  = tier["name"]
        cands = candidates_by_tier.get(name, [])
        rpc   = rpc_stats_by_tier.get(name, {})

        total_sampled = sum(s.get("n_sampled", 0) for s in rpc.values())
        total_ok      = sum(s.get("n_ok", 0) for s in rpc.values())
        total_fail_entry = 0
        for stats in rpc.values():
            total_fail_entry += stats.get("reason_counts", {}).get("fail_v3_entry_zero", 0)

        lbl_key = f"rpc_label_{name}"
        ret_key = f"net_return_{name}"
        all_returns: list[float] = []
        pos_n = 0
        for c in cands:
            if c.get(lbl_key) == 1:
                pos_n += 1
            if c.get(ret_key) is not None:
                all_returns.append(c[ret_key])

        all_returns.sort()
        n_ok = len(all_returns)
        trim_n  = max(1, n_ok // 10)
        trimmed = all_returns[trim_n:-trim_n] if n_ok > 2 * trim_n else all_returns

        rows.append({
            "tier":               name,
            "require_fresh":      tier["require_fresh"],
            "require_exit_short": tier["require_exit_short"],
            "require_exit_full":  tier["require_exit_full"],
            "candidate_count":    len(cands),
            "rpc_sampled":        total_sampled,
            "rpc_ok":             total_ok,
            "quote_success_rate": round(total_ok / max(total_sampled, 1), 4),
            "fail_v3_entry_zero": total_fail_entry,
            "fail_entry_rate":    round(total_fail_entry / max(total_sampled, 1), 4),
            "label_1_share":      round(pos_n / max(total_ok, 1), 4) if total_ok > 0 else None,
            "median_net_return":  round(all_returns[n_ok // 2], 6) if all_returns else None,
            "mean_net_return":    round(sum(all_returns) / n_ok, 6) if all_returns else None,
            "trimmed_mean":       round(sum(trimmed) / max(len(trimmed), 1), 6) if trimmed else None,
            "per_regime":         rpc,
        })
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sprint037B-R3: Liquidity-gated replay + batch RPC validation"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run",      action="store_true", help="full run with RPC labels")
    mode.add_argument("--dry-run",  action="store_true", help="build candidates, skip RPC")
    parser.add_argument("--regime", help="restrict to specific regime week_start")
    args = parser.parse_args()

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    ch_url  = os.environ.get("CLICKHOUSE_URL",  "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "")
    n_workers = int(os.environ.get("LABEL_WORKERS", str(RPC_WORKER_THREADS)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    do_rpc = args.run and bool(rpc_url)
    if args.run and not rpc_url:
        _log("WARNING: --run but no LABEL_RPC_URL / EVM_HTTP_URL; skipping RPC labels")
        do_rpc = False

    regimes_to_run = [
        r for r in REGIMES
        if (not args.regime or r["week_start"] == args.regime)
    ]
    _log(f"Regimes: {[r['week_start'] for r in regimes_to_run]}")

    # ── Phase 1: ClickHouse — feature rows + price series ─────────────────────
    all_candidates: list[dict[str, Any]] = []

    for regime in regimes_to_run:
        week_start = regime["week_start"]
        week_end   = regime["week_end"]
        _log(f"\n{'='*60}")
        _log(f"REGIME {week_start}  ({regime['label']})")

        feat_sql = build_feature_query_r3(week_start, week_end)
        _log(f"  CH features [{week_start}…{week_end}] …")
        t0 = time.time()
        try:
            feature_rows = _ch_query(ch_url, feat_sql, ch_user, ch_pwd, timeout=600.0)
        except Exception as exc:
            _log(f"  ERROR features: {exc}")
            continue
        _log(f"  Feature rows: {len(feature_rows)} in {time.time()-t0:.1f}s")

        end_with_horizon = (
            datetime.strptime(week_end, "%Y-%m-%d") + timedelta(seconds=LABEL_EXIT_HORIZON_SEC)
        ).strftime("%Y-%m-%d %H:%M:%S")
        price_sql = build_price_series_query(week_start, end_with_horizon)
        _log(f"  CH price series …")
        t0 = time.time()
        try:
            price_rows = _ch_query(ch_url, price_sql, ch_user, ch_pwd, timeout=600.0)
        except Exception as exc:
            _log(f"  ERROR price series: {exc}")
            price_rows = []
        _log(f"  Price rows: {len(price_rows)} in {time.time()-t0:.1f}s")

        # build price index
        price_index = PoolPriceIndex()
        for pr in price_rows:
            price_index.ingest(
                str(pr.get("pool_address") or "").lower(),
                int(pr.get("ts_sec") or 0),
                int(pr.get("tick_i32") or 0),
            )
        _log(f"  Price index: {len(price_index.pools())} pools")

        regime_start_sec = int(
            datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )
        regime_end_sec = int(
            datetime.strptime(week_end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )

        regime_candidates: list[dict] = []
        for row in feature_rows:
            pool       = str(row.get("pool_address") or "").lower()
            entry_time = int(row.get("entry_time_sec") or 0)
            w0         = int(row.get("weth_is_token0") or 0)
            if not pool or entry_time <= 0:
                continue
            if entry_time < regime_start_sec or entry_time > regime_end_sec:
                continue

            feats      = compute_features(row)
            tick_proxy = compute_tick_proxy(price_index, pool, entry_time, w0)
            gate_flags = compute_gate_flags(
                {"pool_address": pool, "entry_time_sec": entry_time,
                 "avg_liquidity": feats["avg_liquidity"],
                 "min_liquidity": feats["min_liquidity"],
                 "last_swap_sec": feats["last_swap_sec"]},
                price_index,
            )

            candidate: dict[str, Any] = {
                "regime_week":    week_start,
                "pool_address":   pool,
                "entry_time_sec": entry_time,
                "entry_time_iso": datetime.fromtimestamp(
                    entry_time, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fee_bps":        int(row.get("fee_bps") or 0),
                "token0_addr":    str(row.get("token0_addr") or ""),
                "token1_addr":    str(row.get("token1_addr") or ""),
                "weth_is_token0": w0,
                **feats,
                **gate_flags,
                "tick_proxy_return": round(tick_proxy, 6) if tick_proxy is not None else None,
                "tick_proxy_label":  (1 if tick_proxy is not None and tick_proxy > 0 else
                                      (0 if tick_proxy is not None else None)),
            }
            # RPC label placeholders (one per gate tier)
            for t in GATE_TIERS:
                n = t["name"]
                candidate[f"rpc_label_{n}"]   = None
                candidate[f"net_return_{n}"]  = None
                candidate[f"rpc_type_{n}"]    = None

            regime_candidates.append(candidate)

        _log(f"  Candidates: {len(regime_candidates)}")

        # Gate counts
        for t in GATE_TIERS:
            gate_key = f"gate_{t['name']}"
            n_pass   = sum(1 for c in regime_candidates if c.get(gate_key))
            _log(f"    {t['name']:28s}: {n_pass}/{len(regime_candidates)} pass "
                 f"({n_pass/max(len(regime_candidates),1):.1%})")
        # freshness distribution
        lags = [c.get("freshness_lag_sec", 9999) for c in regime_candidates]
        p50_lag = sorted(lags)[len(lags) // 2]
        p10_lag = sorted(lags)[max(0, len(lags) // 10)]
        _log(f"    freshness_lag_sec: p10={p10_lag}s  p50={p50_lag}s")

        # Write full per-regime CSV (all candidates, all gates)
        out_csv = OUT_DIR / f"candidates_{week_start}.csv"
        if regime_candidates:
            fields = list(regime_candidates[0].keys())
            with out_csv.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(regime_candidates)
            _log(f"  Written: {out_csv} ({len(regime_candidates)} rows)")

        all_candidates.extend(regime_candidates)

    _log(f"\nTotal candidates across all regimes: {len(all_candidates)}")

    # ── Phase 2: RPC label validation per gate tier ────────────────────────────
    rpc_stats_by_tier: dict[str, dict] = {t["name"]: {} for t in GATE_TIERS}

    if do_rpc:
        _log(f"\n{'='*60}")
        _log("RPC LABEL PHASE")
        rpc = RPC(rpc_url)

        try:
            r    = rpc.call("eth_chainId", [])
            chid = int(r["result"], 16)
            if chid != CHAIN_ID:
                _log(f"ERROR: chain_id={chid}, expected {CHAIN_ID}")
                do_rpc = False
            else:
                _log(f"  RPC ok, chain_id={chid}")
        except Exception as exc:
            _log(f"  ERROR: RPC failed: {exc}")
            do_rpc = False

    if do_rpc:
        for tier in GATE_TIERS:
            tier_name = tier["name"]
            gate_key  = f"gate_{tier_name}"
            gated     = [c for c in all_candidates if c.get(gate_key)]

            _log(f"\n{'─'*60}")
            _log(f"GATE TIER: {tier_name}  ({len(gated)} candidates)")

            if not gated:
                _log(f"  No candidates pass {tier_name} gate — skipping RPC")
                continue

            # Build per-regime ts_grid (fast: scans only regime's block range)
            for regime in regimes_to_run:
                week_start = regime["week_start"]
                regime_gated = [c for c in gated if c["regime_week"] == week_start]
                if not regime_gated:
                    continue

                ts_vals   = [c["entry_time_sec"] for c in regime_gated]
                ts_start  = min(ts_vals) - 300
                ts_end    = max(ts_vals) + LABEL_EXIT_HORIZON_SEC + 600

                _log(f"  [{tier_name}] Building ts-grid for {week_start} …")
                try:
                    bn_arr, ts_arr = build_ts_grid_for_regime(rpc, ts_start, ts_end)
                except Exception as exc:
                    _log(f"  ERROR ts-grid: {exc}")
                    continue

                # Label all gated candidates for this regime × tier
                regime_cands_for_tier = [
                    c for c in all_candidates
                    if c["regime_week"] == week_start and c.get(gate_key)
                ]
                sample = stratify_candidates(regime_cands_for_tier, STRATUM_SAMPLES_PER_TIER)
                _log(f"  [{tier_name}] {week_start}: {len(sample)}/{len(regime_cands_for_tier)} sampled")

                reasons: Counter = Counter()
                lock = Lock()
                lbl_key = f"rpc_label_{tier_name}"
                ret_key = f"net_return_{tier_name}"
                typ_key = f"rpc_type_{tier_name}"

                def label_one(c: dict, _b=bn_arr, _t=ts_arr) -> None:  # noqa: B023
                    lbl, net, lt = label_v3_candidate(
                        rpc, c["pool_address"], c["token0_addr"], c["token1_addr"],
                        c["fee_bps"], c["entry_time_sec"], _b, _t,
                    )
                    c[lbl_key] = lbl if lbl >= 0 else None
                    c[ret_key] = net if lbl >= 0 else None
                    c[typ_key] = lt
                    with lock:
                        reasons[lt] += 1

                with ThreadPoolExecutor(max_workers=n_workers) as pool_ex:
                    futs = {pool_ex.submit(label_one, c): c for c in sample}
                    done = 0
                    for fut in as_completed(futs):
                        done += 1
                        try:
                            fut.result()
                        except Exception as exc:
                            _log(f"    worker error: {exc}")
                        if done % 50 == 0:
                            _log(f"    [{tier_name}] {week_start}: {done}/{len(sample)} done")

                n_ok     = sum(1 for c in sample if c.get(lbl_key) is not None)
                n_total  = len(sample)
                ok_rets  = sorted(c[ret_key] for c in sample if c.get(ret_key) is not None)
                pos_n    = sum(1 for c in sample if c.get(lbl_key) == 1)
                trim_n   = max(1, n_ok // 10)
                trimmed  = ok_rets[trim_n:-trim_n] if n_ok > 2 * trim_n else ok_rets

                rpc_stats_by_tier[tier_name][week_start] = {
                    "n_sampled":          n_total,
                    "n_ok":               n_ok,
                    "n_fail":             n_total - n_ok,
                    "quote_success_rate": round(n_ok / max(n_total, 1), 4),
                    "label_1_share":      round(pos_n / max(n_ok, 1), 4) if n_ok > 0 else None,
                    "median_net_return":  round(ok_rets[n_ok // 2], 6) if ok_rets else None,
                    "mean_net_return":    round(sum(ok_rets) / n_ok, 6) if ok_rets else None,
                    "trimmed_mean":       round(sum(trimmed) / max(len(trimmed), 1), 6) if trimmed else None,
                    "reason_counts":      dict(reasons),
                }
                _log(
                    f"  [{tier_name}] {week_start}: "
                    f"ok={n_ok}/{n_total} "
                    f"({rpc_stats_by_tier[tier_name][week_start]['quote_success_rate']:.1%}), "
                    f"label_1={rpc_stats_by_tier[tier_name][week_start]['label_1_share']}, "
                    f"fail_zero={reasons.get('fail_v3_entry_zero', 0)}"
                )

                # Rewrite regime CSV with updated RPC labels
                out_csv = OUT_DIR / f"candidates_{week_start}.csv"
                regime_all = [c for c in all_candidates if c["regime_week"] == week_start]
                if regime_all:
                    fields = list(regime_all[0].keys())
                    with out_csv.open("w", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=fields)
                        w.writeheader()
                        w.writerows(regime_all)

    # ── Phase 3: Comparison table & summary ───────────────────────────────────
    _log(f"\n{'='*60}")
    _log("COMPARISON TABLE")

    candidates_by_tier = {
        t["name"]: [c for c in all_candidates if c.get(f"gate_{t['name']}")]
        for t in GATE_TIERS
    }

    comparison_table = build_comparison_table(candidates_by_tier, rpc_stats_by_tier)

    _log(f"\n{'Tier':<20} {'Cands':>7} {'RPC ok':>7} {'SuccRate':>9} "
         f"{'Fail0':>6} {'Lbl1':>6} {'TrimMean':>9}")
    _log("─" * 75)
    for row in comparison_table:
        _log(
            f"{row['tier']:<20} {row['candidate_count']:>7} "
            f"{row.get('rpc_ok', '-'):>7} "
            f"{row['quote_success_rate']:.1%} " if row['rpc_ok'] else
            f"{row['tier']:<20} {row['candidate_count']:>7} {'N/A':>7} {'N/A':>9} "
            f"{'N/A':>6} {'N/A':>6} {'N/A':>9}"
        )

    # nicer print
    for row in comparison_table:
        qsr  = f"{row['quote_success_rate']:.1%}" if row.get("rpc_sampled") else "N/A"
        l1s  = f"{row['label_1_share']:.1%}" if row.get("label_1_share") is not None else "N/A"
        tmn  = f"{row['trimmed_mean']:.4f}" if row.get("trimmed_mean") is not None else "N/A"
        f0   = str(row.get("fail_v3_entry_zero", "N/A"))
        _log(
            f"  {row['tier']:<20} cands={row['candidate_count']:>5}  "
            f"rpc={row.get('rpc_ok','N/A')}/{row.get('rpc_sampled','N/A')}  "
            f"success={qsr}  fail0={f0}  label1={l1s}  trim={tmn}"
        )

    # Gate-by-gate improvement vs ungated
    ungated_success = next(
        (r["quote_success_rate"] for r in comparison_table if r["tier"] == "ungated"), None
    )
    best_tier = max(
        (r for r in comparison_table if r.get("rpc_sampled")),
        key=lambda r: r["quote_success_rate"],
        default=None,
    )

    if best_tier:
        uplift = best_tier["quote_success_rate"] - (ungated_success or 0)
        _log(f"\n  Best tier: {best_tier['tier']}  "
             f"success={best_tier['quote_success_rate']:.1%}  "
             f"vs ungated {ungated_success:.1%}  "
             f"uplift={uplift:+.1%}")

    # ── Status determination ───────────────────────────────────────────────────
    best_success = max(
        (r["quote_success_rate"] for r in comparison_table if r.get("rpc_sampled")),
        default=0.0,
    )
    best_label1 = max(
        (r["label_1_share"] or 0 for r in comparison_table if r.get("rpc_sampled")),
        default=0.0,
    )
    best_trim = max(
        (r["trimmed_mean"] or -1 for r in comparison_table if r.get("rpc_sampled")),
        default=-1.0,
    )

    if not do_rpc:
        status = "GATE_CANDIDATES_BUILT_NO_RPC"
    elif best_success >= 0.55 and best_label1 >= 0.10 and best_trim > 0.0:
        status = "LIQUIDITY_GATE_VALIDATED_CONSIDER_037C"
    elif best_success >= 0.50:
        status = "GATE_IMPROVED_SUCCESS_RATE_REVIEW_LABELS"
    elif best_success >= 0.35:
        status = "PARTIAL_IMPROVEMENT_NEEDS_STRONGER_GATE"
    else:
        status = "GATE_INSUFFICIENT_STILL_HIGH_DEAD_POOL_RATE"

    sprint037c_allowed = (
        do_rpc and best_success >= 0.50 and best_label1 >= 0.10 and best_trim > 0.0
    )

    # ── Summary JSON ──────────────────────────────────────────────────────────
    total_per_regime = {
        r["week_start"]: sum(1 for c in all_candidates if c["regime_week"] == r["week_start"])
        for r in regimes_to_run
    }
    gate_counts_per_regime: dict[str, dict] = {}
    for regime in regimes_to_run:
        ws = regime["week_start"]
        gate_counts_per_regime[ws] = {
            t["name"]: sum(
                1 for c in all_candidates
                if c["regime_week"] == ws and c.get(f"gate_{t['name']}")
            )
            for t in GATE_TIERS
        }

    summary: dict[str, Any] = {
        "kind":    "sprint037b_r3_liquidity_gate",
        "version": "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":  status,
        "decision_flags": DECISION_FLAGS,
        "sprint037c_training_review_allowed": sprint037c_allowed,
        "gate_definition": [
            {
                "name": t["name"],
                "require_fresh": t["require_fresh"],
                "require_exit_short": t["require_exit_short"],
                "require_exit_full": t["require_exit_full"],
                "freshness_lag_sec": FRESHNESS_LAG_SEC,
                "exit_short_sec": EXIT_SHORT_SEC,
            }
            for t in GATE_TIERS
        ],
        "total_candidates": len(all_candidates),
        "per_regime_candidates": total_per_regime,
        "gate_counts_per_regime": gate_counts_per_regime,
        "comparison_table": comparison_table,
        "rpc_stats_by_tier": rpc_stats_by_tier,
        "r2_baseline": {
            "source": "mvp/diagnostics/sprint037b_r2_summary.json",
            "ungated_quote_success_rate": 0.2705,
            "ungated_fail_v3_entry_zero_rate": 0.7204,
            "ungated_label_1_share": 0.0625,
            "ungated_trimmed_mean": -0.2863,
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    _log(f"\nWritten: {OUT_SUMMARY}")

    _write_report(summary, comparison_table)

    _log(f"\n{'='*60}")
    _log(f"STATUS: {status}")
    _log(f"  Total candidates: {len(all_candidates)}")
    _log(f"  Best gate success rate: {best_success:.1%}")
    _log(f"  Best gate label_1_share: {best_label1:.1%}")
    _log(f"  Sprint037C training review allowed: {sprint037c_allowed}")


def _write_report(summary: dict, comparison_table: list[dict]) -> None:
    r2 = summary.get("r2_baseline", {})

    lines = [
        "# Sprint037B-R3 — Liquidity-gated Replay + Batch RPC Validation",
        "",
        f"Generated: {summary.get('created_at')}  ",
        f"**Status: `{summary.get('status')}`**",
        "",
        "---",
        "",
        "## 1. Root Cause (from Sprint037B-R2)",
        "",
        f"- Ungated quote success rate: **{r2.get('ungated_quote_success_rate', 'N/A'):.1%}**",
        f"- Ungated fail_v3_entry_zero rate: **{r2.get('ungated_fail_v3_entry_zero_rate', 'N/A'):.1%}**",
        f"- Ungated label_1_share (RPC): **{r2.get('ungated_label_1_share', 'N/A')}**",
        f"- Ungated trimmed_mean (RPC): **{r2.get('ungated_trimmed_mean', 'N/A')}**",
        "",
        "Historical meme pool tick movements were dominated by pools with",
        "zero in-range V3 liquidity at entry_time. Dry-run of liq_nonzero gate",
        "showed only 0.3% filtering → pools drained BETWEEN last swap and entry_time.",
        "R3 switched to freshness-based gates targeting this exact failure mode.",
        "",
        "---",
        "",
        "## 2. Gate Definitions",
        "",
        f"- `FRESHNESS_LAG_SEC` = {FRESHNESS_LAG_SEC}s (pool must have swap ≤{FRESHNESS_LAG_SEC}s before entry_time)",
        f"- `EXIT_SHORT_SEC` = {EXIT_SHORT_SEC}s (pool must have swap within first {EXIT_SHORT_SEC}s of exit window)",
        "",
        "| Tier | require_fresh | require_exit_short | require_exit_full | Description |",
        "|------|--------------|-------------------|------------------|-------------|",
        "| ungated | No | No | No | R2 baseline — no filter |",
        f"| exit_full | No | No | Yes | Pool has swap in [entry, +1800s] |",
        f"| fresh | Yes | No | No | Last feature-window swap ≤{FRESHNESS_LAG_SEC}s before entry |",
        f"| fresh_and_exit_short | Yes | Yes | No | fresh AND swap in [entry, +{EXIT_SHORT_SEC}s] |",
        "",
        "---",
        "",
        "## 3. Candidate Counts by Gate",
        "",
        "| Regime | Total | ungated | exit_full | fresh | fresh_and_exit_short |",
        "|--------|-------|---------|-----------|-------|---------------------|",
    ]

    for regime_week, gate_cnts in (summary.get("gate_counts_per_regime") or {}).items():
        total = summary.get("per_regime_candidates", {}).get(regime_week, "?")
        ug    = gate_cnts.get("ungated", "?")
        ef    = gate_cnts.get("exit_full", "?")
        fr    = gate_cnts.get("fresh", "?")
        fae   = gate_cnts.get("fresh_and_exit_short", "?")
        lines.append(f"| {regime_week} | {total} | {ug} | {ef} | {fr} | {fae} |")

    lines += [
        "",
        "---",
        "",
        "## 4. Cross-gate RPC Comparison",
        "",
        "| Tier | Candidates | RPC ok/sampled | Success Rate | fail_entry_zero | label_1_share | TrimMean |",
        "|------|-----------|----------------|--------------|-----------------|---------------|----------|",
    ]

    for row in comparison_table:
        rpc_ok  = row.get("rpc_ok", "N/A")
        rpc_s   = row.get("rpc_sampled", "N/A")
        qsr     = f"{row['quote_success_rate']:.1%}" if rpc_s else "N/A"
        f0      = str(row.get("fail_v3_entry_zero", "N/A"))
        l1s     = f"{row['label_1_share']:.1%}" if row.get("label_1_share") is not None else "N/A"
        tmn     = f"{row['trimmed_mean']:.4f}" if row.get("trimmed_mean") is not None else "N/A"
        lines.append(
            f"| {row['tier']} | {row['candidate_count']} | {rpc_ok}/{rpc_s} "
            f"| {qsr} | {f0} | {l1s} | {tmn} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 5. Per-regime Detail (liq_and_exit gate)",
        "",
        "| Regime | RPC ok/sampled | Success | fail_zero | label_1 | Median | TrimMean |",
        "|--------|---------------|---------|-----------|---------|--------|----------|",
    ]

    lae_stats = (summary.get("rpc_stats_by_tier") or {}).get("fresh_and_exit_short", {})
    for regime_week, rs in lae_stats.items():
        qsr = f"{rs.get('quote_success_rate', 0):.1%}"
        f0  = rs.get("reason_counts", {}).get("fail_v3_entry_zero", 0)
        l1s = f"{rs.get('label_1_share', 0):.1%}" if rs.get("label_1_share") is not None else "N/A"
        med = f"{rs.get('median_net_return', 0):.4f}" if rs.get("median_net_return") is not None else "N/A"
        tmn = f"{rs.get('trimmed_mean', 0):.4f}" if rs.get("trimmed_mean") is not None else "N/A"
        lines.append(
            f"| {regime_week} | {rs.get('n_ok', 'N/A')}/{rs.get('n_sampled', 'N/A')} "
            f"| {qsr} | {f0} | {l1s} | {med} | {tmn} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 6. Decision",
        "",
        f"- **Status**: `{summary.get('status')}`",
        f"- **Sprint037C training review allowed**: {summary.get('sprint037c_training_review_allowed')}",
        "",
        "Gate success criterion: quote_success_rate ≥ 50% on liq_and_exit tier.",
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037b_r3_liquidity_gate.py*",
    ]

    OUT_REPORT.write_text("\n".join(lines))
    _log(f"Written: {OUT_REPORT}")


if __name__ == "__main__":
    main()
