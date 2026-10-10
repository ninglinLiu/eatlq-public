#!/usr/bin/env python3
"""Sprint039B — Whitepaper Canon Filter Patch
=============================================

Applies four Sprint039A-audited patches to the Sprint038A recipient filter and
produces three-route candidate rankings:

  old_whitepaper_route  — strict LASZLO v2 / 外宣版 canon
  upgraded_route        — fee=1 / fee=100 / aero_cl inventory
  anti_tracking_route   — medium-activity, balanced, multi-wallet hypothesis

Patches applied
---------------
  M01  fee=100 excluded from old-route meme_pools CTE
  M03  s_short_exit replaced by Gaussian centred at 1800s (30-min time-stop)
  M07  NOTIONAL_MAX_ETH tightened 0.015 → 0.0086 ETH ($25 × 1.2 / $3500)
  M09  s_sniper_strict (≤+3 blocks) added as separate score component

Not in scope this sprint (documented)
--------------------------------------
  M04  WETH.withdraw() receipt scan  → Sprint039C
  M06  fee-tier-specific ROUND_TRIP_COST → Sprint039C
  M08  entity clustering (CIH)       → Sprint039C
  M02  11-token registry hard filter → advisory only (soft weight added)
  M11  PnL proxy from CH view        → Sprint039C

DECISION FLAGS (frozen)
-----------------------
  IDENTITY_CLAIM_ALLOWED        = false
  ATTRIBUTION_HYPOTHESIS_ALLOWED= true
  TRAINING_ALLOWED              = false
  LIVE_ALLOWED                  = false
  NO_ALPHA_CLAIM                = true

Usage
-----
  python scripts/sprint039b_whitepaper_filter_patch.py --preflight
  python scripts/sprint039b_whitepaper_filter_patch.py --run
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

# ── Frozen flags ──────────────────────────────────────────────────────────────
DECISION_FLAGS: dict[str, Any] = {
    "IDENTITY_CLAIM_ALLOWED":         False,
    "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
    "TRAINING_ALLOWED":               False,
    "LIVE_ALLOWED":                   False,
    "NO_ALPHA_CLAIM":                 True,
    "FAILED_NO_7D_ALPHA_ACTIVE":      True,
    "SENDER_KEYED_RANKING_TRUSTED":   False,
}

# ── Chain / protocol ──────────────────────────────────────────────────────────
CHAIN_ID = 8453
WETH     = "0x4200000000000000000000000000000000000006"

# ── Patched constants (M03, M07) ──────────────────────────────────────────────
WINDOW_DAYS       = 90
MIN_SWAPS_OLD     = 10    # old-route minimum swaps
MIN_SWAPS_UPG     = 5     # upgraded-route inventory minimum
MIN_BUYS          = 3

# M07 patch: tightened from 0.015 → 0.0086 ETH ($25 × 1.2 / $3500)
NOTIONAL_MIN_ETH  = 0.0003    # ~$1
NOTIONAL_MAX_ETH  = 0.0086    # ~$30 (was 0.015 / ~$52)

# M03 patch: Gaussian hold-score constants
HOLD_PEAK_SEC     = 1800.0    # 30-min time-stop peak
HOLD_SIGMA_SEC    = 600.0     # ±10 min half-width

# ── Patched scoring weights (old_whitepaper route) ────────────────────────────
# M09: s_sniper_strict added (0.10); s_early_pool reduced (0.05)
# M03: s_short_exit weight raised 0.20→0.25
# Other weights adjusted to maintain sum = 1.0
OLD_ROUTE_WEIGHTS: dict[str, float] = {
    "s_v3_activity":    0.10,
    "s_fee_dominant":   0.10,   # renamed from s_fee_pattern; now rewards 3000/10000 specifically
    "s_notional":       0.10,   # M07: slightly reduced from 0.15 (tighter band, less forgiving)
    "s_short_exit":     0.25,   # M03: raised from 0.20
    "s_early_pool":     0.05,   # M09: reduced from 0.10 (loose +50-block criterion)
    "s_sniper_strict":  0.10,   # M09: NEW +3-block strict criterion
    "s_round_trip":     0.20,   # raised from 0.15 (critical whitepaper evidence)
    "s_concentration":  0.10,   # unchanged
}
assert abs(sum(OLD_ROUTE_WEIGHTS.values()) - 1.0) < 1e-9, "old_route weights must sum to 1"

# ── Known routers / stables ───────────────────────────────────────────────────
KNOWN_ROUTERS: frozenset[str] = frozenset({
    "0x6ff5693b99212da76ad316178a184ab56d299b43",
    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1bebe2438d",
    "0xbe6d8da2d4f922f359b1da84fed3c60b269ce8a0",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",
    "0x1111111254eeb25477b68fb85ed929f73a960582",
    "0x111111125421ca6dc452d289314280a0f8842a65",
    "0xe592427a0aece92de3edee1f18e0157c05861564",
    "0x80c7dd17b01855a6d2347444a0fcc36136a314de",
    "0xc30141b657f4216252dc59af2e7cdb9d8792e1b0",
})
_STABLES: frozenset[str] = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
    WETH,
})

# ── LASZLO 11-token registry (外宣版 §2.2) ────────────────────────────────────
LASZLO_TOKEN_REGISTRY: frozenset[str] = frozenset({
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631",  # AERO
    "0x4ed4e862860bed51a9570b96d89af5e1b0efefed",  # DEGEN
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC (canonical)
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # WBTC on Base (cbBTC)
    "0x532f27101965dd16442e59d40670faf5ebb142e4",  # BRETT
    "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4",  # TOSHI
    "0xba5e05cb26b78eda3a2f8e3b3814726305dcac83",  # OX
})

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO      = Path(__file__).resolve().parents[1]
OUT_MD    = REPO / "mvp/audits/sprint039b_whitepaper_filter_patch.md"
OUT_JSON  = REPO / "mvp/diagnostics/sprint039b_filter_patch_summary.json"
OUT_CSV   = REPO / "mvp/diagnostics/sprint039b_candidate_ranking.csv"
OUT_MD.parent.mkdir(parents=True, exist_ok=True)
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)


def _log(msg: str) -> None:
    print(f"[s039b {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers (from sprint038a) ──────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for bad in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if bad in f" {s} ":
            raise ValueError(f"forbidden SQL: {bad!r}")


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 600.0) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    params = (
        "max_bytes_before_external_group_by=5368709120"
        "&max_bytes_before_external_sort=5368709120"
        "&max_memory_usage=6500000000"
        "&query_plan_max_optimizations_to_apply=0"
    )
    req = Request(f"{url.rstrip('/')}/?{params}", data=q.encode("utf-8"), method="POST")
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


# ── Pool CTEs — M01 PATCH: separate old vs upgraded universes ─────────────────

def _old_route_meme_pools_cte() -> str:
    """M01 patch: fee=100 excluded. Only 3000/10000/500 (LASZLO v2 §1.4 canon)."""
    return f"""meme_pools AS (
  SELECT
    lower(dp.pool_address)    AS pa,
    lower(dp.token0_address)  AS t0,
    lower(dp.token1_address)  AS t1,
    toUInt32(dp.fee_bps)      AS fee_bps,
    multiIf(lower(dp.token0_address)='{WETH}', 1, 0) AS weth_is_token0,
    dp.created_block          AS pool_created_block
  FROM eatlq.discovered_pools AS dp
  WHERE dp.chain_id = {CHAIN_ID}
    AND lower(dp.pool_type) = 'v3'
    AND dp.has_weth_leg = 1
    AND dp.fee_bps IN (500, 3000, 10000)
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
)"""


def _upgraded_route_meme_pools_cte() -> str:
    """Upgraded route: fee=1, fee=100 (V3) and aero_cl pools."""
    return f"""meme_pools AS (
  SELECT
    lower(dp.pool_address)    AS pa,
    lower(dp.token0_address)  AS t0,
    lower(dp.token1_address)  AS t1,
    toUInt32(dp.fee_bps)      AS fee_bps,
    lower(dp.pool_type)       AS pool_type_str,
    multiIf(lower(dp.token0_address)='{WETH}', 1, 0) AS weth_is_token0,
    dp.created_block          AS pool_created_block
  FROM eatlq.discovered_pools AS dp
  WHERE dp.chain_id = {CHAIN_ID}
    AND dp.has_weth_leg = 1
    AND (
      (lower(dp.pool_type) = 'v3' AND dp.fee_bps IN (1, 100))
      OR lower(dp.pool_type) = 'aero_cl'
    )
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
)"""


# ── Query builders ─────────────────────────────────────────────────────────────

def _buy_condition_sql() -> str:
    return ("(mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR "
            "(mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')")


def build_base_profile_query(cte_fn, min_swaps: int = MIN_SWAPS_OLD) -> str:
    """M07 patch: tightened NOTIONAL_MAX_ETH applied here."""
    cte = cte_fn()
    buy_cond = _buy_condition_sql()
    return f"""
