#!/usr/bin/env python3
"""Sprint038A — Recipient-keyed LASZLO-style fingerprint (diagnostics only)
===========================================================================

CONTEXT
-------
On Base L2 Uniswap V3, `sender` is the router/aggregator contract.
`recipient` is the actual end-user wallet.  sender_*_v1 views therefore
produce router-biased wallet rankings (SENDER_KEYED_RANKING_TRUSTED=false).

This script computes a LASZLO-style "author fingerprint" evidence_score
for each recipient wallet by measuring how closely its on-chain behaviour
matches the system described in the LASZLO whitepaper v2:
  - Base L2, Uniswap V3 WETH-leg pools
  - fee tiers 500 / 3000 / 10000
  - small-notional BUY positions (≈ $5–$35 in WETH)
  - short-horizon exits (trailing 5%, time 30 min, hard 15%)
  - round-trip behaviour with diverse token portfolio
  - early-pool participation (Sniper archetype)

The score is diagnostic only — it is NOT a prediction of profitability,
NOT an alpha claim, and NOT evidence of any individual's identity.

DECISION FLAGS (frozen — never override from CLI)
-------------------------------------------------
  TRAINING_ALLOWED              = false
  LIVE_ALLOWED                  = false
  MODEL_PROMOTION_ALLOWED       = false
  FAILED_NO_7D_ALPHA_ACTIVE     = true
  IDENTITY_CLAIM_ALLOWED        = false
  SENDER_KEYED_RANKING_TRUSTED  = false
  NO_ALPHA_CLAIM                = true
  ROUTE_DECISION                = PROMISING
  WALLET_FEATURE_SIDECAR_DIAGNOSTIC_ONLY = true

OUTPUTS
-------
  mvp/diagnostics/sprint038a_recipient_candidate_summary.csv
  mvp/diagnostics/sprint038a_recipient_exclusion_breakdown.json
  mvp/audits/sprint038a_recipient_lasl_fingerprint.md

USAGE
-----
  # Dry run — CH queries only, no RPC
  python scripts/sprint038a_recipient_lasl_fingerprint.py --dry-run

  # Full run — CH + bounded RPC validation
  LABEL_RPC_URL=https://... python scripts/sprint038a_recipient_lasl_fingerprint.py --run

ENVIRONMENT
-----------
  CLICKHOUSE_URL      (default: http://172.18.0.6:8123/)
  CLICKHOUSE_USER     (default: default)
  CLICKHOUSE_PASSWORD
  LABEL_RPC_URL or EVM_HTTP_URL  (only needed for --run)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── Frozen decision flags ─────────────────────────────────────────────────────
DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED":                        False,
    "LIVE_ALLOWED":                            False,
    "MODEL_PROMOTION_ALLOWED":                 False,
    "FAILED_NO_7D_ALPHA_ACTIVE":               True,
    "IDENTITY_CLAIM_ALLOWED":                  False,
    "SENDER_KEYED_RANKING_TRUSTED":            False,
    "RECIPIENT_KEYED_VIEW_REQUIRED":           True,
    "NO_ALPHA_CLAIM":                          True,
    "ROUTE_DECISION":                          "PROMISING",
    "WALLET_FEATURE_SIDECAR_DIAGNOSTIC_ONLY":  True,
    "SMART_MONEY_GENERAL_LINE_OPEN":           False,
}

# ── Chain constants (immutable) ───────────────────────────────────────────────
CHAIN_ID           = 8453
WETH               = "0x4200000000000000000000000000000000000006"
# LASZLO whitepaper v2 / 外宣版 §4.1 Quoter V2 address
QUOTER_V2_BASE     = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"

# Observation window and thresholds
WINDOW_DAYS        = 90
MIN_SWAPS          = 10   # hard floor for candidate qualification
NOTIONAL_MIN_ETH   = 0.0003   # ~$1 at $3500/ETH — lower bound of position
NOTIONAL_MAX_ETH   = 0.015    # ~$52 at $3500/ETH — upper bound ($25 base × max score)
SHORT_EXIT_SEC     = 1800     # 30 min — LASZLO time-stop threshold

# Evidence scoring weights (must sum to 1.0)
SCORE_WEIGHTS: dict[str, float] = {
    "s_v3_activity":    0.10,
    "s_fee_pattern":    0.10,
    "s_notional":       0.15,
    "s_short_exit":     0.20,
    "s_early_pool":     0.10,
    "s_round_trip":     0.15,
    "s_concentration":  0.10,
    "s_pnl_proxy":      0.10,
}
assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9, "weights must sum to 1"

# ── Known router / aggregator addresses (E1 exclusion list) ──────────────────
# Per sprint038a readout §4 exclusion class E1.
KNOWN_ROUTERS: frozenset[str] = frozenset({
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap Universal Router
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 SwapRouter02
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router v2
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1bebe2438d",  # Aerodrome Router
    "0xbe6d8da2d4f922f359b1da84fed3c60b269ce8a0",  # Aerodrome SlipstreamMixer
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 SwapRouter01
    "0x80c7dd17b01855a6d2347444a0fcc36136a314de",  # OKX DEX aggregator
    "0xc30141b657f4216252dc59af2e7cdb9d8792e1b0",  # Socket.tech
})

# Stablecoin addresses to exclude from meme-pool detection (E5)
_STABLES: frozenset[str] = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",  # USDT (Base)
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    WETH,  # WETH itself is never the meme token
})

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parents[1]
OUT_CSV     = REPO / "mvp/diagnostics/sprint038a_recipient_candidate_summary.csv"
OUT_JSON    = REPO / "mvp/diagnostics/sprint038a_recipient_exclusion_breakdown.json"
OUT_AUDIT   = REPO / "mvp/audits/sprint038a_recipient_lasl_fingerprint.md"
OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)


def _log(msg: str) -> None:
    print(f"[s038a {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers (adapted from sprint037b_r3_liquidity_gate.py) ─────────
def _ch_guard(sql: str) -> None:
    """Read-only guard: only SELECT / WITH allowed."""
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: unexpected statement: {sql[:60]!r}")
    for bad in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if bad in f" {s} ":
            raise ValueError(f"forbidden SQL fragment {bad!r}")


def _ch_query(
    url: str,
    sql: str,
    user: str,
    pwd: str,
    timeout: float = 600.0,
    settings: dict[str, Any] | None = None,
) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"

    # Memory-safe settings (from compute_wallet_scores.py pattern)
    mem_settings = {
        "max_bytes_before_external_group_by":  "5368709120",
        "max_bytes_before_external_sort":      "5368709120",
        "max_memory_usage":                    "6500000000",
        "query_plan_max_optimizations_to_apply": "0",
    }
    if settings:
        mem_settings.update(settings)

    params = "&".join(f"{k}={v}" for k, v in mem_settings.items())
    full_url = f"{url.rstrip('/')}/?{params}"
    req = Request(full_url, data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ── SQL helpers ───────────────────────────────────────────────────────────────
def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))


def _routers_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(KNOWN_ROUTERS))


def _meme_pools_cte() -> str:
    """
    WETH-leg Uniswap V3 pools at canonical fee tiers.
    Excludes stable-only pools (E5) and known excluded tokens.
    Mirrors sprint037b_r3_liquidity_gate._meme_pools_cte() for consistency.
    """
    return f"""meme_pools AS (
  SELECT
    lower(dp.pool_address)    AS pa,
    lower(dp.token0_address)  AS t0,
    lower(dp.token1_address)  AS t1,
    toUInt32(dp.fee_bps)      AS fee_bps,
    dp.has_weth_leg,
    multiIf(lower(dp.token0_address) = '{WETH}', 1, 0) AS weth_is_token0,
    dp.created_block          AS pool_created_block
  FROM eatlq.discovered_pools AS dp
  WHERE dp.chain_id = {CHAIN_ID}
    AND lower(dp.pool_type) = 'v3'
    AND dp.has_weth_leg = 1
    AND dp.fee_bps IN (100, 500, 3000, 10000)
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
)"""


# ── Query 1: Base recipient profile ──────────────────────────────────────────
def build_base_profile_query() -> str:
    """
    Per-recipient aggregate over WINDOW_DAYS days.
    Returns fee tier mix, buy/sell counts, notional band, pool/token diversity.
    Recipients that are known routers or self-routes are pre-excluded in SQL.

    Deliberately simple (no CTE self-join): concentration proxy is derived in
    Python from n_swaps + n_distinct_pools rather than needing max(pool_count).
    """
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)                                                     AS recipient_address_AUDIT_ONLY,
  count()                                                                AS n_swaps,
  uniqCombined(lower(s.pool_address))                                    AS n_distinct_pools,
  uniqCombined(if(mp.weth_is_token0 = 1, mp.t1, mp.t0))                 AS n_distinct_meme_tokens,
  countIf(mp.fee_bps IN (500, 3000, 10000))                              AS n_lasl_fee_swaps,
  countIf(mp.fee_bps = 3000)                                             AS n_fee_3000,
  countIf(mp.fee_bps = 10000)                                            AS n_fee_10000,
  countIf(mp.fee_bps = 500)                                              AS n_fee_500,
  countIf(mp.fee_bps = 100)                                              AS n_fee_100,
  -- BUY = WETH flows into pool (recipient gets meme token)
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                      AS n_buys,
  countIf(
    NOT (
      (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
      (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
    )
  )                                                                      AS n_sells,
  -- Notional band: BUYs where WETH size is in the LASZLO position range
  countIf(
    (
      (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
      (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
    )
    AND multiIf(
      mp.weth_is_token0 = 1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))) / 1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw))) / 1e18
    ) >= {NOTIONAL_MIN_ETH}
    AND multiIf(
      mp.weth_is_token0 = 1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))) / 1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw))) / 1e18
    ) <= {NOTIONAL_MAX_ETH}
  )                                                                      AS n_buys_notional_ok,
  toUnixTimestamp(min(coalesce(s.block_time, s.received_at_ms)))        AS first_activity_ts,
  toUnixTimestamp(max(coalesce(s.block_time, s.received_at_ms)))        AS last_activity_ts,
  -- sample BUY tx hashes for bounded RPC validation (F10)
  groupArray(5)(
    if(
      (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
      (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8'),
      lower(s.tx_hash),
      ''
    )
  )                                                                      AS sample_buy_txs
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
  AND lower(s.sender) != lower(s.recipient)
  AND lower(s.recipient) NOT IN ({_routers_sql()})
GROUP BY recipient_address_AUDIT_ONLY
HAVING n_swaps >= {MIN_SWAPS}
   AND n_buys  >= 3
ORDER BY n_swaps DESC
"""


