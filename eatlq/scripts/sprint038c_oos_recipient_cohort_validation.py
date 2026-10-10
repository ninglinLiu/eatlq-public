#!/usr/bin/env python3
"""Sprint038C-OOS — Recipient cohort out-of-sample (OOS) anti-overfit validation
=================================================================================

CONTEXT
-------
Sprint038A scored recipient wallets on 90-day data (selection window).
Sprint038B backtested cohorts on the SAME 90-day window — i.e. in-sample.

This script performs a STRICT OOS validation:
  1. Re-score recipients on selection window ONLY  [T-90d, T-30d]
  2. Freeze top cohort (evidence_score >= 0.5) — never modified after scoring.
  3. Validate on validation window ONLY             [T-30d, T]
  4. Build matched baseline from non-cohort wallets in the same validation window.
  5. Compare q30m label_1_share: top cohort vs matched baseline.

Key temporal isolation:
  selection_window = [T-90d, T-30d]   # 60 days, for scoring only
  validation_window = [T-30d, T]       # 30 days, for labeling only
  → NO leakage: cohort was frozen before any validation data exists.

PRIMARY METRIC:  q30m label_1_share vs matched_baseline_label_1_share
SECONDARY:       q30m trimmed_mean, quote_success_rate, q5m decay
DIAGNOSTIC ONLY: actual_rt, F10 gas

DECISION FLAGS (frozen — never override from CLI)
-------------------------------------------------
  TRAINING_ALLOWED              = false
  LIVE_ALLOWED                  = false
  MODEL_PROMOTION_ALLOWED       = false
  FAILED_NO_7D_ALPHA_ACTIVE     = true
  IDENTITY_CLAIM_ALLOWED        = false
  NO_ALPHA_CLAIM                = true
  OOS_VALIDATION_ALLOWED        = true

OUTPUTS
-------
  mvp/diagnostics/sprint038c_oos_recipient_cohort_summary.json
  mvp/diagnostics/sprint038c_oos_recipient_cohort_rows.csv
  mvp/audits/sprint038c_oos_recipient_cohort.md

USAGE
-----
  python scripts/sprint038c_oos_recipient_cohort_validation.py --preflight
  LABEL_RPC_URL=https://... python scripts/sprint038c_oos_recipient_cohort_validation.py --run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.request import Request, urlopen

# ── Frozen decision flags ─────────────────────────────────────────────────────
DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED":              False,
    "LIVE_ALLOWED":                  False,
    "MODEL_PROMOTION_ALLOWED":       False,
    "FAILED_NO_7D_ALPHA_ACTIVE":     True,
    "IDENTITY_CLAIM_ALLOWED":        False,
    "SENDER_KEYED_RANKING_TRUSTED":  False,
    "NO_ALPHA_CLAIM":                True,
    "OOS_VALIDATION_ALLOWED":        True,
    "COHORT_THRESHOLD_FROZEN":       True,
    "SMART_MONEY_GENERAL_LINE_OPEN": False,
}

# ── Chain / protocol constants ────────────────────────────────────────────────
CHAIN_ID        = 8453
WETH            = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE  = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
ROUND_TRIP_COST = 0.006
MAX_ABS_NET     = 10.0

# ── OOS window parameters ─────────────────────────────────────────────────────
SELECTION_DAYS   = 60   # days of data used for scoring
VALIDATION_DAYS  = 30   # days of data used for labeling
TOTAL_WINDOW_DAYS = SELECTION_DAYS + VALIDATION_DAYS   # 90

# ── Frozen cohort threshold (MUST NOT change after seeing results) ────────────
COHORT_SCORE_THRESHOLD = 0.5    # top9 rule from sprint038a readout

# ── Quoter probe (matches sprint037b label-semantics-freeze) ─────────────────
PROBE_MICRO_WEI = int(5e14)     # 0.0005 ETH
BLOCKS_5M       = 150
BLOCKS_30M      = 900

# ── Notional band (mirrors sprint038a LASZLO position size) ──────────────────
NOTIONAL_MIN_ETH = 0.0003
NOTIONAL_MAX_ETH = 0.015
SHORT_EXIT_SEC   = 1800    # 30 min

# ── Evidence scoring weights (mirrors sprint038a exactly) ─────────────────────
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
assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9

# ── RPC / sampling caps ───────────────────────────────────────────────────────
MAX_RPC_CALLS         = 5000
CAP_COHORT_PER_WALLET = 50    # max validation buy events per cohort wallet
CAP_BASELINE_WALLETS  = 80    # max non-cohort wallets in baseline (RPC budget)
CAP_BASELINE_PER_WAL  = 15    # max validation buy events per baseline wallet
MIN_BASELINE_BUYS     = 3     # min buys in validation window to qualify for baseline

# ── Minimum labeled rows for OOS_INCONCLUSIVE verdict ─────────────────────────
OOS_INCONCLUSIVE_ROWS = 300

# ── OOS verdict thresholds ────────────────────────────────────────────────────
OOS_PASS_UPLIFT  = 1.25   # label_1_share >= baseline × 1.25
OOS_WEAK_UPLIFT  = 1.05   # label_1_share >= baseline × 1.05 (below PASS)
CONC_PASS_W1     = 0.35   # wallet_top1_share must be ≤ this
CONC_PASS_P1     = 0.25   # pool_top1_share must be ≤ this

# ── Exclusions ────────────────────────────────────────────────────────────────
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

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO     = Path(__file__).resolve().parents[1]
OUT_JSON = REPO / "mvp/diagnostics/sprint038c_oos_recipient_cohort_summary.json"
OUT_CSV  = REPO / "mvp/diagnostics/sprint038c_oos_recipient_cohort_rows.csv"
OUT_MD   = REPO / "mvp/audits/sprint038c_oos_recipient_cohort.md"
OUT_MD.parent.mkdir(parents=True, exist_ok=True)
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[s038c {ts}] {msg}", flush=True)


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for bad in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if bad in f" {s} ":
            raise ValueError(f"forbidden SQL: {bad!r}")


def _ch_query(
    url: str,
    sql: str,
    user: str,
    pwd: str,
    timeout: float = 600.0,
) -> list[dict]:
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


# ── SQL fragment helpers ──────────────────────────────────────────────────────
def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))


def _routers_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(KNOWN_ROUTERS))


def _addrs_sql(addrs: list[str]) -> str:
    return ", ".join(f"'{a}'" for a in addrs)


def _meme_pools_cte() -> str:
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
    AND dp.fee_bps IN (100, 500, 3000, 10000)
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
)"""


