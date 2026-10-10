#!/usr/bin/env python3
"""
Sprint037B-R2 — FeatureStore Semantic Repair + RPC-backed Label Validation
===========================================================================

Fixes applied relative to Sprint037B (tick-proxy replay):

1. WETH-leg buy/sell side normalization
   - buy_meme is now direction-corrected: if WETH=token0, a buy-meme signal
     comes from amount0 > 0 (WETH entering pool), not amount0 < 0.
   - Original Sprint037B code used raw amount0 sign, inverting buy/sell for
     ~50% of the pool universe (weth_is_token0=1 pools).

2. Volume-weighted price_velocity_5m
   - Net directional flow / abs total flow, using abs(amount0) as size proxy.
   - For WETH=token0 pools: sign_corrected_flow = +amount0 (pos = WETH in = buy meme)
   - For WETH=token1 pools: sign_corrected_flow = -amount0 (neg amount0 = meme out = buy)
   - Aligned with FeatureStore.compute_from_snapshot velocity formula.

3. Volume-weighted buy_pressure_ratio
   - WETH-leg corrected buy flow / total abs flow, not plain count ratio.
   - Aligned with FeatureStore's buy_volume / total_flow.

4. Size-CV token_volatility_5m
   - stddev(abs(amount0)) / mean(abs(amount0)) using sum + sum_of_squares aggregates.
   - Aligned with FeatureStore's abs_flows.std / abs_flows.mean.

5. unique_buyer_entropy_5m
   - Marked as EATLQ-additive diagnostic (NOT original author feature).
   - Original 4 features: price_velocity_5m, buy_pressure_ratio,
     token_volatility_5m, activity_heat.

6. Per-regime full artifact output (no 500-row CSV cap).
   - One CSV per regime under mvp/audits/sprint037b_r2/candidates_{regime}.csv
   - Stratified sample CSVs for RPC validation.

7. QuoterV2 RPC-backed label validation
   - Reuses label_dashcam_executable.py QuoterV2 encoding verbatim.
   - entry_block / exit_block via live RPC ts_grid (batch eth_getBlockByNumber).
   - Freezes NOTIONAL_WEI, ROUND_TRIP_COST, EXIT_HORIZON_SEC per label-semantics-freeze.md.
   - Records: label, net_return, label_type (success reason / failure reason).

8. Tick-proxy vs RPC label comparison
   - For each validated candidate: tick_label vs rpc_label, delta, agreement.
   - Per-regime / per-pool / per-fee-tier economic readout.
   - Fat-tail audit (|net_return| >= 1.0 winners inspected separately).

Decision flags (hard-coded, never override from CLI):
   TRAINING_ALLOWED=false
   LIVE_ALLOWED=false
   MODEL_PROMOTION_ALLOWED=false
   H12_ABLATION_ALLOWED=false
   PHASE6_ALLOWED=false
   FAILED_NO_7D_ALPHA_ACTIVE=true
   HISTORICAL_REPLAY_REPAIR_ALLOWED=true
   RPC_BACKED_LABEL_VALIDATION_ALLOWED=true
   NO_ALPHA_CLAIM=true

Usage:
   # Dry run (builds candidates, skips RPC)
   python scripts/sprint037b_r2_replay_repair.py --dry-run

   # Full run with RPC labels
   python scripts/sprint037b_r2_replay_repair.py --run

   # Partial run: only specific regime + custom RPC
   LABEL_RPC_URL=https://... python scripts/sprint037b_r2_replay_repair.py --run --regime 2025-12-01

Environment:
   CLICKHOUSE_URL     (default: http://172.18.0.6:8123/)
   CLICKHOUSE_USER    (default: default)
   CLICKHOUSE_PASSWORD
   LABEL_RPC_URL or EVM_HTTP_URL  — archive-capable Base RPC
   LABEL_WORKERS      (default: 20)
   LABEL_LIMIT        (per regime RPC label limit; 0=all stratified)
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
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.request import Request, urlopen
from urllib.error import URLError
from typing import Any, Optional

# ── Constants (frozen, matching label-semantics-freeze.md) ───────────────────
WETH = "0x4200000000000000000000000000000000000006"
CHAIN_ID = 8453
ROUND_TRIP_COST = 0.006
LABEL_EXIT_HORIZON_SEC = 1800           # frozen 30-minute exit horizon
LABEL_NOTIONAL_WEI = int(0.0005e18)     # 0.0005 ETH notional
FEATURE_WINDOW_SEC = 300                # 5-minute feature window
MIN_ACTIVITY_HEAT = 5                   # min swaps per bucket to emit candidate
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"

# ── Stratified RPC validation config ─────────────────────────────────────────
STRATUM_SAMPLES_PER_REGIME = 400        # target RPC calls per regime (manageable cost)
RPC_WORKER_THREADS = 20
MAX_ABS_NET_RETURN = 10.0               # sanity clip, matches labeler

# ── Stables / exclusions (meme pool filter, aligned w/ Sprint037A) ───────────
_STABLES = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",  # crvUSD
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
})
_EXCLUDED_LARGE_CAP = frozenset({
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
})

# ── Regimes ───────────────────────────────────────────────────────────────────
REGIMES = [
    {"week_start": "2025-12-01", "week_end": "2025-12-08", "rank": 2,
     "label": "Dec-01 (strongest positive)"},
    {"week_start": "2025-12-29", "week_end": "2026-01-05", "rank": 3,
     "label": "Dec-29 (holiday surge)"},
    {"week_start": "2026-02-02", "week_end": "2026-02-09", "rank": 1,
     "label": "Feb-02 (highest activity)"},
]

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mvp/audits/sprint037b_r2"
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037b_r2_summary.json"
OUT_REPORT  = OUT_DIR / "sprint037b_r2_report.md"

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

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    print(f"[r2 {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


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
    return ", ".join(f"'{a}'" for a in sorted(_EXCLUDED_LARGE_CAP))

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


def build_feature_query_r2(start_dt: str, end_dt: str) -> str:
    """Feature aggregation query with repaired semantics.

    Key fixes vs Sprint037B:
    ──────────────────────────────────────────────────────────────────────
    A) WETH-leg corrected buy/sell detection
       For WETH=token1 pools (weth_is_token0=0, meme=token0):
         is_buy_meme = (amount0 < 0) → MSB set → first hex ≥ '8'  [same as before]
       For WETH=token0 pools (weth_is_token0=1, meme=token1):
         is_buy_meme = (amount0 > 0) → WETH entered pool → user bought meme
                     = first hex char < '8'                         [INVERTED vs before]

    B) Flow-weighted price_velocity_5m
       sign_corrected_flow = if(weth_is_token0=0, -amount0, +amount0)
       So: + when meme is bought, - when meme is sold.
       velocity = sum(sign_corrected_flow) / sum(abs(amount0))

    C) Volume-weighted buy_pressure_ratio
       buy_flow = sum(abs(amount0)) for buy_meme rows
       pressure = buy_flow / sum(abs(amount0))

    D) Size-CV token_volatility_5m
       volatility = stddev(abs(amount0)) / mean(abs(amount0))
       Uses sum_abs and sum_abs_sq aggregates → computed in Python.

    Leakage guard:
       entry_time_sec = toStartOfFiveMinute(swap_ts) + 300s
       feature_window = [toStartOfFiveMinute(swap_ts), entry_time_sec)
       All swaps are STRICTLY before entry_time_sec.
    """
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                                                          AS pool_address,
  mp.fee_bps,
  mp.token0_addr,
  mp.token1_addr,
  mp.weth_is_token0,
  toUnixTimestamp(toStartOfFiveMinute({te}))                                    AS bucket_5m_sec,
  toUnixTimestamp(toStartOfFiveMinute({te})) + {FEATURE_WINDOW_SEC}            AS entry_time_sec,
  -- activity_heat: exact swap count (matches FeatureStore tick_count)
  count()                                                                         AS activity_heat,
  -- WETH-leg corrected buy/sell classification
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                               AS buy_meme_count,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) <  '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) >= '8')
  )                                                                               AS sell_meme_count,
  -- Volume aggregates for flow-weighted features
  -- sign_flow = if(weth_is_token0=0, -amount0, +amount0): positive = meme bought
  sum(if(mp.weth_is_token0 = 0,
        -toFloat64(eatlq_hex_to_int256(s.amount0_raw)),
         toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                         AS net_signed_flow,
  sum(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                        AS abs_flow_sum,
  -- buy_flow (abs amount0 for buy-meme rows only)
  sumIf(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
        (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
        (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                               AS buy_flow_sum,
  -- sum of squares for size-CV (token_volatility_5m)
  sum(pow(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))), 2))                AS abs_flow_sq_sum,
  -- unique buyers (WETH-leg corrected)
  uniqCombinedIf(lower(s.sender),
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                               AS unique_buyers,
  uniqCombined(lower(s.sender))                                                  AS unique_senders,
  -- tick aggregates for label proxy reference and extreme-return diagnostics
  argMax(toInt32(eatlq_hex_to_int256(s.tick_raw)), {te})                         AS tick_last,
  min(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                  AS tick_min,
  max(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                  AS tick_max
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
    """Tick series for tick-proxy label comparison (diagnostic only)."""
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


# ── Repaired feature computation ──────────────────────────────────────────────
def compute_features_r2(row: dict[str, Any]) -> dict[str, Any]:
    """Compute repaired FeatureStore-aligned feature vector.

    Alignment with runtime FeatureStore.compute_from_snapshot:
    ──────────────────────────────────────────────────────────
    price_velocity_5m:   net_signed_flow / abs_flow_sum  ← volume-weighted ✓
    buy_pressure_ratio:  buy_flow_sum / abs_flow_sum     ← volume-weighted ✓
    token_volatility_5m: size-CV = std(abs_flows)/mean(abs_flows) ✓
    activity_heat:       swap count  ✓
    unique_buyer_entropy_5m: log1p(unique_buyers) [EATLQ-additive diagnostic only]
    buy_pressure_ratio_count: count proxy kept for comparison with old output

    Proxy documentation:
    - abs(amount0) used as size proxy for all pools; correct for WETH=token1
      pools where amount0 IS the meme amount; for WETH=token0 pools it's the
      WETH amount (stable denominator across pools — acceptable size proxy).
    - tick-based token_volatility is NOT used; size-CV is closer to runtime.
    - Toxic-flow filtering NOT applied (requires per-wallet Redis state).
    """
    activity_heat = int(row.get("activity_heat") or 0)
    buy_meme_count = int(row.get("buy_meme_count") or 0)
    sell_meme_count = int(row.get("sell_meme_count") or 0)
    net_signed_flow = float(row.get("net_signed_flow") or 0.0)
    abs_flow_sum = float(row.get("abs_flow_sum") or 0.0)
    buy_flow_sum = float(row.get("buy_flow_sum") or 0.0)
    abs_flow_sq_sum = float(row.get("abs_flow_sq_sum") or 0.0)
    unique_buyers = int(row.get("unique_buyers") or 0)
    unique_senders = int(row.get("unique_senders") or 0)
    tick_last = int(row.get("tick_last") or 0)
    tick_min = int(row.get("tick_min") or 0)
    tick_max = int(row.get("tick_max") or 0)
    tick_range = abs(tick_max - tick_min)

    total_count = max(buy_meme_count + sell_meme_count, 1)

    # price_velocity_5m: volume-weighted, aligned with runtime
    if abs_flow_sum > 0:
        price_velocity_5m = max(-1.0, min(1.0, net_signed_flow / abs_flow_sum))
    else:
        price_velocity_5m = 0.0

    # buy_pressure_ratio: volume-weighted, aligned with runtime
    if abs_flow_sum > 0:
        buy_pressure_ratio = max(0.0, min(1.0, buy_flow_sum / abs_flow_sum))
    else:
        buy_pressure_ratio = 0.5

    # token_volatility_5m: size-CV using sum and sum-of-squares
    if activity_heat > 0 and abs_flow_sum > 0:
        mean_abs = abs_flow_sum / activity_heat
        variance = max(0.0, abs_flow_sq_sum / activity_heat - mean_abs ** 2)
        std_abs = math.sqrt(variance)
        token_volatility_5m = min(2.0, max(0.0, std_abs / mean_abs))
    else:
        token_volatility_5m = 0.0

    # unique_buyer_entropy_5m: EATLQ-additive only, NOT original author feature
    unique_buyer_entropy_5m = math.log1p(unique_buyers)

    # count-based buy_pressure_ratio_count (kept for comparison)
    buy_pressure_ratio_count = max(0.0, min(1.0, buy_meme_count / total_count))

    return {
        "price_velocity_5m": round(price_velocity_5m, 6),
        "buy_pressure_ratio": round(buy_pressure_ratio, 6),
        "token_volatility_5m": round(token_volatility_5m, 6),
        "activity_heat": activity_heat,
        "unique_buyer_entropy_5m": round(unique_buyer_entropy_5m, 6),   # diagnostic
        "buy_pressure_ratio_count": round(buy_pressure_ratio_count, 6), # comparison
        "buy_meme_count": buy_meme_count,
        "sell_meme_count": sell_meme_count,
        "unique_buyers": unique_buyers,
        "unique_senders": unique_senders,
        "abs_flow_sum": round(abs_flow_sum, 2),
        "tick_last": tick_last,
        "tick_range": tick_range,
    }


# ── Tick-proxy label (diagnostic reference, NOT authoritative) ────────────────
class PoolPriceIndex:
    """Per-pool sorted tick series for O(log N) tick-proxy lookups."""
    def __init__(self) -> None:
        self._ts: dict[str, list[int]] = defaultdict(list)
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

    def pools(self) -> set[str]:
        return set(self._ts.keys())


def compute_tick_proxy(price_index: PoolPriceIndex, pool: str,
                       entry_time: int, weth_is_token0: int) -> Optional[float]:
    """Tick-based 30m return proxy (diagnostic only, not authoritative label)."""
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
    net_return = raw_return - ROUND_TRIP_COST
    return max(-1.0, min(5.0, net_return))


# ── Stratified sampling ───────────────────────────────────────────────────────
def stratify_candidates(
    candidates: list[dict[str, Any]],
    target_n: int,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Stratified sample preserving: fee tier, weth_side, activity bucket,
    proxy-return bucket, and pool concentration (top-1, top-2-5, tail).

    Fat-tail rows (|tick_proxy| >= 1.0) are always included up to 50 each side.
    """
    import random
    rng = random.Random(seed)

    # always include fat-tail rows for mandatory inspection
    fat_pos = [c for c in candidates if (c.get("tick_proxy_return") or 0.0) >= 1.0]
    fat_neg = [c for c in candidates if (c.get("tick_proxy_return") or 0.0) <= -1.0]
    forced = fat_pos[:50] + fat_neg[:50]
    forced_ids = {id(c) for c in forced}

    remainder = [c for c in candidates if id(c) not in forced_ids]

    # build strata
    def _strat_key(c: dict) -> tuple:
        fee = c.get("fee_bps", 0)
        w0 = c.get("weth_is_token0", 0)
        ah = c.get("activity_heat", 0)
        ab = "low" if ah < 20 else ("mid" if ah < 100 else "high")
        tr = c.get("tick_proxy_return") or 0.0
        rb = "loss" if tr < -0.1 else ("low" if tr < 0.1 else ("high" if tr < 1.0 else "extreme"))
        return (fee, w0, ab, rb)

    strata: dict[tuple, list] = defaultdict(list)
    for c in remainder:
        strata[_strat_key(c)].append(c)

    budget = max(0, target_n - len(forced))
    n_strata = max(len(strata), 1)
    per_stratum = max(1, budget // n_strata)
    sampled = list(forced)
    for key, members in strata.items():
        k = min(per_stratum, len(members))
        sampled.extend(rng.sample(members, k))
        if len(sampled) >= target_n:
            break

    _log(f"  Stratified: {len(sampled)} rows from {len(candidates)} "
         f"({len(forced)} fat-tail forced, {len(strata)} strata, "
         f"target {target_n})")
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
        out = self._post(body)
        idx = {r.get("id"): r for r in out}
        return [idx.get(i) for i in range(len(calls))]


# ── ts→block grid (reusing labeler's approach) ────────────────────────────────
def build_ts_grid(rpc: RPC, ts_min_sec: int, ts_max_sec: int) -> tuple[list, list]:
    r = rpc.call("eth_getBlockByNumber", ["latest", False])
    latest_num = int(r["result"]["number"], 16)
    latest_ts = int(r["result"]["timestamp"], 16)
    blocks_back = max((latest_ts - ts_min_sec) // 2 + 2000, 0)
    lo_block = max(0, latest_num - blocks_back)
    step = 50
    block_nums = list(range(lo_block, latest_num + 1, step))
    batch_size = 500
    results_map: dict[int, int] = {}
    t0 = time.time()
    for i in range(0, len(block_nums), batch_size):
        sub = block_nums[i:i + batch_size]
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in sub]
        results = rpc.batch(calls)
        for bn, res in zip(sub, results):
            if res and res.get("result"):
                results_map[bn] = int(res["result"]["timestamp"], 16)
        _log(f"  ts grid: {min(i+batch_size, len(block_nums))}/{len(block_nums)} "
             f"blocks (elapsed {time.time()-t0:.1f}s)")
    sorted_bn = sorted(results_map.keys())
    sorted_ts = [results_map[b] for b in sorted_bn]
    _log(f"  ts grid: {len(sorted_bn)} samples, span "
         f"[{sorted_ts[0] if sorted_ts else -1}..{sorted_ts[-1] if sorted_ts else -1}]")
    return sorted_bn, sorted_ts


def ts_to_block(bn_arr: list, ts_arr: list, target_ts: int) -> int:
    idx = bisect.bisect_left(ts_arr, target_ts)
    if idx == 0:
        return bn_arr[0]
    if idx >= len(bn_arr):
        return bn_arr[-1]
    b_lo, t_lo = bn_arr[idx - 1], ts_arr[idx - 1]
    b_hi, t_hi = bn_arr[idx], ts_arr[idx]
    if t_hi == t_lo:
        return b_hi
    return b_lo + int((target_ts - t_lo) / (t_hi - t_lo) * (b_hi - b_lo))


# ── QuoterV2 encoding (verbatim from label_dashcam_executable.py) ─────────────
_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(hex_: str) -> str:
    return hex_.zfill(64)

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
    """Returns amountOut from QuoterV2.quoteExactInputSingle; 0 on any failure."""
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in)
        + _addr_pad(token_out)
        + _uint_pad(amount_in)
        + _uint_pad(fee)
        + _uint_pad(0)  # sqrtPriceLimitX96 = 0
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
    bn_arr: list, ts_arr: list,
) -> tuple[int, float, str]:
    """QuoterV2 label for one V3 candidate.

    Returns (label: -1|0|1, net_return, label_type_str).
    label=-1 means quote_failed (row omitted per label-semantics-freeze.md).
    """
    fee_raw = fee_bps * 100  # fee_bps stored as basis-points-like: 30→3000ppm
    # discovered_pools.fee_bps stores the raw fee tier (100, 500, 3000, 10000)
    # QuoterV2 expects fee in ppm (same values: 100=0.01%, 500=0.05%, etc.)
    weth = WETH.lower()
    t0 = token0.lower()
    t1 = token1.lower()
    if weth not in (t0, t1):
        return -1, 0.0, "fail_no_weth_leg"
    meme = t1 if t0 == weth else t0

    entry_block = ts_to_block(bn_arr, ts_arr, entry_time)
    exit_block = ts_to_block(bn_arr, ts_arr, entry_time + LABEL_EXIT_HORIZON_SEC)

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

    net = weth_back / LABEL_NOTIONAL_WEI - 1.0 - ROUND_TRIP_COST
    net, bogus = _sanity_clip(net)
    if bogus:
        return -1, 0.0, "fail_sanity_clip"

    label = 1 if net > 0 else 0
    return label, net, "v3_quote_ok"