# ── Query 2: Round-trip and hold time ────────────────────────────────────────
def build_round_trip_query() -> str:
    """
    Per-recipient round-trip count and hold time statistics.
    Round trip = wallet bought a token in a pool AND sold from same pool.
    Hold time = first_sell_ts - first_buy_ts per (recipient, pool).
    """
    return f"""
WITH {_meme_pools_cte()},
events AS (
  SELECT
    lower(s.recipient)   AS rec,
    mp.pa                AS pa,
    multiIf(
      (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8'), 'buy',
      (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8'), 'buy',
      'sell'
    ) AS side,
    toUnixTimestamp(coalesce(s.block_time, s.received_at_ms)) AS ts
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID}
    AND s.removed = 0
    AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
),
buy_min AS (
  SELECT rec, pa, min(ts) AS first_buy_ts
  FROM events WHERE side = 'buy'
  GROUP BY rec, pa
),
sell_min AS (
  SELECT rec, pa, min(ts) AS first_sell_ts
  FROM events WHERE side = 'sell'
  GROUP BY rec, pa
),
rt AS (
  SELECT
    b.rec,
    b.pa,
    greatest(toInt64(s.first_sell_ts) - toInt64(b.first_buy_ts), 0) AS hold_sec
  FROM buy_min b
  INNER JOIN sell_min s ON b.rec = s.rec AND b.pa = s.pa
  WHERE s.first_sell_ts > b.first_buy_ts
)
SELECT
  rec                                                    AS recipient_address_AUDIT_ONLY,
  count()                                                AS n_round_trips,
  countIf(hold_sec <= {SHORT_EXIT_SEC})                  AS n_short_exits,
  countIf(hold_sec > 0 AND hold_sec <= 60)               AS n_very_short_exits,
  avg(hold_sec)                                          AS avg_hold_sec,
  quantile(0.10)(hold_sec)                               AS p10_hold_sec,
  quantile(0.50)(hold_sec)                               AS p50_hold_sec,
  quantile(0.90)(hold_sec)                               AS p90_hold_sec
FROM rt
GROUP BY rec
"""


