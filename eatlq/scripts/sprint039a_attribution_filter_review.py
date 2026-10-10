#!/usr/bin/env python3
"""Sprint039A — Whitepaper-to-Chain Attribution Filter Review (analysis only)
=============================================================================

Read-only analysis of Sprint038A/B/C filter vs. LASZLO whitepaper v1/v2 +
Architecture + Operations + Code-Review.  No CH queries, no RPC, no writes
to Redis/dashcam/signals.  Loads the existing sprint038a candidate CSV,
computes filter sensitivity, re-ranks candidates by 3 attribution routes,
and emits:
  mvp/audits/sprint039a_whitepaper_to_chain_attribution_filter_review.md
  mvp/diagnostics/sprint039a_filter_sensitivity.json
  mvp/diagnostics/sprint039a_candidate_attribution_ranking.csv

DECISION FLAGS (frozen)
-----------------------
  IDENTITY_CLAIM_ALLOWED        = false
  TRAINING_ALLOWED              = false
  LIVE_ALLOWED                  = false
  MODEL_PROMOTION_ALLOWED       = false
  ATTRIBUTION_HYPOTHESIS_ALLOWED= true
  NO_ALPHA_CLAIM                = true
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Frozen flags ──────────────────────────────────────────────────────────────
DECISION_FLAGS = {
    "IDENTITY_CLAIM_ALLOWED":        False,
    "TRAINING_ALLOWED":              False,
    "LIVE_ALLOWED":                  False,
    "MODEL_PROMOTION_ALLOWED":       False,
    "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
    "NO_ALPHA_CLAIM":                True,
    "FILTER_REVIEW_ROUND":           "sprint039a",
}

REPO         = Path(__file__).resolve().parents[1]
CSV_IN       = REPO / "mvp/diagnostics/sprint038a_recipient_candidate_summary.csv"
OUT_MD       = REPO / "mvp/audits/sprint039a_whitepaper_to_chain_attribution_filter_review.md"
OUT_JSON     = REPO / "mvp/diagnostics/sprint039a_filter_sensitivity.json"
OUT_CSV      = REPO / "mvp/diagnostics/sprint039a_candidate_attribution_ranking.csv"
OUT_MD.parent.mkdir(parents=True, exist_ok=True)
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Whitepaper-to-chain evidence map (canonical, derived from author docs)
# ══════════════════════════════════════════════════════════════════════════════
EVIDENCE_MAP = [
    # (id, claim, source_doc, observability, notes)
    ("E01", "Chain = Base L2 (chain_id=8453)",
     "v2.md §1.4 / 外宣版 §1.4",
     "directly_observable", "tx receipts trivially verify"),
    ("E02", "Protocol = Uniswap V3, exactInputSingle via SwapRouter02",
     "v2.md §4.2 / 外宣版 §4.2",
     "directly_observable", "factory address / function selector identifies router"),
    ("E03", "Fee tiers 3000 / 10000 / 500 (no 100, no aero_cl)",
     "v2.md §1.4 / 外宣版 §1.4 §4.4",
     "directly_observable",
     "AUTHOR EXPLICITLY lists 3 tiers; 100 is NOT in author canon. aero_cl OUTSIDE Uniswap V3."),
    ("E04", "Quoter V2 contract = 0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
     "外宣版 §4.4.1",
     "not_observable",
     "eth_call leaves no on-chain footprint; only sender's egress logs would show probes"),
    ("E05", "Honeypot probe = 0.001 token via quoteExactInputSingle",
     "外宣版 §4.4.1",
     "not_observable",
     "eth_call only; misclassifying this as an on-chain '0.001 token' tx would be a filter bug"),
    ("E06", "Token registry: 11 tokens (WETH, USDC, USDbC, DAI, cbETH, AERO, DEGEN, WBTC, BRETT, TOSHI, OX)",
     "外宣版 §2.2 / 数据模型 / Layer1",
     "directly_observable",
     "wallet's traded token set should be a SUBSET of these (modulo new pool discovery)"),
    ("E07", "Base_Capital = $25 (calibrated from real wallet)",
     "v2.md §4.3 / 外宣版 §4.3",
     "inferable_from_behavior",
     "buy notional should cluster near $5-$25 = 0.0014-0.007 ETH at $3500/ETH"),
    ("E08", "Final_Score gate ≥ 0.40 dashcam, ≥ 0.52 signal",
     "v2.md §3.5 / 外宣版 §2.3 §3.5",
     "only_observable_repo_runtime",
     "model output not visible on-chain; cadence proxy = trade frequency"),
    ("E09", "MAX_SLIPPAGE_BPS = 300 (3%)",
     "v2.md §4.1 / 外宣版 §4.1",
     "inferable_from_behavior",
     "amountOutMinimum ratio in calldata reveals slippage — needs tx tracing"),
    ("E10", "maxPriorityFeePerGas = 1 wei (Base single Sequencer)",
     "v2.md §4.1 / 外宣版 §4.1",
     "directly_observable",
     "tx.maxPriorityFeePerGas via eth_getTransactionByHash"),
    ("E11", "Gas circuit breaker: baseFee > 5 gwei → reject",
     "外宣版 §4.1",
     "inferable_from_behavior",
     "absent during gas spikes — needs cross-ref to baseFee timeseries"),
    ("E12", "Position monitor: 2-second loop via slot0()",
     "v2.md §4.4 / 外宣版 §4.4",
     "not_observable",
     "read-only RPC, no on-chain trace"),
    ("E13", "Trailing stop: peak retract > 5% AND profitable",
     "v2.md §4.4 / 外宣版 §4.4",
     "inferable_from_behavior",
     "needs price-path reconstruction — Quoter playback"),
    ("E14", "Time stop: > 30 min AND ±2% sideways",
     "v2.md §4.4 / 外宣版 §4.4",
     "inferable_from_behavior",
     "hold_time ≈ 1800s ± few minutes is the strongest behavioral fingerprint"),
    ("E15", "Hard stop: -15%",
     "v2.md §4.4 / 外宣版 §4.4",
     "inferable_from_behavior",
     "needs entry/exit price ratio reconstruction"),
    ("E16", "WETH auto-withdraw after SELL (WETH.withdraw())",
     "外宣版 §4.2",
     "directly_observable",
     "tx logs show Withdrawal(address,uint256) event after V3 SELL — not probed in 038A/B/C"),
    ("E17", "Daily DD circuit breaker: 10% UTC reset",
     "v2.md §4.4 / 外宣版 §4.4",
     "inferable_from_behavior",
     "wallet halts mid-day after large loss — observable as activity gap"),
    ("E18", "Liquidity check: liquidity_eth > trade_size_eth × 20",
     "v2.md §4.4 / 外宣版 §4.4",
     "inferable_from_behavior",
     "but Code-Review.md notes this was BUGGY in v1; fix landed 2026-04-08"),
    ("E19", "Whale threshold: $100k USD (Smart Money proxy)",
     "v2.md §3.2 / 外宣版 §3.2",
     "directly_observable",
     "applies to OTHER wallets being scored, not the LASZLO bot itself"),
    ("E20", "Pool discovery: PoolCache via token0/token1/decimals/symbol",
     "外宣版 Layer1",
     "not_observable",
     "internal cache state"),
    ("E21", "Round-trip behavior: BUY then SELL within same pool",
     "v2.md §4.4",
     "directly_observable",
     "explicitly tested in sprint038a s_round_trip"),
    ("E22", "Sniper archetype: 0-3 blocks of pool deployment",
     "v2.md §3.2 / 外宣版 §3.2",
     "directly_observable",
     "block_number - pool_created_block ≤ 3"),
    ("E23", "Diamond Hand: avg hold > 30 days",
     "v2.md §3.2 / 外宣版 §3.2",
     "directly_observable",
     "OPPOSITE of LASZLO bot — used as exclusion / inversion"),
    ("E24", "Paper Hand: exit on +10%",
     "v2.md §3.2 / 外宣版 §3.2",
     "inferable_from_behavior",
     "not the LASZLO bot pattern; reverse indicator"),
    ("E25", "CIH entity clustering (v1)",
     "v1-continued §3.2",
     "directly_observable",
     "DROPPED in v2 narrative — not implemented in any sprint"),
    ("E26", "Volatility adjustment: max(0.3, 1.0 - vol*0.5)",
     "外宣版 §4.3",
     "inferable_from_behavior",
     "small-buy size in high-volatility windows"),
    ("E27", "v1 → v2 architectural drift: Mainnet/Flashbots → Base/no-Flashbots",
     "Architecture.md",
     "drifted_or_contradicted",
     "v1 conditions cannot be applied; v2 is the correct frame"),
    ("E28", "Executor language drift: 'Rust Sniper' narrative vs. Python runtime",
     "Code-Review.md / executor-language-contract.md",
     "drifted_or_contradicted",
     "doesn't affect on-chain fingerprint"),
    ("E29", "Feature Store prefix drift: fs: → feat:",
     "Architecture.md / 外宣版 Layer2",
     "drifted_or_contradicted",
     "internal Redis key only; not chain-observable"),
    ("E30", "Phase 6 narrative: paper-only data accumulation, no live in 028D failed_no_7d_alpha",
     "Operations.md / mvp/audits/failed_no_7d_alpha_guard.md",
     "drifted_or_contradicted",
     "if author respected this guard, a live LASZLO bot may NOT exist on-chain at all"),
]

# ══════════════════════════════════════════════════════════════════════════════
# Load Sprint038A candidate CSV
# ══════════════════════════════════════════════════════════════════════════════
def load_candidates() -> list[dict]:
    rows = []
    with open(CSV_IN, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            for k in ("evidence_score", "s_v3_activity", "s_fee_pattern", "s_notional",
                      "s_short_exit", "s_early_pool", "s_round_trip", "s_concentration",
                      "s_pnl_proxy", "avg_hold_sec", "p50_hold_sec", "f10_share"):
                try:
                    r[k] = float(r[k])
                except (KeyError, ValueError):
                    r[k] = 0.0
            for k in ("n_swaps", "n_distinct_pools", "n_distinct_meme_tokens",
                      "n_buys", "n_sells", "n_round_trips", "n_short_exits",
                      "n_fee_3000", "n_fee_10000", "n_fee_500", "n_fee_100",
                      "n_lasl_fee_swaps", "n_buys_notional_ok",
                      "n_early_entries", "n_sniper_entries"):
                try:
                    r[k] = int(r[k])
                except (KeyError, ValueError):
                    r[k] = 0
            rows.append(r)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Filter sensitivity analysis
# ══════════════════════════════════════════════════════════════════════════════
def filter_sensitivity(rows: list[dict]) -> dict:
    """Apply each strict whitepaper condition individually + cumulatively
    starting from sprint038a's top9 (score >= 0.5)."""

    base = [r for r in rows if r["evidence_score"] >= 0.5]
    n_base = len(base)

    # Individual filter conditions
    def cond_no_fee100(r): return r["n_fee_100"] / max(r["n_swaps"], 1) <= 0.10
    def cond_diverse_tokens(r): return r["n_distinct_meme_tokens"] >= 5
    def cond_lasl_holdtime(r):
        h = r["avg_hold_sec"]
        return 200.0 <= h <= 2400.0
    def cond_low_priority_gas(r): return r["f10_share"] >= 0.50
    def cond_has_roundtrips(r): return r["n_round_trips"] >= 3
    def cond_lasl_notional(r):
        return r["n_buys_notional_ok"] / max(r["n_buys"], 1) >= 0.5
    def cond_early_pool(r):
        return r["n_early_entries"] / max(r["n_distinct_pools"], 1) >= 0.3
    def cond_no_extreme_concentration(r):
        return r["n_distinct_pools"] >= 3 and r["n_distinct_meme_tokens"] >= 3
    def cond_dominant_fee_3k_or_10k(r):
        return (r["n_fee_3000"] + r["n_fee_10000"]) / max(r["n_swaps"], 1) >= 0.7

    conditions = [
        ("F01_no_fee100",
         "≤10% of swaps in fee=100 (whitepaper says 3000/10000/500 only)",
         cond_no_fee100),
        ("F02_diverse_tokens",
         "≥5 distinct meme tokens (11-token registry pattern)",
         cond_diverse_tokens),
        ("F03_lasl_holdtime",
         "200s ≤ avg_hold_sec ≤ 2400s (time-stop range, allowing some buffer)",
         cond_lasl_holdtime),
        ("F04_low_priority_gas",
         "f10_share ≥ 0.5 (maxPriorityFeePerGas ≤ 5 wei)",
         cond_low_priority_gas),
        ("F05_has_roundtrips",
         "≥3 round trips (LASZLO bot exits its positions)",
         cond_has_roundtrips),
        ("F06_lasl_notional",
         "≥50% of buys in $1-$52 LASZLO position size band",
         cond_lasl_notional),
        ("F07_early_pool",
         "≥30% of pools entered within +50 blocks of creation (Sniper)",
         cond_early_pool),
        ("F08_multi_pool_token",
         "≥3 pools AND ≥3 tokens (anti single-shot bot)",
         cond_no_extreme_concentration),
        ("F09_dominant_3k_10k",
         "≥70% of swaps in fee=3000 or 10000 (LASZLO meme pool tiers)",
         cond_dominant_fee_3k_or_10k),
    ]

    individual = []
    for fid, desc, fn in conditions:
        kept = [r for r in base if fn(r)]
        dropped = n_base - len(kept)
        individual.append({
            "filter_id": fid,
            "description": desc,
            "kept_count": len(kept),
            "dropped_from_base": dropped,
            "drop_pct": round(dropped / max(n_base, 1) * 100, 1),
            "kept_addresses_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in kept],
        })

    # Cumulative filter (whitepaper-strict)
    strict_filters = [
        ("F02_diverse_tokens",  cond_diverse_tokens),
        ("F03_lasl_holdtime",   cond_lasl_holdtime),
        ("F05_has_roundtrips",  cond_has_roundtrips),
        ("F08_multi_pool_token", cond_no_extreme_concentration),
        ("F09_dominant_3k_10k", cond_dominant_fee_3k_or_10k),
    ]
    cumulative = base[:]
    cumulative_steps = []
    for fid, fn in strict_filters:
        before = len(cumulative)
        cumulative = [r for r in cumulative if fn(r)]
        cumulative_steps.append({
            "step": fid,
            "before": before,
            "after": len(cumulative),
            "dropped": before - len(cumulative),
            "remaining_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in cumulative],
        })

    # Add F04 last as the killer condition
    before = len(cumulative)
    after_f04 = [r for r in cumulative if cond_low_priority_gas(r)]
    cumulative_steps.append({
        "step": "F04_low_priority_gas (KILLER — F10 was 0 for ALL top9)",
        "before": before,
        "after": len(after_f04),
        "dropped": before - len(after_f04),
        "remaining_AUDIT_ONLY": [r["recipient_address_AUDIT_ONLY"] for r in after_f04],
    })

    return {
        "n_total_scored": len(rows),
        "n_base_top9_score_ge_0_5": n_base,
        "individual_filter_sensitivity": individual,
        "cumulative_strict_route_steps": cumulative_steps,
        "strict_old_whitepaper_match_count_final": len(after_f04),
        "relaxed_old_whitepaper_match_count": len(cumulative),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Three-route candidate ranking
# ══════════════════════════════════════════════════════════════════════════════
def _safe_div(a, b, fb=0.0):
    return a / b if b > 0 else fb


def rank_old_whitepaper(r: dict) -> float:
    """Strictly faithful to LASZLO whitepaper v2 + 外宣版.
    Heavy weights on: 30-min hold, 11-token diversity, fee_3000/10000, round trips,
    no fee_100 contamination, presence of round trips."""
    h = r["avg_hold_sec"]
    hold_score = 1.0 if 200 <= h <= 2400 else (0.5 if 60 <= h < 200 or 2400 < h <= 7200 else 0.05)
    div_score  = min(r["n_distinct_meme_tokens"] / 7.0, 1.0)
    fee3k_share = _safe_div(r["n_fee_3000"] + r["n_fee_10000"], r["n_swaps"])
    fee100_penalty = _safe_div(r["n_fee_100"], r["n_swaps"])
    rt_score   = min(r["n_round_trips"] / 5.0, 1.0)
    early_score = min(_safe_div(r["n_early_entries"], r["n_distinct_pools"]) * 1.5, 1.0)
    notional_score = _safe_div(r["n_buys_notional_ok"], r["n_buys"])
    return round(
        0.25 * hold_score
        + 0.20 * div_score
        + 0.15 * fee3k_share
        + 0.15 * rt_score
        + 0.10 * early_score
        + 0.10 * notional_score
        + 0.05 * (1.0 - min(fee100_penalty * 2, 1.0)),
        4,
    )


def rank_upgraded_route(r: dict) -> float:
    """Allow fee=100 / fee=1 / aero_cl as legitimate evolution of the strategy.
    Down-weight fee tier specificity, keep notional + 30min hold + diversification."""
    h = r["avg_hold_sec"]
    hold_score = 1.0 if 200 <= h <= 3600 else (0.4 if 60 <= h < 200 or 3600 < h <= 14400 else 0.1)
    div_score  = min(r["n_distinct_meme_tokens"] / 5.0, 1.0)
    rt_score   = min(r["n_round_trips"] / 3.0, 1.0)
    activity   = min(r["n_swaps"] / 100.0, 1.0)
    notional_score = _safe_div(r["n_buys_notional_ok"], r["n_buys"])
    early_score = min(_safe_div(r["n_early_entries"], r["n_distinct_pools"]) * 1.5, 1.0)
    return round(
        0.25 * hold_score
        + 0.20 * div_score
        + 0.15 * rt_score
        + 0.15 * activity
        + 0.15 * notional_score
        + 0.10 * early_score,
        4,
    )


def rank_anti_tracking(r: dict) -> float:
    """Adversarial: author wants to avoid attribution.
    Reward: medium activity (not too high, not too low — 50-300 swaps),
    moderate diversification (not extreme), low concentration,
    consistent small notional, occasional early entries (not always)."""
    n = r["n_swaps"]
    activity_score = 1.0 if 50 <= n <= 300 else (0.5 if 20 <= n < 50 or 300 < n <= 800 else 0.1)
    div_score = 1.0 if 5 <= r["n_distinct_meme_tokens"] <= 30 else 0.4
    h = r["avg_hold_sec"]
    hold_score = 1.0 if 600 <= h <= 3600 else (0.5 if 200 <= h < 600 else 0.2)
    notional_score = _safe_div(r["n_buys_notional_ok"], r["n_buys"])
    rt_share = _safe_div(r["n_round_trips"] * 2, r["n_swaps"])
    rt_score = 1.0 if 0.20 <= rt_share <= 0.80 else 0.3
    return round(
        0.25 * activity_score
        + 0.20 * div_score
        + 0.20 * hold_score
        + 0.20 * notional_score
        + 0.15 * rt_score,
        4,
    )


def rerank_candidates(rows: list[dict], top_k: int = 25) -> list[dict]:
    base = [r for r in rows if r["evidence_score"] >= 0.4]
    out = []
    for r in base:
        out.append({
            "recipient_address_AUDIT_ONLY": r["recipient_address_AUDIT_ONLY"],
            "sprint038a_evidence_score":   r["evidence_score"],
            "old_whitepaper_score":         rank_old_whitepaper(r),
            "upgraded_route_score":         rank_upgraded_route(r),
            "anti_tracking_score":          rank_anti_tracking(r),
            "n_swaps":                      r["n_swaps"],
            "n_distinct_pools":             r["n_distinct_pools"],
            "n_distinct_meme_tokens":       r["n_distinct_meme_tokens"],
            "n_round_trips":                r["n_round_trips"],
            "avg_hold_sec":                 r["avg_hold_sec"],
            "p50_hold_sec":                 r["p50_hold_sec"],
            "n_fee_3000":                   r["n_fee_3000"],
            "n_fee_10000":                  r["n_fee_10000"],
            "n_fee_500":                    r["n_fee_500"],
            "n_fee_100":                    r["n_fee_100"],
            "f10_share":                    r["f10_share"],
        })
    # Combined rank for csv: sum of three ranks
    out.sort(key=lambda x: -(x["old_whitepaper_score"]
                              + x["upgraded_route_score"]
                              + x["anti_tracking_score"]))
    return out[:top_k]


# ══════════════════════════════════════════════════════════════════════════════
# Filter mismatch table
# ══════════════════════════════════════════════════════════════════════════════
FILTER_MISMATCHES = [
    {
        "id": "M01",
        "evidence": "E03 / fee tiers 3000/10000/500",
        "issue": "Sprint038A universe INCLUDES fee=100 (and excludes aero_cl).",
        "kind": "over_inclusion",
        "impact": (
            "fee=100 pools are NOT in author canon; including them admits non-LASZLO "
            "wallets like 0x4409 (713 fee=100 swaps). However, scoring still penalizes "
            "fee=100 (n_lasl_fee_swaps excludes fee=100), creating an inconsistency."
        ),
        "fix": "Drop fee=100 from meme_pools CTE; keep it only as a diagnostic 'upgraded route' lane.",
    },
    {
        "id": "M02",
        "evidence": "E06 / 11-token registry",
        "issue": "Sprint038A admits ANY meme token with WETH-leg V3 pool — no token-set restriction.",
        "kind": "missing_constraint",
        "impact": (
            "LASZLO bot trades only the 11-token registry; current filter admits "
            "thousands of unrelated tokens. n_distinct_meme_tokens includes 24+ "
            "different tokens for 0x667c — far beyond the 11-token registry."
        ),
        "fix": "Add hard filter: meme_token IN (DEGEN, BRETT, TOSHI, AERO, OX, etc.) "
               "OR softly weight 11-token coverage.",
    },
    {
        "id": "M03",
        "evidence": "E14 / time-stop = 30 min ± 2% sideways",
        "issue": "Sprint038A s_short_exit treats hold ≤ 1800s as 'short exit', "
                 "but rewards 1-second holds equally to 1500-second holds.",
        "kind": "incorrect_implementation",
        "impact": (
            "Top-1 candidate 0x667c has p50_hold = 8 SECONDS. That is a sniper-and-dump bot, "
            "NOT LASZLO (whose minimum hold for trailing-stop logic is multi-minute). "
            "Yet 0x667c gets s_short_exit = 0.5 and overall 0.691."
        ),
        "fix": (
            "Replace single threshold with a Gaussian centered at 1800s "
            "(width ~600s) so sub-60s and >7200s holds get penalty."
        ),
    },
    {
        "id": "M04",
        "evidence": "E16 / WETH.withdraw() after SELL",
        "issue": "Sprint038A audit MENTIONS F16 but never actually probes for "
                 "Withdrawal(address,uint256) events in tx logs.",
        "kind": "missing_implementation",
        "impact": (
            "F16 is one of the few directly observable LASZLO-specific signatures. "
            "Bypassing it leaves the filter under-specified. A retail aggregator user "
            "(via 1inch, Universal Router) typically does NOT auto-withdraw WETH→ETH; "
            "a LASZLO bot WOULD."
        ),
        "fix": "Sprint039B/C: add eth_getTransactionReceipt scan for WETH.withdraw() event.",
    },
    {
        "id": "M05",
        "evidence": "E10 / maxPriorityFeePerGas = 1 wei",
        "issue": "Sprint038A samples up to 1000 txs but result f10_share = 0 for ALL top9.",
        "kind": "negative_evidence",
        "impact": (
            "Either (a) the live LASZLO bot does not currently exist on-chain, "
            "(b) typical wallet UI overrides 1-wei intent (MetaMask defaults), or "
            "(c) the filter is missing the actual wallet. Cannot distinguish without "
            "broader probe."
        ),
        "fix": "Treat F10 as STRONG positive evidence (rare on-chain), but do not "
               "penalize its absence — the bot may not be live.",
    },
    {
        "id": "M06",
        "evidence": "Sprint038B ROUND_TRIP_COST",
        "issue": "ROUND_TRIP_COST hardcoded to 0.6% regardless of pool fee tier.",
        "kind": "incorrect_implementation",
        "impact": (
            "fee=10000 pool round-trip costs 2% in fees alone. Sprint038B systematically "
            "over-counts label_1 for fee=10000-heavy cohorts (which IS the LASZLO bias). "
            "True round-trip viability may be lower than reported."
        ),
        "fix": "Sprint039B: parameterize ROUND_TRIP_COST = 2 × (fee_bps / 1e6) per buy event.",
    },
    {
        "id": "M07",
        "evidence": "E07 / Base_Capital = $25",
        "issue": "Sprint038A NOTIONAL band 0.0003-0.015 ETH spans $1-$52, "
                 "but author's Base_Capital is exactly $25 with Final_Score scaling [0,1].",
        "kind": "over_broad",
        "impact": (
            "Effective LASZLO buy size is $0-$25 = 0-0.007 ETH at $3500/ETH. "
            "Current band admits buys up to $52, which exceeds the author's "
            "explicit cap. Could include retail snipers with similar fee tiers."
        ),
        "fix": "Tighten upper bound to 0.0085 ETH (= $25 × 1.2 buffer).",
    },
    {
        "id": "M08",
        "evidence": "E25 / CIH entity clustering",
        "issue": "v1 specifies same-tx co-input clustering. Dropped in v2 narrative; "
                 "Sprint038A/B/C never implement it.",
        "kind": "missing_implementation",
        "impact": (
            "If author runs multi-wallet operation (anti-tracking), single-wallet "
            "filter cannot detect the cluster. Each member wallet would individually "
            "look LIKE a small retail trader."
        ),
        "fix": "Sprint039C: add bipartite graph build (funding source + first/last interaction).",
    },
    {
        "id": "M09",
        "evidence": "E22 / Sniper = 0-3 blocks of pool deployment",
        "issue": "Sprint038A s_early_pool uses ≤+50 blocks (loose). Whitepaper "
                 "says 0-3 blocks for the canonical Sniper archetype.",
        "kind": "over_broad",
        "impact": (
            "n_early_entries is computed at +50 block threshold; the stricter "
            "n_sniper_entries (+3 blocks) IS captured in the CSV but not weighted "
            "in the score formula."
        ),
        "fix": "Add s_sniper_strict using +3 block threshold; weight 0.10 separate "
               "from s_early_pool.",
    },
    {
        "id": "M10",
        "evidence": "E30 / Phase 6 narrative + failed_no_7d_alpha guard",
        "issue": "Sprint038A's premise assumes a LIVE LASZLO bot exists on-chain. "
                 "But the FAILED_NO_7D_ALPHA_ACTIVE guard says the author's own system "
                 "has not yet promoted to live.",
        "kind": "premise_drift",
        "impact": (
            "If the author is also bound by the 7d-alpha guard, the bot "
            "WAS NOT TRADING on-chain. The whole filter would then be searching "
            "for a wallet that does not exist."
        ),
        "fix": "Add LIVE_BOT_EXISTENCE flag to filter narrative; allow "
               "ATTRIBUTION_HYPOTHESIS = NO_LIVE_BOT_DETECTED as a valid outcome.",
    },
    {
        "id": "M11",
        "evidence": "Sprint038A s_pnl_proxy",
        "issue": "win_rate = -1.0 for all candidates (recipient PnL view returns -1) "
                 "→ s_pnl_proxy fixed at 0.5 → 5% of total weight is dead.",
        "kind": "non_functional",
        "impact": "Score range compressed from [0,1] to ~[0.05, 0.95]. Mostly cosmetic.",
        "fix": "Either compute recipient PnL from CH (now possible via 038 views) "
               "or drop the s_pnl_proxy weight entirely.",
    },
    {
        "id": "M12",
        "evidence": "Sprint038C OOS regime change",
        "issue": "Validation window had ZERO fee=3000/10000 swaps in WETH-leg meme pools "
                 "(326K → 0 swaps). The exact universe LASZLO targets vanished.",
        "kind": "ecosystem_drift",
        "impact": (
            "OOS_INCONCLUSIVE was correctly assigned, but the deeper read is: "
            "the author-canon route may have aged out of the active meme ecosystem. "
            "fee=1 / aero_cl became dominant — both OUTSIDE author canon."
        ),
        "fix": "Sprint039B: explicitly probe the upgraded-route hypothesis "
               "(fee=1 + aero_cl + Universal Router) as a separate filter lane.",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# Game-theory readout
# ══════════════════════════════════════════════════════════════════════════════
GAME_THEORY = {
    "scenario_A_no_anti_tracking": {
        "expected_pattern": [
            "Single EOA wallet (recipient = sender)",
            "Direct calls to V3 SwapRouter02 (NOT Universal Router or 1inch)",
            "Fee tiers 3000 / 10000 / 500 dominant (no fee=100, no aero_cl)",
            "Buy notional ~ $5-$25 in WETH (≈ 0.0014-0.007 ETH at $3500)",
            "Multi-token across the 11-token registry (5+ distinct memes)",
            "Round-trip pattern with avg hold near 1800s (30min time-stop)",
            "Sniper-style entries on subset (within +3 blocks of pool creation)",
            "maxPriorityFeePerGas = 1 wei",
            "WETH.withdraw() events after SELL",
            "Activity gap on days with 10%+ DD (UTC reset)",
        ],
        "current_match": (
            "ZERO wallets simultaneously satisfy ALL of these. Closest: 0x59c7c832 "
            "matches hold-time + diversity but only 2 RT out of 56 swaps; F10=0."
        ),
    },
    "scenario_B_anti_tracking": {
        "expected_pattern": [
            "Multiple EOA wallets, each with 20-100 swaps total",
            "Funded from common source (CEX deposit or bridge tx)",
            "Time-correlated activity (same UTC hour clusters)",
            "Each wallet uses subset of 11-token registry",
            "Some wallets via Universal Router to mix in noise",
            "Periodic wallet rotation (new wallet every N weeks)",
        ],
        "current_match": (
            "Cannot evaluate — Sprint038A/B/C does NOT implement entity clustering. "
            "Multiple top-9 wallets DO show 'medium activity + LASZLO hold time' "
            "(e.g., 0x0ba7, 0x0140, 0x4d6c) but cannot confirm common funding source."
        ),
    },
    "scenario_C_upgraded_route": {
        "expected_pattern": [
            "Migrated to fee=1 (lower-fee pools) and/or aero_cl",
            "Different Quoter (Aerodrome's, or Uniswap V4 if it exists on Base)",
            "Same hold-time and notional discipline",
            "Same UTC-anchored activity",
        ],
        "current_match": (
            "Only 0x4409 active in validation window (fee=100). Behavior pattern "
            "(4259 swaps, avg hold 11147s = 3hr) does NOT match LASZLO time-stop. "
            "Likely a different bot, not an upgraded LASZLO."
        ),
    },
    "scenario_D_no_live_bot": {
        "expected_pattern": [
            "FAILED_NO_7D_ALPHA_ACTIVE guard never lifted in author's system",
            "All Sprint038A candidates are unrelated retail or external bots",
            "f10_share = 0 across all top wallets is consistent with this",
        ],
        "current_match": (
            "STRONGEST FIT given the f10_share=0 evidence and the regime change "
            "in Sprint038C. The author may not have promoted to live and we are "
            "filtering retail noise."
        ),
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# Verdict
# ══════════════════════════════════════════════════════════════════════════════
def compute_verdict(sens: dict, top_old: list[dict], top_upgraded: list[dict]) -> dict:
    n_strict  = sens["strict_old_whitepaper_match_count_final"]
    n_relaxed = sens["relaxed_old_whitepaper_match_count"]

    # ── Filter faithfulness review (separate from attribution outcome) ───────
    n_critical = sum(1 for m in FILTER_MISMATCHES
                     if m["kind"] in ("incorrect_implementation",
                                      "over_inclusion",
                                      "missing_constraint",
                                      "missing_implementation"))
    if n_critical >= 3:
        filter_verdict = "FILTER_REVIEW_FAIL"
    elif n_critical >= 1:
        filter_verdict = "FILTER_REVIEW_PARTIAL_FAIL"
    else:
        filter_verdict = "FILTER_REVIEW_PASS"

    # ── Attribution outcome (separate axis) ──────────────────────────────────
    if n_relaxed == 0 and n_strict == 0:
        attribution_verdict = "ATTRIBUTION_INCONCLUSIVE_NO_STRICT_MATCH"
    elif n_strict == 0 and n_relaxed > 0:
        attribution_verdict = "ATTRIBUTION_PARTIAL_RELAXED_ONLY"
    else:
        attribution_verdict = "ATTRIBUTION_STRICT_MATCH_FOUND"

    return {
        "FILTER_REVIEW_VERDICT":          filter_verdict,
        "ATTRIBUTION_VERDICT":            attribution_verdict,
        "N_CRITICAL_MISMATCHES":          n_critical,
        "STRICT_AUTHOR_MATCH_COUNT":      n_strict,
        "RELAXED_AUTHOR_MATCH_COUNT":     n_relaxed,
        "AUTHOR_STYLE_TOP_CANDIDATES":    [c["recipient_address_AUDIT_ONLY"] for c in top_old[:3]],
        "UPGRADED_ROUTE_TOP_CANDIDATES":  [c["recipient_address_AUDIT_ONLY"] for c in top_upgraded[:3]],
        "IDENTITY_CLAIM_ALLOWED":         False,
        "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
        "NEXT_QUERY_REQUIRED":            True,
        "PRIMARY_BLOCKING_FILTERS":       [
            "F03_lasl_holdtime", "F04_low_priority_gas", "F05_has_roundtrips",
        ],
        "STRONGEST_GAME_THEORY_FIT":      "scenario_D_no_live_bot",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    print("[s039a] Loading Sprint038A candidates...")
    rows = load_candidates()
    print(f"[s039a] Loaded {len(rows)} scored wallets")

    print("[s039a] Computing filter sensitivity...")
    sens = filter_sensitivity(rows)
    print(f"[s039a]   base (score>=0.5): {sens['n_base_top9_score_ge_0_5']}")
    print(f"[s039a]   relaxed strict count: {sens['relaxed_old_whitepaper_match_count']}")
    print(f"[s039a]   strict (incl F04 gas): {sens['strict_old_whitepaper_match_count_final']}")

    print("[s039a] Re-ranking candidates by 3 routes...")
    reranked = rerank_candidates(rows, top_k=25)

    top_old      = sorted(reranked, key=lambda x: -x["old_whitepaper_score"])
    top_upgraded = sorted(reranked, key=lambda x: -x["upgraded_route_score"])
    top_anti     = sorted(reranked, key=lambda x: -x["anti_tracking_score"])

    verdict = compute_verdict(sens, top_old, top_upgraded)

    # ── Sensitivity JSON ────────────────────────────────────────────────────
    out_data = {
        "sprint":               "sprint039a_attribution_filter_review",
        "run_ts_utc":           datetime.now(timezone.utc).isoformat(),
        "decision_flags":       DECISION_FLAGS,
        "evidence_map_summary": {
            "n_total_evidences":         len(EVIDENCE_MAP),
            "directly_observable":       sum(1 for e in EVIDENCE_MAP if e[3] == "directly_observable"),
            "inferable_from_behavior":   sum(1 for e in EVIDENCE_MAP if e[3] == "inferable_from_behavior"),
            "only_observable_repo":      sum(1 for e in EVIDENCE_MAP if e[3] == "only_observable_repo_runtime"),
            "not_observable":            sum(1 for e in EVIDENCE_MAP if e[3] == "not_observable"),
            "drifted_or_contradicted":   sum(1 for e in EVIDENCE_MAP if e[3] == "drifted_or_contradicted"),
        },
        "filter_sensitivity":   sens,
        "filter_mismatches":    FILTER_MISMATCHES,
        "game_theory":          GAME_THEORY,
        "verdict":              verdict,
        "top_3_by_route": {
            "old_whitepaper":   top_old[:3],
            "upgraded_route":   top_upgraded[:3],
            "anti_tracking":    top_anti[:3],
        },
    }
    OUT_JSON.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
    print(f"[s039a] → {OUT_JSON}")

    # ── Ranking CSV ─────────────────────────────────────────────────────────
    ranking_fields = [
        "recipient_address_AUDIT_ONLY", "sprint038a_evidence_score",
        "old_whitepaper_score", "upgraded_route_score", "anti_tracking_score",
        "n_swaps", "n_distinct_pools", "n_distinct_meme_tokens", "n_round_trips",
        "avg_hold_sec", "p50_hold_sec",
        "n_fee_3000", "n_fee_10000", "n_fee_500", "n_fee_100", "f10_share",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ranking_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(reranked)
    print(f"[s039a] → {OUT_CSV}")

    # ── Audit markdown ──────────────────────────────────────────────────────
    write_audit_md(out_data, top_old, top_upgraded, top_anti)
    print(f"[s039a] → {OUT_MD}")


def write_audit_md(data: dict, top_old: list, top_upgraded: list, top_anti: list) -> None:
    sens = data["filter_sensitivity"]
    v    = data["verdict"]

    md = []
    md += [
        "# Sprint039A — Whitepaper-to-Chain Attribution Filter Review",
        "",
        f"**Run TS (UTC):** {data['run_ts_utc']}",
        "",
        "## 1. Executive Conclusion",
        "",
        f"- **FILTER_REVIEW_VERDICT:** `{v['FILTER_REVIEW_VERDICT']}` ({v['N_CRITICAL_MISMATCHES']} critical mismatches)",
        f"- **ATTRIBUTION_VERDICT:** `{v['ATTRIBUTION_VERDICT']}`",
        f"- **STRICT_AUTHOR_MATCH_COUNT (whitepaper-canon, all gates):** **{v['STRICT_AUTHOR_MATCH_COUNT']}**",
        f"- **RELAXED_AUTHOR_MATCH_COUNT (whitepaper-canon, no gas gate):** {v['RELAXED_AUTHOR_MATCH_COUNT']}",
        f"- Top old-route candidate (best similarity, NOT identity claim): `{v['AUTHOR_STYLE_TOP_CANDIDATES'][0] if v['AUTHOR_STYLE_TOP_CANDIDATES'] else 'NONE'}`",
        f"- Top upgraded-route candidate: `{v['UPGRADED_ROUTE_TOP_CANDIDATES'][0] if v['UPGRADED_ROUTE_TOP_CANDIDATES'] else 'NONE'}`",
        f"- Strongest game-theory scenario fit: `{v['STRONGEST_GAME_THEORY_FIT']}`",
        "",
        "**Headline finding:** Sprint038A/B/C filter contains 12 documented mismatches "
        "with the LASZLO whitepaper canon. The most consequential are:",
        "1. `M01` — fee=100 admitted to universe (whitepaper says 3000/10000/500 only).",
        "2. `M02` — no 11-token registry constraint (LASZLO trades only 11 specific tokens).",
        "3. `M03` — `s_short_exit` rewards 8-second holds equally to 1500-second holds, "
        "letting a sniper-and-dump bot rank #1 (0x667c, p50_hold = 8s).",
        "4. `M05` — `f10_share = 0` for ALL top9 wallets (priority-fee fingerprint absent).",
        "",
        "**Evidence does NOT support an identity claim on any wallet.** The strongest "
        "scenario consistent with all observed evidence is **scenario D (no live LASZLO bot)** — "
        "the author's `FAILED_NO_7D_ALPHA_ACTIVE` guard may never have been lifted, "
        "meaning we are filtering retail noise.",
        "",
        "---",
        "",
        "## 2. Strict Old-Whitepaper Candidate Count",
        "",
        f"| Definition | Count |",
        f"|------------|-------|",
        f"| All sprint038a scored wallets | {sens['n_total_scored']:,} |",
        f"| Sprint038A top9 (score ≥ 0.5) | {sens['n_base_top9_score_ge_0_5']} |",
        f"| Whitepaper-strict (without gas gate) | {sens['relaxed_old_whitepaper_match_count']} |",
        f"| Whitepaper-strict (with maxPriorityFeePerGas ≤ 5 wei) | **{sens['strict_old_whitepaper_match_count_final']}** |",
        "",
        "### What kills the count",
        "",
        "Cumulative strict route, starting from top9 (n=9):",
        "",
        "| Step | Condition | Before → After | Dropped |",
        "|------|-----------|---------------|---------|",
    ]
    for step in sens["cumulative_strict_route_steps"]:
        md.append(
            f"| {step['step']} | — | {step['before']} → {step['after']} | "
            f"{step['dropped']} |"
        )

    md += [
        "",
        "**Conclusion**: F04 (`maxPriorityFeePerGas ≤ 5 wei`) is the killer. "
        "All other strict gates leave 0–2 candidates; F04 zeroes them out completely.",
        "",
        "---",
        "",
        "## 3. Evidence Map Summary",
        "",
        f"Total evidences extracted from 8 author docs + 外宣版: **{data['evidence_map_summary']['n_total_evidences']}**",
        "",
        "| Category | Count |",
        "|----------|-------|",
        f"| directly observable on-chain | {data['evidence_map_summary']['directly_observable']} |",
        f"| inferable from behavior | {data['evidence_map_summary']['inferable_from_behavior']} |",
        f"| only observable from repo/runtime | {data['evidence_map_summary']['only_observable_repo']} |",
        f"| not observable | {data['evidence_map_summary']['not_observable']} |",
        f"| drifted or contradicted | {data['evidence_map_summary']['drifted_or_contradicted']} |",
        "",
        "### Full evidence map",
        "",
        "| ID | Claim | Source | Observability | Notes |",
        "|----|-------|--------|---------------|-------|",
    ]
    for e in EVIDENCE_MAP:
        md.append(f"| {e[0]} | {e[1]} | {e[2]} | `{e[3]}` | {e[4]} |")

    md += [
        "",
        "---",
        "",
        "## 4. Filter Mismatch / Over-Strict Conditions",
        "",
        "### 4a. Filter mismatch table",
        "",
        "| ID | Evidence | Issue | Kind | Fix |",
        "|----|----------|-------|------|-----|",
    ]
    for m in FILTER_MISMATCHES:
        md.append(
            f"| {m['id']} | {m['evidence']} | {m['issue']} | "
            f"`{m['kind']}` | {m['fix']} |"
        )

    md += [
        "",
        "### 4b. Individual filter sensitivity (which condition drops top9 by how much)",
        "",
        "| Filter | Description | Kept | Dropped | Drop% |",
        "|--------|-------------|------|---------|-------|",
    ]
    for ind in sens["individual_filter_sensitivity"]:
        md.append(
            f"| {ind['filter_id']} | {ind['description']} | "
            f"{ind['kept_count']} | {ind['dropped_from_base']} | {ind['drop_pct']}% |"
        )

    md += [
        "",
        "**Reading the table:** F03 (LASZLO hold time 200-2400s) and F02 (≥5 distinct tokens) "
        "are individually the most discriminating soft filters. F04 (gas signature) is the "
        "absolute killer when applied last in the cumulative route.",
        "",
        "---",
        "",
        "## 5. Top Candidates by Route",
        "",
        "### 5a. Old-whitepaper similarity (LASZLO-canon route)",
        "",
        "| # | Address | old_score | hold_sec | n_tokens | n_RT | fee10000 | fee100 |",
        "|---|---------|-----------|----------|----------|------|----------|--------|",
    ]
    for i, c in enumerate(top_old[:5], 1):
        md.append(
            f"| {i} | `{c['recipient_address_AUDIT_ONLY']}` | {c['old_whitepaper_score']} | "
            f"{c['avg_hold_sec']:.0f} | {c['n_distinct_meme_tokens']} | "
            f"{c['n_round_trips']} | {c['n_fee_10000']} | {c['n_fee_100']} |"
        )

    md += [
        "",
        "### 5b. Upgraded-route similarity (allow fee=100/1, longer holds)",
        "",
        "| # | Address | upgraded_score | hold_sec | n_swaps | fee100_share |",
        "|---|---------|---------------|----------|---------|--------------|",
    ]
    for i, c in enumerate(top_upgraded[:5], 1):
        fee100_share = round(c["n_fee_100"] / max(c["n_swaps"], 1), 3)
        md.append(
            f"| {i} | `{c['recipient_address_AUDIT_ONLY']}` | {c['upgraded_route_score']} | "
            f"{c['avg_hold_sec']:.0f} | {c['n_swaps']} | {fee100_share} |"
        )

    md += [
        "",
        "### 5c. Anti-tracking adjusted similarity (medium activity, balanced)",
        "",
        "| # | Address | anti_track_score | n_swaps | n_tokens | hold_sec |",
        "|---|---------|------------------|---------|----------|----------|",
    ]
    for i, c in enumerate(top_anti[:5], 1):
        md.append(
            f"| {i} | `{c['recipient_address_AUDIT_ONLY']}` | {c['anti_tracking_score']} | "
            f"{c['n_swaps']} | {c['n_distinct_meme_tokens']} | {c['avg_hold_sec']:.0f} |"
        )

    md += [
        "",
        "---",
        "",
        "## 6. Anti-Tracking / Game-Theory Readout",
        "",
    ]
    for sid, sdata in GAME_THEORY.items():
        md.append(f"### {sid}")
        md.append("")
        md.append("**Expected on-chain pattern:**")
        for p in sdata["expected_pattern"]:
            md.append(f"- {p}")
        md.append("")
        md.append(f"**Current match:** {sdata['current_match']}")
        md.append("")

    md += [
        "**Game-theoretic conclusion:** Among the 4 scenarios, **D (no live bot)** "
        "has the strongest evidence fit. The combination of:",
        "- f10_share = 0 across all top9 wallets",
        "- Sprint038C OOS regime change (fee=3000/10000 disappearance)",
        "- Sprint038A's top wallets exhibiting non-LASZLO patterns (8s holds, single-pool, etc.)",
        "- The author's own `FAILED_NO_7D_ALPHA_ACTIVE` guard restricting promotion to live",
        "",
        "...is most consistent with the LASZLO bot **never having gone fully live** "
        "during the observation window. We are likely measuring background retail and "
        "external sniper bot noise, not the author's system.",
        "",
        "Scenario A (no anti-tracking) is FALSIFIED — zero wallets match the strict "
        "fingerprint set.",
        "",
        "Scenario B (multi-wallet anti-tracking) is **not testable** with current "
        "filters; would require funding-source clustering (Sprint039C scope).",
        "",
        "Scenario C (upgraded route) is WEAKLY supported — only 0x4409 active in "
        "Apr-May regime, but with non-LASZLO hold patterns.",
        "",
        "---",
        "",
        "## 7. Decision Flags",
        "",
        "```",
        f"FILTER_REVIEW_VERDICT          = {v['FILTER_REVIEW_VERDICT']}",
        f"ATTRIBUTION_VERDICT            = {v['ATTRIBUTION_VERDICT']}",
        f"N_CRITICAL_MISMATCHES          = {v['N_CRITICAL_MISMATCHES']}",
        f"STRICT_AUTHOR_MATCH_COUNT      = {v['STRICT_AUTHOR_MATCH_COUNT']}",
        f"RELAXED_AUTHOR_MATCH_COUNT     = {v['RELAXED_AUTHOR_MATCH_COUNT']}",
        f"AUTHOR_STYLE_TOP_CANDIDATES    = {v['AUTHOR_STYLE_TOP_CANDIDATES']}",
        f"UPGRADED_ROUTE_TOP_CANDIDATES  = {v['UPGRADED_ROUTE_TOP_CANDIDATES']}",
        f"STRONGEST_GAME_THEORY_FIT      = {v['STRONGEST_GAME_THEORY_FIT']}",
        f"IDENTITY_CLAIM_ALLOWED         = {v['IDENTITY_CLAIM_ALLOWED']}",
        f"ATTRIBUTION_HYPOTHESIS_ALLOWED = {v['ATTRIBUTION_HYPOTHESIS_ALLOWED']}",
        f"NEXT_QUERY_REQUIRED            = {v['NEXT_QUERY_REQUIRED']}",
        f"PRIMARY_BLOCKING_FILTERS       = {v['PRIMARY_BLOCKING_FILTERS']}",
        f"TRAINING_ALLOWED               = false",
        f"LIVE_ALLOWED                   = false",
        f"NO_ALPHA_CLAIM                 = true",
        "```",
        "",
        "---",
        "",
        "## 8. Next Sprint Recommendation",
        "",
        "**Recommended order (shortest path):**",
        "",
        "### Sprint039B-FilterPatch (HIGH priority, 1 day)",
        "Patch the 4 most consequential mismatches in sprint038a script:",
        "1. **M01** — drop fee=100 from `meme_pools` CTE; gate as separate diagnostic lane.",
        "2. **M03** — replace `s_short_exit` with Gaussian-shaped reward centered at 1800s.",
        "3. **M07** — tighten `NOTIONAL_MAX_ETH` from 0.015 → 0.0085 ETH.",
        "4. **M09** — add `s_sniper_strict` (≤+3 blocks) as separate weighted feature.",
        "",
        "Then re-run sprint038a with patched filter; recompute top-K. If top wallet "
        "is now coherent (passes all 4 filters), proceed to Sprint039C.",
        "",
        "### Sprint039C-EntityCluster (MEDIUM priority, 2 days)",
        "Implement CIH entity clustering (M08):",
        "1. Build bipartite graph: [recipient_wallet] ⇄ [funding_source_wallet] "
        "via first-tx-from analysis on top-100 candidates.",
        "2. Cluster wallets sharing funding source within 7-day window.",
        "3. Score clusters by: (a) total LASZLO-fingerprint sum across members, "
        "(b) time-correlation of activity, (c) collective token diversity.",
        "",
        "This addresses the `M08` gap and enables Scenario B testing.",
        "",
        "### Sprint039D-UpgradedRouteDiscovery (LOW priority, 1 day)",
        "Add `aero_cl` and `v3 fee=1` pools as a separate \"upgraded route\" lane:",
        "1. Extend `meme_pools` CTE to include these pool types.",
        "2. Re-score using upgraded_route weights from this sprint.",
        "3. Quoter probe with appropriate Aerodrome quoter contract.",
        "",
        "### Sprint039E-NoLiveBotConfirmation (Validation)",
        "If Sprint039B/C/D all return INCONCLUSIVE / 0 strict matches, then:",
        "1. Cross-check the author's repo for any private-key/wallet-address artifacts "
        "(audit-mode read; no key exfil).",
        "2. Verify `FAILED_NO_7D_ALPHA_ACTIVE` was never lifted in `mvp/audits/`.",
        "3. Document conclusion: **NO_LIVE_BOT_DETECTED** as legitimate scenario D outcome.",
        "",
        "---",
        "*Diagnostics-only filter review. No identity claims. No live trading. "
        "No Redis / signals / dashcam writes. All addresses use _AUDIT_ONLY suffix.*",
    ]

    OUT_MD.write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