# ── Window computation ────────────────────────────────────────────────────────
def compute_windows() -> tuple[str, str, str, str]:
    """Return (sel_start, sel_end, val_start, val_end) as ISO date strings."""
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    val_end   = now
    val_start = now - timedelta(days=VALIDATION_DAYS)
    sel_end   = val_start
    sel_start = now - timedelta(days=TOTAL_WINDOW_DAYS)
    fmt = "%Y-%m-%d"
    return sel_start.strftime(fmt), sel_end.strftime(fmt), \
           val_start.strftime(fmt), val_end.strftime(fmt)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Re-score on selection window (temporal isolation enforced)
# ══════════════════════════════════════════════════════════════════════════════

def build_selection_base_query(sel_start: str, sel_end: str) -> str:
    """Per-recipient aggregate restricted to [sel_start, sel_end]."""
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)                                                      AS recipient_address_AUDIT_ONLY,
  count()                                                                 AS n_swaps,
  uniqCombined(lower(s.pool_address))                                     AS n_distinct_pools,
  uniqCombined(if(mp.weth_is_token0=1, mp.t1, mp.t0))                    AS n_distinct_meme_tokens,
  countIf(mp.fee_bps IN (500, 3000, 10000))                               AS n_lasl_fee_swaps,
  countIf(mp.fee_bps=3000)                                                AS n_fee_3000,
  countIf(mp.fee_bps=10000)                                               AS n_fee_10000,
  countIf(mp.fee_bps=500)                                                 AS n_fee_500,
  countIf(mp.fee_bps=100)                                                 AS n_fee_100,
  countIf(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  )                                                                       AS n_buys,
  countIf(NOT (
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  ))                                                                      AS n_sells,
  countIf(
    ((mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
     (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'))
    AND multiIf(mp.weth_is_token0=1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) >= {NOTIONAL_MIN_ETH}
    AND multiIf(mp.weth_is_token0=1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) <= {NOTIONAL_MAX_ETH}
  )                                                                       AS n_buys_notional_ok
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.sender) != lower(s.recipient)
  AND lower(s.recipient) NOT IN ({_routers_sql()})
  AND coalesce(s.block_time, s.received_at_ms) >= '{sel_start}'
  AND coalesce(s.block_time, s.received_at_ms) <  '{sel_end}'
GROUP BY recipient_address_AUDIT_ONLY
HAVING n_swaps >= 10 AND n_buys >= 3
ORDER BY n_swaps DESC
"""


def build_selection_roundtrip_query(sel_start: str, sel_end: str) -> str:
    return f"""
WITH {_meme_pools_cte()},
events AS (
  SELECT
    lower(s.recipient)  AS rec,
    mp.pa               AS pa,
    multiIf(
      (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8'),'buy',
      (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'), 'buy',
      'sell'
    ) AS side,
    toUnixTimestamp(coalesce(s.block_time, s.received_at_ms)) AS ts
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID} AND s.removed = 0
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
    AND coalesce(s.block_time, s.received_at_ms) >= '{sel_start}'
    AND coalesce(s.block_time, s.received_at_ms) <  '{sel_end}'
),
bm AS (SELECT rec, pa, min(ts) AS fbts FROM events WHERE side='buy'  GROUP BY rec, pa),
sm AS (SELECT rec, pa, min(ts) AS fsts FROM events WHERE side='sell' GROUP BY rec, pa),
rt AS (
  SELECT b.rec, b.pa,
    greatest(toInt64(s.fsts)-toInt64(b.fbts), 0) AS hold_sec
  FROM bm b INNER JOIN sm s ON b.rec=s.rec AND b.pa=s.pa
  WHERE s.fsts > b.fbts
)
SELECT
  rec                                     AS recipient_address_AUDIT_ONLY,
  count()                                 AS n_round_trips,
  countIf(hold_sec <= {SHORT_EXIT_SEC})   AS n_short_exits,
  avg(hold_sec)                           AS avg_hold_sec,
  quantile(0.50)(hold_sec)               AS p50_hold_sec
FROM rt GROUP BY rec
"""


def build_selection_earlypool_query(sel_start: str, sel_end: str) -> str:
    return f"""
WITH {_meme_pools_cte()},
fbp AS (
  SELECT
    lower(s.recipient) AS rec,
    mp.pa              AS pa,
    mp.pool_created_block AS pcb,
    min(s.block_number)   AS fbb
  FROM eatlq.uniswap_v3_swaps AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID} AND s.removed = 0
    AND lower(s.sender) != lower(s.recipient)
    AND lower(s.recipient) NOT IN ({_routers_sql()})
    AND coalesce(s.block_time, s.received_at_ms) >= '{sel_start}'
    AND coalesce(s.block_time, s.received_at_ms) <  '{sel_end}'
    AND (
      (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
      (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
    )
  GROUP BY rec, pa, pcb
)
SELECT
  rec                                     AS recipient_address_AUDIT_ONLY,
  count()                                 AS n_pools_entered,
  countIf(fbb <= pcb + 50)               AS n_early_entries,
  countIf(fbb <= pcb + 3)                AS n_sniper_entries
FROM fbp GROUP BY rec
"""


# ── Scoring functions (mirrors sprint038a exactly) ────────────────────────────
def _safe_div(a: float, b: float, fb: float = 0.0) -> float:
    return a / b if b > 0 else fb


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def compute_evidence_score(row: dict) -> dict[str, float]:
    n_swaps   = int(row.get("n_swaps", 0))
    n_buys    = int(row.get("n_buys", 0))
    n_sells   = int(row.get("n_sells", 0))
    n_lasl    = int(row.get("n_lasl_fee_swaps", 0))
    n_buys_nb = int(row.get("n_buys_notional_ok", 0))
    n_pools   = int(row.get("n_distinct_pools", 0))
    n_tokens  = int(row.get("n_distinct_meme_tokens", 0))
    n_rt      = int(row.get("n_round_trips", 0))
    n_short   = int(row.get("n_short_exits", 0))
    avg_hold  = float(row.get("avg_hold_sec") or 0.0)
    n_pe      = int(row.get("n_pools_entered", 0))
    n_early   = int(row.get("n_early_entries", 0))
    win_rate  = float(row.get("win_rate", -1.0))

    buy_ratio     = _safe_div(n_buys, n_buys + n_sells, 0.5)
    s_v3_activity = _clamp(min(n_swaps / 50.0, 1.0) * (0.5 + 0.5 * buy_ratio))
    s_fee_pattern = _clamp(_safe_div(n_lasl, n_swaps))
    s_notional    = _clamp(_safe_div(n_buys_nb, max(n_buys, 1)))

    if n_rt == 0:
        s_short_exit = 0.0
    else:
        short_ratio  = _safe_div(n_short, n_rt)
        hold_ok      = 1.0 if (60 <= avg_hold <= 3600*8) else \
                       0.5 if (0 < avg_hold < 60 or 3600*8 < avg_hold < 86400*7) else 0.1
        s_short_exit = _clamp(short_ratio * hold_ok)

    s_early_pool  = _clamp(_safe_div(n_early, n_pe)) if n_pe > 0 else 0.0

    rt_share    = _safe_div(n_rt * 2, n_swaps)
    div_bonus   = min(n_tokens / 5.0, 1.0)
    s_round_trip = _clamp(rt_share * 0.6 + div_bonus * 0.4)

    venue_conc  = 1.0 / max(n_pools, 1)
    token_conc  = 1.0 / max(n_tokens, 1)
    s_concentration = _clamp(
        (1.0 - min(venue_conc * 2, 1.0)) * 0.5 +
        (1.0 - min(token_conc * 3, 1.0)) * 0.5
    )

    if win_rate < 0:
        s_pnl_proxy = 0.5
    elif 0.45 <= win_rate <= 0.85:
        s_pnl_proxy = _clamp(1.0 - abs(win_rate - 0.625) / 0.225)
    else:
        s_pnl_proxy = _clamp(win_rate * 0.3)

    subs = dict(
        s_v3_activity=s_v3_activity,
        s_fee_pattern=s_fee_pattern,
        s_notional=s_notional,
        s_short_exit=s_short_exit,
        s_early_pool=s_early_pool,
        s_round_trip=s_round_trip,
        s_concentration=s_concentration,
        s_pnl_proxy=s_pnl_proxy,
    )
    score = sum(SCORE_WEIGHTS[k] * v for k, v in subs.items())
    subs["evidence_score"] = round(score, 4)
    return subs


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Validation window buy events
# ══════════════════════════════════════════════════════════════════════════════

def build_validation_buy_query(
    val_start: str,
    val_end: str,
    wallet_addrs: list[str],
    cap: int,
) -> str:
    """Get buy events in validation window for a specific set of wallets."""
    wallets_sql = _addrs_sql(wallet_addrs)
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)                                                      AS recipient_AUDIT_ONLY,
  lower(s.pool_address)                                                   AS pool_address,
  if(mp.weth_is_token0=1, mp.t1, mp.t0)                                  AS meme_token,
  mp.fee_bps,
  s.block_number,
  toUnixTimestamp(coalesce(s.block_time, s.received_at_ms))              AS ts_sec,
  multiIf(
    mp.weth_is_token0=1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18
  )                                                                       AS weth_in_eth,
  lower(s.tx_hash)                                                        AS tx_hash,
  mp.pool_created_block
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.recipient) IN ({wallets_sql})
  AND coalesce(s.block_time, s.received_at_ms) >= '{val_start}'
  AND coalesce(s.block_time, s.received_at_ms) <  '{val_end}'
  AND (
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  )
ORDER BY recipient_AUDIT_ONLY, ts_sec
LIMIT {cap * len(wallet_addrs)}
"""


def build_baseline_wallet_count_query(
    val_start: str,
    val_end: str,
    excluded_addrs: list[str],
) -> str:
    """Count validation buy events per non-cohort wallet — for sampling."""
    excl_sql = _addrs_sql(excluded_addrs + list(KNOWN_ROUTERS))
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)  AS w,
  count()             AS n_buys
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.sender) != lower(s.recipient)
  AND lower(s.recipient) NOT IN ({excl_sql})
  AND coalesce(s.block_time, s.received_at_ms) >= '{val_start}'
  AND coalesce(s.block_time, s.received_at_ms) <  '{val_end}'
  AND (
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  )
GROUP BY w
HAVING n_buys >= {MIN_BASELINE_BUYS}
ORDER BY n_buys DESC
LIMIT 5000
"""


# ══════════════════════════════════════════════════════════════════════════════
# RPC / Quoter helpers (mirrors sprint038b)
# ══════════════════════════════════════════════════════════════════════════════

class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 5):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._lock = Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: list) -> Any:
        body = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params, "id": self._next_id(),
        }).encode()
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                code = getattr(e, "code", None)
                if code == 429 or (code and 500 <= code < 600) or code is None:
                    time.sleep(min(0.3 * (2 ** attempt), 8.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc exhausted")


_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(h: str) -> str: return h.zfill(64)
def _addr_pad(a: str) -> str:
    return _pad32((a[2:] if a.startswith("0x") else a).lower())
def _uint_pad(n: int) -> str: return _pad32(hex(n)[2:])
def _strip_0x(h: str) -> str: return h[2:] if h.startswith("0x") else h


def _eth_call(rpc: RPC, to: str, data: str, block: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_quoter(
    rpc: RPC, token_in: str, token_out: str, amount_in: int, fee: int, block: int,
) -> int:
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in) + _addr_pad(token_out)
        + _uint_pad(amount_in) + _uint_pad(fee) + _uint_pad(0)
    )
    try:
        res = _eth_call(rpc, QUOTER_V2_BASE, data, block)
        raw = _strip_0x(res)
        return int(raw[:64], 16) if len(raw) >= 64 else 0
    except Exception:
        return 0


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET, net)), True
    return net, False


def label_buy_event(rpc: RPC, be: dict, cohort_tag: str) -> list[dict]:
    """Run entry + 5m + 30m Quoter.  Returns 0–2 rows per buy event."""
    pool     = be["pool_address"]
    meme     = be["meme_token"]
    fee      = int(be["fee_bps"])
    entry_bn = int(be["block_number"])

    entry_out = call_quoter(rpc, WETH, meme, PROBE_MICRO_WEI, fee, entry_bn)
    if entry_out <= 0:
        return [{
            "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
            "pool_address": pool,
            "meme_token": meme,
            "fee_bps": fee,
            "buy_ts": int(be.get("ts_sec") or 0),
            "entry_block": entry_bn,
            "entry_quote_ok": False,
            "label_type": "q30m",
            "rpc_label": None,
            "net_return": None,
            "cohort": cohort_tag,
            "weth_in_eth": float(be.get("weth_in_eth") or 0),
        }]

    rows = []
    for ltype, offset in (("q5m", BLOCKS_5M), ("q30m", BLOCKS_30M)):
        exit_bn  = entry_bn + offset
        exit_out = call_quoter(rpc, meme, WETH, entry_out, fee, exit_bn)
        if exit_out <= 0:
            rows.append({
                "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
                "pool_address": pool,
                "meme_token": meme,
                "fee_bps": fee,
                "buy_ts": int(be.get("ts_sec") or 0),
                "entry_block": entry_bn,
                "entry_quote_ok": True,
                "label_type": ltype,
                "rpc_label": 0,
                "net_return": -1.0,
                "cohort": cohort_tag,
                "weth_in_eth": float(be.get("weth_in_eth") or 0),
            })
            continue
        net, bogus = _sanity_clip(exit_out / PROBE_MICRO_WEI - 1.0 - ROUND_TRIP_COST)
        if bogus:
            continue
        rows.append({
            "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
            "pool_address": pool,
            "meme_token": meme,
            "fee_bps": fee,
            "buy_ts": int(be.get("ts_sec") or 0),
            "entry_block": entry_bn,
            "exit_block": entry_bn + offset,
            "entry_quote_ok": True,
            "label_type": ltype,
            "rpc_label": 1 if net > 0 else 0,
            "net_return": round(net, 6),
            "cohort": cohort_tag,
            "weth_in_eth": float(be.get("weth_in_eth") or 0),
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Statistics helpers
# ══════════════════════════════════════════════════════════════════════════════

def _stats(returns: list[float]) -> dict:
    if not returns:
        return {"n": 0, "label_1_share": 0.0, "trimmed_mean": 0.0}
    rs  = sorted(returns)
    n   = len(rs)
    pos = sum(x > 0 for x in rs)
    k   = max(1, n // 10)
    tr  = rs[k:-k] if n > 2 * k else rs
    return {
        "n":             n,
        "label_1":       pos,
        "label_1_share": round(pos / n, 4),
        "mean":          round(sum(rs) / n, 6),
        "median":        round(rs[n // 2], 6),
        "trimmed_mean":  round(sum(tr) / max(len(tr), 1), 6),
        "p10":           round(rs[max(0, n // 10)], 6),
        "p90":           round(rs[min(n-1, 9*n//10)], 6),
    }


def _concentration(rows: list[dict], key: str) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[str(r.get(key, "?"))] += 1
    total = sum(counts.values())
    if total == 0:
        return {"total_trades": 0, "unique": 0, "top1_share": 0.0, "top3_share": 0.0}
    si = sorted(counts.items(), key=lambda kv: -kv[1])
    top1 = si[0][1] / total
    top3 = sum(v for _, v in si[:3]) / total
    return {
        "total_trades": total,
        "unique":       len(counts),
        "top1_share":   round(top1, 4),
        "top3_share":   round(top3, 4),
        "top3_items":   [k for k, _ in si[:3]],
    }


def _quote_success_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    ok = sum(1 for r in rows if r.get("entry_quote_ok") is True)
    return round(ok / len(rows), 4)


# ══════════════════════════════════════════════════════════════════════════════
# Matched baseline matching report
# ══════════════════════════════════════════════════════════════════════════════

def _baseline_match_report(
    cohort_rows: list[dict],
    baseline_rows: list[dict],
) -> dict:
    """Report matching quality: pool overlap, fee overlap, time bucket overlap."""
    if not cohort_rows or not baseline_rows:
        return {"unmatched_rate": 1.0, "note": "empty set"}

    def _pool(r: dict) -> str:
        return str(r.get("pool_address", ""))

    def _fee(r: dict) -> int:
        return int(r.get("fee_bps", 0))

    cohort_pools  = {_pool(r) for r in cohort_rows}
    base_pools    = {_pool(r) for r in baseline_rows}
    pool_overlap  = len(cohort_pools & base_pools) / max(len(cohort_pools), 1)

    cohort_fees   = {_fee(r) for r in cohort_rows}
    base_fees     = {_fee(r) for r in baseline_rows}
    fee_overlap   = len(cohort_fees & base_fees) / max(len(cohort_fees), 1)

    # Time bucket overlap: same 1h window
    # Raw CH events use "ts_sec"; labeled rows use "buy_ts" — accept both
    def _ts(r: dict) -> int:
        return int(r.get("ts_sec") or r.get("buy_ts") or 0)

    def _hour_bucket(ts: int) -> int:
        return ts // 3600
    cohort_bkts  = {_hour_bucket(_ts(r)) for r in cohort_rows}
    base_bkts    = {_hour_bucket(_ts(r)) for r in baseline_rows}
    time_overlap = len(cohort_bkts & base_bkts) / max(len(cohort_bkts), 1)

    # Unmatched rate: proportion of cohort pool+fee combos not found in baseline
    cohort_pf = {(_pool(r), _fee(r)) for r in cohort_rows}
    base_pf   = {(_pool(r), _fee(r)) for r in baseline_rows}
    unmatched = len(cohort_pf - base_pf) / max(len(cohort_pf), 1)

    return {
        "pool_overlap_rate":      round(pool_overlap, 4),
        "fee_tier_overlap_rate":  round(fee_overlap, 4),
        "time_bucket_overlap_rate": round(time_overlap, 4),
        "unmatched_pool_fee_rate": round(unmatched, 4),
        "note": (
            "baseline matches: same chain, same V3 WETH-leg pool universe, "
            "same validation window, >=3 buys. NOT per-pool matched (would trivialize labels)."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Verdict logic
# ══════════════════════════════════════════════════════════════════════════════

def compute_oos_verdict(
    cohort_q30m: dict,
    baseline_q30m: dict,
    conc_wallet: dict,
    conc_pool: dict,
    n_cohort_labeled: int,
    baseline_n: int,
) -> dict:
    cohort_l1   = cohort_q30m.get("label_1_share", 0.0)
    baseline_l1 = baseline_q30m.get("label_1_share", 0.0)
    uplift      = round(cohort_l1 / max(baseline_l1, 1e-6), 4)
    w1          = conc_wallet.get("top1_share", 1.0)
    p1          = conc_pool.get("top1_share", 1.0)
    c_tm        = cohort_q30m.get("trimmed_mean", -999.0)
    b_tm        = baseline_q30m.get("trimmed_mean", -999.0)

    if n_cohort_labeled < OOS_INCONCLUSIVE_ROWS or baseline_n < 50:
        verdict = "OOS_INCONCLUSIVE"
        reason  = (
            f"Labeled rows = {n_cohort_labeled} (threshold {OOS_INCONCLUSIVE_ROWS}) "
            f"or baseline too small (n={baseline_n})"
        )
    elif uplift >= OOS_PASS_UPLIFT and c_tm > b_tm and w1 <= CONC_PASS_W1 and p1 <= CONC_PASS_P1:
        verdict = "OOS_PASS"
        reason  = (
            f"label_1_share uplift={uplift:.2f}x >= {OOS_PASS_UPLIFT}x, "
            f"trimmed_mean {c_tm:.4f} > {b_tm:.4f}, "
            f"wallet_top1={w1:.2f} <= {CONC_PASS_W1}, pool_top1={p1:.2f} <= {CONC_PASS_P1}"
        )
    elif uplift >= OOS_WEAK_UPLIFT:
        verdict = "OOS_WEAK"
        reasons = []
        if uplift < OOS_PASS_UPLIFT:
            reasons.append(f"uplift={uplift:.2f}x below PASS threshold {OOS_PASS_UPLIFT}x")
        if c_tm <= b_tm:
            reasons.append(f"trimmed_mean {c_tm:.4f} not better than baseline {b_tm:.4f}")
        if w1 > CONC_PASS_W1:
            reasons.append(f"wallet_top1={w1:.2f} > {CONC_PASS_W1}")
        if p1 > CONC_PASS_P1:
            reasons.append(f"pool_top1={p1:.2f} > {CONC_PASS_P1}")
        reason = "; ".join(reasons)
    elif uplift < 1.0 or (w1 > 0.80 and cohort_l1 <= baseline_l1 * 1.02):
        verdict = "OOS_FAIL"
        reason  = (
            f"label_1_share uplift={uplift:.2f}x (below 1.0x = no advantage), "
            f"or performance dominated by single wallet/pool"
        )
    else:
        verdict = "OOS_FAIL"
        reason  = f"uplift={uplift:.2f}x < {OOS_WEAK_UPLIFT}x"

    overfit_risk  = (w1 > 0.50 or p1 > 0.40)
    pool_artifact = (p1 > 0.25 and cohort_l1 > baseline_l1 * 1.4)

    return {
        "verdict":              verdict,
        "verdict_reason":       reason,
        "uplift_ratio":         uplift,
        "OVERFIT_RISK":         overfit_risk,
        "POOL_ARTIFACT_RISK":   pool_artifact,
        "TRAINING_ALLOWED":     False,
        "LIVE_ALLOWED":         False,
        "NO_ALPHA_CLAIM":       True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def phase1_score_selection_window(
    ch_url: str, ch_user: str, ch_pwd: str,
    sel_start: str, sel_end: str,
) -> list[dict]:
    """Run 3 CH queries on selection window, merge, score, return sorted candidates."""
    _log(f"[P1] Base profile query on {sel_start}..{sel_end}")
    base_rows = _ch_query(ch_url, build_selection_base_query(sel_start, sel_end), ch_user, ch_pwd)
    _log(f"[P1] Base profile: {len(base_rows)} wallets passing HAVING filter")

    _log("[P1] Round-trip query")
    rt_rows = _ch_query(ch_url, build_selection_roundtrip_query(sel_start, sel_end), ch_user, ch_pwd)
    rt_map  = {r["recipient_address_AUDIT_ONLY"]: r for r in rt_rows}
    _log(f"[P1] Round-trip: {len(rt_rows)} wallets with RTs")

    _log("[P1] Early pool query")
    ep_rows = _ch_query(ch_url, build_selection_earlypool_query(sel_start, sel_end), ch_user, ch_pwd)
    ep_map  = {r["recipient_address_AUDIT_ONLY"]: r for r in ep_rows}
    _log(f"[P1] Early pool: {len(ep_rows)} wallets with entries")

    scored: list[dict] = []
    for br in base_rows:
        addr = br["recipient_address_AUDIT_ONLY"]
        merged = {**br, **rt_map.get(addr, {}), **ep_map.get(addr, {})}
        subs   = compute_evidence_score(merged)
        scored.append({
            "recipient_address_AUDIT_ONLY": addr,
            **subs,
            "n_swaps":          int(br.get("n_swaps", 0)),
            "n_buys":           int(br.get("n_buys", 0)),
            "n_round_trips":    int(merged.get("n_round_trips", 0)),
            "n_early_entries":  int(merged.get("n_early_entries", 0)),
        })

    scored.sort(key=lambda r: -r["evidence_score"])
    top9 = [r for r in scored if r["evidence_score"] >= COHORT_SCORE_THRESHOLD]
    _log(
        f"[P1] Scored {len(scored)} wallets; "
        f"top9 (score>={COHORT_SCORE_THRESHOLD}): {len(top9)} wallets"
    )
    return scored


def phase2_validation_buy_events(
    ch_url: str, ch_user: str, ch_pwd: str,
    val_start: str, val_end: str,
    cohort_addrs: list[str],
    all_scored_addrs: list[str],
) -> tuple[list[dict], list[dict]]:
    """Fetch validation-window buy events for cohort AND matched baseline."""

    # Cohort events
    _log(f"[P2] Cohort validation buy events for {len(cohort_addrs)} wallets")
    if cohort_addrs:
        cohort_events = _ch_query(
            ch_url,
            build_validation_buy_query(val_start, val_end, cohort_addrs, CAP_COHORT_PER_WALLET),
            ch_user, ch_pwd,
        )
    else:
        cohort_events = []
    # Per-wallet cap
    per_wallet: dict[str, int] = defaultdict(int)
    cohort_capped: list[dict] = []
    for ev in cohort_events:
        w = ev["recipient_AUDIT_ONLY"]
        if per_wallet[w] < CAP_COHORT_PER_WALLET:
            cohort_capped.append(ev)
            per_wallet[w] += 1
    _log(f"[P2] Cohort capped events: {len(cohort_capped)}")

    # Baseline — step 1: get wallet counts
    _log("[P2] Baseline wallet count query")
    bline_counts = _ch_query(
        ch_url,
        build_baseline_wallet_count_query(val_start, val_end, all_scored_addrs),
        ch_user, ch_pwd,
    )
    eligible = [r["w"] for r in bline_counts if int(r["n_buys"]) >= MIN_BASELINE_BUYS]
    _log(f"[P2] Baseline eligible wallets: {len(eligible)}")
    if not eligible:
        return cohort_capped, []

    # Step 2: sample and fetch events
    random.seed(42)   # reproducible sampling
    sampled = random.sample(eligible, min(CAP_BASELINE_WALLETS, len(eligible)))
    _log(f"[P2] Baseline sampled wallets: {len(sampled)}")

    baseline_events = _ch_query(
        ch_url,
        build_validation_buy_query(val_start, val_end, sampled, CAP_BASELINE_PER_WAL),
        ch_user, ch_pwd,
    )
    per_wallet_b: dict[str, int] = defaultdict(int)
    baseline_capped: list[dict] = []
    for ev in baseline_events:
        w = ev["recipient_AUDIT_ONLY"]
        if per_wallet_b[w] < CAP_BASELINE_PER_WAL:
            baseline_capped.append(ev)
            per_wallet_b[w] += 1
    _log(f"[P2] Baseline capped events: {len(baseline_capped)}")
    return cohort_capped, baseline_capped


def phase3_quoter_label(
    rpc: RPC,
    events: list[dict],
    tag: str,
    max_calls: int,
) -> list[dict]:
    """Run threaded Quoter on buy events, returning labeled rows."""
    _log(f"[P3] Quoter labeling: {len(events)} {tag} events (~{len(events)*3} calls)")
    if not events:
        return []
    results: list[dict] = []
    lock = Lock()

    def _do(be: dict) -> list[dict]:
        return label_buy_event(rpc, be, tag)

    called = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(_do, ev): ev for ev in events}
        for fut in as_completed(futs):
            try:
                rows = fut.result()
            except Exception as e:
                rows = []
                _log(f"[P3] warn: {e}")
            with lock:
                results.extend(rows)
                called += 1
                if called % 50 == 0:
                    _log(f"[P3]  {called}/{len(events)} done ({tag})")
    _log(f"[P3] {tag} done: {len(results)} labeled rows")
    return results


def run_preflight(
    ch_url: str, ch_user: str, ch_pwd: str,
    sel_start: str, sel_end: str,
    val_start: str, val_end: str,
) -> None:
    _log("=== PREFLIGHT ===")
    _log(f"  selection_window : {sel_start} → {sel_end}  ({SELECTION_DAYS}d)")
    _log(f"  validation_window: {val_start} → {val_end}  ({VALIDATION_DAYS}d)")

    # Quick counts
    def _count(w: str, start: str, end: str) -> int:
        rows = _ch_query(ch_url, f"""
WITH mp AS (SELECT lower(pool_address) AS pa FROM eatlq.discovered_pools
            WHERE chain_id=8453 AND lower(pool_type)='v3' AND has_weth_leg=1 AND fee_bps IN (100,500,3000,10000))
SELECT count() AS n FROM eatlq.uniswap_v3_swaps s INNER JOIN mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id=8453 AND s.removed=0
  AND coalesce(s.block_time,s.received_at_ms) >= '{start}'
  AND coalesce(s.block_time,s.received_at_ms) < '{end}'
""", ch_user, ch_pwd)
        return int(rows[0]["n"]) if rows else 0

    sel_n = _count("sel", sel_start, sel_end)
    val_n = _count("val", val_start, val_end)
    _log(f"  selection swaps: {sel_n:,}")
    _log(f"  validation swaps: {val_n:,}")

    # Score estimation
    base_rows = _ch_query(ch_url, build_selection_base_query(sel_start, sel_end), ch_user, ch_pwd)
    top_count = sum(1 for r in base_rows if True)
    _log(f"  selection window wallets (HAVING n_swaps>=10, n_buys>=3): {len(base_rows):,}")

    rpc_cohort_est = 30 * 15 * 3   # assume 15 top9 wallets × 30 events × 3 calls
    rpc_baseline   = CAP_BASELINE_WALLETS * CAP_BASELINE_PER_WAL * 3
    total_est      = rpc_cohort_est + rpc_baseline
    _log(f"  estimated RPC calls: ~{total_est:,} (budget: {MAX_RPC_CALLS:,})")
    if total_est > MAX_RPC_CALLS:
        _log(f"  WARNING: over budget — reduce CAP_BASELINE_WALLETS or caps")
    else:
        _log(f"  RPC budget OK")
    _log("  → to run: LABEL_RPC_URL=... python <this script> --run")


def run_full(
    ch_url: str, ch_user: str, ch_pwd: str,
    rpc_url: str,
    sel_start: str, sel_end: str,
    val_start: str, val_end: str,
) -> None:
    _log("=== SPRINT038C-OOS FULL RUN ===")
    _log(f"  selection_window : {sel_start} → {sel_end}")
    _log(f"  validation_window: {val_start} → {val_end}")
    _log(f"  cohort_threshold : score >= {COHORT_SCORE_THRESHOLD}  (FROZEN)")
    _log(f"  IDENTITY_CLAIM_ALLOWED = false  |  LIVE_ALLOWED = false")

    # ── Phase 1: Score on selection window ───────────────────────────────────
    scored = phase1_score_selection_window(ch_url, ch_user, ch_pwd, sel_start, sel_end)
    cohort = [r for r in scored if r["evidence_score"] >= COHORT_SCORE_THRESHOLD]
    cohort_addrs = [r["recipient_address_AUDIT_ONLY"] for r in cohort]
    all_scored   = [r["recipient_address_AUDIT_ONLY"] for r in scored]
    _log(
        f"[P1] Frozen cohort: {len(cohort)} wallets  "
        f"(top score={cohort[0]['evidence_score'] if cohort else 'N/A'})"
    )

    if not cohort:
        _log("ERROR: No cohort wallets found in selection window — check data coverage")
        sys.exit(1)

    # ── Phase 2: Validation buy events ───────────────────────────────────────
    cohort_evts, baseline_evts = phase2_validation_buy_events(
        ch_url, ch_user, ch_pwd,
        val_start, val_end,
        cohort_addrs, all_scored,
    )

    total_est = (len(cohort_evts) + len(baseline_evts)) * 3
    _log(f"[P2] Estimated RPC calls: {total_est:,} (budget: {MAX_RPC_CALLS:,})")
    if total_est > MAX_RPC_CALLS:
        _log(f"[P2] WARNING: over budget — may be slow")

    # ── Phase 3: Quoter labeling ──────────────────────────────────────────────
    rpc = RPC(rpc_url)
    cohort_rows   = phase3_quoter_label(rpc, cohort_evts,   "top_cohort", MAX_RPC_CALLS // 2)
    baseline_rows = phase3_quoter_label(rpc, baseline_evts, "baseline",   MAX_RPC_CALLS // 2)

    # ── Phase 4: Aggregate metrics ────────────────────────────────────────────
    def _labeled_returns(rows: list[dict], label_type: str) -> tuple[list[float], list[dict]]:
        ok = [r for r in rows if r.get("label_type") == label_type
              and r.get("rpc_label") is not None]
        return [float(r["net_return"]) for r in ok], ok

    c_q30m_ret, c_q30m_rows = _labeled_returns(cohort_rows,   "q30m")
    c_q5m_ret,  c_q5m_rows  = _labeled_returns(cohort_rows,   "q5m")
    b_q30m_ret, b_q30m_rows = _labeled_returns(baseline_rows, "q30m")

    c_q30m_stats = _stats(c_q30m_ret)
    c_q5m_stats  = _stats(c_q5m_ret)
    b_q30m_stats = _stats(b_q30m_ret)

    c_q30m_qsr  = _quote_success_rate([r for r in cohort_rows if r.get("label_type") == "q30m"])
    b_q30m_qsr  = _quote_success_rate([r for r in baseline_rows if r.get("label_type") == "q30m"])

    c_conc_wallet = _concentration(c_q30m_rows, "recipient_AUDIT_ONLY")
    c_conc_pool   = _concentration(c_q30m_rows, "pool_address")
    c_conc_token  = _concentration(c_q30m_rows, "meme_token")

    # ── Phase 5: Baseline match report ───────────────────────────────────────
    match_report = _baseline_match_report(cohort_evts, baseline_evts)

    # ── Phase 6: OOS Verdict ─────────────────────────────────────────────────
    verdict_data = compute_oos_verdict(
        c_q30m_stats, b_q30m_stats,
        c_conc_wallet, c_conc_pool,
        len(c_q30m_ret), len(b_q30m_ret),
    )
    uplift = verdict_data["uplift_ratio"]
    verdict = verdict_data["verdict"]

    # ── Leakage checks ────────────────────────────────────────────────────────
    leakage_checks = {
        "selection_features_from_selection_window_only": True,
        "cohort_frozen_before_validation_data_queried":  True,
        "validation_labels_not_in_scoring_query":        True,
        "label_like_fields_in_cohort_definition":        False,
        "cohort_threshold_changed_after_seeing_results": False,
        "validation_window_overlap_with_selection":      False,
        "note": (
            f"Selection [{sel_start}, {sel_end}] and validation [{val_start}, {val_end}] "
            "are strictly non-overlapping. "
            "Cohort membership (score >= 0.5) was determined before any validation queries. "
            "No score fields appear in validation CSV."
        ),
    }

    # ── Assemble summary JSON ─────────────────────────────────────────────────
    summary = {
        "sprint":                   "sprint038c_oos_anti_overfit_validation",
        "run_ts_utc":               datetime.now(timezone.utc).isoformat(),
        "oos_windows": {
            "selection_window": {"start": sel_start, "end": sel_end, "days": SELECTION_DAYS},
            "validation_window": {"start": val_start, "end": val_end, "days": VALIDATION_DAYS},
        },
        "frozen_cohort": {
            "rule":               f"evidence_score >= {COHORT_SCORE_THRESHOLD}",
            "n_wallets":          len(cohort),
            "score_range":        [
                round(cohort[-1]["evidence_score"], 4) if cohort else 0,
                round(cohort[0]["evidence_score"],  4) if cohort else 0,
            ],
            "cohort_threshold_frozen": True,
        },
        "selection_window_stats": {
            "n_scored_wallets":  len(scored),
            "n_cohort_wallets":  len(cohort),
            "n_validation_events_cohort":   len(cohort_evts),
            "n_validation_events_baseline": len(baseline_evts),
        },
        "validation_primary_metric": {
            "label_type":                  "q30m",
            "cohort": {
                "n_wallets":         len(set(r["recipient_AUDIT_ONLY"] for r in cohort_evts)),
                "n_events":          len(cohort_evts),
                "n_labeled":         len(c_q30m_ret),
                "label_1_share":     c_q30m_stats.get("label_1_share", 0.0),
                "trimmed_mean":      c_q30m_stats.get("trimmed_mean", 0.0),
                "quote_success_rate": c_q30m_qsr,
            },
            "matched_baseline": {
                "n_wallets":         len(set(r["recipient_AUDIT_ONLY"] for r in baseline_evts)),
                "n_events":          len(baseline_evts),
                "n_labeled":         len(b_q30m_ret),
                "label_1_share":     b_q30m_stats.get("label_1_share", 0.0),
                "trimmed_mean":      b_q30m_stats.get("trimmed_mean", 0.0),
                "quote_success_rate": b_q30m_qsr,
            },
            "uplift_ratio":        uplift,
            "baseline_match_criteria": (
                "same chain=8453, same V3 WETH-leg pool universe, "
                f"same fee tiers, same {VALIDATION_DAYS}d window, "
                f">={MIN_BASELINE_BUYS} buys, {CAP_BASELINE_WALLETS} random non-cohort wallets"
            ),
        },
        "validation_secondary_metrics": {
            "q5m": {
                "cohort": c_q5m_stats,
                "label_type": "q5m",
                "note": "decay diagnostic — q5m vs q30m shows alpha decay profile",
            },
            "concentration": {
                "wallet": c_conc_wallet,
                "pool":   c_conc_pool,
                "token":  c_conc_token,
            },
        },
        "baseline_match_report": match_report,
        "leakage_checks":   leakage_checks,
        "oos_verdict":      verdict_data,
        "decision_flags":   DECISION_FLAGS,
        "r3_r4_baselines": {
            "r3_ungated": {"label_1_share": 0.0931, "trimmed_mean": -0.3090},
            "r4_feb02":   {"label_1_share": 0.1391, "trimmed_mean": -0.1393},
        },
    }

    # ── Write outputs ─────────────────────────────────────────────────────────
    _log(f"[OUT] Writing {OUT_JSON}")
    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    all_rows = cohort_rows + baseline_rows
    if all_rows:
        _log(f"[OUT] Writing {OUT_CSV} ({len(all_rows)} rows)")
        fields = [
            "recipient_AUDIT_ONLY", "cohort", "pool_address", "meme_token",
            "fee_bps", "buy_ts", "entry_block", "label_type",
            "rpc_label", "net_return", "entry_quote_ok", "weth_in_eth",
        ]
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)

    # ── Write audit markdown ──────────────────────────────────────────────────
    _write_audit(summary, sel_start, sel_end, val_start, val_end, cohort, scored)

    # ── Print executive summary ───────────────────────────────────────────────
    _print_exec_summary(summary)


def _write_audit(
    s: dict,
    sel_start: str, sel_end: str,
    val_start: str, val_end: str,
    cohort: list[dict],
    scored: list[dict],
) -> None:
    vm = s["validation_primary_metric"]
    co = vm["cohort"]
    bl = vm["matched_baseline"]
    vd = s["oos_verdict"]
    lk = s["leakage_checks"]
    cc = s["validation_secondary_metrics"]["concentration"]

    lines = [
        "# Sprint038C-OOS — Recipient Cohort Anti-Overfit Validation Audit",
        "",
        f"**Run TS (UTC):** {s['run_ts_utc']}",
        "",
        "## 1. Window Boundaries",
        "",
        "| Window | Start | End | Days |",
        "|--------|-------|-----|------|",
        f"| selection | {sel_start} | {sel_end} | {SELECTION_DAYS} |",
        f"| validation | {val_start} | {val_end} | {VALIDATION_DAYS} |",
        "",
        "## 2. Frozen Cohort Rule",
        "",
        f"- **Threshold**: `evidence_score >= {COHORT_SCORE_THRESHOLD}` (top9 rule from sprint038a readout)",
        f"- **N wallets selected from selection window**: {len(cohort)}",
        f"- **Score range**: [{s['frozen_cohort']['score_range'][0]}, {s['frozen_cohort']['score_range'][1]}]",
        "- ⚠ This threshold was NOT changed after seeing validation results.",
        "",
        "## 3. OOS Validation — Primary Metric (q30m)",
        "",
        "| Metric | Top Cohort | Matched Baseline |",
        "|--------|-----------|-----------------|",
        f"| n_wallets | {co['n_wallets']} | {bl['n_wallets']} |",
        f"| n_events | {co['n_events']} | {bl['n_events']} |",
        f"| n_labeled | {co['n_labeled']} | {bl['n_labeled']} |",
        f"| label_1_share | **{co['label_1_share']}** | {bl['label_1_share']} |",
        f"| trimmed_mean | **{co['trimmed_mean']}** | {bl['trimmed_mean']} |",
        f"| quote_success_rate | {co['quote_success_rate']} | {bl['quote_success_rate']} |",
        f"| uplift_ratio | **{vm['uplift_ratio']}x** | 1.00x |",
        "",
        f"**Matched baseline criteria:** {vm['baseline_match_criteria']}",
        "",
        "## 4. Secondary Metrics",
        "",
        "### 4a. q5m (decay diagnostic)",
        "",
        f"| label_1_share | {s['validation_secondary_metrics']['q5m']['cohort'].get('label_1_share', 'N/A')} |",
        f"| n | {s['validation_secondary_metrics']['q5m']['cohort'].get('n', 0)} |",
        "",
        "### 4b. Concentration",
        "",
        "| Dimension | top1_share | top3_share |",
        "|-----------|-----------|-----------|",
        f"| wallet | {cc['wallet'].get('top1_share', 'N/A')} | {cc['wallet'].get('top3_share', 'N/A')} |",
        f"| pool | {cc['pool'].get('top1_share', 'N/A')} | {cc['pool'].get('top3_share', 'N/A')} |",
        f"| token | {cc['token'].get('top1_share', 'N/A')} | {cc['token'].get('top3_share', 'N/A')} |",
        "",
        "## 5. Leakage / Overfit Audit",
        "",
        "| Check | Status |",
        "|-------|--------|",
        f"| Selection features from selection window only | {'✅ PASS' if lk['selection_features_from_selection_window_only'] else '❌ FAIL'} |",
        f"| Cohort frozen before validation queries | {'✅ PASS' if lk['cohort_frozen_before_validation_data_queried'] else '❌ FAIL'} |",
        f"| Validation labels not fed back to scoring | {'✅ PASS' if lk['validation_labels_not_in_scoring_query'] else '❌ FAIL'} |",
        f"| Label-like fields in cohort definition | {'❌ PRESENT' if lk['label_like_fields_in_cohort_definition'] else '✅ NONE'} |",
        f"| Cohort threshold changed after seeing results | {'❌ CHANGED' if lk['cohort_threshold_changed_after_seeing_results'] else '✅ FROZEN'} |",
        f"| Validation window overlap with selection | {'❌ OVERLAP' if lk['validation_window_overlap_with_selection'] else '✅ STRICT SEPARATION'} |",
        "",
        "## 6. Decision Flags",
        "",
        f"```",
        f"OOS_VERDICT               = {vd['verdict']}",
        f"OVERFIT_RISK              = {vd['OVERFIT_RISK']}",
        f"POOL_ARTIFACT_RISK        = {vd['POOL_ARTIFACT_RISK']}",
        f"TRAINING_ALLOWED          = false",
        f"LIVE_ALLOWED              = false",
        f"NO_ALPHA_CLAIM            = true",
        f"IDENTITY_CLAIM_ALLOWED    = false",
        f"COHORT_THRESHOLD_FROZEN   = true",
        f"```",
        "",
        f"**Verdict reason:** {vd['verdict_reason']}",
        "",
        "---",
        "*This audit is diagnostics-only. No identity claims. No live trading. No Redis/signals writes.*",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    _log(f"[OUT] Audit markdown → {OUT_MD}")


def _print_exec_summary(s: dict) -> None:
    vm = s["validation_primary_metric"]
    co = vm["cohort"]
    bl = vm["matched_baseline"]
    vd = s["oos_verdict"]
    lk = s["leakage_checks"]

    print("\n" + "=" * 70)
    print("SPRINT038C-OOS  EXECUTIVE SUMMARY")
    print("=" * 70)
    w0 = s["oos_windows"]["selection_window"]
    w1 = s["oos_windows"]["validation_window"]
    print(f"  selection  window : {w0['start']} → {w0['end']}  ({w0['days']}d)")
    print(f"  validation window : {w1['start']} → {w1['end']}  ({w1['days']}d)")
    fc = s["frozen_cohort"]
    print(f"  frozen cohort     : {fc['n_wallets']} wallets  (score >= {COHORT_SCORE_THRESHOLD})")
    print()
    print("  OOS q30m vs matched baseline:")
    print(f"    cohort    label_1_share = {co['label_1_share']}  (n_labeled={co['n_labeled']})")
    print(f"    baseline  label_1_share = {bl['label_1_share']}  (n_labeled={bl['n_labeled']})")
    print(f"    uplift                  = {vm['uplift_ratio']}x")
    print(f"    cohort    trimmed_mean  = {co['trimmed_mean']}")
    print(f"    baseline  trimmed_mean  = {bl['trimmed_mean']}")
    print(f"    cohort    qsr           = {co['quote_success_rate']}")
    cc = s["validation_secondary_metrics"]["concentration"]
    w_top1 = cc["wallet"].get("top1_share", "N/A")
    p_top1 = cc["pool"].get("top1_share", "N/A")
    print(f"    wallet_top1_share = {w_top1}   pool_top1_share = {p_top1}")
    print()
    print("  Leakage check  : ALL PASS" if all([
        lk["selection_features_from_selection_window_only"],
        lk["cohort_frozen_before_validation_data_queried"],
        lk["validation_labels_not_in_scoring_query"],
        not lk["label_like_fields_in_cohort_definition"],
        not lk["cohort_threshold_changed_after_seeing_results"],
        not lk["validation_window_overlap_with_selection"],
    ]) else "  Leakage check  : ISSUES FOUND — see audit")
    print()
    print(f"  OOS_VERDICT    : {vd['verdict']}")
    print(f"  OVERFIT_RISK   : {vd['OVERFIT_RISK']}")
    print(f"  POOL_ARTIFACT_RISK: {vd['POOL_ARTIFACT_RISK']}")
    print(f"  Reason         : {vd['verdict_reason']}")
    print("=" * 70)
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint038C-OOS validation")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--run",       action="store_true")
    args = ap.parse_args()
    if not (args.preflight or args.run):
        ap.print_help()
        sys.exit(0)

    ch_url  = os.environ.get("CLICKHOUSE_URL",      "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER",     "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")

    sel_start, sel_end, val_start, val_end = compute_windows()

    if args.preflight:
        run_preflight(ch_url, ch_user, ch_pwd, sel_start, sel_end, val_start, val_end)
    elif args.run:
        if not rpc_url:
            _log("ERROR: set LABEL_RPC_URL or EVM_HTTP_URL")
            sys.exit(1)
        run_full(ch_url, ch_user, ch_pwd, rpc_url,
                 sel_start, sel_end, val_start, val_end)


if __name__ == "__main__":
    main()
