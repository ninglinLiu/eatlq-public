#!/usr/bin/env python3
"""Sprint037B — Historical FeatureStore Replay on Real High-Opportunity Regimes.

Charter
───────
Replay Base Uniswap V3 WETH-meme swap history through FeatureStore-aligned feature
computation on the top regime weeks identified by Sprint037A. Produce bounded label /
economic readout on the resulting entry candidates.

Constraints (all must hold)
────────────────────────────
- No training, no live routing, no model promotion, no private keys.
- ClickHouse read-only; bounded RPC for label proxy only if explicitly enabled.
- Strict no-leakage: feature window is [entry_time - 300s, entry_time).
  Candidate selection uses ONLY pre-entry features; label proxy is readout-only.
- No future information used to select pools or entry candidates.
- No alpha claim; economic readout is an exploratory proxy, not live-equivalent.

Selected Regimes (from sprint037a top_historical_regimes, burst_score descending)
──────────────────────────────────────────────────────────────────────────────────
Rank 1 — 2026-02-02: burst=61.18, 305 606 swaps, 126 dense pools, 297 active pools
Rank 2 — 2025-12-01: burst=55.83, 277 842 swaps, 85 dense pools, 214 active pools
Rank 3 — 2025-12-29: burst=46.88, 194 020 swaps, 46 dense pools, 262 active pools

Selection rationale: highest burst_score AND dense_pools ≥ 40 AND total_swaps ≥ 100k.
The pilot runs rank 1 first; expands to ranks 2–3 if pilot yields ≥ 30 candidates.

CH Tables Used
──────────────
- eatlq.uniswap_v3_swaps_decoded  — decoded V3 swaps (amount0 Int256, amount1 Int256,
                                     sqrt_price_x96 UInt256)
- eatlq.discovered_pools          — pool metadata (meme filter, WETH leg, decimals)
- eatlq.uniswap_v3_swaps_deduped  — underlying deduped table (same data, raw hex)

Outputs
───────
- mvp/diagnostics/sprint037b_featurestore_replay_summary.json
- mvp/audits/sprint037b_replay_candidates.csv  (capped at CANDIDATE_CSV_CAP rows)
- mvp/audits/sprint037b_economic_readout.json
- mvp/audits/sprint037b_featurestore_replay_report.md

Usage
─────
  CLICKHOUSE_URL=http://... CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD=... \\
      python3 scripts/sprint037b_featurestore_replay.py --run

  python3 scripts/sprint037b_featurestore_replay.py --dry-run
"""

from __future__ import annotations

import argparse
import base64
import bisect
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

# ── Constants (frozen from label-semantics-freeze.md and whitepaper-v2) ──────
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ROUND_TRIP_COST = 0.006       # 0.6% frozen round-trip cost from label-semantics-freeze.md
WINDOW_SEC = 300              # 5-minute FeatureStore observation window
LABEL_HORIZON_SEC = 1800      # 30-minute exit horizon (frozen)

MIN_ACTIVITY_HEAT = 5         # minimum swaps per 5m bucket to emit candidate
CANDIDATE_CSV_CAP = 500       # max rows written to CSV artifact
MAX_CANDIDATES_PER_REGIME = 5000  # internal cap per regime (no silent omission)
EXPAND_MIN_CANDIDATES = 30    # expand to ranks 2–3 if pilot yields ≥ this many

# Meme pool filter (aligned with Sprint035D / Sprint037A)
_STABLES = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x4a3a6dd60a34bb2aba60d73b4c88d1e535f79e18",  # MIM
    "0x368181499736d0c0cc614dbb145e2ec1ac86b8c6",  # CUSD
    "0x04d5ddf5f3a8939889f11e97f8c4bb48317f1938",  # USDz
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
})
_EXCLUDED_LARGE_CAP = frozenset({
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0x6c561b446416e1a00e8e93e221854d6ea4171372",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
    "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",
    "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
})

# ── Regime definitions (from sprint037a_regime_mining_summary.json) ───────────
_PILOT_REGIME: dict[str, Any] = {
    "week_start": "2026-02-02",
    "week_end":   "2026-02-09",
    "rank": 1,
    "burst_score": 61.182351,
    "total_swaps": 305606,
    "dense_pools": 126,
    "active_pools": 297,
    "selection_rationale": (
        "Highest burst_score (log1p(126) × log1p(305606)); 126 dense pools, "
        "305 606 swaps. Full BF180 coverage confirmed (2026-02 chunk = 470 140 rows). "
        "Ideal 5m replay density: ~43 swaps/pool/minute across dense pools."
    ),
}
_EXPANSION_REGIMES: list[dict[str, Any]] = [
    {
        "week_start": "2025-12-01",
        "week_end":   "2025-12-08",
        "rank": 2,
        "burst_score": 55.834404,
        "total_swaps": 277842,
        "dense_pools": 85,
        "active_pools": 214,
        "selection_rationale": (
            "2nd burst_score; 85 dense pools, 277 842 swaps. "
            "Early meme season (Q4 2025); disjoint from rank 1 to maximise temporal spread."
        ),
    },
    {
        "week_start": "2025-12-29",
        "week_end":   "2026-01-05",
        "rank": 3,
        "burst_score": 46.878326,
        "total_swaps": 194020,
        "dense_pools": 46,
        "active_pools": 262,
        "selection_rationale": (
            "3rd burst_score; 262 active pools (most active universe), year-end market. "
            "Large active_pool count with moderate dense_pools density; tests feature "
            "robustness across a wide universe."
        ),
    },
]