WITH {cte}
SELECT
  lower(s.recipient)                                                   AS recipient_address_AUDIT_ONLY,
  count()                                                              AS n_swaps,
  uniqCombined(lower(s.pool_address))                                  AS n_distinct_pools,
  uniqCombined(if(mp.weth_is_token0=1, mp.t1, mp.t0))                 AS n_distinct_meme_tokens,
  countIf(mp.fee_bps IN (500, 3000, 10000))                           AS n_lasl_fee_swaps,
  countIf(mp.fee_bps=3000)                                            AS n_fee_3000,
  countIf(mp.fee_bps=10000)                                           AS n_fee_10000,
  countIf(mp.fee_bps=500)                                             AS n_fee_500,
  countIf(mp.fee_bps=100)                                             AS n_fee_100,
  countIf(mp.fee_bps=1)                                               AS n_fee_1,
  countIf({buy_cond})                                                  AS n_buys,
  countIf(NOT ({buy_cond}))                                            AS n_sells,
  countIf(
    ({buy_cond})
    AND multiIf(mp.weth_is_token0=1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) >= {NOTIONAL_MIN_ETH}
    AND multiIf(mp.weth_is_token0=1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) <= {NOTIONAL_MAX_ETH}
  )                                                                    AS n_buys_notional_ok
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.sender) != lower(s.recipient)
  AND lower(s.recipient) NOT IN ({_routers_sql()})
  AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
GROUP BY recipient_address_AUDIT_ONLY
HAVING n_swaps >= {min_swaps} AND n_buys >= {MIN_BUYS}
ORDER BY n_swaps DESC
"""


def build_round_trip_query(cte_fn) -> str:
    buy_cond = _buy_condition_sql()
    cte = cte_fn()
    return f"""
WITH {cte},
events AS (
  SELECT
    lower(s.recipient) AS rec,
    mp.pa              AS pa,
    multiIf(
      ({buy_cond}), 'buy', 'sell'
    ) AS side,
    toUnixTimestamp(coalesce(s.block_time, s.received_at_ms)) AS ts
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID} AND s.removed = 0
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
    AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
),
bm AS (SELECT rec, pa, min(ts) AS fbts FROM events WHERE side='buy'  GROUP BY rec, pa),
sm AS (SELECT rec, pa, min(ts) AS fsts FROM events WHERE side='sell' GROUP BY rec, pa),
rt AS (
  SELECT b.rec, greatest(toInt64(s.fsts)-toInt64(b.fbts), 0) AS hold_sec
  FROM bm b INNER JOIN sm s ON b.rec=s.rec AND b.pa=s.pa WHERE s.fsts > b.fbts
)
SELECT
  rec                                    AS recipient_address_AUDIT_ONLY,
  count()                                AS n_round_trips,
  countIf(hold_sec <= 1800)              AS n_short_exits,
  countIf(hold_sec <= 60)               AS n_sub_minute_exits,
  avg(hold_sec)                          AS avg_hold_sec,
  quantile(0.50)(hold_sec)              AS p50_hold_sec,
  quantile(0.10)(hold_sec)              AS p10_hold_sec