# ── Query 3: Early pool participation ────────────────────────────────────────
def build_early_pool_query() -> str:
    """
    Per-recipient: how many of their first buys were within 50 blocks
    of the pool's creation (Sniper archetype per whitepaper v2 §3.2).
    """
    return f"""
WITH {_meme_pools_cte()},
first_buy_per_pool AS (
  SELECT
    lower(s.recipient)  AS rec,
    mp.pa               AS pa,
    mp.pool_created_block AS pool_created_block,
    min(s.block_number) AS first_buy_block
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID}
    AND s.removed = 0
    AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
    -- only BUY events
    AND (
      (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8')
      OR
      (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
    )
  GROUP BY rec, pa, pool_created_block
)
SELECT
  rec                                                      AS recipient_address_AUDIT_ONLY,
  count()                                                  AS n_pools_entered,
  countIf(first_buy_block <= pool_created_block + 50)      AS n_early_entries,
  countIf(first_buy_block <= pool_created_block + 3)       AS n_sniper_entries,
  avg(toInt64(first_buy_block) - toInt64(pool_created_block)) AS avg_entry_block_offset
FROM first_buy_per_pool
GROUP BY rec
"""


# ── Scoring functions ─────────────────────────────────────────────────────────

def _safe_div(a: float, b: float, fallback: float = 0.0) -> float:
    return a / b if b > 0 else fallback


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def score_base(row: dict) -> dict[str, float]:
    """Compute 8 evidence sub-scores from CH query results."""
    n_swaps     = int(row.get("n_swaps", 0))
    n_buys      = int(row.get("n_buys", 0))
    n_sells     = int(row.get("n_sells", 0))
    n_lasl_fee  = int(row.get("n_lasl_fee_swaps", 0))
    n_buys_nb   = int(row.get("n_buys_notional_ok", 0))
    n_pools     = int(row.get("n_distinct_pools", 0))
    n_tokens    = int(row.get("n_distinct_meme_tokens", 0))

    # Round-trip data (may be absent if no round trips found)
    n_rt        = int(row.get("n_round_trips", 0))
    n_short     = int(row.get("n_short_exits", 0))
    avg_hold    = float(row.get("avg_hold_sec", 0.0) or 0.0)
    p50_hold    = float(row.get("p50_hold_sec", 0.0) or 0.0)

    # Early pool data
    n_pe        = int(row.get("n_pools_entered", 0))
    n_early     = int(row.get("n_early_entries", 0))

    # PnL proxy from recipient_pnl_summary_v1 (if available)
    win_rate    = float(row.get("win_rate", -1.0))

    # ── S1: V3 Activity ──────────────────────────────────────────────────────
    # min(n_swaps / 50, 1.0) — rewards consistent V3 activity
    # Penalised if >80% are pure sells (not a buyer)
    buy_ratio = _safe_div(n_buys, n_buys + n_sells, 0.5)
    s_v3_activity = _clamp(min(n_swaps / 50.0, 1.0) * (0.5 + 0.5 * buy_ratio))

    # ── S2: Fee pattern ──────────────────────────────────────────────────────
    # Share of swaps in {500, 3000, 10000}; penalise heavy 100-tier presence
    s_fee_pattern = _clamp(_safe_div(n_lasl_fee, n_swaps))

    # ── S3: Notional band ────────────────────────────────────────────────────
    # Share of BUYs with WETH notional in [NOTIONAL_MIN_ETH, NOTIONAL_MAX_ETH]
    s_notional = _clamp(_safe_div(n_buys_nb, max(n_buys, 1)))

    # ── S4: Short-horizon exits ──────────────────────────────────────────────
    # ≥60% of round trips exited within 30 min (LASZLO time-stop window)
    # Also require avg hold < 7d (not a diamond hand)
    if n_rt == 0:
        s_short_exit = 0.0
    else:
        short_ratio = _safe_div(n_short, n_rt)
        hold_ok     = 1.0 if (60 <= avg_hold <= 3600 * 8) else \
                      0.5 if (0 < avg_hold < 60 or 3600 * 8 < avg_hold < 86400 * 7) else 0.1
        s_short_exit = _clamp(short_ratio * hold_ok)

    # ── S5: Early pool participation ─────────────────────────────────────────
    if n_pe == 0:
        s_early_pool = 0.0
    else:
        s_early_pool = _clamp(_safe_div(n_early, n_pe))

    # ── S6: Round-trip behaviour ─────────────────────────────────────────────
    # n_round_trips / n_swaps ≥ 0.3 AND n_distinct_tokens ≥ 3
    rt_share = _safe_div(n_rt * 2, n_swaps)   # *2: each RT = 2 swaps
    diversity_bonus = min(n_tokens / 5.0, 1.0)
    s_round_trip = _clamp(rt_share * 0.6 + diversity_bonus * 0.4)

    # ── S7: Concentration ────────────────────────────────────────────────────
    # Proxy: 1/n_pools (if 1 pool → 100% concentrated; many pools → low)
    # and inverse token concentration
    venue_conc  = 1.0 / max(n_pools, 1)               # pools proxy (1 = fully concentrated)
    token_conc  = 1.0 / max(n_tokens, 1)              # token proxy
    conc_score  = (1.0 - min(venue_conc * 2, 1.0)) * 0.5 + \
                  (1.0 - min(token_conc * 3, 1.0)) * 0.5
    s_concentration = _clamp(conc_score)

    # ── S8: PnL proxy ────────────────────────────────────────────────────────
    # win_rate from recipient_pnl_summary_v1; target range [0.45, 0.85]
    # -1.0 means data unavailable → neutral 0.5
    if win_rate < 0:
        s_pnl_proxy = 0.5   # neutral when unavailable
    elif 0.45 <= win_rate <= 0.85:
        # Peak at 0.55–0.70 (realistic good trader, not suspiciously perfect)
        s_pnl_proxy = _clamp(1.0 - abs(win_rate - 0.625) / 0.225)
    else:
        s_pnl_proxy = _clamp(win_rate * 0.3)   # outside sweet spot

    scores = {
        "s_v3_activity":   round(s_v3_activity, 4),
        "s_fee_pattern":   round(s_fee_pattern, 4),
        "s_notional":      round(s_notional,    4),
        "s_short_exit":    round(s_short_exit,  4),
        "s_early_pool":    round(s_early_pool,  4),
        "s_round_trip":    round(s_round_trip,  4),
        "s_concentration": round(s_concentration, 4),
        "s_pnl_proxy":     round(s_pnl_proxy,   4),
    }
    evidence_score = sum(SCORE_WEIGHTS[k] * v for k, v in scores.items())
    scores["evidence_score"] = round(_clamp(evidence_score), 4)
    return scores