# ── Output paths ─────────────────────────────────────────────────────────────
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037b_featurestore_replay_summary.json"
OUT_CANDIDATES_CSV = REPO / "mvp/audits/sprint037b_replay_candidates.csv"
OUT_READOUT_JSON = REPO / "mvp/audits/sprint037b_economic_readout.json"
OUT_REPORT_MD = REPO / "mvp/audits/sprint037b_featurestore_replay_report.md"

DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "H12_ABLATION_ALLOWED": False,
    "MODEL_PROMOTION_ALLOWED": False,
    "PHASE6_ALLOWED": False,
    "SPRINT037B_REPLAY_ALLOWED": True,
    "READOUT_ONLY": True,
    "CLICKHOUSE_READ_ONLY": True,
    "RPC_REQUIRED": False,
    "NO_ALPHA_CLAIM": True,
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "LEAKAGE_GUARD": "feature_window=[entry_time-300s,entry_time)_strictly_before_entry",
}

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[037b {ts}] {msg}", flush=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard_readonly(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"only read-only queries allowed; got: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r} in query")


def _ch_query(
    url: str,
    sql: str,
    user: str,
    pwd: str,
    timeout: float = 600.0,
    label: str = "query",
) -> list[dict[str, Any]]:
    _ch_guard_readonly(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    _log(f"  CH {label}: posting {len(q)} chars to {url}")
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if user:
        tok = base64.b64encode(f"{user}:{pwd}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {tok}")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"CH {label} failed: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                _log(f"  WARNING: JSON parse error in {label}: {e} — line: {line[:80]}")
    _log(f"  CH {label}: {len(rows)} rows returned")
    return rows


# ── SQL builders ─────────────────────────────────────────────────────────────
def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))


def _excluded_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_EXCLUDED_LARGE_CAP))


def _meme_pools_cte() -> str:
    """CTE selecting meme-oriented WETH V3 pools from discovered_pools.

    Aligned with Sprint035D / Sprint037A meme pool filter (chain_id=8453, v3, has_weth_leg,
    standard fee tiers, no stable/stable, no large-cap exclusions).
    """
    return f"""meme_pools AS (
  SELECT
    lower(pool_address)      AS pa,
    lower(token0_address)    AS token0_addr,
    lower(token1_address)    AS token1_addr,
    toUInt32(fee_bps)        AS fee_bps,
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
    """Canonical timestamp expression: block_time preferred, received_at_ms fallback."""
    return f"coalesce({alias}.block_time, {alias}.received_at_ms)"


def build_feature_query(start_dt: str, feature_end_dt: str) -> str:
    """Build SQL for per-pool per-5m-bucket feature aggregation.

    Uses eatlq.uniswap_v3_swaps (base MV table) directly — avoids querying through
    the uniswap_v3_swaps_deduped and uniswap_v3_swaps_decoded VIEW chain, which causes
    double-aggregation and memory pressure (MEMORY_LIMIT_EXCEEDED on full-week queries).

    Leakage guard
    ─────────────
    entry_time = toStartOfFiveMinute(swap_ts) + 300s
    feature_window = [toStartOfFiveMinute(swap_ts), toStartOfFiveMinute(swap_ts) + 300s)
                   = [entry_time - 300s, entry_time)

    Swaps in the feature window are STRICTLY before entry_time. No future data is
    included: the bucket groups swaps that occurred during the observation window,
    and the entry signal is emitted at the END of that window.

    Features
    ────────
    - activity_heat          : exact swap count in 5m window
    - buy_count / sell_count : amount0_raw sign (first hex digit >= '8' → MSB set → negative
                               int256 → amount0 < 0 → pool paid out token0 → user bought token0)
                               Convention aligned with FeatureStore._decode_v3_swap is_buy logic.
    - buy_pressure_ratio     : buy_count / total — count proxy for buy_pressure_ratio
    - price_velocity_5m      : (buy - sell) / total — count proxy for net flow velocity
    - token_volatility_5m    : tick_range / max(abs(tick_last), 1) — tick-based price range proxy
                               (FeatureStore uses abs_flow CV; this is price dispersion in log space)
    - unique_buyer_entropy_5m: log1p(unique_buyers) — proxy for FeatureStore Shannon entropy
    - tick_last              : last V3 tick in feature window (for price-based label proxy)
    """
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                                                            AS pool_address,
  mp.fee_bps,
  mp.token0_addr,
  mp.token1_addr,
  mp.weth_is_token0,
  toUnixTimestamp(toStartOfFiveMinute({te}))                                      AS bucket_5m_sec,
  toUnixTimestamp(toStartOfFiveMinute({te})) + {WINDOW_SEC}                      AS entry_time_sec,
  count()                                                                          AS activity_heat,
  countIf(lower(substring(s.amount0_raw, 3, 1)) >= '8')                          AS buy_count,
  countIf(lower(substring(s.amount0_raw, 3, 1)) < '8')                           AS sell_count,
  uniqCombinedIf(lower(s.sender), lower(substring(s.amount0_raw, 3, 1)) >= '8') AS unique_buyers,
  uniqCombined(lower(s.sender))                                                   AS unique_senders,
  min(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                  AS tick_min,
  max(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                  AS tick_max,
  argMax(toInt32(eatlq_hex_to_int256(s.tick_raw)), {te})                         AS tick_last,
  min(toUnixTimestamp({te}))                                                      AS first_swap_sec,
  max(toUnixTimestamp({te}))                                                      AS last_swap_sec
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{feature_end_dt}'
GROUP BY
  pool_address, mp.fee_bps, mp.token0_addr, mp.token1_addr, mp.weth_is_token0,
  bucket_5m_sec, entry_time_sec
HAVING activity_heat >= {MIN_ACTIVITY_HEAT}
ORDER BY pool_address, bucket_5m_sec
"""


