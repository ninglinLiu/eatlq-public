#!/usr/bin/env python3
"""
Sprint049A: Smart-Money Follow Sidecar Preflight

Read-only aggregation script. No RPC, no signals, no training, no live.
Reads Sprint048A/B artifacts to produce:
  - sprint049a_follow_candidate_preflight.csv
  - sprint049a_toxic_liquidity_flags.csv
  - sprint049a_component_reuse_matrix.csv
  - sprint049a_sidecar_contract.json
  - sprint049a_decision_flags.json
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIAG = ROOT / "mvp" / "diagnostics"
AUD  = ROOT / "mvp" / "audits"

# ─── Load Sprint048A/B artifacts ─────────────────────────────────────────────

def load_scores() -> list[dict]:
    rows = []
    with open(DIAG / "sprint048a_smart_money_scores.csv") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows

def load_realized_pnl() -> list[dict]:
    rows = []
    with open(DIAG / "sprint048b_realized_pnl.csv") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows

def load_candidate_entities() -> list[dict]:
    rows = []
    with open(DIAG / "sprint048a_candidate_entities.csv") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows

# ─── Toxic-liquidity detection heuristics (offline, no RPC) ──────────────────

def detect_toxic_flags(ctrl: str, scr: dict, pairs: list[dict]) -> list[dict]:
    """
    Return list of toxic-liquidity flag dicts for a controller.
    All checks are offline heuristics using tx_map-derived statistics.
    """
    flags = []
    q_buy = int(scr.get("quoteable_event_count", 0))
    q_share = float(scr.get("quoteable_event_share", 0))
    win_rate = float(scr.get("win_rate", 0))
    top1 = float(scr.get("top1_token_share", 0))
    active_days = int(scr.get("active_days_inventory", 0) or scr.get("active_days", 0))
    pnl = float(scr.get("pnl_total_usd", 0))
    n_tok = int(scr.get("unique_tokens", 1))
    n_pool = int(scr.get("unique_pools", 1))

    ctrl_pairs = [p for p in pairs if p.get("controller") == ctrl and p.get("sell_tx")]
    hold_mins  = [float(p["hold_min"]) for p in ctrl_pairs if float(p.get("hold_min", -1)) > 0]
    hold_secs  = [float(p["hold_sec"]) for p in ctrl_pairs if float(p.get("hold_sec", -1)) > 0]

    # 1. Ultra-short + ultra-regular hold → bot/MEV pattern
    if hold_secs:
        median_sec = sorted(hold_secs)[len(hold_secs) // 2]
        cv = (
            (sum((h - sum(hold_secs) / len(hold_secs)) ** 2 for h in hold_secs) / len(hold_secs)) ** 0.5
            / max(1, sum(hold_secs) / len(hold_secs))
        )
        if median_sec < 30 and len(ctrl_pairs) >= 5:
            flags.append({
                "controller": ctrl, "flag": "MEV_SANDWICH_SUSPECT",
                "why": f"median_hold={median_sec:.0f}s < 30s with {len(ctrl_pairs)} matched pairs",
                "exclude_from_follow": "true", "keep_as_shadow": "false"
            })
        elif median_sec < 120 and cv < 0.20 and len(ctrl_pairs) >= 5:
            flags.append({
                "controller": ctrl, "flag": "BOT_REGULARITY_SUSPECT",
                "why": f"median_hold={median_sec:.0f}s, hold_time_cv={cv:.2f} (too regular)",
                "exclude_from_follow": "true", "keep_as_shadow": "true"
            })

    # 2. Win rate = 1.00 with many trades → suspect wash/self-churn or very small notional
    if win_rate >= 1.0 and q_buy >= 10:
        flags.append({
            "controller": ctrl, "flag": "PERFECT_WINRATE_SUSPECT",
            "why": f"win_rate=1.00 with {q_buy} quoteable buys — may indicate wash/self-churn or very low notional",
            "exclude_from_follow": "false", "keep_as_shadow": "true"
        })

    # 3. Extreme single-token concentration (≥95%) with many events → scoped bot / market maker
    if top1 >= 0.95 and q_buy >= 20:
        flags.append({
            "controller": ctrl, "flag": "SINGLE_TOKEN_CONCENTRATION",
            "why": f"top1_token_share={top1:.2f} with {q_buy} buys → likely token-specific bot",
            "exclude_from_follow": "false",
            "keep_as_shadow": "true"
        })

    # 4. q_share < 0.15 → mostly private router → not followable
    if q_share < 0.15 and q_buy < 5:
        flags.append({
            "controller": ctrl, "flag": "PRIVATE_ROUTE_DOMINANT",
            "why": f"quoteable_share={q_share:.2f} < 0.15 → not publicly reproducible",
            "exclude_from_follow": "true", "keep_as_shadow": "true"
        })

    # 5. Symmetric buy/sell churn heuristic
    ctrl_buys  = len([p for p in pairs if p.get("controller") == ctrl])
    ctrl_sells = len([p for p in pairs if p.get("controller") == ctrl and p.get("sell_tx")])
    if ctrl_buys > 0 and ctrl_sells / max(1, ctrl_buys) >= 0.95 and q_buy >= 20:
        flags.append({
            "controller": ctrl, "flag": "SYMMETRIC_CHURN",
            "why": f"sell/buy ratio={ctrl_sells/ctrl_buys:.2f} ≥ 0.95 with {q_buy} buys → market-maker pattern",
            "exclude_from_follow": "false", "keep_as_shadow": "true"
        })

    return flags

# ─── Followability score ──────────────────────────────────────────────────────

def followability_score(scr: dict, pairs: list[dict], toxic_flags: list[dict]) -> dict:
    """
    Compute a followability score (0-100). Does NOT use 'like author' as criterion.
    Primary factors: quoteability, BUY observability, hold duration, realized PnL.
    """
    ctrl = scr.get("controller", "")
    q_buy    = int(scr.get("quoteable_event_count", 0))
    q_share  = float(scr.get("quoteable_event_share", 0))
    win_rate = float(scr.get("win_rate", 0))
    top1     = float(scr.get("top1_token_share", 1.0))
    top3     = float(scr.get("top3_token_share", 1.0))
    n_tok    = int(scr.get("unique_tokens", 1))
    n_pool   = int(scr.get("unique_pools", 1))
    priv_pen = float(scr.get("private_router_dependency_penalty", 0))
    active_d = int(scr.get("active_days_inventory", 0) or 0)
    pnl      = float(scr.get("pnl_total_usd", 0))

    ctrl_pairs   = [p for p in pairs if p.get("controller") == ctrl and p.get("sell_tx")]
    hold_mins    = [float(p["hold_min"]) for p in ctrl_pairs if float(p.get("hold_min", -1)) > 0]
    pnl_vals     = [float(p["realized_pnl"]) for p in ctrl_pairs]
    no_exit_n    = len([p for p in pairs if p.get("controller") == ctrl and not p.get("sell_tx")])
    total_buy_n  = len([p for p in pairs if p.get("controller") == ctrl])
    matched_n    = len(ctrl_pairs)

    # --- Component scores ---
    # 1. Quoteable BUY event count (max 25 pts, log-scaled)
    s_qe = min(25, math.log1p(q_buy) * 5)

    # 2. Quoteable share (max 20 pts)
    s_qs = q_share * 20

    # 3. Hold duration fit for follow: prefer 30m-24h holds
    #    Too short (<5m) = MEV risk. Too long (>7d) = operationally hard.
    if hold_mins:
        med_h = sorted(hold_mins)[len(hold_mins) // 2]
        if med_h < 5:
            s_hold = 0   # too short, MEV risk
        elif med_h <= 1440:  # 24h
            s_hold = min(15, 15 * (1 - abs(math.log(med_h / 60)) / 3))  # peaks around 1h
        else:
            s_hold = 5   # long hold, operational burden
    else:
        s_hold = 0

    # 4. Realized PnL consistency (max 15 pts)
    if pnl_vals:
        pos_rate = sum(1 for v in pnl_vals if v > 0) / len(pnl_vals)
        s_pnl = pos_rate * 15
    else:
        s_pnl = 0

    # 5. Token/pool diversity (max 10 pts)
    #    Diversity is good (not locked to one token), but extreme diversity = noisy
    s_div = min(10, (1 - top1) * 10)

    # 6. Exit observability: was SELL visible in tx_map?
    if total_buy_n > 0:
        exit_obs = matched_n / total_buy_n
    else:
        exit_obs = 0
    s_exit = exit_obs * 10

    # 7. Active days (presence over time, not just burst)
    s_act = min(5, math.log1p(active_d))

    # 8. Penalties
    n_toxic = len([f for f in toxic_flags if f.get("exclude_from_follow") == "true"])
    penalty_toxic = n_toxic * 15  # 15 pts per hard toxic flag
    penalty_priv  = priv_pen * 10  # 0-3 pts for private route dependency

    raw_score = s_qe + s_qs + s_hold + s_pnl + s_div + s_exit + s_act
    final = max(0, min(100, raw_score - penalty_toxic - penalty_priv))

    # Category classification
    if n_toxic > 0:
        cat = "TOXIC_LIQUIDITY_RISK"
    elif final >= 55 and q_buy >= 10 and q_share >= 0.5:
        cat = "FOLLOW_CANDIDATE_STRONG"
    elif final >= 35 and q_buy >= 5:
        cat = "FOLLOW_CANDIDATE_WEAK"
    elif q_share < 0.15:
        cat = "SHADOW_ONLY_PRIVATE_ROUTE"
    elif matched_n < 3 and q_buy < 5:
        cat = "INSUFFICIENT_FOLLOWABILITY"
    else:
        cat = "FOLLOW_CANDIDATE_WEAK"

    # Recommended max paper size (USD) — conservative sizing
    if cat == "FOLLOW_CANDIDATE_STRONG":
        max_paper = 50  # USD
    elif cat == "FOLLOW_CANDIDATE_WEAK":
        max_paper = 20
    else:
        max_paper = 0

    # Next action
    if cat == "FOLLOW_CANDIDATE_STRONG":
        next_action = "INCLUDE_IN_SPRINT049B_PAPER_FOLLOW"
    elif cat == "FOLLOW_CANDIDATE_WEAK":
        next_action = "MONITOR_ONLY_FOR_NOW"
    elif cat == "TOXIC_LIQUIDITY_RISK":
        next_action = "FLAG_AND_EXCLUDE_FROM_FOLLOW"
    elif cat == "SHADOW_ONLY_PRIVATE_ROUTE":
        next_action = "SHADOW_FEATURE_ONLY"
    else:
        next_action = "COLLECT_MORE_DATA"

    med_h_out = round(sorted(hold_mins)[len(hold_mins) // 2] / 60, 2) if hold_mins else -1
    p90_h_out = round(sorted(hold_mins)[int(len(hold_mins) * 0.9)] / 60, 2) if hold_mins else -1
    total_real = round(sum(pnl_vals), 4) if pnl_vals else 0.0
    pos_r = round(sum(1 for v in pnl_vals if v > 0) / max(1, len(pnl_vals)), 4) if pnl_vals else 0.0

    return {
        "controller": ctrl,
        "bucket": scr.get("bucket", ""),
        "category": cat,
        "followability_score": round(final, 1),
        "quoteable_buy_count": q_buy,
        "quoteable_share": round(q_share, 4),
        "matched_buy_sell_pairs": matched_n,
        "no_exit_pairs": no_exit_n,
        "own_realized_pnl_weth": total_real,
        "own_positive_rate": pos_r,
        "median_hold_hours": med_h_out,
        "p90_hold_hours": p90_h_out,
        "unique_tokens": n_tok,
        "unique_pools": n_pool,
        "top1_token_share": round(top1, 4),
        "top3_token_share": round(top3, 4),
        "public_route_share": round(q_share, 4),
        "private_route_share": round(1 - q_share, 4),
        "win_rate_inventory": round(win_rate, 4),
        "pnl_total_usd_inventory": round(float(scr.get("pnl_total_usd", 0)), 2),
        "active_days": active_d,
        "n_toxic_flags": n_toxic,
        "recommended_max_paper_size_usd": max_paper,
        "next_action": next_action,
        "score_detail": f"qe={s_qe:.1f}+qs={s_qs:.1f}+hold={s_hold:.1f}+pnl={s_pnl:.1f}+div={s_div:.1f}+exit={s_exit:.1f}+act={s_act:.1f}-toxic={penalty_toxic:.0f}-priv={penalty_priv:.1f}",
    }


# ─── Component reuse matrix ───────────────────────────────────────────────────

REUSE_MATRIX = [
    {
        "component": "Ingestor (Rust WebSocket → Redis market_ticks)",
        "current_role": "Emits all raw on-chain events via Redis stream for strategy consumption",
        "reusable_for_sidecar": "YES",
        "required_patch": "None — sidecar consumer reads same market_ticks stream. Add consumer group copy_follow_group.",
        "risk": "LOW — read-only consumer, no write to ingestor",
        "new_system_needed": "NO",
    },
    {
        "component": "FeatureStore (services/strategy/src/feature_store.py)",
        "current_role": "Redis-backed price velocity, buy pressure, volatility, entropy features",
        "reusable_for_sidecar": "PARTIAL",
        "required_patch": "Sidecar reads FeatureStore for pool context but does NOT write to main feature keys. Add copy_follow:features:{pool} prefix.",
        "risk": "LOW — read-only access to existing keys; new prefix isolated",
        "new_system_needed": "NO",
    },
    {
        "component": "SmartMoneyTracker (services/strategy/src/main.py)",
        "current_role": "Tracks wallet:smart_money Redis set; emits signals when >=2 SM buys in new pool",
        "reusable_for_sidecar": "PARTIAL",
        "required_patch": "New Redis set wallet:copy_follow_candidates loaded from sprint049a_follow_candidate_preflight.csv. Sidecar checks this set independently.",
        "risk": "LOW — separate key, no contamination of wallet:smart_money",
        "new_system_needed": "NO",
    },
    {
        "component": "SmartMoneyTracker.check_honeypot_sim()",
        "current_role": "QuoterV2-based sell simulation: V2 getReserves or V3 quoteExactInputSingle",
        "reusable_for_sidecar": "YES",
        "required_patch": "Zero changes needed; call from sidecar decision loop before simulated follow BUY",
        "risk": "LOW — read-only RPC call (eth_call), no state change",
        "new_system_needed": "NO",
    },
    {
        "component": "SmartMoneyTracker.check_liquidity()",
        "current_role": "Checks WETH balance >= 0.5 ETH in pool",
        "reusable_for_sidecar": "YES",
        "required_patch": "Zero changes needed",
        "risk": "LOW",
        "new_system_needed": "NO",
    },
    {
        "component": "executor/src/swap.py: quote_sell_eth_wei, check_honeypot",
        "current_role": "V3 QuoterV2 sell estimate + honeypot detection for live executor",
        "reusable_for_sidecar": "YES (paper mode only)",
        "required_patch": "Import as library; call in simulation mode (no actual tx). Add PAPER_MODE guard.",
        "risk": "MEDIUM — must ensure sidecar never calls execute_buy/execute_sell",
        "new_system_needed": "NO",
    },
    {
        "component": "executor/src/position.py (OpenPosition, Redis positions:*)",
        "current_role": "Redis-backed position tracking with pending_sell/timeout/peak_eth",
        "reusable_for_sidecar": "YES (with prefix isolation)",
        "required_patch": "New prefix copy_follow:positions:{pool} vs positions:{pool}. Lua pending_sell TTL logic reusable.",
        "risk": "LOW — prefix isolation prevents live position contamination",
        "new_system_needed": "NO",
    },
    {
        "component": "executor/src/registry.py (pool registry)",
        "current_role": "Pool metadata lookup for routing BUY/SELL decisions",
        "reusable_for_sidecar": "YES",
        "required_patch": "None — read-only pool metadata used for sidecar routing decisions",
        "risk": "LOW",
        "new_system_needed": "NO",
    },
    {
        "component": "toxic_filter.py (load_mev_blacklist)",
        "current_role": "Loads MEV searcher/aggregator blacklist from infra/config/mev_blacklist.yaml",
        "reusable_for_sidecar": "YES",
        "required_patch": "None — sidecar applies same blacklist to controller addresses at startup",
        "risk": "LOW",
        "new_system_needed": "NO",
    },
    {
        "component": "ClickHouse (raw_evm_logs, uniswap_v3_swaps_trade_semantic)",
        "current_role": "Historical swap data store; queried by label/backfill scripts",
        "reusable_for_sidecar": "YES",
        "required_patch": "Sidecar reads CH to build block_time index and decode buy events. No new CH tables needed initially.",
        "risk": "LOW — CH is read-only for sidecar",
        "new_system_needed": "NO",
    },
    {
        "component": "dashcam.csv artifact pattern (scripts/label_dashcam.py)",
        "current_role": "Append-only decision log for strategy signals",
        "reusable_for_sidecar": "YES",
        "required_patch": "New file copy_follow_dashcam.csv with sidecar-specific columns: controller, follow_decision, hold_duration, follow_pnl_weth",
        "risk": "LOW — separate file, no contamination of main dashcam",
        "new_system_needed": "NO",
    },
    {
        "component": "XGBoost model / ModelScorer",
        "current_role": "Price-velocity / buy-pressure ML scorer for signal generation",
        "reusable_for_sidecar": "NO",
        "required_patch": "Sidecar MUST NOT use model for copy-follow decisions. Decision is purely: controller observed BUY -> quoteability check -> toxic check -> simulated follow.",
        "risk": "HIGH if mistakenly applied — would contaminate model signal path",
        "new_system_needed": "NO (not needed for sidecar)",
    },
    {
        "component": "signals stream (Redis XADD signals)",
        "current_role": "Carries model-scored trading signals to executor",
        "reusable_for_sidecar": "NO — hard isolation required",
        "required_patch": "Sidecar writes to copy_follow_decisions stream only. Never touches signals stream.",
        "risk": "CRITICAL if contaminated — would trigger live executor",
        "new_system_needed": "NO (separate stream key is sufficient)",
    },
    {
        "component": "Phase6 training dataset / label pipeline",
        "current_role": "Accumulates labeled events for model retrain gate",
        "reusable_for_sidecar": "NO — must not pollute",
        "required_patch": "Sidecar events are never written to training dataset. Use copy_follow_shadow_events table only.",
        "risk": "CRITICAL if contaminated — would corrupt training labels",
        "new_system_needed": "NO (separate ClickHouse table if needed)",
    },
]


# ─── Sidecar contract ─────────────────────────────────────────────────────────

def build_sidecar_contract(candidates: list[dict]) -> dict:
    strong = [r for r in candidates if r["category"] == "FOLLOW_CANDIDATE_STRONG"]
    weak   = [r for r in candidates if r["category"] == "FOLLOW_CANDIDATE_WEAK"]

    return {
        "name": "smart_money_follow_sidecar",
        "version": "v0_design_only",
        "sprint": "049A",
        "status": "DESIGN_PREFLIGHT_ONLY",
        "live_authorized": False,
        "training_authorized": False,
        "model_promotion_authorized": False,
        "mainline_contamination_allowed": False,
        "state_machine": [
            "1_controller_buy_observed: ingestor market_ticks → decode tx_from/pool/token",
            "2_candidate_check: is tx_from in copy_follow_candidates? → skip if not",
            "3_quoteability_check: lane_type in {uniswap_routers,1inch,0x} → skip if not",
            "4_toxic_filter: check MEV blacklist + regularity flags → skip if toxic",
            "5_honeypot_check: SmartMoneyTracker.check_honeypot_sim() via QuoterV2",
            "6_liquidity_check: SmartMoneyTracker.check_liquidity() → skip if depth < 0.5 ETH",
            "7_slippage_estimate: quote_sell_eth_wei() for simulated position size",
            "8_simulated_follow_buy: record copy_follow:positions:{pool} in Redis (NO live tx)",
            "9_wait_controller_sell_or_timeout: monitor market_ticks for controller SELL from same pool; timeout = 48h",
            "10_simulated_exit: on SELL detected or timeout → compute follow_pnl_weth",
            "11_dashcam_append: write to copy_follow_dashcam.csv",
        ],
        "isolation_boundaries": {
            "redis_prefix": "copy_follow:*",
            "stream_read": "market_ticks (consumer group: copy_follow_group)",
            "stream_write": "copy_follow_decisions (NOT signals)",
            "positions_prefix": "copy_follow:positions:{pool} (NOT positions:{pool})",
            "candidates_key": "wallet:copy_follow_candidates (NOT wallet:smart_money)",
            "dashcam_file": "logs/copy_follow_dashcam.csv (NOT logs/dashcam.csv)",
            "never_touch": ["signals", "positions:*", "wallet:smart_money", "training_dataset", "model_dir"],
        },
        "candidate_selection": {
            "source": "sprint049a_follow_candidate_preflight.csv",
            "strong_candidates": [r["controller"] for r in strong],
            "strong_count": len(strong),
            "weak_candidates": [r["controller"] for r in weak],
            "weak_count": len(weak),
            "recommended_sprint049b_start": [r["controller"] for r in strong],
        },
        "paper_follow_parameters": {
            "max_position_size_usd": 50,
            "entry_delay_blocks": 1,
            "exit_on_controller_sell": True,
            "exit_timeout_hours": 48,
            "gas_cost_estimate_weth": 0.0002,
            "slippage_model": "tiered_by_notional",
            "quoteable_only": True,
            "all_lane_proxy_separate": True,
        },
        "follow_pnl_accounting": {
            "own_realized_pnl": "controller's tx-matched BUY→SELL PnL (Sprint048B data)",
            "delayed_follow_pnl": "sidecar's simulated BUY at +1 block → exit at controller SELL detection",
            "all_lane_proxy": "if controller uses private router, all-lane result only (not executable)",
            "quoteable_only": "primary feasibility metric",
        },
        "reuse_score_out_of_10": 9,
        "new_system_needed": False,
        "new_system_justification": "All required components (ingestor, Redis, FeatureStore, honeypot, liquidity, position tracking, dashcam) exist and are isolatable via prefix/stream separation.",
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Sprint049A: Follow Sidecar Preflight")
    print(f"Timestamp: {datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")

    scores  = load_scores()
    pairs   = load_realized_pnl()
    entities = load_candidate_entities()

    # Filter to 31 PUBLIC_EXECUTABLE_SMART_MONEY_CANDIDATE
    candidates_scores = [s for s in scores if s.get("category") == "PUBLIC_EXECUTABLE_SMART_MONEY_CANDIDATE"]
    print(f"\nCandidates from Sprint048A: {len(candidates_scores)}")

    # --- Toxic-liquidity flags ---
    all_flags: list[dict] = []
    for scr in candidates_scores:
        ctrl = scr["controller"]
        flags = detect_toxic_flags(ctrl, scr, pairs)
        all_flags.extend(flags)

    FIELDS_TOX = ["controller","flag","why","exclude_from_follow","keep_as_shadow"]
    with open(DIAG / "sprint049a_toxic_liquidity_flags.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_TOX)
        w.writeheader()
        w.writerows(all_flags)
    print(f"Toxic flags written: {len(all_flags)} flags for {len(set(r['controller'] for r in all_flags))} controllers")

    # --- Followability preflight ---
    ctrl_flags = defaultdict(list)
    for fl in all_flags:
        ctrl_flags[fl["controller"]].append(fl)

    preflight_rows: list[dict] = []
    for scr in candidates_scores:
        ctrl = scr["controller"]
        row = followability_score(scr, pairs, ctrl_flags[ctrl])
        preflight_rows.append(row)

    preflight_rows.sort(key=lambda x: -x["followability_score"])

    FIELDS_PF = [
        "controller","bucket","category","followability_score",
        "quoteable_buy_count","quoteable_share","matched_buy_sell_pairs","no_exit_pairs",
        "own_realized_pnl_weth","own_positive_rate","median_hold_hours","p90_hold_hours",
        "unique_tokens","unique_pools","top1_token_share","top3_token_share",
        "public_route_share","private_route_share","win_rate_inventory","pnl_total_usd_inventory",
        "active_days","n_toxic_flags","recommended_max_paper_size_usd","next_action","score_detail",
    ]
    with open(DIAG / "sprint049a_follow_candidate_preflight.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_PF)
        w.writeheader()
        w.writerows(preflight_rows)
    print(f"Follow candidate preflight written: {len(preflight_rows)} rows")

    cat_counts = Counter(r["category"] for r in preflight_rows)
    for cat, n in cat_counts.most_common():
        print(f"  {cat}: {n}")

    # --- Component reuse matrix ---
    FIELDS_RM = ["component","current_role","reusable_for_sidecar","required_patch","risk","new_system_needed"]
    with open(DIAG / "sprint049a_component_reuse_matrix.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_RM)
        w.writeheader()
        w.writerows(REUSE_MATRIX)
    print(f"Component reuse matrix written: {len(REUSE_MATRIX)} components")

    # --- Sidecar contract ---
    contract = build_sidecar_contract(preflight_rows)
    with open(DIAG / "sprint049a_sidecar_contract.json","w") as f:
        json.dump(contract, f, indent=2)
    print(f"Sidecar contract written")

    # --- Decision flags ---
    strong_n = cat_counts.get("FOLLOW_CANDIDATE_STRONG", 0)
    weak_n   = cat_counts.get("FOLLOW_CANDIDATE_WEAK", 0)
    toxic_n  = cat_counts.get("TOXIC_LIQUIDITY_RISK", 0)
    shadow_n = cat_counts.get("SHADOW_ONLY_PRIVATE_ROUTE", 0)
    insuf_n  = cat_counts.get("INSUFFICIENT_FOLLOWABILITY", 0)

    flags_out = {
        "sprint": "049A",
        "timestamp": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "parent_sprints": ["048A", "048B"],
        # Route declaration
        "MAINLINE_CONTINUES_WHITEPAPER": True,
        "SMART_MONEY_FOLLOW_IS_SIDECAR": True,
        "ATTRIBUTION_LINE_PAUSED": True,
        "AUTHOR_WALLET_FOUND": False,
        "LIVE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        # Engineering
        "NEW_SYSTEM_NEEDED": False,
        "REUSE_SCORE": 9,
        "REUSE_SCORE_REASON": "All key components (ingestor, Redis, FeatureStore, honeypot, liquidity, position, dashcam) are isolatable without new systems",
        "MAINLINE_CONTAMINATION_ALLOWED": False,
        # Candidate counts
        "TOTAL_CANDIDATES_EVALUATED": len(preflight_rows),
        "FOLLOW_CANDIDATE_STRONG": strong_n,
        "FOLLOW_CANDIDATE_WEAK": weak_n,
        "TOXIC_LIQUIDITY_RISK": toxic_n,
        "SHADOW_ONLY_PRIVATE_ROUTE": shadow_n,
        "INSUFFICIENT_FOLLOWABILITY": insuf_n,
        "FOLLOW_CANDIDATE_FOUND": (strong_n + weak_n) > 0,
        # Recommendations
        "PAPER_SHADOW_IMPLEMENTATION_RECOMMENDED": strong_n >= 1,
        "SPRINT049B_SCOPE": "paper_shadow_follow_for_FOLLOW_CANDIDATE_STRONG_only" if strong_n >= 1 else "NO_FOLLOW_CANDIDATE_YET",
        "EXPAND_TO_1000_EVENTS": False,
        "COPY_TRADE_5M_30M_FEASIBILITY": "FAIL_CONFIRMED",
        "LONG_HOLD_ALPHA_SIGNAL": "POSITIVE_BUT_UNVERIFIED_FOR_FOLLOWER",
        # Key design decisions
        "SIDECAR_USES_MODEL": False,
        "SIDECAR_WRITES_TO_SIGNALS_STREAM": False,
        "SIDECAR_REDIS_PREFIX": "copy_follow:*",
        "SIDECAR_DASHCAM": "logs/copy_follow_dashcam.csv",
        "HOLD_STYLE_OF_STRONG_CANDIDATES": "MEDIUM_TO_LONG_NOT_SHORT_SCALP",
    }

    with open(DIAG / "sprint049a_decision_flags.json","w") as f:
        json.dump(flags_out, f, indent=2)
    print(f"Decision flags written")

    # Summary printout
    print(f"\n=== SUMMARY ===")
    strong_ctrls = [r["controller"] for r in preflight_rows if r["category"]=="FOLLOW_CANDIDATE_STRONG"]
    print(f"FOLLOW_CANDIDATE_STRONG: {strong_n}")
    for c in strong_ctrls:
        r = next(x for x in preflight_rows if x["controller"]==c)
        print(f"  {c[:16]}...  score={r['followability_score']}  q_buy={r['quoteable_buy_count']}  wr={r['win_rate_inventory']}  med_hold={r['median_hold_hours']}h  pnl={r['own_realized_pnl_weth']}")
    print(f"FOLLOW_CANDIDATE_WEAK: {weak_n}")
    print(f"TOXIC_LIQUIDITY_RISK: {toxic_n}")
    print(f"PAPER_SHADOW_IMPLEMENTATION_RECOMMENDED: {flags_out['PAPER_SHADOW_IMPLEMENTATION_RECOMMENDED']}")
    print(f"\nAll artifacts written to {DIAG}")


if __name__ == "__main__":
    main()