# ── Exclusion logic ───────────────────────────────────────────────────────────
def classify_exclusion(row: dict) -> str | None:
    """
    Returns exclusion reason string or None if the wallet should be scored.

    E1 — router / aggregator contract (pre-filtered in SQL, belt-and-suspenders)
    E2 — MEV / arb / sandwich bot
    E3 — deployer / pool owner / manipulation (very high pool concentration)
    E4 — single-recipient hardcoded bot
    E5 — (stable-stable filtered in SQL via meme_pools CTE)
    """
    addr    = (row.get("recipient_address_AUDIT_ONLY") or "").lower()
    n_swaps = int(row.get("n_swaps", 0))
    n_pools = int(row.get("n_distinct_pools", 0))
    n_tok   = int(row.get("n_distinct_meme_tokens", 0))
    n_rt    = int(row.get("n_round_trips", 0))
    n_sells = int(row.get("n_sells", 0))
    n_buys  = int(row.get("n_buys", 0))
    avg_hold = float(row.get("avg_hold_sec", 9999.0) or 9999.0)
    top_pool = int(row.get("top_pool_swap_count", 0))

    # E1 belt-and-suspenders (already filtered in SQL)
    if addr in KNOWN_ROUTERS:
        return "E1_router"

    # E4 — single-pool high-frequency bot
    if n_swaps >= 500 and n_pools <= 2 and n_tok <= 2:
        return "E4_single_pool_bot"

    # E2 — MEV / sandwich: very short avg hold AND very high round-trip rate
    # Hold < 12s on Base (≈ 6 blocks × 2s) with >85% round-trip rate
    if n_rt > 5 and n_swaps > 20:
        rt_rate = (n_rt * 2) / n_swaps
        if avg_hold > 0 and avg_hold < 12 and rt_rate > 0.85:
            return "E2_mev_sandwich"

    # E3 — pool manipulation: single pool dominates + many buys before any sells
    if n_pools >= 1 and top_pool / max(n_swaps, 1) > 0.90 and n_buys > 20 and n_sells < 2:
        return "E3_pool_manipulation"

    # E4 extended: concentrated with zero diversity
    if n_swaps >= 200 and n_pools == 1 and n_tok <= 1:
        return "E4_single_pool_bot"

    return None