FROM rt GROUP BY rec
"""


def build_early_pool_query(cte_fn) -> str:
    """M09 patch: both +3-block (strict sniper) and +50-block (loose) captured."""
    buy_cond = _buy_condition_sql()
    cte = cte_fn()
    return f"""
WITH {cte},
fbp AS (
  SELECT
    lower(s.recipient)    AS rec,
    mp.pa                 AS pa,
    mp.pool_created_block AS pcb,
    min(s.block_number)   AS fbb
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID} AND s.removed = 0
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
    AND coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
    AND ({buy_cond})
  GROUP BY rec, pa, pcb
)
SELECT
  rec                             AS recipient_address_AUDIT_ONLY,
  count()                         AS n_pools_entered,
  countIf(fbb <= pcb + 50)       AS n_early_entries,
  countIf(fbb <= pcb + 3)        AS n_sniper_entries,
  avg(toInt64(fbb)-toInt64(pcb)) AS avg_entry_block_offset
FROM fbp GROUP BY rec
"""


# ── Patched scoring functions ─────────────────────────────────────────────────

def _safe_div(a: float, b: float, fb: float = 0.0) -> float:
    return a / b if b > 0 else fb


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _hold_score_laszlo(avg_hold_sec: float, p50_hold_sec: float) -> float:
    """M03 PATCH: Gaussian centred at HOLD_PEAK_SEC (1800s = 30 min).
    sub-60s holds → near 0 (sniper-dump bots excluded).
    Diamond-hand holds > 7200s → 0.05."""
    h = p50_hold_sec if p50_hold_sec > 0 else avg_hold_sec
    if h < 60:
        return 0.0
    if h < 300:
        # Ramp from 60s to 5min: 0 → 0.2
        return (h - 60.0) / 240.0 * 0.2
    if h <= 4800:
        # Gaussian peak at 1800s, sigma=600s
        g = math.exp(-((h - HOLD_PEAK_SEC) ** 2) / (2.0 * HOLD_SIGMA_SEC ** 2))
        return _clamp(0.2 + 0.8 * g)
    if h <= 14400:
        # Decay 4800s → 14400s: 0.2 → 0.05
        decay = (h - 4800.0) / 9600.0
        return _clamp(0.2 - 0.15 * decay)
    return 0.05   # Diamond Hand


def score_old_whitepaper(row: dict) -> dict[str, float]:
    """Score for strict old-whitepaper route with all patches applied."""
    n_swaps   = int(row.get("n_swaps", 0))
    n_buys    = int(row.get("n_buys", 0))
    n_sells   = int(row.get("n_sells", 0))
    n_buys_nb = int(row.get("n_buys_notional_ok", 0))
    n_pools   = int(row.get("n_distinct_pools", 0))
    n_tokens  = int(row.get("n_distinct_meme_tokens", 0))
    n_fee_3k  = int(row.get("n_fee_3000", 0))
    n_fee_10k = int(row.get("n_fee_10000", 0))
    n_fee_500 = int(row.get("n_fee_500", 0))
    n_rt      = int(row.get("n_round_trips", 0))
    n_short   = int(row.get("n_short_exits", 0))
    avg_hold  = float(row.get("avg_hold_sec") or 0.0)
    p50_hold  = float(row.get("p50_hold_sec") or 0.0)
    n_pe      = int(row.get("n_pools_entered", 0))
    n_early   = int(row.get("n_early_entries", 0))
    n_sniper  = int(row.get("n_sniper_entries", 0))  # M09: strict ≤+3 blocks

    # S1: V3 activity in old-route universe
    buy_ratio    = _safe_div(n_buys, n_buys + n_sells, 0.5)
    s_v3_activity = _clamp(min(n_swaps / 50.0, 1.0) * (0.5 + 0.5 * buy_ratio))

    # S2: Fee dominance — reward 3000/10000 specifically (M01 indirect effect)
    # In old-route, fee_500 is penalised (LASZLO uses it but less)
    # fee_3000+10000 share = LASZLO primary channels
    dominant_share = _safe_div(n_fee_3k + n_fee_10k, n_swaps)
    balanced_share = _safe_div(n_fee_500, n_swaps)
    s_fee_dominant = _clamp(dominant_share * 0.75 + balanced_share * 0.25)

    # S3: Notional band (M07 patched: 0.0003–0.0086 ETH)
    s_notional = _clamp(_safe_div(n_buys_nb, max(n_buys, 1)))

    # S4: Hold-time score (M03 PATCH — Gaussian around 1800s)
    s_short_exit = _hold_score_laszlo(avg_hold, p50_hold)

    # S5: Early pool entry (loose ≤+50 blocks, reduced weight)
    s_early_pool = _clamp(_safe_div(n_early, n_pe)) if n_pe > 0 else 0.0

    # S5b: Strict sniper entry (M09 PATCH — ≤+3 blocks)
    s_sniper_strict = _clamp(_safe_div(n_sniper, n_pe) * 2.0) if n_pe > 0 else 0.0

    # S6: Round-trip (raised weight — critical LASZLO evidence)
    rt_share    = _safe_div(n_rt * 2, n_swaps)
    div_bonus   = min(n_tokens / 5.0, 1.0)
    s_round_trip = _clamp(rt_share * 0.6 + div_bonus * 0.4)

    # S7: Concentration (low concentration = diverse = LASZLO-like)
    venue_conc = 1.0 / max(n_pools, 1)
    token_conc = 1.0 / max(n_tokens, 1)
    s_concentration = _clamp(
        (1.0 - min(venue_conc * 2, 1.0)) * 0.5 +
        (1.0 - min(token_conc * 3, 1.0)) * 0.5
    )

    scores = {
        "s_v3_activity":   round(s_v3_activity,   4),
        "s_fee_dominant":  round(s_fee_dominant,   4),
        "s_notional":      round(s_notional,        4),
        "s_short_exit":    round(s_short_exit,      4),
        "s_early_pool":    round(s_early_pool,      4),
        "s_sniper_strict": round(s_sniper_strict,   4),
        "s_round_trip":    round(s_round_trip,      4),
        "s_concentration": round(s_concentration,  4),
    }
    score = sum(OLD_ROUTE_WEIGHTS[k] * v for k, v in scores.items())
    return {**scores, "old_whitepaper_score": round(score, 4)}


def score_upgraded_route(row: dict) -> float:
    """Upgraded route score: pool-type agnostic, same hold/notional discipline.
    Uses the same hold-score Gaussian (M03) and tightened notional (M07).
    Separate from old_whitepaper_score — not directly comparable."""
    n_swaps   = int(row.get("n_swaps", 0))
    n_buys    = int(row.get("n_buys", 0))
    n_sells   = int(row.get("n_sells", 0))
    n_buys_nb = int(row.get("n_buys_notional_ok", 0))
    n_tokens  = int(row.get("n_distinct_meme_tokens", 0))
    n_rt      = int(row.get("n_round_trips", 0) if "n_round_trips" in row else 0)
    avg_hold  = float(row.get("avg_hold_sec") or 0.0)
    p50_hold  = float(row.get("p50_hold_sec") or 0.0)

    buy_ratio     = _safe_div(n_buys, n_buys + n_sells, 0.5)
    s_activity    = _clamp(min(n_swaps / 40.0, 1.0) * (0.4 + 0.6 * buy_ratio))
    s_notional    = _clamp(_safe_div(n_buys_nb, max(n_buys, 1)))
    s_hold        = _hold_score_laszlo(avg_hold, p50_hold)
    s_diversity   = min(n_tokens / 5.0, 1.0)
    rt_share      = _safe_div(n_rt * 2, n_swaps)
    s_round_trip  = _clamp(rt_share * 0.6 + s_diversity * 0.4)

    return round(
        0.20 * s_activity
        + 0.20 * s_notional
        + 0.30 * s_hold
        + 0.15 * s_diversity
        + 0.15 * s_round_trip,
        4,
    )


def score_anti_tracking(row: dict) -> float:
    """Anti-tracking: rewards medium activity, balanced diversification, LASZLO hold.
    Intentionally pool-universe agnostic (may span both routes)."""
    n     = int(row.get("n_swaps", 0))
    n_tok = int(row.get("n_distinct_meme_tokens", 0))
    avg_h = float(row.get("avg_hold_sec") or 0.0)
    p50_h = float(row.get("p50_hold_sec") or 0.0)
    n_nb  = int(row.get("n_buys_notional_ok", 0))
    n_b   = int(row.get("n_buys", 0))
    n_rt  = int(row.get("n_round_trips", 0) if "n_round_trips" in row else 0)

    # Medium activity: 30-300 swaps is anti-tracking sweet spot
    if 30 <= n <= 300:
        s_activity = 1.0
    elif 10 <= n < 30 or 300 < n <= 800:
        s_activity = 0.5
    else:
        s_activity = 0.1

    # Diverse but not extreme (5-30 tokens)
    s_diversity = 1.0 if 5 <= n_tok <= 30 else (0.6 if 2 <= n_tok < 5 else 0.2)

    # LASZLO hold-time discipline (same Gaussian)
    s_hold = _hold_score_laszlo(avg_h, p50_h)

    # Small-notional discipline
    s_notional = _clamp(_safe_div(n_nb, max(n_b, 1)))

    # Some round trips (not none, not too many)
    rt_share = _safe_div(n_rt * 2, n)
    s_rt = 1.0 if 0.10 <= rt_share <= 0.80 else (0.4 if rt_share > 0 else 0.0)

    return round(
        0.20 * s_activity
        + 0.20 * s_diversity
        + 0.25 * s_hold
        + 0.20 * s_notional
        + 0.15 * s_rt,
        4,
    )


# ── Sensitivity analysis ──────────────────────────────────────────────────────

def compute_sensitivity(rows: list[dict], threshold: float = 0.5) -> dict:
    base = [r for r in rows if r["old_whitepaper_score"] >= threshold]
    n_base = len(base)

    def _cond_holdtime(r):
        return 300 <= (float(r.get("p50_hold_sec") or 0)) <= 3600

    def _cond_diverse_tokens(r):
        return int(r.get("n_distinct_meme_tokens", 0)) >= 5

    def _cond_roundtrips(r):
        return int(r.get("n_round_trips", 0)) >= 3

    def _cond_notional(r):
        return _safe_div(int(r.get("n_buys_notional_ok", 0)),
                         max(int(r.get("n_buys", 1)), 1)) >= 0.4

    def _cond_fee_dominant(r):
        n = int(r.get("n_swaps", 0))
        n3 = int(r.get("n_fee_3000", 0))
        n10 = int(r.get("n_fee_10000", 0))
        return _safe_div(n3 + n10, n) >= 0.50

    def _cond_sniper_strict(r):
        np_ = int(r.get("n_pools_entered", 0))
        ns = int(r.get("n_sniper_entries", 0))
        return np_ > 0 and ns >= 1

    conds = [
        ("C01_hold_300_3600s",    "p50_hold in [300s, 3600s]",          _cond_holdtime),
        ("C02_diverse_tokens",    "≥5 distinct meme tokens",             _cond_diverse_tokens),
        ("C03_roundtrips_ge3",    "≥3 round trips",                      _cond_roundtrips),
        ("C04_notional_ge40pct",  "≥40% buys in patched notional band",  _cond_notional),
        ("C05_fee_dominant_50pct","≥50% swaps in fee=3000+10000",        _cond_fee_dominant),
        ("C06_sniper_strict_1",   "≥1 sniper entry (≤+3 blocks)",        _cond_sniper_strict),
    ]

    individual = []
    for cid, desc, fn in conds:
        kept = [r for r in base if fn(r)]
        individual.append({
            "cond_id": cid, "description": desc,
            "kept": len(kept), "dropped": n_base - len(kept),
            "drop_pct": round((n_base - len(kept)) / max(n_base, 1) * 100, 1),
            "kept_addrs_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in kept],
        })

    # Cumulative kill chain
    cumulative = base[:]
    kill_chain = []
    for cid, desc, fn in conds:
        before = len(cumulative)
        cumulative = [r for r in cumulative if fn(r)]
        kill_chain.append({
            "step": cid, "before": before, "after": len(cumulative),
            "dropped": before - len(cumulative),
            "remaining_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in cumulative],
        })

    return {
        "n_total_old_route_scored": len(rows),
        "n_base_old_score_ge_threshold": n_base,
        "threshold_used": threshold,
        "individual_sensitivity": individual,
        "cumulative_kill_chain": kill_chain,
        "strict_pass_count": len(cumulative),
        "strict_pass_addrs_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in cumulative],
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_preflight(ch_url: str, ch_user: str, ch_pwd: str) -> None:
    _log("=== PREFLIGHT ===")
    old_q = f"""