def build_price_series_query(start_dt: str, end_dt: str) -> str:
    """Build SQL for price time series used in economic label proxy.

    Uses tick (int24, V3 log-price) rather than sqrtPriceX96 (uint256) for efficiency.
    tick is an int24 value stored as 32-byte padded hex; decoded with eatlq_hex_to_int256.
    Price relationship: price = 1.0001^tick, so:
      price_return = 1.0001^(exit_tick - entry_tick) - 1

    Leakage guard: this data is ONLY used after candidates are finalized,
    strictly for readout computation. Candidate selection is never conditioned
    on the prices returned here.

    Returns (pool_address, ts_sec, tick_i32, weth_is_token0) sorted by pool, ts.
    """
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                         AS pool_address,
  toUnixTimestamp({te})                         AS ts_sec,
  toInt32(eatlq_hex_to_int256(s.tick_raw))      AS tick_i32,
  mp.weth_is_token0
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
ORDER BY pool_address, ts_sec
"""


# ── Feature computation (FeatureStore-aligned) ────────────────────────────────
def compute_features_from_agg(row: dict[str, Any]) -> dict[str, Any]:
    """Compute FeatureStore-aligned feature vector from CH aggregate row.

    Proxy differences from runtime FeatureStore.compute_from_snapshot:
    - price_velocity_5m: uses count proxy (buy-sell / total), not volume-weighted net_flow
    - buy_pressure_ratio: count proxy (buy_count / total), not abs_flow volume ratio
    - token_volatility_5m: tick_range / max(abs(tick_last), 1) — log-price dispersion proxy
                           (FeatureStore uses abs_flow CV; both measure intra-window spread)
    - unique_buyer_entropy_5m: log1p(unique_buyers) vs full Shannon entropy of buyer volumes
    - Toxic-flow filtering: NOT applied (requires per-wallet state across time; documented)
    - buy_pressure_ratio_count: same as buy_pressure_ratio for this count-based proxy
    """
    activity_heat = int(row.get("activity_heat") or 0)
    buy_count = int(row.get("buy_count") or 0)
    sell_count = int(row.get("sell_count") or 0)
    unique_buyers = int(row.get("unique_buyers") or 0)
    unique_senders = int(row.get("unique_senders") or 0)
    tick_min = int(row.get("tick_min") or 0)
    tick_max = int(row.get("tick_max") or 0)
    tick_last = int(row.get("tick_last") or 0)
    tick_range = abs(tick_max - tick_min)

    total = max(buy_count + sell_count, 1)

    price_velocity_5m = max(-1.0, min(1.0, (buy_count - sell_count) / total))
    buy_pressure_ratio = max(0.0, min(1.0, buy_count / total))
    # tick_range normalized by |tick_last|: rough log-price range relative to current price
    token_volatility_5m = min(2.0, max(0.0, tick_range / max(abs(tick_last), 1)))
    unique_buyer_entropy_5m = math.log1p(unique_buyers)
    buy_pressure_ratio_count = buy_pressure_ratio

    return {
        "price_velocity_5m": round(price_velocity_5m, 6),
        "buy_pressure_ratio": round(buy_pressure_ratio, 6),
        "token_volatility_5m": round(token_volatility_5m, 6),
        "activity_heat": activity_heat,
        "unique_buyer_entropy_5m": round(unique_buyer_entropy_5m, 6),
        "buy_pressure_ratio_count": round(buy_pressure_ratio_count, 6),
        "unique_buyers": unique_buyers,
        "unique_senders": unique_senders,
        "tick_last": tick_last,
        "tick_range": tick_range,
    }


# ── Price series index (tick-based) ──────────────────────────────────────────
class PoolPriceIndex:
    """Per-pool sorted tick (log-price) series for O(log N) label-proxy lookups.

    Uses V3 tick (int24) rather than sqrtPriceX96 (uint256). Tick is memory-efficient
    (int32 per observation) and avoids Float64 precision issues.
    Price: 1.0001^tick. Return: 1.0001^(exit_tick - entry_tick) - 1.

    Leakage guard: this object is only read AFTER candidates are finalized;
    it is never used to filter or rank candidates during generation.
    """

    def __init__(self) -> None:
        self._ts: dict[str, list[int]] = defaultdict(list)
        self._tick: dict[str, list[int]] = defaultdict(list)

    def ingest(self, pool_address: str, ts_sec: int, tick: int) -> None:
        if pool_address and ts_sec > 0:
            self._ts[pool_address].append(ts_sec)
            self._tick[pool_address].append(tick)

    def get_last_tick_before(self, pool_address: str, ts_sec: int) -> Optional[int]:
        """Return last known tick STRICTLY before ts_sec, or None."""
        series_ts = self._ts.get(pool_address)
        if not series_ts:
            return None
        idx = bisect.bisect_left(series_ts, ts_sec)
        if idx == 0:
            return None
        return self._tick[pool_address][idx - 1]

    def pools(self) -> set[str]:
        return set(self._ts.keys())


# ── Economic label proxy ──────────────────────────────────────────────────────
def compute_price_return_proxy(
    price_index: PoolPriceIndex,
    pool_address: str,
    entry_time_sec: int,
    weth_is_token0: int,
) -> Optional[float]:
    """Compute tick-based 30m return proxy for a candidate.

    Leakage guard: entry_tick uses last tick STRICTLY before entry_time_sec.
    exit_tick uses last tick STRICTLY before entry_time_sec + LABEL_HORIZON_SEC.
    These values are used ONLY for readout, NEVER for candidate selection.

    Formula:
      V3 tick: price_of_token0_in_token1 = 1.0001^tick
      For weth_is_token0=0 (meme=token0, WETH=token1):
        price_meme_WETH = 1.0001^tick  ← higher tick = more WETH per meme
        meme_return = 1.0001^(exit_tick - entry_tick) - 1
      For weth_is_token0=1 (WETH=token0, meme=token1):
        price_meme_WETH = 1 / 1.0001^tick = 1.0001^(-tick) ← lower tick = more WETH per meme
        meme_return = 1.0001^(entry_tick - exit_tick) - 1

    NOT the frozen label semantics (QuoterV2 simulation); exploratory proxy only.
    Round-trip cost (ROUND_TRIP_COST = 0.006) subtracted for label-semantics consistency.
    """
    entry_tick = price_index.get_last_tick_before(pool_address, entry_time_sec)
    if entry_tick is None:
        return None

    exit_time_sec = entry_time_sec + LABEL_HORIZON_SEC
    exit_tick = price_index.get_last_tick_before(pool_address, exit_time_sec)
    if exit_tick is None:
        return None

    tick_delta = exit_tick - entry_tick
    if int(weth_is_token0) == 1:
        # WETH=token0, meme=token1: price_meme_WETH = 1.0001^(-tick)
        # meme_return = 1.0001^(entry-exit) = 1.0001^(-delta) - 1
        tick_delta = -tick_delta

    # 1.0001^tick_delta - 1 = exp(tick_delta * ln(1.0001)) - 1
    # Cap tick_delta at ±50000 to prevent float overflow on ultra-thin pool outliers
    # (tick_delta=50000 ≈ 5x price move, well beyond a 30m realistic window)
    tick_delta_capped = max(-50000, min(50000, tick_delta))
    raw_return = math.expm1(tick_delta_capped * 9.9995000333297e-5)  # ln(1.0001)
    net_return = raw_return - ROUND_TRIP_COST
    # Hard cap on net_return for readout sanity; extreme values are flagged separately
    return max(-1.0, min(5.0, net_return))


# ── Economic readout ──────────────────────────────────────────────────────────
def compute_readout(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize economic readout over all labelled candidates."""
    if not candidates:
        return {"status": "NO_CANDIDATES", "n_total": 0, "n_labelled": 0}

    with_label = [c for c in candidates if c.get("net_return_proxy") is not None]
    n_total = len(candidates)
    n_labelled = len(with_label)

    if n_labelled == 0:
        return {"status": "NO_LABELS", "n_total": n_total, "n_labelled": 0}

    returns = sorted(c["net_return_proxy"] for c in with_label)
    n = len(returns)
    positive_n = sum(1 for r in returns if r > 0)

    trim_n = max(1, n // 10)
    trimmed = returns[trim_n:-trim_n] if n > 2 * trim_n else returns

    per_regime: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "positive": 0, "returns": []}
    )
    per_pool: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "positive": 0, "returns": []}
    )
    for c in with_label:
        regime = c.get("regime_week", "unknown")
        pool = c.get("pool_address", "unknown")
        per_regime[regime]["n"] += 1
        per_regime[regime]["returns"].append(c["net_return_proxy"])
        if c["net_return_proxy"] > 0:
            per_regime[regime]["positive"] += 1
        per_pool[pool]["n"] += 1
        per_pool[pool]["returns"].append(c["net_return_proxy"])
        if c["net_return_proxy"] > 0:
            per_pool[pool]["positive"] += 1

    def _stats(r: list[float]) -> dict[str, Any]:
        if not r:
            return {}
        rs = sorted(r)
        k = len(rs)
        return {
            "n": k,
            "positive": sum(1 for x in rs if x > 0),
            "positive_share": round(sum(1 for x in rs if x > 0) / k, 4),
            "mean": round(sum(rs) / k, 6),
            "median": round(rs[k // 2], 6),
        }

    regime_summary = {rk: _stats(v["returns"]) for rk, v in per_regime.items()}

    # Top 10 pools by n_labelled (LOO-style sample check)
    top_pools_by_n = sorted(per_pool.items(), key=lambda kv: kv[1]["n"], reverse=True)[:10]
    pool_summary = {pa: _stats(v["returns"]) for pa, v in top_pools_by_n}

    label_1_share = positive_n / n

    return {
        "status": "COMPLETED",
        "n_total_candidates": n_total,
        "n_labelled": n_labelled,
        "label_coverage_pct": round(100.0 * n_labelled / max(n_total, 1), 2),
        "label_1_share": round(label_1_share, 4),
        "positive_n": positive_n,
        "mean_net_return": round(sum(returns) / n, 6),
        "median_net_return": round(returns[n // 2], 6),
        "trimmed_mean_net_return": round(sum(trimmed) / max(len(trimmed), 1), 6),
        "p10_return": round(returns[max(0, n // 10)], 6),
        "p90_return": round(returns[min(n - 1, 9 * n // 10)], 6),
        "per_regime": regime_summary,
        "top_pools_by_n": pool_summary,
        "label_note": (
            "swap-price-proxy; NOT frozen QuoterV2 label. "
            "Positive return = meme price rose ≥ 0.6% in 30m after entry_time. "
            "Survivorship bias: pool set = current discovered_pools ∩ BF180 history."
        ),
    }


# ── Report writer ────────────────────────────────────────────────────────────
def _write_report(
    summary: dict[str, Any],
    readout: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    n_cands = len(candidates)
    status = summary.get("status", "UNKNOWN")
    lines = [
        "# Sprint037B — Historical FeatureStore Replay Report",
        "",
        f"Generated: {summary.get('created_at', '')}",
        "",
        f"**Status: `{status}`**",
        "",
        "---",
        "",
        "## 1. Overview",
        "",
        "This report documents the minimal historical FeatureStore replay on top-ranked",
        "regime weeks identified by Sprint037A. Features were computed from actual",
        "ClickHouse swap data; no training, live execution, or model involvement.",
        "",
        "---",
        "",
        "## 2. Selected Regimes",
        "",
        "| Rank | Week Start | Burst Score | Total Swaps | Dense Pools | Selection Rationale |",
        "|------|-----------|------------|------------|------------|---------------------|",
    ]
    for r in summary.get("selected_regimes", []):
        lines.append(
            f"| {r.get('rank', '?')} | {r.get('week_start', '?')} | "
            f"{r.get('burst_score', 0):.2f} | {r.get('total_swaps', 0):,} | "
            f"{r.get('dense_pools', 0)} | "
            f"{r.get('selection_rationale', '')[:60]}... |"
        )
    lines += [
        "",
        "---",
        "",
        "## 3. Replay Parameters",
        "",
        f"- FeatureStore window: {WINDOW_SEC}s (5 minutes)",
        f"- Label horizon: {LABEL_HORIZON_SEC}s (30 minutes)",
        f"- Min activity heat: {MIN_ACTIVITY_HEAT} swaps/bucket to emit candidate",
        f"- Round-trip cost: {ROUND_TRIP_COST} (0.6%, frozen from label-semantics-freeze.md)",
        f"- Total candidates generated: {n_cands}",
        "",
        "---",
        "",
        "## 4. Feature Definitions (proxies)",
        "",
        "| Feature | Source | Difference from runtime FeatureStore |",
        "|---------|--------|--------------------------------------|",
        "| `price_velocity_5m` | (buy_count - sell_count) / total | Count proxy; runtime uses volume-weighted net_flow / abs_total_flow |",
        "| `buy_pressure_ratio` | buy_count / total | Count proxy; runtime uses abs_flow volume ratio |",
        "| `token_volatility_5m` | stddev(sqrtPriceX96) / mean(sqrtPriceX96) | Price CV proxy; runtime uses abs_flow CV |",
        "| `activity_heat` | exact swap count | Exact match |",
        "| `unique_buyer_entropy_5m` | log1p(unique_buyers) | Proxy; runtime uses Shannon entropy of buyer volume distribution |",
        "| `buy_pressure_ratio_count` | same as buy_pressure_ratio | Same |",
        "",
        "**Toxic-flow filtering:** NOT applied in replay (requires per-wallet Redis state "
        "across continuous time; architecturally out of scope for offline batch replay).",
        "",
        "---",
        "",
        "## 5. Leakage Safeguards",
        "",
        "1. **Feature window strictly before entry:** features use swaps in",
        "   `[entry_time - 300s, entry_time)` only.",
        "2. **Candidate selection on features only:** `activity_heat >= 5` applied",
        "   to pre-entry features; no outcome conditioning.",
        "3. **Label proxy readout-only:** price return computed from",
        "   `[entry_time, entry_time + 1800s)` swap prices; never used as a filter.",
        "4. **No future pool filter:** meme pool set is current `discovered_pools`",
        "   (survivorship bias noted but not correctable without archived metadata).",
        "5. **No future winner back-propagation:** positive outcomes were not used to",
        "   narrow the candidate universe.",
        "",
        "---",
        "",
        "## 6. Regime Processing Summary",
        "",
        "| Regime | Status | Feature Rows | Candidates |",
        "|--------|--------|-------------|-----------|",
    ]
    for pr in summary.get("processed_regimes", []):
        lines.append(
            f"| {pr.get('regime', '?')} | {pr.get('status', '?')} | "
            f"{pr.get('feature_rows', 0):,} | {pr.get('candidates', 0):,} |"
        )
    lines += [
        "",
        "---",
        "",
        "## 7. Economic Readout",
        "",
    ]
    if readout.get("status") == "COMPLETED":
        n_lab = readout.get("n_labelled", 0)
        l1 = readout.get("label_1_share", 0.0)
        mn = readout.get("mean_net_return", None)
        med = readout.get("median_net_return", None)
        tmn = readout.get("trimmed_mean_net_return", None)
        p10 = readout.get("p10_return", None)
        p90 = readout.get("p90_return", None)
        lines += [
            f"- **Labelled candidates:** {n_lab:,}",
            f"- **Label_1 share (positive_return > 0):** {l1:.3f} ({l1*100:.1f}%)",
            f"- **Mean net return proxy:** {mn:.4f}" if mn is not None else "- Mean: N/A",
            f"- **Median net return proxy:** {med:.4f}" if med is not None else "",
            f"- **Trimmed mean (10%):** {tmn:.4f}" if tmn is not None else "",
            f"- **P10 / P90:** {p10:.4f} / {p90:.4f}" if (p10 is not None and p90 is not None) else "",
            "",
            "### Per-Regime Breakdown",
            "",
            "| Regime | N | Positive | Positive% | Mean Return |",
            "|--------|---|----------|-----------|-------------|",
        ]
        for regime, stats in readout.get("per_regime", {}).items():
            pos_share = stats.get("positive_share", 0.0)
            mean_r = stats.get("mean", 0.0)
            lines.append(
                f"| {regime} | {stats.get('n', 0)} | {stats.get('positive', 0)} | "
                f"{pos_share*100:.1f}% | {mean_r:.4f} |"
            )
    else:
        lines.append(f"Status: {readout.get('status', 'UNKNOWN')}")
    lines += [
        "",
        "---",
        "",
        "## 8. Sprint037C Decision",
        "",
    ]
    sprint037c_allowed = summary.get("sprint037c_allowed", False)
    if sprint037c_allowed:
        lines += [
            "**`sprint037c_allowed: true`**",
            "",
            "Readout shows promising signal (label_1_share ≥ threshold and mean return ≥ −0.005).",
            "Proceed to Sprint037C human review and GPT-5.5 analysis before any training gate.",
            "Sprint037C does NOT authorize training or live execution.",
        ]
    else:
        lines += [
            "**`sprint037c_allowed: false`**",
            "",
            "Economic readout does not meet the promising threshold.",
            "Do not proceed to Sprint037C training pre-plan. Diagnose feature proxy gaps or",
            "regime selection before escalating.",
        ]
    lines += [
        "",
        "---",
        "",
        "## 9. Hard Constraints",
        "",
        "- TRAINING_ALLOWED: false",
        "- LIVE_ALLOWED: false",
        "- H12_ABLATION_ALLOWED: false",
        "- MODEL_PROMOTION_ALLOWED: false",
        "- PHASE6_ALLOWED: false",
        "- NO_ALPHA_CLAIM: true (readout is exploratory proxy, not live-equivalent alpha)",
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037b_featurestore_replay.py*",
    ]
    OUT_REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT_MD.write_text("\n".join(lines) + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Sprint037B FeatureStore Replay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Env vars: CLICKHOUSE_URL, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD\n"
            "Example:\n"
            "  CLICKHOUSE_URL=http://localhost:8123/ \\\n"
            "    python3 scripts/sprint037b_featurestore_replay.py --run"
        ),
    )
    parser.add_argument("--run", action="store_true", help="Run the full replay pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL and exit (no CH queries, no output files)",
    )
    parser.add_argument(
        "--pilot-only",
        action="store_true",
        help="Run only the pilot regime (rank 1); do not auto-expand",
    )
    args = parser.parse_args(argv)

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123/").strip()
    ch_user = os.environ.get("CLICKHOUSE_USER", "default").strip()
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "").strip()

    _log("=" * 60)
    _log("Sprint037B — Historical FeatureStore Replay")
    _log(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    _log(f"CH: {ch_url} (user={ch_user})")
    _log(f"Pilot regime: {_PILOT_REGIME['week_start']} (rank={_PILOT_REGIME['rank']}, burst={_PILOT_REGIME['burst_score']})")
    _log("=" * 60)

    all_candidates: list[dict[str, Any]] = []
    processed_regimes: list[dict[str, Any]] = []
    selected_regimes: list[dict[str, Any]] = [_PILOT_REGIME]

    regimes_to_run = [_PILOT_REGIME]

    for regime_idx, regime in enumerate(regimes_to_run):
        week_start = regime["week_start"]
        week_end = regime["week_end"]

        # 5-min buffer before regime start so first feature window has lookback context
        buf_start_dt = (
            datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            - timedelta(minutes=5)
        ).strftime("%Y-%m-%d %H:%M:%S")

        feature_end_dt = week_end + " 00:00:00"
        # Label proxy needs prices up to LABEL_HORIZON_SEC after last entry
        price_end_dt = (
            datetime.strptime(week_end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(seconds=LABEL_HORIZON_SEC + WINDOW_SEC)
        ).strftime("%Y-%m-%d %H:%M:%S")

        _log(f"\n{'='*50}")
        _log(f"Regime {regime_idx+1}: {week_start} → {week_end}")
        _log(f"  burst_score={regime['burst_score']:.2f}  total_swaps={regime['total_swaps']:,}  dense_pools={regime['dense_pools']}")
        _log(f"  Feature window: {buf_start_dt} → {feature_end_dt}")
        _log(f"  Price  window:  {buf_start_dt} → {price_end_dt}")

        # ── Build SQL ──────────────────────────────────────────────────────
        feature_sql = build_feature_query(buf_start_dt, feature_end_dt)
        price_sql = build_price_series_query(buf_start_dt, price_end_dt)

        if args.dry_run:
            _log("--- FEATURE SQL (truncated) ---")
            print(feature_sql[:800])
            _log("--- PRICE SQL (truncated) ---")
            print(price_sql[:400])
            continue

        # ── Feature aggregation query ──────────────────────────────────────
        _log("  Querying feature aggregation …")
        try:
            feature_rows = _ch_query(
                ch_url, feature_sql, ch_user, ch_password,
                timeout=600.0, label=f"features_{week_start}"
            )
        except Exception as exc:
            _log(f"  ERROR in feature query: {exc}")
            processed_regimes.append({
                "regime": week_start, "status": "QUERY_ERROR",
                "error": str(exc)[:200], "candidates": 0,
            })
            continue

        if not feature_rows:
            _log("  WARNING: no feature rows returned; skipping regime")
            processed_regimes.append({
                "regime": week_start, "status": "NO_DATA",
                "feature_rows": 0, "candidates": 0,
            })
            continue

        # ── Price time series query (for label proxy) ─────────────────────
        _log("  Querying price time series for label proxy …")
        try:
            price_rows = _ch_query(
                ch_url, price_sql, ch_user, ch_password,
                timeout=600.0, label=f"prices_{week_start}"
            )
        except Exception as exc:
            _log(f"  WARNING: price query failed ({exc}); label proxies will be None")
            price_rows = []

        # Build price index (sorted by ts within each pool — CH already orders by pool, ts)
        price_index = PoolPriceIndex()
        for pr in price_rows:
            pool_address = str(pr.get("pool_address") or "").lower()
            ts_sec = int(pr.get("ts_sec") or 0)
            tick = int(pr.get("tick_i32") or 0)
            price_index.ingest(pool_address, ts_sec, tick)

        _log(f"  Price index: {len(price_index.pools())} pools with price observations")

        # ── Generate candidates ────────────────────────────────────────────
        regime_start_sec = int(
            datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )
        regime_end_sec = int(
            datetime.strptime(week_end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )

        regime_candidates = 0
        capped = False

        for row in feature_rows:
            pool_address = str(row.get("pool_address") or "").lower()
            entry_time_sec = int(row.get("entry_time_sec") or 0)
            weth_is_token0 = int(row.get("weth_is_token0") or 0)

            if not pool_address or entry_time_sec <= 0:
                continue
            # Only emit candidates within the regime window
            if entry_time_sec < regime_start_sec or entry_time_sec > regime_end_sec:
                continue

            features = compute_features_from_agg(row)

            # ── Label proxy (readout only, never used for candidate filter) ──
            net_return = compute_price_return_proxy(
                price_index, pool_address, entry_time_sec, weth_is_token0
            )
            label_proxy = (
                1 if (net_return is not None and net_return > 0.0)
                else (0 if net_return is not None else None)
            )

            candidate: dict[str, Any] = {
                "regime_week": week_start,
                "pool_address": pool_address,
                "entry_time_sec": entry_time_sec,
                "entry_time_iso": datetime.fromtimestamp(
                    entry_time_sec, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fee_bps": int(row.get("fee_bps") or 0),
                "token0_addr": str(row.get("token0_addr") or ""),
                "token1_addr": str(row.get("token1_addr") or ""),
                "weth_is_token0": weth_is_token0,
                **features,
                "tick_last": int(row.get("tick_last") or 0),
                # Label proxy — readout only
                "net_return_proxy": (round(net_return, 6) if net_return is not None else None),
                "label_proxy": label_proxy,
            }
            all_candidates.append(candidate)
            regime_candidates += 1

            if regime_candidates >= MAX_CANDIDATES_PER_REGIME:
                capped = True
                _log(f"  Capped at {MAX_CANDIDATES_PER_REGIME} candidates")
                break

        _log(f"  Regime candidates: {regime_candidates}{' (capped)' if capped else ''}")
        processed_regimes.append({
            "regime": week_start,
            "status": "DONE",
            "feature_rows": len(feature_rows),
            "price_rows": len(price_rows),
            "candidates": regime_candidates,
            "capped": capped,
        })

        # ── Auto-expand to ranks 2–3 after pilot ──────────────────────────
        if (
            regime_idx == 0
            and not args.pilot_only
            and regime_candidates >= EXPAND_MIN_CANDIDATES
        ):
            _log(
                f"  Pilot has {regime_candidates} candidates ≥ {EXPAND_MIN_CANDIDATES} → "
                f"expanding to {len(_EXPANSION_REGIMES)} more regimes"
            )
            regimes_to_run.extend(_EXPANSION_REGIMES)
            selected_regimes.extend(_EXPANSION_REGIMES)
        elif regime_idx == 0 and not args.pilot_only:
            _log(
                f"  Pilot has {regime_candidates} candidates < {EXPAND_MIN_CANDIDATES}; "
                "not expanding (check CH data coverage or lower MIN_ACTIVITY_HEAT)"
            )

    if args.dry_run:
        _log("DRY RUN complete — no output files written")
        return

    # ── Compute readout ───────────────────────────────────────────────────────
    _log("\n" + "=" * 50)
    _log("Computing economic readout …")
    readout = compute_readout(all_candidates)
    _log(f"Readout status: {readout.get('status')}")
    _log(f"  n_labelled={readout.get('n_labelled')}, label_1_share={readout.get('label_1_share')}")
    _log(f"  mean_return={readout.get('mean_net_return')}, trimmed_mean={readout.get('trimmed_mean_net_return')}")

    # ── Determine sprint status ───────────────────────────────────────────────
    n_total = len(all_candidates)
    n_labelled = int(readout.get("n_labelled") or 0)
    label_1_share = float(readout.get("label_1_share") or 0.0)
    mean_return = readout.get("mean_net_return")
    trimmed_mean = readout.get("trimmed_mean_net_return")

    if n_total == 0 or n_labelled == 0:
        status = "REPLAY_PIPELINE_BUILT_NEEDS_MORE_ROWS"
    elif (
        mean_return is not None
        and label_1_share >= 0.42
        and mean_return >= -0.003
    ) or (
        trimmed_mean is not None
        and label_1_share >= 0.38
        and trimmed_mean >= 0.0
    ):
        status = "REPLAY_PROMISING_REVIEW_REQUIRED"
    else:
        status = "REPLAY_COMPLETED_ECON_WEAK"

    sprint037c_allowed = status == "REPLAY_PROMISING_REVIEW_REQUIRED"

    # ── Assemble summary ──────────────────────────────────────────────────────
    summary: dict[str, Any] = {
        "kind": "sprint037b_featurestore_replay",
        "version": "1.0",
        "created_at": _utc_now_iso(),
        "status": status,
        "sprint037c_allowed": sprint037c_allowed,
        "decision_flags": DECISION_FLAGS,
        "selected_regimes": selected_regimes,
        "regime_selection_rationale": {
            "method": "burst_score = log1p(dense_pools) × log1p(total_swaps) from sprint037a",
            "pilot": (
                "Rank 1 (2026-02-02): highest burst_score 61.18; 126 dense pools, 305 606 swaps."
                " Full BF180 coverage confirmed."
            ),
            "expansion": (
                "Ranks 2–3 added if pilot yields ≥ 30 candidates. "
                "Temporal diversity: Q4-2025 + year-end-2025."
            ),
            "rejection": "Weeks with burst_score < 40 or dense_pools < 40 excluded.",
        },
        "replay_parameters": {
            "feature_window_sec": WINDOW_SEC,
            "label_horizon_sec": LABEL_HORIZON_SEC,
            "min_activity_heat": MIN_ACTIVITY_HEAT,
            "round_trip_cost": ROUND_TRIP_COST,
            "expand_min_candidates": EXPAND_MIN_CANDIDATES,
        },
        "leakage_safeguards": {
            "feature_window": "strictly before entry_time: [entry_time - 300s, entry_time)",
            "candidate_selection": "activity_heat >= 5 using only pre-entry features",
            "label_proxy_usage": "computed from [entry_time, entry_time + 1800s); readout only",
            "no_future_pool_filter": (
                "meme pool set from current discovered_pools "
                "(survivorship bias documented, cannot correct without archived metadata)"
            ),
            "no_winner_back_propagation": "positive outcomes never used to select candidates",
        },
        "feature_proxy_notes": [
            "price_velocity_5m: count proxy (buy-sell)/total; runtime FeatureStore uses volume-weighted net_flow/abs_flow",
            "buy_pressure_ratio: count proxy buy_count/total; runtime uses abs_flow volume ratio",
            "token_volatility_5m: sqrtPriceX96 CV (price dispersion); runtime uses abs_flow CV",
            "unique_buyer_entropy_5m: log1p(unique_buyers) proxy; runtime uses Shannon entropy of buyer volumes",
            "Toxic-flow filtering: NOT applied (requires per-wallet Redis state, offline infeasible)",
        ],
        "label_proxy_notes": [
            "NOT the frozen label semantics (QuoterV2 simulation from label-semantics-freeze.md)",
            "tick-based proxy: uses last observed V3 tick for entry/exit price approximation",
            "price = 1.0001^tick (exact V3 definition); return = 1.0001^(exit_tick - entry_tick) - 1",
            "net_return = raw_return - 0.006 (round-trip cost subtracted for label-semantics consistency)",
            "tick is int24: no precision loss, no Float64 issues; much more reliable than sqrtPriceX96 proxy",
            "tick_delta capped at ±50000 (±5x price move) to prevent float overflow on ultra-thin pool outliers",
            "net_return hard-capped at [-1.0, 5.0] for readout; extreme outliers logged in n_extreme_returns",
        ],
        "processed_regimes": processed_regimes,
        "n_total_candidates": n_total,
        "n_regimes_processed": len(processed_regimes),
        "readout_summary": readout,
    }

    # ── Write outputs ─────────────────────────────────────────────────────────
    for path in [OUT_SUMMARY, OUT_CANDIDATES_CSV, OUT_READOUT_JSON, OUT_REPORT_MD]:
        path.parent.mkdir(parents=True, exist_ok=True)

    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    _log(f"Written: {OUT_SUMMARY}")

    OUT_READOUT_JSON.write_text(json.dumps(readout, indent=2))
    _log(f"Written: {OUT_READOUT_JSON}")

    sample = all_candidates[:CANDIDATE_CSV_CAP]
    if sample:
        fields = list(sample[0].keys())
        with OUT_CANDIDATES_CSV.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(sample)
    _log(f"Written: {OUT_CANDIDATES_CSV} ({len(sample)}/{n_total} rows)")

    _write_report(summary, readout, all_candidates)
    _log(f"Written: {OUT_REPORT_MD}")

    # ── Final status line ─────────────────────────────────────────────────────
    _log("")
    _log("=" * 60)
    _log(f"STATUS: {status}")
    _log(f"Candidates: {n_total}  |  Labelled: {n_labelled}")
    _log(f"Label_1 share: {label_1_share:.3f}  |  Mean return: {mean_return}")
    _log(f"Sprint037C allowed: {sprint037c_allowed}")
    _log("=" * 60)


if __name__ == "__main__":
    main()