# ── RPC helpers ───────────────────────────────────────────────────────────────
def _rpc_call(rpc_url: str, method: str, params: list, req_id: int = 1) -> Any:
    payload = json.dumps({
        "jsonrpc": "2.0", "method": method, "params": params, "id": req_id
    }).encode("utf-8")
    req = Request(rpc_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def _check_gas_signature_f10(rpc_url: str, tx_hash: str) -> dict:
    """
    F10: LASZLO-style transactions use maxPriorityFeePerGas = 1 wei.
    Returns dict with the gas fields and whether it matches the signature.
    """
    result = {"tx_hash": tx_hash, "f10_checked": False, "f10_match": None,
              "max_priority_fee_wei": None, "max_fee_wei": None}
    try:
        tx = _rpc_call(rpc_url, "eth_getTransactionByHash", [tx_hash])
        if not tx:
            return result
        mpf = tx.get("maxPriorityFeePerGas")
        mf  = tx.get("maxFeePerGas")
        if mpf is None:
            return result  # legacy tx, not EIP-1559
        mpf_wei = int(mpf, 16) if isinstance(mpf, str) else int(mpf)
        mf_wei  = int(mf, 16)  if isinstance(mf, str)  else int(mf)
        result.update({
            "f10_checked": True,
            "f10_match": mpf_wei <= 5,   # ≤ 5 wei priority fee (LASZLO = 1 wei)
            "max_priority_fee_wei": mpf_wei,
            "max_fee_wei": mf_wei,
        })
    except Exception as exc:  # noqa: BLE001
        result["f10_error"] = str(exc)[:80]
    return result


MAX_RPC_WALLETS = 200
MAX_RPC_SAMPLES = 5   # tx hashes per wallet


def run_rpc_validation(
    candidates: list[dict],
    rpc_url: str,
    workers: int = 10,
) -> dict[str, dict]:
    """
    Bounded RPC validation for top MAX_RPC_WALLETS candidates.
    Checks F10 (gas signature) on up to MAX_RPC_SAMPLES buy txs per wallet.
    Returns mapping: recipient_address → rpc_result dict.
    """
    top = candidates[:MAX_RPC_WALLETS]
    _log(f"RPC validation: {len(top)} wallets × up to {MAX_RPC_SAMPLES} samples")

    # Build (wallet, tx_hash) pairs
    pairs: list[tuple[str, str]] = []
    for row in top:
        addr = row["recipient_address_AUDIT_ONLY"]
        txs = [t for t in row.get("sample_buy_txs", []) if t and len(t) == 66][:MAX_RPC_SAMPLES]
        for tx in txs:
            pairs.append((addr, tx))

    rpc_results: dict[str, dict] = {}
    done = 0

    def _check(addr_tx: tuple[str, str]) -> tuple[str, dict]:
        addr, tx = addr_tx
        res = _check_gas_signature_f10(rpc_url, tx)
        return addr, res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check, p): p for p in pairs}
        for fut in as_completed(futures):
            addr, res = fut.result()
            existing = rpc_results.setdefault(addr, {
                "n_txs_checked": 0, "n_f10_match": 0, "n_f10_fail": 0,
                "n_rpc_error": 0, "f10_share": 0.0, "sample_results": []
            })
            existing["sample_results"].append(res)
            existing["n_txs_checked"] += 1
            if res.get("f10_checked"):
                if res["f10_match"]:
                    existing["n_f10_match"] += 1
                else:
                    existing["n_f10_fail"] += 1
            elif "f10_error" in res:
                existing["n_rpc_error"] += 1

            done += 1
            if done % 50 == 0:
                _log(f"  RPC: {done}/{len(pairs)} tx checks done")

    # Compute per-wallet F10 share
    for addr, r in rpc_results.items():
        n = r["n_txs_checked"] - r["n_rpc_error"]
        r["f10_share"] = round(r["n_f10_match"] / n, 3) if n > 0 else 0.0
        # Clean up sample_results to keep JSON small
        r["sample_results"] = r["sample_results"][:3]

    return rpc_results


# ── CSV / JSON / Markdown output ─────────────────────────────────────────────
CSV_COLUMNS = [
    "recipient_address_AUDIT_ONLY",
    "evidence_score",
    "s_v3_activity",
    "s_fee_pattern",
    "s_notional",
    "s_short_exit",
    "s_early_pool",
    "s_round_trip",
    "s_concentration",
    "s_pnl_proxy",
    "n_swaps",
    "n_distinct_pools",
    "n_distinct_meme_tokens",
    "n_buys",
    "n_sells",
    "n_round_trips",
    "n_short_exits",
    "avg_hold_sec",
    "p50_hold_sec",
    "n_fee_3000",
    "n_fee_10000",
    "n_fee_500",
    "n_fee_100",
    "n_lasl_fee_swaps",
    "n_buys_notional_ok",
    "n_early_entries",
    "n_sniper_entries",
    "win_rate",
    "f10_share",
    "exclusion_reason_AUDIT_ONLY",
    "schema_version",
    "sidecar_feature_source",
]