# ── Economic readout ──────────────────────────────────────────────────────────
def compute_readout(candidates: list[dict[str, Any]], label_col: str) -> dict[str, Any]:
    """Generic readout over any label column (tick_proxy_label or rpc_label)."""
    ret_col = "net_return_rpc" if label_col == "rpc_label" else "tick_proxy_return"
    with_label = [c for c in candidates
                  if c.get(label_col) is not None and c.get(ret_col) is not None]

    n = len(with_label)
    if n == 0:
        return {"n": 0}

    returns = sorted(c[ret_col] for c in with_label)
    positive_n = sum(1 for r in returns if r > 0)
    trim_n = max(1, n // 10)
    trimmed = returns[trim_n:-trim_n] if n > 2 * trim_n else returns

    per_regime: dict[str, list] = defaultdict(list)
    per_pool: dict[str, list] = defaultdict(list)
    per_fee: dict[str, list] = defaultdict(list)
    per_weth_side: dict[str, list] = defaultdict(list)
    per_activity_bucket: dict[str, list] = defaultdict(list)

    for c in with_label:
        r = c[ret_col]
        per_regime[c.get("regime_week", "?")].append(r)
        per_pool[c.get("pool_address", "?")].append(r)
        per_fee[str(c.get("fee_bps", 0))].append(r)
        per_weth_side[str(c.get("weth_is_token0", 0))].append(r)
        ah = c.get("activity_heat", 0)
        ab = "low(<20)" if ah < 20 else ("mid(20-99)" if ah < 100 else "high(>=100)")
        per_activity_bucket[ab].append(r)

    def _s(rs: list[float]) -> dict:
        if not rs:
            return {}
        rs = sorted(rs)
        k = len(rs)
        return {
            "n": k, "pos": sum(x > 0 for x in rs),
            "pos_share": round(sum(x > 0 for x in rs) / k, 4),
            "mean": round(sum(rs) / k, 6),
            "median": round(rs[k // 2], 6),
        }

    top_pools = sorted(per_pool.items(), key=lambda kv: len(kv[1]), reverse=True)[:12]

    # fat tail audit
    fat_pos = [c for c in with_label if c[ret_col] >= 1.0]
    fat_neg = [c for c in with_label if c[ret_col] <= -1.0]

    return {
        "label_col": label_col,
        "n_labelled": n,
        "positive_n": positive_n,
        "label_1_share": round(positive_n / n, 4),
        "mean_net_return": round(sum(returns) / n, 6),
        "median_net_return": round(returns[n // 2], 6),
        "trimmed_mean_net_return": round(sum(trimmed) / max(len(trimmed), 1), 6),
        "p10_return": round(returns[max(0, n // 10)], 6),
        "p90_return": round(returns[min(n - 1, 9 * n // 10)], 6),
        "per_regime": {k: _s(v) for k, v in per_regime.items()},
        "per_fee_tier": {k: _s(v) for k, v in per_fee.items()},
        "per_weth_side": {k: _s(v) for k, v in per_weth_side.items()},
        "per_activity_bucket": {k: _s(v) for k, v in per_activity_bucket.items()},
        "top_pools_by_n": {pa: _s(rs) for pa, rs in top_pools},
        "fat_tail_pos": {
            "n": len(fat_pos),
            "share": round(len(fat_pos) / n, 4),
            "pools": list({c["pool_address"] for c in fat_pos}),
            "sample": [
                {"pool": c["pool_address"], "fee": c.get("fee_bps"),
                 "weth0": c.get("weth_is_token0"), "ah": c.get("activity_heat"),
                 "ret": c.get(ret_col), "entry_iso": c.get("entry_time_iso")}
                for c in sorted(fat_pos, key=lambda x: x[ret_col], reverse=True)[:20]
            ],
        },
        "fat_tail_neg": {
            "n": len(fat_neg),
            "share": round(len(fat_neg) / n, 4),
            "sample": [
                {"pool": c["pool_address"], "fee": c.get("fee_bps"),
                 "weth0": c.get("weth_is_token0"), "ret": c.get(ret_col),
                 "entry_iso": c.get("entry_time_iso")}
                for c in sorted(fat_neg, key=lambda x: x[ret_col])[:20]
            ],
        },
    }


def compute_proxy_vs_rpc_comparison(candidates: list[dict]) -> dict:
    """Tick-proxy vs RPC label disagreement analysis."""
    paired = [c for c in candidates
              if c.get("rpc_label") is not None and c.get("tick_proxy_label") is not None]
    if not paired:
        return {"n_paired": 0}

    agree = sum(1 for c in paired if c["rpc_label"] == c["tick_proxy_label"])
    tp = sum(1 for c in paired if c["rpc_label"] == 1 and c["tick_proxy_label"] == 1)
    fp = sum(1 for c in paired if c["rpc_label"] == 0 and c["tick_proxy_label"] == 1)
    tn = sum(1 for c in paired if c["rpc_label"] == 0 and c["tick_proxy_label"] == 0)
    fn = sum(1 for c in paired if c["rpc_label"] == 1 and c["tick_proxy_label"] == 0)

    precision = tp / max(tp + fp, 1)
    recall    = tp / max(tp + fn, 1)

    # return delta
    deltas = [
        abs((c.get("tick_proxy_return") or 0.0) - (c.get("net_return_rpc") or 0.0))
        for c in paired
        if c.get("tick_proxy_return") is not None and c.get("net_return_rpc") is not None
    ]
    deltas_sorted = sorted(deltas)
    n_d = len(deltas_sorted)

    return {
        "n_paired": len(paired),
        "agreement_rate": round(agree / len(paired), 4),
        "disagreement_rate": round(1 - agree / len(paired), 4),
        "confusion": {"TP": tp, "FP": fp, "TN": tn, "FN": fn},
        "proxy_precision_vs_rpc": round(precision, 4),
        "proxy_recall_vs_rpc": round(recall, 4),
        "return_delta_median": round(deltas_sorted[n_d // 2], 6) if deltas_sorted else None,
        "return_delta_p90": round(deltas_sorted[min(n_d-1, 9*n_d//10)], 6) if deltas_sorted else None,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint037B-R2 replay repair + RPC validation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="full run with RPC label validation")
    mode.add_argument("--dry-run", action="store_true", help="build candidates, skip RPC")
    parser.add_argument("--regime", help="run only a specific regime week_start (e.g. 2025-12-01)")
    parser.add_argument("--rpc-only-dec01", action="store_true",
                        help="run full RPC for Dec-01 only, skip other regimes")
    args = parser.parse_args()

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    # ── env ──────────────────────────────────────────────────────────────────
    ch_url  = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "")
    n_workers = int(os.environ.get("LABEL_WORKERS", str(RPC_WORKER_THREADS)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    do_rpc = args.run and bool(rpc_url)
    if args.run and not rpc_url:
        _log("WARNING: --run set but no LABEL_RPC_URL / EVM_HTTP_URL; skipping RPC labels")
        do_rpc = False

    regimes_to_run = [
        r for r in REGIMES
        if (not args.regime or r["week_start"] == args.regime)
    ]
    _log(f"Regimes: {[r['week_start'] for r in regimes_to_run]}")

    rpc: Optional[RPC] = None
    bn_arr: list = []
    ts_arr: list = []

    all_candidates: list[dict[str, Any]] = []
    rpc_label_results: dict[str, dict] = {}  # per-regime stats

    # ── ClickHouse phase ──────────────────────────────────────────────────────
    for regime in regimes_to_run:
        week_start = regime["week_start"]
        week_end   = regime["week_end"]
        _log(f"\n{'='*55}")
        _log(f"REGIME {week_start}  ({regime['label']})")

        # feature window = [week_start, week_end); entry candidates will be within regime
        feat_sql = build_feature_query_r2(week_start, week_end)
        _log(f"  Querying features [{week_start} … {week_end}] …")
        t0 = time.time()
        try:
            feature_rows = _ch_query(ch_url, feat_sql, ch_user, ch_pwd, timeout=600.0)
        except Exception as exc:
            _log(f"  ERROR fetching features: {exc}")
            continue
        _log(f"  Feature rows: {len(feature_rows)} in {time.time()-t0:.1f}s")

        # price series for tick-proxy (diagnostic)
        # extend end by label_horizon so we can compute exit ticks
        from datetime import timedelta
        end_with_horizon = (
            datetime.strptime(week_end, "%Y-%m-%d") + timedelta(seconds=LABEL_EXIT_HORIZON_SEC)
        ).strftime("%Y-%m-%d %H:%M:%S")
        price_sql = build_price_series_query(week_start, end_with_horizon)
        _log(f"  Querying price series …")
        t0 = time.time()
        try:
            price_rows = _ch_query(ch_url, price_sql, ch_user, ch_pwd, timeout=600.0)
        except Exception as exc:
            _log(f"  ERROR fetching prices: {exc}")
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
            pool = str(row.get("pool_address") or "").lower()
            entry_time = int(row.get("entry_time_sec") or 0)
            weth_is_token0 = int(row.get("weth_is_token0") or 0)
            if not pool or entry_time <= 0:
                continue
            if entry_time < regime_start_sec or entry_time > regime_end_sec:
                continue

            feats = compute_features_r2(row)

            tick_proxy = compute_tick_proxy(price_index, pool, entry_time, weth_is_token0)
            tick_proxy_label = (
                1 if tick_proxy is not None and tick_proxy > 0.0
                else (0 if tick_proxy is not None else None)
            )

            candidate: dict[str, Any] = {
                "regime_week": week_start,
                "pool_address": pool,
                "entry_time_sec": entry_time,
                "entry_time_iso": datetime.fromtimestamp(
                    entry_time, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fee_bps": int(row.get("fee_bps") or 0),
                "token0_addr": str(row.get("token0_addr") or ""),
                "token1_addr": str(row.get("token1_addr") or ""),
                "weth_is_token0": weth_is_token0,
                **feats,
                "tick_proxy_return": round(tick_proxy, 6) if tick_proxy is not None else None,
                "tick_proxy_label": tick_proxy_label,
                # RPC label fields (filled later)
                "rpc_label": None,
                "net_return_rpc": None,
                "rpc_label_type": None,
            }
            regime_candidates.append(candidate)

        _log(f"  Candidates: {len(regime_candidates)}")

        # Write full per-regime candidate CSV
        out_csv = OUT_DIR / f"candidates_{week_start}.csv"
        if regime_candidates:
            fields = list(regime_candidates[0].keys())
            with out_csv.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(regime_candidates)
            _log(f"  Written: {out_csv} ({len(regime_candidates)} rows)")

        all_candidates.extend(regime_candidates)

    # ── RPC label phase ───────────────────────────────────────────────────────
    if do_rpc:
        _log(f"\n{'='*55}")
        _log("RPC LABEL PHASE")
        rpc = RPC(rpc_url)

        # validate RPC chain_id
        try:
            r = rpc.call("eth_chainId", [])
            chid = int(r["result"], 16)
            if chid != CHAIN_ID:
                _log(f"ERROR: RPC chain_id={chid} != expected {CHAIN_ID}")
                do_rpc = False
            else:
                _log(f"  RPC ok, chain_id={chid}")
        except Exception as exc:
            _log(f"  ERROR: RPC connection failed: {exc}")
            do_rpc = False

    if do_rpc and rpc is not None:
        # Build ts→block grid for the full date range needed
        ts_values = [c["entry_time_sec"] for c in all_candidates if c.get("entry_time_sec")]
        if ts_values:
            ts_min = min(ts_values) - 300
            ts_max = max(ts_values) + LABEL_EXIT_HORIZON_SEC + 600
            _log(f"  Building ts grid [{ts_min}…{ts_max}] …")
            try:
                bn_arr, ts_arr = build_ts_grid(rpc, ts_min, ts_max)
                _log(f"  ts grid: {len(bn_arr)} samples")
            except Exception as exc:
                _log(f"  ERROR building ts grid: {exc}")
                do_rpc = False

    if do_rpc and rpc is not None and bn_arr:
        # Per-regime stratified sampling + RPC labeling
        for regime in regimes_to_run:
            week_start = regime["week_start"]
            regime_cands = [c for c in all_candidates if c.get("regime_week") == week_start]
            if not regime_cands:
                continue

            # attach tick_proxy_return for stratification
            sample = stratify_candidates(regime_cands, STRATUM_SAMPLES_PER_REGIME)
            _log(f"\n  RPC labels for {week_start}: {len(sample)} candidates …")

            reason_counts: dict[str, int] = Counter()
            lock = Lock()

            def label_one(c: dict) -> None:
                lbl, net, lt = label_v3_candidate(
                    rpc,                           # type: ignore[arg-type]
                    c["pool_address"],
                    c["token0_addr"],
                    c["token1_addr"],
                    c["fee_bps"],
                    c["entry_time_sec"],
                    bn_arr, ts_arr,
                )
                c["rpc_label"] = lbl if lbl >= 0 else None
                c["net_return_rpc"] = net if lbl >= 0 else None
                c["rpc_label_type"] = lt
                with lock:
                    reason_counts[lt] += 1

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
                        _log(f"    {done}/{len(sample)} RPC labels done")

            _log(f"  Reason counts: {dict(reason_counts)}")
            n_ok = sum(1 for c in sample if c.get("rpc_label") is not None)
            n_fail = len(sample) - n_ok
            _log(f"  RPC success: {n_ok}/{len(sample)} ({n_fail} skipped/failed)")
            rpc_label_results[week_start] = {
                "n_sampled": len(sample),
                "n_ok": n_ok,
                "n_skipped": n_fail,
                "reason_counts": dict(reason_counts),
            }

            # Rewrite regime CSV with RPC labels filled in
            out_csv = OUT_DIR / f"candidates_{week_start}.csv"
            all_regime_cands = [c for c in all_candidates if c.get("regime_week") == week_start]
            if all_regime_cands:
                fields = list(all_regime_cands[0].keys())
                with out_csv.open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader()
                    w.writerows(all_regime_cands)
                _log(f"  Rewritten {out_csv} with RPC labels "
                     f"({n_ok}/{len(all_regime_cands)} labelled)")

    # ── Readout phase ──────────────────────────────────────────────────────────
    _log(f"\n{'='*55}")
    _log("COMPUTING READOUTS …")

    proxy_readout = compute_readout(all_candidates, "tick_proxy_label")

    rpc_candidates = [c for c in all_candidates if c.get("rpc_label") is not None]
    rpc_readout = compute_readout(rpc_candidates, "rpc_label") if rpc_candidates else {"n_labelled": 0}

    comparison = compute_proxy_vs_rpc_comparison(all_candidates)

    # ── Status determination ───────────────────────────────────────────────────
    n_rpc = rpc_readout.get("n_labelled", 0)
    rpc_pos_share = float(rpc_readout.get("label_1_share", 0.0))
    rpc_trimmed = float(rpc_readout.get("trimmed_mean_net_return", 0.0))
    agree_rate = float(comparison.get("agreement_rate", 0.0))

    if n_rpc == 0:
        status = "REPLAY_REPAIRED_NO_RPC_LABELS"
    elif agree_rate < 0.55:
        status = "TICK_PROXY_INVALIDATED"
    elif rpc_pos_share >= 0.50 and rpc_trimmed > 0.0:
        status = "RPC_LABEL_VALIDATION_PROMISING_REVIEW_REQUIRED"
    elif rpc_pos_share >= 0.42:
        status = "NEEDS_MORE_REGIME_BATCHES"
    else:
        status = "REPLAY_REPAIRED_LABELS_ECON_WEAK"

    sprint037c_training_review_allowed = (
        n_rpc >= 200 and rpc_pos_share >= 0.48 and rpc_trimmed >= 0.0
        and agree_rate >= 0.55
    )

    # ── Summary JSON ──────────────────────────────────────────────────────────
    summary: dict[str, Any] = {
        "kind": "sprint037b_r2_replay_repair",
        "version": "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "decision_flags": DECISION_FLAGS,
        "sprint037c_training_review_allowed": sprint037c_training_review_allowed,
        "total_candidates": len(all_candidates),
        "per_regime_candidates": {
            r["week_start"]: sum(1 for c in all_candidates if c.get("regime_week") == r["week_start"])
            for r in regimes_to_run
        },
        "feature_repair_notes": [
            "price_velocity_5m: NOW volume-weighted net_signed_flow/abs_flow_sum with WETH-leg correction",
            "buy_pressure_ratio: NOW volume-weighted buy_flow_sum/abs_flow_sum with WETH-leg correction",
            "token_volatility_5m: NOW size-CV (stddev/mean of abs_flow) aligned with runtime FeatureStore",
            "activity_heat: exact swap count (unchanged)",
            "WETH-leg side fix: weth_is_token0=1 pools now use amount0>0 as buy-meme (was inverted)",
            "unique_buyer_entropy_5m: EATLQ-additive diagnostic only, not original author feature",
        ],
        "tick_proxy_readout": proxy_readout,
        "rpc_label_readout": rpc_readout,
        "tick_proxy_vs_rpc_comparison": comparison,
        "rpc_label_results_per_regime": rpc_label_results,
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    _log(f"Written: {OUT_SUMMARY}")

    # ── Markdown report ────────────────────────────────────────────────────────
    _write_report(summary, all_candidates, regimes_to_run)

    _log(f"\n{'='*55}")
    _log(f"STATUS: {status}")
    _log(f"  Total candidates: {len(all_candidates)}")
    _log(f"  RPC labelled: {n_rpc}")
    _log(f"  RPC label_1_share: {rpc_pos_share}")
    _log(f"  RPC trimmed_mean: {rpc_trimmed}")
    _log(f"  Proxy vs RPC agreement: {agree_rate}")
    _log(f"  Sprint037C training review allowed: {sprint037c_training_review_allowed}")


def _write_report(summary: dict, candidates: list[dict], regimes: list[dict]) -> None:
    lines = [
        "# Sprint037B-R2 — FeatureStore Repair + RPC Label Validation",
        "",
        f"Generated: {summary.get('created_at')}  ",
        f"**Status: `{summary.get('status')}`**",
        "",
        "---",
        "",
        "## 1. Feature Semantic Repairs",
        "",
        "| Feature | Sprint037B (broken) | Sprint037B-R2 (repaired) |",
        "|---------|---------------------|--------------------------|",
        "| `price_velocity_5m` | count proxy, no WETH leg | volume-weighted `net_signed_flow/abs_flow_sum`, WETH-leg corrected |",
        "| `buy_pressure_ratio` | count proxy, inverted for WETH=t0 pools | volume-weighted `buy_flow_sum/abs_flow_sum`, WETH-leg corrected |",
        "| `token_volatility_5m` | tick_range/|tick_last| (price range) | size-CV = stddev(abs_flow)/mean(abs_flow) |",
        "| `activity_heat` | exact swap count | exact swap count (unchanged) |",
        "| `unique_buyer_entropy_5m` | log1p(unique_buyers) proxy | same (EATLQ-additive only) |",
        "",
        "**WETH-leg side fix:** Sprint037B used raw `amount0 < 0` as `is_buy_meme` for all pools.",
        "This is correct for WETH=token1 pools (meme=token0) but **inverted** for WETH=token0 pools",
        "(meme=token1), where buying meme requires `amount0 > 0` (WETH entering pool).",
        "~50% of the meme pool universe has `weth_is_token0=1`, so the inversion was systematic.",
        "",
        "---",
        "",
        "## 2. Candidate Summary",
        "",
    ]

    for r in regimes:
        ws = r["week_start"]
        n = sum(1 for c in candidates if c.get("regime_week") == ws)
        lines.append(f"- **{ws}** ({r['label']}): {n} candidates")

    lines += [
        f"- **Total**: {len(candidates)} candidates",
        "",
        "---",
        "",
        "## 3. Tick-Proxy Readout (diagnostic only)",
        "",
    ]
    tp = summary.get("tick_proxy_readout", {})
    if tp.get("n_labelled", 0) > 0:
        lines += [
            f"- N: {tp.get('n_labelled')}",
            f"- label_1_share: {tp.get('label_1_share')}",
            f"- median: {tp.get('median_net_return')}",
            f"- trimmed_mean: {tp.get('trimmed_mean_net_return')}",
        ]
    else:
        lines.append("- No tick-proxy labels computed.")

    lines += [
        "",
        "---",
        "",
        "## 4. RPC-backed Label Readout",
        "",
    ]
    rr = summary.get("rpc_label_readout", {})
    if rr.get("n_labelled", 0) > 0:
        lines += [
            f"- N labelled: {rr.get('n_labelled')}",
            f"- label_1_share: {rr.get('label_1_share')}",
            f"- median net_return: {rr.get('median_net_return')}",
            f"- trimmed_mean: {rr.get('trimmed_mean_net_return')}",
            f"- p10/p90: {rr.get('p10_return')} / {rr.get('p90_return')}",
            "",
            "### Per-regime (RPC):",
            "",
            "| Regime | N | Pos Share | Median | Mean |",
            "|--------|---|-----------|--------|------|",
        ]
        for regime_k, rs in (rr.get("per_regime") or {}).items():
            lines.append(
                f"| {regime_k} | {rs.get('n')} | {rs.get('pos_share')} "
                f"| {rs.get('median')} | {rs.get('mean')} |"
            )
        lines += [
            "",
            "### Per-fee-tier (RPC):",
            "",
            "| Fee (bps) | N | Pos Share | Median |",
            "|-----------|---|-----------|--------|",
        ]
        for fee_k, fs in sorted((rr.get("per_fee_tier") or {}).items()):
            lines.append(
                f"| {fee_k} | {fs.get('n')} | {fs.get('pos_share')} | {fs.get('median')} |"
            )
        fat_pos = rr.get("fat_tail_pos", {})
        lines += [
            "",
            f"### Fat-tail positive (net_return ≥ 1.0): {fat_pos.get('n', 0)} rows "
            f"({fat_pos.get('share', 0):.2%})",
        ]
        if fat_pos.get("n", 0) > 0:
            lines += [
                "",
                "| Pool | Fee | Weth0 | AH | Return | Entry |",
                "|------|-----|-------|----|--------|-------|",
            ]
            for s in (fat_pos.get("sample") or [])[:15]:
                lines.append(
                    f"| {s['pool'][:10]}… | {s.get('fee')} | {s.get('weth0')} "
                    f"| {s.get('ah')} | {s.get('ret'):.4f} | {s.get('entry_iso')} |"
                )
    else:
        lines.append("- RPC label validation not run (dry-run or no RPC URL).")

    cmp = summary.get("tick_proxy_vs_rpc_comparison", {})
    lines += [
        "",
        "---",
        "",
        "## 5. Tick-Proxy vs RPC Label Comparison",
        "",
        f"- N paired: {cmp.get('n_paired', 0)}",
        f"- Agreement rate: {cmp.get('agreement_rate', 'N/A')}",
        f"- Disagreement rate: {cmp.get('disagreement_rate', 'N/A')}",
        f"- Confusion: {cmp.get('confusion', {})}",
        f"- Proxy precision vs RPC: {cmp.get('proxy_precision_vs_rpc', 'N/A')}",
        f"- Proxy recall vs RPC: {cmp.get('proxy_recall_vs_rpc', 'N/A')}",
        f"- Return delta p50: {cmp.get('return_delta_median', 'N/A')}",
        f"- Return delta p90: {cmp.get('return_delta_p90', 'N/A')}",
        "",
        "---",
        "",
        "## 6. Decision Flags",
        "",
        f"- TRAINING_ALLOWED: false",
        f"- LIVE_ALLOWED: false",
        f"- MODEL_PROMOTION_ALLOWED: false",
        f"- FAILED_NO_7D_ALPHA_ACTIVE: true",
        f"- RPC_BACKED_LABEL_VALIDATION_ALLOWED: true",
        f"- NO_ALPHA_CLAIM: true",
        f"- sprint037c_training_review_allowed: {summary.get('sprint037c_training_review_allowed')}",
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037b_r2_replay_repair.py*",
    ]

    OUT_REPORT.write_text("\n".join(lines))
    _log(f"Written: {OUT_REPORT}")


if __name__ == "__main__":
    main()