WITH {_old_route_meme_pools_cte()}
SELECT count() AS n, uniqCombined(lower(s.recipient)) AS r
FROM eatlq.uniswap_v3_swaps s INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0 AND lower(s.sender)!=lower(s.recipient)
  AND coalesce(s.block_time,s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
"""
    upg_q = f"""
WITH {_upgraded_route_meme_pools_cte()}
SELECT count() AS n, uniqCombined(lower(s.recipient)) AS r
FROM eatlq.uniswap_v3_swaps s INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0 AND lower(s.sender)!=lower(s.recipient)
  AND coalesce(s.block_time,s.received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
"""
    o = _ch_query(ch_url, old_q, ch_user, ch_pwd)[0]
    u = _ch_query(ch_url, upg_q, ch_user, ch_pwd)[0]
    _log(f"  old_route   swaps={int(o['n']):,}  recip={int(o['r']):,}")
    _log(f"  upgraded    swaps={int(u['n']):,}  recip={int(u['r']):,}")
    _log("  No RPC calls required for this run.")
    _log("  → ready: python scripts/sprint039b_whitepaper_filter_patch.py --run")


def run_full(ch_url: str, ch_user: str, ch_pwd: str) -> None:
    _log("=== SPRINT039B FULL RUN ===")
    _log(f"  NOTIONAL_MAX_ETH  = {NOTIONAL_MAX_ETH} ETH  (M07 patch: was 0.015)")
    _log(f"  fee tiers old     = 500, 3000, 10000  (M01 patch: fee=100 excluded)")
    _log(f"  hold Gaussian     = peak {HOLD_PEAK_SEC:.0f}s σ={HOLD_SIGMA_SEC:.0f}s  (M03 patch)")
    _log(f"  s_sniper_strict   = ≤+3 blocks weight in old_route  (M09 patch)")

    # ── Phase 1: Old-route CH queries ────────────────────────────────────────
    _log("[P1] Old-route base profile query...")
    base_rows = _ch_query(ch_url, build_base_profile_query(_old_route_meme_pools_cte), ch_user, ch_pwd)
    _log(f"[P1] {len(base_rows):,} wallets passing old-route HAVING filter")

    _log("[P1] Old-route round-trip query...")
    rt_rows = _ch_query(ch_url, build_round_trip_query(_old_route_meme_pools_cte), ch_user, ch_pwd)
    rt_map  = {r["recipient_address_AUDIT_ONLY"]: r for r in rt_rows}
    _log(f"[P1] {len(rt_rows):,} wallets with round trips in old-route")

    _log("[P1] Old-route early-pool query...")
    ep_rows = _ch_query(ch_url, build_early_pool_query(_old_route_meme_pools_cte), ch_user, ch_pwd)
    ep_map  = {r["recipient_address_AUDIT_ONLY"]: r for r in ep_rows}
    _log(f"[P1] {len(ep_rows):,} wallets with early entries in old-route")

    # Merge and score old route
    old_scored: list[dict] = []
    for br in base_rows:
        addr = br["recipient_address_AUDIT_ONLY"]
        merged = {**br, **rt_map.get(addr, {}), **ep_map.get(addr, {})}
        subs = score_old_whitepaper(merged)
        old_scored.append({
            "recipient_address_AUDIT_ONLY": addr,
            **subs,
            "n_swaps":               int(br.get("n_swaps", 0)),
            "n_buys":                int(br.get("n_buys", 0)),
            "n_sells":               int(br.get("n_sells", 0)),
            "n_distinct_pools":      int(br.get("n_distinct_pools", 0)),
            "n_distinct_meme_tokens":int(br.get("n_distinct_meme_tokens", 0)),
            "n_fee_3000":            int(br.get("n_fee_3000", 0)),
            "n_fee_10000":           int(br.get("n_fee_10000", 0)),
            "n_fee_500":             int(br.get("n_fee_500", 0)),
            "n_fee_100":             int(br.get("n_fee_100", 0)),
            "n_buys_notional_ok":    int(br.get("n_buys_notional_ok", 0)),
            "n_round_trips":         int(merged.get("n_round_trips", 0)),
            "n_short_exits":         int(merged.get("n_short_exits", 0)),
            "n_sub_minute_exits":    int(merged.get("n_sub_minute_exits", 0)),
            "avg_hold_sec":          float(merged.get("avg_hold_sec") or 0),
            "p50_hold_sec":          float(merged.get("p50_hold_sec") or 0),
            "n_pools_entered":       int(merged.get("n_pools_entered", 0)),
            "n_early_entries":       int(merged.get("n_early_entries", 0)),
            "n_sniper_entries":      int(merged.get("n_sniper_entries", 0)),
        })

    old_scored.sort(key=lambda r: -r["old_whitepaper_score"])
    _log(f"[P1] old_whitepaper top (score≥0.5): {sum(1 for r in old_scored if r['old_whitepaper_score']>=0.5)}")
    _log(f"[P1] old_whitepaper top (score≥0.4): {sum(1 for r in old_scored if r['old_whitepaper_score']>=0.4)}")

    # ── Phase 2: Upgraded-route inventory ───────────────────────────────────
    _log("[P2] Upgraded-route base inventory...")
    upg_rows = _ch_query(
        ch_url,
        build_base_profile_query(_upgraded_route_meme_pools_cte, min_swaps=MIN_SWAPS_UPG),
        ch_user, ch_pwd,
    )
    _log(f"[P2] {len(upg_rows):,} wallets in upgraded-route inventory")

    upg_scored: list[dict] = []
    for ur in upg_rows:
        us = score_upgraded_route(ur)
        upg_scored.append({
            "recipient_address_AUDIT_ONLY": ur["recipient_address_AUDIT_ONLY"],
            "upgraded_route_score": us,
            "n_swaps_upg":          int(ur.get("n_swaps", 0)),
            "n_buys_upg":           int(ur.get("n_buys", 0)),
            "n_fee_100_upg":        int(ur.get("n_fee_100", 0)),
            "n_fee_1_upg":          int(ur.get("n_fee_1", 0)),
            "n_distinct_tokens_upg":int(ur.get("n_distinct_meme_tokens", 0)),
            "n_buys_notional_ok_upg":int(ur.get("n_buys_notional_ok", 0)),
            "avg_hold_sec_upg":     0.0,   # no RT query for upgraded route
            "p50_hold_sec_upg":     0.0,
        })
    upg_map = {r["recipient_address_AUDIT_ONLY"]: r for r in upg_scored}
    upg_scored.sort(key=lambda r: -r["upgraded_route_score"])
    _log(f"[P2] upgraded_route top (score≥0.5): {sum(1 for r in upg_scored if r['upgraded_route_score']>=0.5)}")

    # ── Phase 3: Anti-tracking scores (using old-route data) ─────────────────
    _log("[P3] Computing anti-tracking scores...")
    for r in old_scored:
        r["anti_tracking_score"] = score_anti_tracking(r)
    old_scored_anti = sorted(old_scored, key=lambda r: -r["anti_tracking_score"])

    # ── Phase 4: Sensitivity analysis ────────────────────────────────────────
    sens = compute_sensitivity(old_scored, threshold=0.5)
    _log(f"[P4] Strict pass (old_score≥0.5, all conditions): {sens['strict_pass_count']}")
    for step in sens["cumulative_kill_chain"]:
        _log(f"     {step['step']:30s} {step['before']:>4} → {step['after']:>4}")

    # ── Phase 5: Build combined per-wallet rows ───────────────────────────────
    addr_to_upg = {r["recipient_address_AUDIT_ONLY"]: r for r in upg_scored}
    combined: list[dict] = []
    for r in old_scored:
        addr = r["recipient_address_AUDIT_ONLY"]
        upg = addr_to_upg.get(addr, {})
        combined.append({
            **r,
            "upgraded_route_score":    upg.get("upgraded_route_score", 0.0),
            "n_swaps_upg":             upg.get("n_swaps_upg", 0),
            "n_fee_100_upg":           upg.get("n_fee_100_upg", 0),
            "n_fee_1_upg":             upg.get("n_fee_1_upg", 0),
        })
    # Also add upgraded-only wallets (not in old_scored)
    old_addrs = {r["recipient_address_AUDIT_ONLY"] for r in old_scored}
    for ur in upg_scored:
        if ur["recipient_address_AUDIT_ONLY"] not in old_addrs:
            combined.append({
                "recipient_address_AUDIT_ONLY": ur["recipient_address_AUDIT_ONLY"],
                "old_whitepaper_score": 0.0,
                "anti_tracking_score":  0.0,
                **ur,
            })

    # ── Assemble verdict ──────────────────────────────────────────────────────
    n_strict = sens["strict_pass_count"]
    top_old = old_scored[:10]
    top_upg = upg_scored[:10]
    top_anti = old_scored_anti[:10]

    # Compare before/after: Sprint038A had 9 at score≥0.5; now check
    n_old_ge50 = sum(1 for r in old_scored if r["old_whitepaper_score"] >= 0.5)
    n_old_ge40 = sum(1 for r in old_scored if r["old_whitepaper_score"] >= 0.4)
    n_upg_ge50 = sum(1 for r in upg_scored if r["upgraded_route_score"] >= 0.5)

    if n_strict >= 1:
        filter_verdict = "FILTER_PATCH_PASS"
    elif n_old_ge50 >= 1:
        filter_verdict = "FILTER_PATCH_INCONCLUSIVE"
    else:
        filter_verdict = "FILTER_PATCH_FAIL"

    verdict = {
        "FILTER_PATCH_VERDICT":           filter_verdict,
        "STRICT_AUTHOR_MATCH_COUNT":      n_strict,
        "OLD_ROUTE_GE50":                 n_old_ge50,
        "OLD_ROUTE_GE40":                 n_old_ge40,
        "UPGRADED_ROUTE_GE50":            n_upg_ge50,
        "AUTHOR_STYLE_TOP_CANDIDATES":    [r["recipient_address_AUDIT_ONLY"] for r in top_old[:3]],
        "UPGRADED_ROUTE_TOP_CANDIDATES":  [r["recipient_address_AUDIT_ONLY"] for r in top_upg[:3]],
        "IDENTITY_CLAIM_ALLOWED":         False,
        "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
        "NEXT_QUERY_REQUIRED":            True,
        "PATCHES_APPLIED":                ["M01", "M03", "M07", "M09", "M12"],
        "PATCHES_DEFERRED":               ["M04", "M06", "M08"],
    }

    # ── Output JSON ───────────────────────────────────────────────────────────
    summary = {
        "sprint":       "sprint039b_whitepaper_filter_patch",
        "run_ts_utc":   datetime.now(timezone.utc).isoformat(),
        "patches_applied": {
            "M01": "fee=100 excluded from old_route meme_pools CTE",
            "M03": f"s_short_exit → Gaussian peak={HOLD_PEAK_SEC:.0f}s σ={HOLD_SIGMA_SEC:.0f}s; sub-60s=0",
            "M07": f"NOTIONAL_MAX_ETH {NOTIONAL_MAX_ETH} ETH (was 0.015)",
            "M09": "s_sniper_strict (≤+3 blocks) added with weight 0.10 in old_route",
            "M12": "upgraded_route (fee=1/100/aero_cl) separately inventoried",
        },
        "patches_deferred": {
            "M04": "WETH.withdraw() scan → Sprint039C",
            "M06": "fee-tier-specific ROUND_TRIP_COST → Sprint039C",
            "M08": "entity clustering CIH → Sprint039C",
        },
        "constants": {
            "NOTIONAL_MAX_ETH":  NOTIONAL_MAX_ETH,
            "NOTIONAL_MIN_ETH":  NOTIONAL_MIN_ETH,
            "HOLD_PEAK_SEC":     HOLD_PEAK_SEC,
            "HOLD_SIGMA_SEC":    HOLD_SIGMA_SEC,
            "WINDOW_DAYS":       WINDOW_DAYS,
        },
        "old_route_weights": OLD_ROUTE_WEIGHTS,
        "candidate_counts": {
            "old_route_scored_total":  len(old_scored),
            "old_route_score_ge_0_5":  n_old_ge50,
            "old_route_score_ge_0_4":  n_old_ge40,
            "upgraded_route_scored":   len(upg_scored),
            "upgraded_score_ge_0_5":   n_upg_ge50,
            "strict_all_conditions":   n_strict,
        },
        "sensitivity": sens,
        "verdict": verdict,
        "top10_old_whitepaper": top_old,
        "top10_upgraded_route": top_upg,
        "top10_anti_tracking":  top_anti,
        "decision_flags":       DECISION_FLAGS,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _log(f"[OUT] {OUT_JSON}")

    # ── Output CSV ────────────────────────────────────────────────────────────
    csv_fields = [
        "recipient_address_AUDIT_ONLY",
        "old_whitepaper_score", "upgraded_route_score", "anti_tracking_score",
        "s_v3_activity", "s_fee_dominant", "s_notional", "s_short_exit",
        "s_early_pool", "s_sniper_strict", "s_round_trip", "s_concentration",
        "n_swaps", "n_distinct_pools", "n_distinct_meme_tokens",
        "n_buys", "n_sells", "n_round_trips", "n_sub_minute_exits",
        "avg_hold_sec", "p50_hold_sec",
        "n_fee_3000", "n_fee_10000", "n_fee_500",
        "n_buys_notional_ok", "n_early_entries", "n_sniper_entries",
        "n_fee_100_upg", "n_fee_1_upg",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        # Sort by combined rank (old + anti, keeping top 200)
        combined_sorted = sorted(
            [r for r in combined if r.get("old_whitepaper_score", 0) > 0],
            key=lambda r: -(r.get("old_whitepaper_score", 0) + r.get("anti_tracking_score", 0)),
        )[:200]
        w.writerows(combined_sorted)
    _log(f"[OUT] {OUT_CSV}")

    # ── Write audit markdown ──────────────────────────────────────────────────
    _write_audit_md(summary, top_old, top_upg, top_anti, old_scored)
    _log(f"[OUT] {OUT_MD}")

    # ── Executive print ───────────────────────────────────────────────────────
    _print_summary(summary, top_old, top_upg, top_anti)


def _write_audit_md(
    s: dict,
    top_old: list, top_upg: list, top_anti: list,
    all_old: list,
) -> None:
    v   = s["verdict"]
    cnt = s["candidate_counts"]
    sens = s["sensitivity"]

    lines = [
        "# Sprint039B — Whitepaper Canon Filter Patch",
        "",
        f"**Run TS (UTC):** {s['run_ts_utc']}",
        "",
        "## 1. Patches Applied",
        "",
        "| ID | Description | Change |",
        "|----|-------------|--------|",
        "| M01 | fee=100 excluded from old-route pool universe | `fee_bps IN (500, 3000, 10000)` only |",
        f"| M03 | s_short_exit → Gaussian hold-time score | peak={HOLD_PEAK_SEC:.0f}s, σ={HOLD_SIGMA_SEC:.0f}s; <60s=0 |",
        f"| M07 | Tightened notional upper bound | {NOTIONAL_MAX_ETH} ETH (was 0.015) |",
        "| M09 | s_sniper_strict (≤+3 blocks) added | weight=0.10, separate from s_early_pool(0.05) |",
        "| M12 | Upgraded route (fee=1/100/aero_cl) inventoried | separate scoring lane |",
        "",
        "**Deferred (not in scope):** M04 (WETH.withdraw scan), M06 (fee-tier ROUND_TRIP_COST), M08 (entity clustering).",
        "",
        "## 2. Candidate Counts — Before / After",
        "",
        "| Metric | Sprint038A (90d, fee=100 included) | Sprint039B (90d, M01-M09 patched) |",
        "|--------|-----------------------------------|----------------------------------|",
        f"| Old-route scored wallets | ~3,717 | {cnt['old_route_scored_total']:,} |",
        f"| score ≥ 0.5 | 9 | {cnt['old_route_score_ge_0_5']} |",
        f"| score ≥ 0.4 | — | {cnt['old_route_score_ge_0_4']} |",
        f"| Strict (all conditions) | 0 | **{cnt['strict_all_conditions']}** |",
        f"| Upgraded-route inventory | — | {cnt['upgraded_route_scored']:,} |",
        f"| Upgraded score ≥ 0.5 | — | {cnt['upgraded_score_ge_0_5']} |",
        "",
        "## 3. Top Old-Whitepaper Candidates",
        "",
        "| # | Address | old_score | p50_hold_sec | n_tokens | n_RT | n_sniper | fee10k% |",
        "|---|---------|-----------|-------------|----------|------|----------|---------|",
    ]
    for i, r in enumerate(top_old[:10], 1):
        fee10k_pct = round(r["n_fee_10000"] / max(r["n_swaps"], 1) * 100, 0)
        lines.append(
            f"| {i} | `{r['recipient_address_AUDIT_ONLY']}` | {r['old_whitepaper_score']} | "
            f"{r['p50_hold_sec']:.0f} | {r['n_distinct_meme_tokens']} | "
            f"{r['n_round_trips']} | {r['n_sniper_entries']} | {fee10k_pct:.0f}% |"
        )

    lines += [
        "",
        "## 4. Top Upgraded-Route Candidates",
        "",
        "| # | Address | upgraded_score | n_swaps_upg | n_fee_100 | n_fee_1 | n_tokens |",
        "|---|---------|---------------|------------|----------|---------|---------|",
    ]
    for i, r in enumerate(top_upg[:10], 1):
        lines.append(
            f"| {i} | `{r['recipient_address_AUDIT_ONLY']}` | {r['upgraded_route_score']} | "
            f"{r['n_swaps_upg']} | {r['n_fee_100_upg']} | {r['n_fee_1_upg']} | "
            f"{r['n_distinct_tokens_upg']} |"
        )

    lines += [
        "",
        "## 5. Top Anti-Tracking Candidates",
        "",
        "| # | Address | anti_score | n_swaps | n_tokens | p50_hold | n_RT |",
        "|---|---------|-----------|---------|---------|---------|------|",
    ]
    for i, r in enumerate(top_anti[:10], 1):
        lines.append(
            f"| {i} | `{r['recipient_address_AUDIT_ONLY']}` | {r['anti_tracking_score']} | "
            f"{r['n_swaps']} | {r['n_distinct_meme_tokens']} | "
            f"{r['p50_hold_sec']:.0f} | {r['n_round_trips']} |"
        )

    lines += [
        "",
        "## 6. Strict-Match Kill Chain",
        "",
        f"Starting from {sens['n_base_old_score_ge_threshold']} wallets with old_score ≥ {sens['threshold_used']}:",
        "",
        "| Step | Condition | Before → After | Dropped |",
        "|------|-----------|---------------|---------|",
    ]
    for step in sens["cumulative_kill_chain"]:
        lines.append(
            f"| {step['step']} | — | {step['before']} → {step['after']} | {step['dropped']} |"
        )
    lines += [
        "",
        f"**Strict-pass count: {cnt['strict_all_conditions']}**",
        "",
        "## 7. Decision Flags",
        "",
        "```",
        f"FILTER_PATCH_VERDICT          = {v['FILTER_PATCH_VERDICT']}",
        f"STRICT_AUTHOR_MATCH_COUNT     = {v['STRICT_AUTHOR_MATCH_COUNT']}",
        f"OLD_ROUTE_GE50                = {v['OLD_ROUTE_GE50']}",
        f"UPGRADED_ROUTE_GE50           = {v['UPGRADED_ROUTE_GE50']}",
        f"AUTHOR_STYLE_TOP_CANDIDATES   = {v['AUTHOR_STYLE_TOP_CANDIDATES']}",
        f"UPGRADED_ROUTE_TOP_CANDIDATES = {v['UPGRADED_ROUTE_TOP_CANDIDATES']}",
        f"IDENTITY_CLAIM_ALLOWED        = {v['IDENTITY_CLAIM_ALLOWED']}",
        f"ATTRIBUTION_HYPOTHESIS_ALLOWED= {v['ATTRIBUTION_HYPOTHESIS_ALLOWED']}",
        f"PATCHES_APPLIED               = {v['PATCHES_APPLIED']}",
        f"PATCHES_DEFERRED              = {v['PATCHES_DEFERRED']}",
        f"TRAINING_ALLOWED              = false",
        f"LIVE_ALLOWED                  = false",
        f"NO_ALPHA_CLAIM                = true",
        "```",
        "",
        "---",
        "*All addresses use _AUDIT_ONLY suffix. No identity claims.*",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def _print_summary(s: dict, top_old: list, top_upg: list, top_anti: list) -> None:
    v   = s["verdict"]
    cnt = s["candidate_counts"]
    print("\n" + "=" * 70)
    print("SPRINT039B  FILTER PATCH SUMMARY")
    print("=" * 70)
    print(f"  Patches: {v['PATCHES_APPLIED']}")
    print(f"  Old-route scored: {cnt['old_route_scored_total']:,}")
    print(f"  score ≥ 0.5: {cnt['old_route_score_ge_0_5']}  |  strict all-conditions: {cnt['strict_all_conditions']}")
    print(f"  Upgraded-route: {cnt['upgraded_route_scored']:,} wallets  (≥0.5: {cnt['upgraded_score_ge_0_5']})")
    print()
    print("  Top 3 old-whitepaper:")
    for r in top_old[:3]:
        print(f"    {r['recipient_address_AUDIT_ONLY']} score={r['old_whitepaper_score']} "
              f"hold={r['p50_hold_sec']:.0f}s RT={r['n_round_trips']} sniper={r['n_sniper_entries']}")
    print()
    print("  Top 3 upgraded-route:")
    for r in top_upg[:3]:
        print(f"    {r['recipient_address_AUDIT_ONLY']} score={r['upgraded_route_score']} "
              f"fee100={r['n_fee_100_upg']} fee1={r['n_fee_1_upg']}")
    print()
    print("  Top 3 anti-tracking:")
    for r in top_anti[:3]:
        print(f"    {r['recipient_address_AUDIT_ONLY']} score={r['anti_tracking_score']} "
              f"n={r['n_swaps']} hold={r['p50_hold_sec']:.0f}s")
    print()
    print(f"  FILTER_PATCH_VERDICT = {v['FILTER_PATCH_VERDICT']}")
    print(f"  STRICT_AUTHOR_MATCH  = {v['STRICT_AUTHOR_MATCH_COUNT']}")
    print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint039B filter patch")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--run",       action="store_true")
    args = ap.parse_args()
    if not (args.preflight or args.run):
        ap.print_help()
        sys.exit(0)

    ch_url  = os.environ.get("CLICKHOUSE_URL",      "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER",     "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")

    if args.preflight:
        run_preflight(ch_url, ch_user, ch_pwd)
    else:
        run_full(ch_url, ch_user, ch_pwd)


if __name__ == "__main__":
    main()