SCHEMA_VERSION = "sprint038a_v1"
SIDECAR_SOURCE = "ch_uniswap_v3_swaps_discovered_pools_90d"


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    _log(f"CSV written: {path} ({len(rows)} rows)")


def write_exclusion_json(
    total_candidates: int,
    scored_count: int,
    excluded_rows: list[dict],
    path: Path,
    elapsed_sec: float,
) -> None:
    breakdown: dict[str, int] = defaultdict(int)
    for r in excluded_rows:
        breakdown[r.get("exclusion_reason_AUDIT_ONLY") or "unknown"] += 1

    payload = {
        "kind": "sprint038a_recipient_exclusion_breakdown",
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decision_flags": DECISION_FLAGS,
        "total_candidates_pre_exclusion": total_candidates,
        "scored_candidates": scored_count,
        "excluded_count": len(excluded_rows),
        "elapsed_sec": round(elapsed_sec, 1),
        "window_days": WINDOW_DAYS,
        "min_swaps": MIN_SWAPS,
        "exclusion_breakdown": dict(breakdown),
        "exclusion_classes": {
            "E1_router":           "Router / aggregator contract (known list or self-route)",
            "E2_mev_sandwich":     "MEV / arbitrage / sandwich bot (sub-12s avg hold, >85% RT rate)",
            "E3_pool_manipulation":"Deployer / pool manipulation (>90% single pool, no sells)",
            "E4_single_pool_bot":  "Single-recipient hardcoded bot (≥500 swaps, ≤2 pools/tokens)",
            "E5_stable_stable":    "Stable-stable pool (filtered in SQL via meme_pools CTE)",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log(f"Exclusion JSON written: {path}")


def write_audit_md(
    candidates: list[dict],
    excluded_rows: list[dict],
    rpc_results: dict,
    path: Path,
    dry_run: bool,
) -> None:
    n_total   = len(candidates) + len(excluded_rows)
    n_scored  = len(candidates)
    n_excl    = len(excluded_rows)
    score_counts = {
        "≥0.6": sum(1 for r in candidates if float(r.get("evidence_score", 0)) >= 0.6),
        "≥0.5": sum(1 for r in candidates if float(r.get("evidence_score", 0)) >= 0.5),
        "≥0.4": sum(1 for r in candidates if float(r.get("evidence_score", 0)) >= 0.4),
    }
    top5 = sorted(candidates, key=lambda r: -float(r.get("evidence_score", 0)))[:5]

    lines = [
        "# Sprint038A — Recipient-keyed LASZLO-style Fingerprint",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}  ",
        f"**Mode:** {'DRY-RUN (no RPC validation)' if dry_run else 'FULL RUN'}  ",
        f"**Schema version:** `{SCHEMA_VERSION}`",
        "",
        "## Decision Flags",
        "",
        "```",
    ]
    for k, v in DECISION_FLAGS.items():
        lines.append(f"{k:45s} = {v}")
    lines += [
        "```",
        "",
        "## Summary",
        "",
        f"- **Observation window:** {WINDOW_DAYS} days (Base L2, chain_id=8453)",
        f"- **Pool filter:** Uniswap V3, WETH-leg, fee_bps ∈ {{100, 500, 3000, 10000}}",
        f"- **Min swaps threshold:** {MIN_SWAPS}",
        f"- **Total candidates (pre-exclusion):** {n_total}",
        f"- **Excluded:** {n_excl}",
        f"- **Scored:** {n_scored}",
        f"- **evidence_score ≥ 0.6:** {score_counts['≥0.6']}",
        f"- **evidence_score ≥ 0.5:** {score_counts['≥0.5']}",
        f"- **evidence_score ≥ 0.4:** {score_counts['≥0.4']}",
        "",
        "## Author-style Fingerprint Basis",
        "",
        "The evidence_score measures alignment with the LASZLO v2 execution profile:",
        "",
        "| Sub-score | Weight | Measures |",
        "|-----------|--------|----------|",
        "| s_v3_activity | 0.10 | Base L2 V3 WETH-leg swap frequency |",
        "| s_fee_pattern | 0.10 | Fee tier ∈ {500, 3000, 10000} share |",
        "| s_notional | 0.15 | BUY WETH size ∈ [0.0003, 0.015] ETH |",
        "| s_short_exit | 0.20 | Round-trip exits ≤ 30 min |",
        "| s_early_pool | 0.10 | First buy within 50 blocks of pool creation |",
        "| s_round_trip | 0.15 | Round-trip rate × token diversity |",
        "| s_concentration | 0.10 | Low venue + token concentration |",
        "| s_pnl_proxy | 0.10 | Win-rate proxy ∈ [0.45, 0.85] |",
        "",
        "**What this does NOT prove:** identity of any specific person,",
        "profitability, or alpha. IDENTITY_CLAIM_ALLOWED = false.",
        "",
        "## Top 5 Candidates (highest evidence_score)",
        "",
        "*Note: recipient_address_AUDIT_ONLY — these are anonymous EOAs.*",
        "",
        "| # | evidence_score | n_swaps | n_pools | n_rt | p50_hold_s | f10_share |",
        "|---|----------------|---------|---------|------|-----------|-----------|",
    ]
    for i, row in enumerate(top5, 1):
        addr  = row.get("recipient_address_AUDIT_ONLY", "?")
        score = row.get("evidence_score", 0)
        ns    = row.get("n_swaps", 0)
        np_   = row.get("n_distinct_pools", 0)
        nrt   = row.get("n_round_trips", 0)
        p50   = row.get("p50_hold_sec", 0)
        f10   = row.get("f10_share", "N/A")
        # Partial address for audit trail without full PII
        addr_short = f"{addr[:8]}…{addr[-4:]}" if len(addr) >= 12 else addr
        lines.append(f"| {i} | {score:.4f} | {ns} | {np_} | {nrt} | {p50:.0f} | {f10} |")

    lines += [
        "",
        "## Exclusion Breakdown",
        "",
        "| Class | Count | Description |",
        "|-------|-------|-------------|",
        "| E1_router | — | Router/aggregator (filtered in SQL) |",
        "| E2_mev_sandwich | — | MEV/arb: avg hold < 12s, >85% RT rate |",
        "| E3_pool_manipulation | — | >90% single pool, no sell activity |",
        "| E4_single_pool_bot | — | ≥500 swaps on ≤2 pools/tokens |",
        "| E5_stable_stable | — | Filtered at SQL meme_pools CTE level |",
    ]
    if rpc_results:
        n_f10 = sum(1 for r in rpc_results.values() if r.get("f10_share", 0) > 0.5)
        lines += [
            "",
            "## RPC Validation (F10 Gas Signature)",
            "",
            f"- Wallets sampled: {len(rpc_results)}",
            f"- Wallets with F10 share > 0.5 (majority low-priority-fee): {n_f10}",
            "",
            "F10 = maxPriorityFeePerGas ≤ 5 wei (LASZLO default: 1 wei).",
            "High F10 share indicates LASZLO-compatible gas behaviour but is NOT",
            "sufficient alone to identify any specific system.",
        ]
    else:
        lines += [
            "",
            "## RPC Validation",
            "",
            "Skipped (--dry-run mode). Run with `--run` and `LABEL_RPC_URL` to enable.",
        ]

    lines += [
        "",
        "## Guard Statements",
        "",
        "1. This sidecar is **DIAGNOSTIC ONLY**. evidence_score is NOT alpha.",
        "2. recipient_address_AUDIT_ONLY columns must NOT enter `retrain_model.py`",
        "   FEATURE_COLUMNS or any live signal path.",
        "3. No candidate wallet may be added to `wallet:smart_money` or",
        "   `wallet:sniper` Redis tiers based solely on this sidecar.",
        "4. FAILED_NO_7D_ALPHA_ACTIVE = true — not superseded by this readout.",
        "5. IDENTITY_CLAIM_ALLOWED = false — evidence_score does not prove",
        "   any candidate is or is not the author of the LASZLO system.",
        "",
        f"*Sprint038A — {SCHEMA_VERSION}*",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    _log(f"Audit markdown written: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint038A recipient LASZLO fingerprint")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="CH queries only, no RPC calls")
    mode.add_argument("--run", action="store_true",
                      help="Full run: CH queries + bounded RPC validation")
    args = parser.parse_args()
    dry_run = args.dry_run

    ch_url  = os.environ.get("CLICKHOUSE_URL",      "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER",     "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")

    if not dry_run and not rpc_url:
        print("ERROR: --run requires LABEL_RPC_URL or EVM_HTTP_URL env var", file=sys.stderr)
        return 1

    t0 = time.monotonic()

    # ── PREFLIGHT ────────────────────────────────────────────────────────────
    _log("=== Sprint038A preflight ===")
    _log(f"Window: {WINDOW_DAYS} days | Chain: {CHAIN_ID} | min_swaps: {MIN_SWAPS}")
    _log(f"Mode: {'DRY-RUN' if dry_run else 'FULL RUN'}")
    if not dry_run:
        _log(f"RPC: {rpc_url[:50]}...")
        _log(f"RPC budget: {MAX_RPC_WALLETS} wallets × {MAX_RPC_SAMPLES} samples = "
             f"{MAX_RPC_WALLETS * MAX_RPC_SAMPLES} max calls")

    # Quick preflight count
    pf_sql = f"""
WITH {_meme_pools_cte()}
SELECT
  uniqCombined(lower(s.recipient)) AS est_recipients,
  count() AS est_swaps
FROM eatlq.uniswap_v3_swaps s
INNER JOIN meme_pools mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID} AND s.removed = 0
  AND lower(s.sender) != lower(s.recipient)
  AND lower(s.recipient) NOT IN ({_routers_sql()})
  AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
"""
    pf = _ch_query(ch_url, pf_sql, ch_user, ch_pwd, timeout=60)
    if pf:
        _log(f"Preflight: {pf[0]['est_swaps']} swaps, "
             f"{pf[0]['est_recipients']} distinct recipients (30d, WETH-leg filter)")
    # Abort if scan looks too expensive (safety valve)
    est_swaps = int((pf[0]["est_swaps"] if pf else 0))
    if est_swaps > 5_000_000:
        _log("ABORT: estimated swap scan > 5M rows — reduce WINDOW_DAYS or add more filters")
        return 2

    # ── QUERY 1: Base profile ────────────────────────────────────────────────
    _log("Running query 1 (base profile)…")
    base_rows = _ch_query(ch_url, build_base_profile_query(), ch_user, ch_pwd, timeout=300)
    _log(f"Base profile: {len(base_rows)} candidates with ≥{MIN_SWAPS} swaps")

    if not base_rows:
        _log("No candidates found — check WINDOW_DAYS / MIN_SWAPS / pool coverage")
        return 3

    # ── QUERY 2: Round trips ─────────────────────────────────────────────────
    _log("Running query 2 (round trips + hold times)…")
    rt_rows = _ch_query(ch_url, build_round_trip_query(), ch_user, ch_pwd, timeout=300)
    rt_map: dict[str, dict] = {r["recipient_address_AUDIT_ONLY"]: r for r in rt_rows}
    _log(f"Round trips: {len(rt_rows)} wallets with ≥1 round trip")

    # ── QUERY 3: Early pool participation ────────────────────────────────────
    _log("Running query 3 (early pool participation)…")
    early_rows = _ch_query(ch_url, build_early_pool_query(), ch_user, ch_pwd, timeout=300)
    early_map: dict[str, dict] = {r["recipient_address_AUDIT_ONLY"]: r for r in early_rows}
    _log(f"Early pool: {len(early_rows)} wallets with pool entry data")

    # ── Merge rows ───────────────────────────────────────────────────────────
    merged: list[dict] = []
    for row in base_rows:
        addr = row["recipient_address_AUDIT_ONLY"]
        merged_row = dict(row)
        rt = rt_map.get(addr, {})
        ep = early_map.get(addr, {})
        merged_row.update({
            "n_round_trips":     int(rt.get("n_round_trips", 0)),
            "n_short_exits":     int(rt.get("n_short_exits", 0)),
            "avg_hold_sec":      float(rt.get("avg_hold_sec", 0.0) or 0.0),
            "p10_hold_sec":      float(rt.get("p10_hold_sec", 0.0) or 0.0),
            "p50_hold_sec":      float(rt.get("p50_hold_sec", 0.0) or 0.0),
            "p90_hold_sec":      float(rt.get("p90_hold_sec", 0.0) or 0.0),
            "n_pools_entered":   int(ep.get("n_pools_entered", 0)),
            "n_early_entries":   int(ep.get("n_early_entries", 0)),
            "n_sniper_entries":  int(ep.get("n_sniper_entries", 0)),
            "win_rate":          -1.0,   # pnl_proxy: not yet computed from views
            "f10_share":         "N/A",
            "schema_version":    SCHEMA_VERSION,
            "sidecar_feature_source": SIDECAR_SOURCE,
        })
        merged.append(merged_row)

    # ── Exclusion pass ───────────────────────────────────────────────────────
    _log("Applying exclusion rules (E1–E5)…")
    scored_rows: list[dict] = []
    excluded_rows: list[dict] = []
    excl_counts: dict[str, int] = defaultdict(int)

    for row in merged:
        reason = classify_exclusion(row)
        if reason:
            row["exclusion_reason_AUDIT_ONLY"] = reason
            row["evidence_score"] = 0.0
            excluded_rows.append(row)
            excl_counts[reason] += 1
        else:
            row["exclusion_reason_AUDIT_ONLY"] = ""
            scored_rows.append(row)

    _log(f"Exclusions: {len(excluded_rows)} | Scoring: {len(scored_rows)}")
    for cls, cnt in sorted(excl_counts.items()):
        _log(f"  {cls}: {cnt}")

    # ── Score candidates ─────────────────────────────────────────────────────
    _log("Computing evidence scores…")
    for row in scored_rows:
        scores = score_base(row)
        row.update(scores)

    # Sort by evidence_score descending
    scored_rows.sort(key=lambda r: -float(r.get("evidence_score", 0)))

    if scored_rows:
        top_score = scored_rows[0]["evidence_score"]
        med_score = scored_rows[len(scored_rows) // 2]["evidence_score"]
        _log(f"Score range: max={top_score:.4f} median={med_score:.4f}")

    # ── RPC validation ───────────────────────────────────────────────────────
    rpc_results: dict[str, dict] = {}
    if not dry_run and rpc_url and scored_rows:
        _log("Running bounded RPC validation (F10 gas signature)…")
        rpc_results = run_rpc_validation(scored_rows, rpc_url)
        # Attach F10 share back to rows
        for row in scored_rows:
            addr = row["recipient_address_AUDIT_ONLY"]
            row["f10_share"] = rpc_results.get(addr, {}).get("f10_share", "N/A")
        _log(f"RPC validation complete: {len(rpc_results)} wallets sampled")

    # ── Outputs ──────────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t0

    all_output_rows = scored_rows + excluded_rows
    # Ensure all CSV fields are present
    for row in all_output_rows:
        for col in CSV_COLUMNS:
            row.setdefault(col, "" if col.endswith("_AUDIT_ONLY") or col in ("f10_share",)
                           else 0)

    write_csv(scored_rows, OUT_CSV)
    write_exclusion_json(len(merged), len(scored_rows), excluded_rows,
                         OUT_JSON, elapsed)
    write_audit_md(scored_rows, excluded_rows, rpc_results, OUT_AUDIT, dry_run)

    _log(f"=== Sprint038A complete in {elapsed:.1f}s ===")
    _log(f"Outputs:")
    _log(f"  {OUT_CSV}")
    _log(f"  {OUT_JSON}")
    _log(f"  {OUT_AUDIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
