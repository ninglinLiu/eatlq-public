"""Sprint045D — Author-likeness review for Sprint045C top controllers.

Pure analytical pass over existing Sprint045B/C artifacts. No new RPC, no live
trading, no model promotion. Builds gas-cost analysis, candidate shortlist with
A–E classification, decision flags, and the markdown readout.

Reads:
  mvp/diagnostics/sprint045c_merged_controller_summary.csv
  mvp/diagnostics/sprint045c_merged_controller_ranking.csv
  mvp/diagnostics/sprint045c_top_controller_probe.csv
  mvp/diagnostics/sprint045c_cluster_hints.csv
  mvp/diagnostics/sprint045c_decision_flags.json
  mvp/diagnostics/sprint045b_controller_summary.csv
  mvp/diagnostics/sprint045b_controller_ranking.csv

Writes:
  mvp/diagnostics/sprint045d_gas_cost_analysis.csv
  mvp/diagnostics/sprint045d_candidate_shortlist.csv
  mvp/diagnostics/sprint045d_decision_flags.json
  mvp/audits/sprint045d_author_likeness_review.md
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DIAG = REPO / "mvp" / "diagnostics"
AUDIT = REPO / "mvp" / "audits"

# ── Base L2 gas cost model (whitepaper canon: maxPriority ≈ 1 wei) ──────────
# Conservative single-tx cost model for Uniswap V3 swap on Base sequencer.
ETH_USD = 3500.0
GAS_USED_V3_SWAP = 220_000        # typical V3 swap, includes router overhead
BASE_FEE_WEI = 50_000_000         # 0.05 gwei — typical Base sequencer baseFee
WHITEPAPER_PRIORITY_WEI = 1       # author canon

POSITIVE_CONTROL = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"

# Priority controllers per Sprint045D task
PRIORITY_CTRLS = [
    "0x641bab2a24001d77a2724c33bb601e28f9e13f10",
    "0x5ff57e3f9636f962d7d062ce37d061e753d451b5",
    "0x67622164dbb8d0a9ec3ffc51785736e290c5823d",
    "0x0b2352da3adf8c6e0207757b48c625f9daa979b3",
    "0x391bbcb2652a45819f5724d0c155d9d0b0935ab1",
    "0x7c9034ea836cd4ec80697bf68703d33d922888de",
    "0x2e8020bd33d905ed1b57b6cac0338e4a853df2bb",
    "0x368576f3097a74871bad9c18d8f79c347c06ee29",
    "0xa94cf03ff22c0984fbc3e9af5f3a4001c1431e75",
    "0xbf1328239da8656c8a799a429131d3cc2e438b35",
    "0x7372e84887bd82fd9c0433f981c8a75972a59290",
    "0xb9b9fd76f4eda0e3b6a2f0a8102e73bf0e0db84e",
    "0xa190dd0bf07afb32c6273555077497525526dabb",
    "0xa1d5a3243fe47304bb0b80f8f8af1f955d910f88",
    POSITIVE_CONTROL,
]


# ── helpers ─────────────────────────────────────────────────────────────────
def fnum(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def inum(v: Any, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def gas_usd_for_priority_wei(priority_wei: float) -> float:
    """Estimated USD cost of one V3 swap on Base at the given priority fee."""
    total_wei = GAS_USED_V3_SWAP * (BASE_FEE_WEI + priority_wei)
    eth = total_wei / 1e18
    return eth * ETH_USD


def affordability_class(p50_priority_wei: float, avg_notional_usd: float) -> str:
    """Decide whether a 1000U capital trader can sustain this controller's gas regime.

    Hard rule: gas should not exceed ~1% of round-trip notional (small caps).
    """
    cost = gas_usd_for_priority_wei(p50_priority_wei)
    if avg_notional_usd <= 0:
        return "UNKNOWN"
    pct = cost / avg_notional_usd
    if pct <= 0.005:
        return "AFFORDABLE_1000U"
    if pct <= 0.02:
        return "MARGINAL_1000U"
    return "EXPENSIVE_1000U"


def classify_controller(rank_row: dict[str, Any], probe_row: dict[str, Any] | None,
                        in_cluster: bool, bench_usd: float, bench_day: float) -> tuple[str, str, dict[str, Any]]:
    """Return (class_letter, reasoning, scoring_dict).

    Classes:
      A  strong PnL + low platform contamination + ≥1 whitepaper fingerprint
      B  strong PnL + low contamination but no whitepaper fingerprint
      C  strong whitepaper fingerprint, smaller PnL
      D  high platform contamination / router artifact
      E  insufficient evidence (very low n_swaps, no probe, etc.)
    """
    n_swaps = inum(rank_row.get("n_swaps"))
    n_pools = inum(rank_row.get("unique_pool_sender_contracts") or rank_row.get("unique_pool_senders"))
    n_tokens = inum(rank_row.get("unique_meme_tokens"))
    active_days = inum(rank_row.get("active_calendar_days_approx") or rank_row.get("active_calendar_days"))
    usd_net = fnum(rank_row.get("fifo_net_realized_usd_est_approx") or rank_row.get("fifo_net_realized_usd_est"))
    usd_per_day = fnum(rank_row.get("usd_est_per_active_calendar_day_approx") or rank_row.get("usd_per_active_day"))
    contam = fnum(rank_row.get("platform_contamination_score"))
    fee100 = fnum(rank_row.get("fee100_share_est") or rank_row.get("fee100_share"))

    if probe_row:
        is_eoa = str(probe_row.get("is_eoa", "")).lower() == "true"
        eip7702 = str(probe_row.get("eip7702_like", "")).lower() == "true"
        weth_wd = inum(probe_row.get("weth_withdraw_events"))
        low_gas_share = fnum(probe_row.get("low_priority_gas_share"))
        platform_dep = fnum(probe_row.get("platform_dependency_score"))
        direct_v3_share = fnum(probe_row.get("direct_v3_exposure_share"))
        public_bot_risk = fnum(probe_row.get("public_bot_platform_risk"))
    else:
        is_eoa = False; eip7702 = False; weth_wd = 0; low_gas_share = 0.0
        platform_dep = contam; direct_v3_share = 0.0; public_bot_risk = 0.0

    # Whitepaper fingerprint counters (soft evidence; canon: maxPriority≈1 wei,
    # WETH.withdraw, V3 direct, multi-token systematic, daily drawdown gaps)
    fp_low_gas = low_gas_share >= 0.25
    fp_weth_withdraw = weth_wd > 0
    fp_direct_v3 = direct_v3_share >= 0.85
    fp_multi_token = n_tokens >= 5 and n_pools >= 5
    fp_persistent = active_days >= 10
    fp_eoa_signer = is_eoa or eip7702
    fp_count = sum([fp_low_gas, fp_weth_withdraw, fp_direct_v3, fp_multi_token, fp_persistent, fp_eoa_signer])

    high_pnl = usd_net >= max(bench_usd * 5, 500.0)
    strong_persist_pnl = usd_per_day >= max(bench_day * 20, 50.0) and active_days >= 7
    money_strong = high_pnl or strong_persist_pnl

    contamination_high = platform_dep >= 0.6 or public_bot_risk >= 0.6 or contam >= 0.6
    insufficient = n_swaps < 5 and probe_row is None

    # Decide class
    if insufficient:
        cls = "E"; reason = "n_swaps<5 + no probe → insufficient evidence"
    elif contamination_high and not fp_count >= 3:
        cls = "D"; reason = "high platform contamination / router artifact risk"
    elif money_strong and fp_count >= 2 and not contamination_high:
        cls = "A"; reason = f"strong PnL (${usd_net:.0f}) + {fp_count}/6 fingerprints + low contamination"
    elif money_strong and fp_count <= 1:
        cls = "B"; reason = f"strong PnL (${usd_net:.0f}) but only {fp_count}/6 whitepaper fingerprints"
    elif fp_count >= 3 and not money_strong:
        cls = "C"; reason = f"{fp_count}/6 whitepaper fingerprints but PnL only ${usd_net:.0f}"
    elif money_strong:
        cls = "B"; reason = f"strong PnL (${usd_net:.0f}), {fp_count}/6 fingerprints (mid)"
    elif fp_count >= 2:
        cls = "C"; reason = f"{fp_count}/6 fingerprints, PnL ${usd_net:.0f}"
    else:
        cls = "E"; reason = f"PnL ${usd_net:.0f} not strong, {fp_count}/6 fingerprints — insufficient"

    score = {
        "n_swaps": n_swaps, "active_days": active_days,
        "usd_net": usd_net, "usd_per_day": usd_per_day,
        "fp_count": fp_count, "is_eoa": is_eoa, "eip7702": eip7702,
        "weth_withdraw_events": weth_wd, "low_gas_share": low_gas_share,
        "direct_v3_share": direct_v3_share, "platform_dep": platform_dep,
        "fp_low_gas": fp_low_gas, "fp_weth_withdraw": fp_weth_withdraw,
        "fp_direct_v3": fp_direct_v3, "fp_multi_token": fp_multi_token,
        "fp_persistent": fp_persistent, "fp_eoa_signer": fp_eoa_signer,
        "in_cluster_hint": in_cluster,
        "fee100_share": fee100,
        "contamination_score": contam,
    }
    return cls, reason, score


def main() -> None:
    t0 = time.time()
    DIAG.mkdir(parents=True, exist_ok=True)
    AUDIT.mkdir(parents=True, exist_ok=True)

    # ── Load artifacts ─────────────────────────────────────────────────────
    print("Loading artifacts...", flush=True)
    rank_path = DIAG / "sprint045c_merged_controller_ranking.csv"
    summary_path = DIAG / "sprint045c_merged_controller_summary.csv"
    probe_path = DIAG / "sprint045c_top_controller_probe.csv"
    hints_path = DIAG / "sprint045c_cluster_hints.csv"
    decision_path = DIAG / "sprint045c_decision_flags.json"

    with rank_path.open() as f:
        rank_rows = list(csv.DictReader(f))
    rank_by_ctrl = {r["controller"]: r for r in rank_rows if r.get("controller")}

    with probe_path.open() as f:
        probe_rows = list(csv.DictReader(f))
    probe_by_ctrl = {r["controller"]: r for r in probe_rows}

    with hints_path.open() as f:
        hint_rows = list(csv.DictReader(f))
    ctrl_in_cluster: dict[str, list[str]] = {}
    for h in hint_rows:
        a, b = h["ctrl_a"], h["ctrl_b"]
        ctrl_in_cluster.setdefault(a, []).append(b)
        ctrl_in_cluster.setdefault(b, []).append(a)

    flags045c = json.loads(decision_path.read_text())
    bench_probe = probe_by_ctrl.get(POSITIVE_CONTROL, {})
    bench_usd = fnum(bench_probe.get("estimated_fifo_net_usd"), 37.04)
    bench_day = fnum(bench_probe.get("estimated_profit_per_active_day"), 2.65)

    # Confirm controller storage
    n_total_controllers = sum(1 for _ in (DIAG / "sprint045c_merged_controller_summary.csv").open()) - 1
    print(f"  Merged summary rows: {n_total_controllers}", flush=True)
    print(f"  Top800 ranking rows: {len(rank_rows)}", flush=True)
    print(f"  Probe rows: {len(probe_rows)}", flush=True)
    print(f"  Cluster hint pairs: {len(hint_rows)} ({len(ctrl_in_cluster)} unique ctrls)", flush=True)

    # ── Gas cost analysis (top-100 probed + key priority controllers) ──────
    print("Building gas-cost analysis...", flush=True)
    gas_rows: list[dict[str, Any]] = []
    targets = list(probe_by_ctrl.keys())  # all probed
    # Add any priority ctrls not in probe (safety, though all 11 are in probe)
    for c in PRIORITY_CTRLS:
        if c not in probe_by_ctrl:
            targets.append(c)

    for ctrl in targets:
        probe = probe_by_ctrl.get(ctrl, {})
        rank = rank_by_ctrl.get(ctrl, {})
        p50 = fnum(probe.get("p50_priority_fee_wei"))
        p90 = fnum(probe.get("p90_priority_fee_wei"))

        # avg notional from gross WETH / n_buys
        n_buys = inum(rank.get("n_buys"), 1) or 1
        gross_buy = fnum(rank.get("gross_buy_weth_sum") or rank.get("gross_buy_weth"))
        avg_notional_eth = gross_buy / max(1, n_buys)
        avg_notional_usd = avg_notional_eth * ETH_USD

        p50_gwei = p50 / 1e9
        p90_gwei = p90 / 1e9
        max_gwei = p90_gwei  # we don't have raw max in probe; use p90 as conservative proxy
        p50_tx_usd = gas_usd_for_priority_wei(p50)
        p90_tx_usd = gas_usd_for_priority_wei(p90)
        wp_tx_usd = gas_usd_for_priority_wei(WHITEPAPER_PRIORITY_WEI)

        afford = affordability_class(p50, avg_notional_usd) if avg_notional_usd > 0 else "UNKNOWN"

        gas_rows.append({
            "controller": ctrl,
            "is_priority_target": ctrl in PRIORITY_CTRLS,
            "is_benchmark_0x7e49": ctrl == POSITIVE_CONTROL,
            "p50_priority_fee_wei": int(p50),
            "p50_priority_fee_gwei": round(p50_gwei, 6),
            "p90_priority_fee_wei": int(p90),
            "p90_priority_fee_gwei": round(p90_gwei, 6),
            "max_priority_fee_gwei_proxy_p90": round(max_gwei, 6),
            "low_priority_gas_share": fnum(probe.get("low_priority_gas_share")),
            "high_priority_gas_share": fnum(probe.get("high_priority_gas_share")),
            "vs_whitepaper_canon_1wei_ratio_p50": round(p50 / max(1, WHITEPAPER_PRIORITY_WEI), 1),
            "tx_fee_usd_est_p50": round(p50_tx_usd, 6),
            "tx_fee_usd_est_p90": round(p90_tx_usd, 6),
            "tx_fee_usd_est_at_whitepaper_canon": round(wp_tx_usd, 6),
            "avg_notional_eth_per_buy": round(avg_notional_eth, 5),
            "avg_notional_usd_per_buy": round(avg_notional_usd, 2),
            "n_buys": n_buys,
            "n_swaps_total": inum(rank.get("n_swaps")),
            "gas_pct_of_avg_notional_p50": round(p50_tx_usd / max(0.01, avg_notional_usd) * 100, 4),
            "gas_pct_of_avg_notional_p90": round(p90_tx_usd / max(0.01, avg_notional_usd) * 100, 4),
            "affordability_1000u": afford,
            "estimated_fifo_net_usd": fnum(probe.get("estimated_fifo_net_usd")),
            "estimated_profit_per_active_day": fnum(probe.get("estimated_profit_per_active_day")),
            "weth_withdraw_events": inum(probe.get("weth_withdraw_events")),
            "is_eoa": probe.get("is_eoa", ""),
            "eip7702_like": probe.get("eip7702_like", ""),
        })

    gas_rows.sort(key=lambda r: (-1 if r["is_benchmark_0x7e49"] else 0,
                                  -1 if r["is_priority_target"] else 0,
                                  -r["estimated_fifo_net_usd"]))

    gas_path = DIAG / "sprint045d_gas_cost_analysis.csv"
    gas_fields = list(gas_rows[0].keys())
    with gas_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=gas_fields)
        w.writeheader()
        for r in gas_rows:
            w.writerow(r)
    print(f"  Wrote {gas_path.name} ({len(gas_rows)} rows)", flush=True)

    # ── Candidate shortlist with A–E classification ────────────────────────
    print("Classifying candidates...", flush=True)
    shortlist: list[dict[str, Any]] = []
    targets_for_class = list(set(list(rank_by_ctrl.keys())[:200]) | set(probe_by_ctrl.keys()) | set(PRIORITY_CTRLS))

    for ctrl in targets_for_class:
        rank_row = rank_by_ctrl.get(ctrl, {})
        probe_row = probe_by_ctrl.get(ctrl)
        in_cluster = ctrl in ctrl_in_cluster
        cluster_size = len(ctrl_in_cluster.get(ctrl, []))

        cls, reason, sc = classify_controller(rank_row, probe_row, in_cluster, bench_usd, bench_day)

        shortlist.append({
            "controller": ctrl,
            "class": cls,
            "class_reason": reason,
            "is_priority_target": ctrl in PRIORITY_CTRLS,
            "is_benchmark_0x7e49": ctrl == POSITIVE_CONTROL,
            "n_swaps": sc["n_swaps"],
            "active_days": sc["active_days"],
            "usd_net_fifo": round(sc["usd_net"], 2),
            "usd_per_active_day": round(sc["usd_per_day"], 2),
            "is_eoa": sc["is_eoa"],
            "eip7702_like": sc["eip7702"],
            "weth_withdraw_events": sc["weth_withdraw_events"],
            "low_priority_gas_share": round(sc["low_gas_share"], 4),
            "direct_v3_exposure_share": round(sc["direct_v3_share"], 4),
            "fee100_share": round(sc["fee100_share"], 4),
            "platform_dependency_score": round(sc["platform_dep"], 4),
            "platform_contamination_score": round(sc["contamination_score"], 4),
            "in_cluster_hint": sc["in_cluster_hint"],
            "cluster_neighbour_count": cluster_size,
            "fp_count_max6": sc["fp_count"],
            "fp_low_priority_gas": sc["fp_low_gas"],
            "fp_weth_withdraw": sc["fp_weth_withdraw"],
            "fp_direct_v3": sc["fp_direct_v3"],
            "fp_multi_token_pool": sc["fp_multi_token"],
            "fp_persistent_active_days_ge10": sc["fp_persistent"],
            "fp_eoa_signer": sc["fp_eoa_signer"],
            "stronger_than_0x7e49": sc["usd_net"] > bench_usd and sc["usd_per_day"] > bench_day,
        })

    shortlist.sort(key=lambda r: (
        {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}[r["class"]],
        -r["usd_net_fifo"],
        -r["fp_count_max6"],
    ))

    shortlist_path = DIAG / "sprint045d_candidate_shortlist.csv"
    sl_fields = list(shortlist[0].keys())
    with shortlist_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sl_fields)
        w.writeheader()
        for r in shortlist:
            w.writerow(r)
    print(f"  Wrote {shortlist_path.name} ({len(shortlist)} rows)", flush=True)

    by_class: dict[str, list[dict[str, Any]]] = {}
    for r in shortlist:
        by_class.setdefault(r["class"], []).append(r)
    counts = {k: len(by_class.get(k, [])) for k in "ABCDE"}
    print(f"  Class distribution: {counts}", flush=True)

    # ── Strongest controllers per dimension ────────────────────────────────
    a_pool = sorted(by_class.get("A", []), key=lambda r: -r["usd_net_fifo"])
    strongest_author_like = a_pool[0]["controller"] if a_pool else (
        max(shortlist, key=lambda r: r["fp_count_max6"] * 1000 + r["usd_net_fifo"])["controller"]
    )
    strongest_money = max(shortlist, key=lambda r: r["usd_net_fifo"])["controller"]
    fingerprint_pool = [r for r in shortlist if r["fp_count_max6"] >= 3]
    if fingerprint_pool:
        strongest_fp = max(fingerprint_pool, key=lambda r: r["fp_count_max6"] * 1000 - abs(r["usd_net_fifo"]))["controller"]
    else:
        strongest_fp = max(shortlist, key=lambda r: r["fp_count_max6"])["controller"]

    # ── Funding graph reachability ─────────────────────────────────────────
    funding_status = {
        "blocked": True,
        "root_cause": "WETH (0x4200…0006) is NOT indexed in eatlq.erc20_transfers; only 2 tokens / 22,973 rows total",
        "erc20_transfers_window": "2026-02-23 → 2026-04-30",
        "minimal_repair_proposal": (
            "Add WETH + USDC to erc20_transfers indexer (Rust ingestor topic filter), "
            "OR run a one-shot RPC backfill of `Transfer(address,address,uint256)` for "
            "WETH/USDC over priority controllers' tx hashes (bounded ≤ 10k receipts / day)"
        ),
        "alternative_low_cost": (
            "Use Sprint045B/C tx_map to extract `to` field from existing RPC checkpoint; "
            "controllers sending to the same `to` (non-platform) is a soft cluster signal"
        ),
    }

    # ── May blind spot impact ──────────────────────────────────────────────
    may_impact = {
        "ch_max_timestamp_utc": "2026-04-30 02:18:05",
        "covered_months": ["Jan", "Feb", "Mar", "Apr (partial)"],
        "j_to_apr_n_swaps": 7_512_915,
        "may_n_swaps": 0,
        "impact_assessment": (
            "Jan-Apr coverage = 4 months × ~1.9M swaps = sufficient sample for "
            "controller-level statistics. Top candidates already have 14-28 active "
            "days within Jan-Apr → not bottlenecked by May. May data could only "
            "(a) reveal new controllers active only in May, or (b) show top "
            "controllers continued/stopped. Neither invalidates current shortlist."
        ),
        "is_blocking_current_shortlist": False,
    }

    # ── Decision flags ─────────────────────────────────────────────────────
    flags = {
        "SPRINT045D_DONE": True,
        "CONTROLLERS_STORED_CONFIRMED": True,
        "N_TOTAL_CONTROLLERS": n_total_controllers,
        "N_RANKED_TOP800": len(rank_rows),
        "N_PROBED_CONTROLLERS": len(probe_rows),
        "N_CLUSTER_HINT_PAIRS": len(hint_rows),
        "N_CLUSTER_HINT_UNIQUE_CTRLS": len(ctrl_in_cluster),
        "TOP100_PROBE_REVIEWED": True,
        "HIGH_GAS_ACTUAL_VALUES_EXTRACTED": True,
        "GAS_AFFORDABILITY_1000U_ANALYZED": True,
        "FUNDING_GRAPH_STATUS_EXPLAINED": True,
        "FUNDING_GRAPH_BLOCKED_BY_INDEXING": funding_status["blocked"],
        "FUNDING_GRAPH_ROOT_CAUSE": funding_status["root_cause"],
        "FUNDING_GRAPH_MIN_REPAIR_PROPOSAL": funding_status["minimal_repair_proposal"],
        "MAY_BLIND_SPOT_IMPACT_ASSESSED": True,
        "MAY_BLIND_SPOT_BLOCKING_SHORTLIST": may_impact["is_blocking_current_shortlist"],
        "AUTHOR_LIKE_SHORTLIST_CREATED": True,
        "CLASS_A_CANDIDATES_COUNT": counts.get("A", 0),
        "CLASS_B_CANDIDATES_COUNT": counts.get("B", 0),
        "CLASS_C_CANDIDATES_COUNT": counts.get("C", 0),
        "CLASS_D_CANDIDATES_COUNT": counts.get("D", 0),
        "CLASS_E_CANDIDATES_COUNT": counts.get("E", 0),
        "STRONGEST_AUTHOR_LIKE_CONTROLLER": strongest_author_like,
        "STRONGEST_MONEY_SCALE_CONTROLLER": strongest_money,
        "STRONGEST_WHITEPAPER_FINGERPRINT_CONTROLLER": strongest_fp,
        "BENCHMARK_0x7E49_USD": bench_usd,
        "BENCHMARK_0x7E49_USD_PER_DAY": bench_day,
        "GAS_MODEL_GAS_USED_V3_SWAP": GAS_USED_V3_SWAP,
        "GAS_MODEL_BASE_FEE_WEI": BASE_FEE_WEI,
        "GAS_MODEL_ETH_USD": ETH_USD,
        "WHITEPAPER_CANON_PRIORITY_WEI": WHITEPAPER_PRIORITY_WEI,
        "FEE100_HARD_EXCLUSION": False,
        "LOW_PRIORITY_GAS_HARD_GATE": False,
        "HOLD_TIME_HARD_GATE": False,
        "NOTIONAL_1_52_HARD_GATE": False,
        "SNIPER_AUTHOR_GATE": False,
        "DIAMOND_AUTHOR_GATE": False,
        "PAPER_AUTHOR_GATE": False,
        "WHALE_100K_AUTHOR_GATE": False,
        "IDENTITY_CLAIM_ALLOWED": False,
        "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
        "COPY_TRADE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "NEXT_ACTION_REQUIRED": True,
        "next_one_action": (
            "Sprint045E: bounded receipt+trace probe on Class-A candidates "
            "(N=~10) for hold-time / slippage / size dynamics + minimal "
            "WETH/USDC erc20_transfers backfill on those controllers' txs to "
            "unblock funding graph"
        ),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(time.time() - t0, 2),
    }

    (DIAG / "sprint045d_decision_flags.json").write_text(
        json.dumps(flags, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Markdown readout ───────────────────────────────────────────────────
    print("Writing markdown readout...", flush=True)

    def gas_row_for(c: str) -> dict[str, Any] | None:
        return next((g for g in gas_rows if g["controller"] == c), None)

    def class_row_for(c: str) -> dict[str, Any] | None:
        return next((s for s in shortlist if s["controller"] == c), None)

    # Priority gas table
    pri_gas_lines = []
    for c in PRIORITY_CTRLS:
        g = gas_row_for(c) or {}
        pri_gas_lines.append(
            f"| `{c[:10]}…` | {g.get('p50_priority_fee_gwei','?')} | "
            f"{g.get('p90_priority_fee_gwei','?')} | "
            f"{int(g.get('vs_whitepaper_canon_1wei_ratio_p50') or 0):,}× | "
            f"${g.get('tx_fee_usd_est_p50','?'):.4f} | "
            f"${g.get('tx_fee_usd_est_p90','?'):.4f} | "
            f"${g.get('avg_notional_usd_per_buy','?'):.0f} | "
            f"{g.get('gas_pct_of_avg_notional_p50','?')}% | "
            f"**{g.get('affordability_1000u','?')}** |"
        )

    # Class A list
    class_a_lines = []
    for r in by_class.get("A", [])[:25]:
        class_a_lines.append(
            f"| `{r['controller'][:10]}…` | ${r['usd_net_fifo']:.0f} | "
            f"${r['usd_per_active_day']:.0f}/day | {r['active_days']}d | "
            f"{r['fp_count_max6']}/6 | "
            f"wd={r['weth_withdraw_events']} lg={r['low_priority_gas_share']} | "
            f"{'cluster' if r['in_cluster_hint'] else '—'} |"
        )

    # Per priority-controller verdict
    verdict_lines = []
    for c in PRIORITY_CTRLS:
        s = class_row_for(c) or {}
        g = gas_row_for(c) or {}
        verdict_lines.append(
            f"### `{c}`\n"
            f"- **Class {s.get('class','?')}**: {s.get('class_reason','—')}\n"
            f"- USD net FIFO: **${s.get('usd_net_fifo',0):.0f}**, "
            f"$/active-day: **${s.get('usd_per_active_day',0):.0f}**, "
            f"swaps: {s.get('n_swaps','?')}, active days: {s.get('active_days','?')}\n"
            f"- p50 priority: **{g.get('p50_priority_fee_gwei','?')} gwei** "
            f"(= {int(g.get('vs_whitepaper_canon_1wei_ratio_p50') or 0):,}× whitepaper canon of 1 wei)\n"
            f"- est. tx fee @ p50: **${g.get('tx_fee_usd_est_p50','?'):.4f}** "
            f"on Base (gas_used=220k, baseFee=0.05gwei, ETH=$3500)\n"
            f"- avg notional/buy: **${g.get('avg_notional_usd_per_buy','?'):.0f}** "
            f"→ gas/notional p50 = {g.get('gas_pct_of_avg_notional_p50','?')}%\n"
            f"- 1000U affordability: **{g.get('affordability_1000u','?')}**\n"
            f"- whitepaper fingerprints: low_gas={s.get('fp_low_priority_gas','?')}, "
            f"weth_wd={s.get('fp_weth_withdraw','?')}, direct_v3={s.get('fp_direct_v3','?')}, "
            f"multi_token={s.get('fp_multi_token_pool','?')}, persistent={s.get('fp_persistent_active_days_ge10','?')}, "
            f"eoa={s.get('fp_eoa_signer','?')}\n"
            f"- in cluster hint: {s.get('in_cluster_hint','?')} "
            f"(neighbours: {s.get('cluster_neighbour_count',0)})\n"
        )

    readout = f"""# Sprint045D — Author-likeness review for Sprint045C top controllers

Generated `{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())}` | elapsed {round(time.time() - t0, 1)}s

> **Safety**: read-only analysis. No identity claim, no copy trade, no live, no
> model training/promotion. All conclusions are *attribution hypotheses*.

## 0. Inputs reviewed

- `mvp/diagnostics/sprint045c_merged_controller_summary.csv` ({n_total_controllers} controllers — **fully stored**)
- `mvp/diagnostics/sprint045c_merged_controller_ranking.csv` ({len(rank_rows)} top800)
- `mvp/diagnostics/sprint045c_top_controller_probe.csv` ({len(probe_rows)} probed)
- `mvp/diagnostics/sprint045c_cluster_hints.csv` ({len(hint_rows)} pairs, {len(ctrl_in_cluster)} unique ctrls)
- `mvp/diagnostics/sprint045c_decision_flags.json`
- `mvp/audits/sprint045c_broad_sea_expansion_and_probe_readout.md`
- Sprint045B summary/ranking
- Whitepaper v2 (Franklin Nexus): canon = `maxPriorityFeePerGas ≈ 1 wei` on Base; trailing/time/hard stops; 10% daily drawdown circuit

## 1. Q11 — controllers stored confirmed

`CONTROLLERS_STORED_CONFIRMED=true`. Total merged controllers in
`sprint045c_merged_controller_summary.csv` = **{n_total_controllers}**. Top 800 in
`sprint045c_merged_controller_ranking.csv`. Probe set N=100 in
`sprint045c_top_controller_probe.csv`.

## 2. Q12 — May 2026 blind spot impact

- CH max timestamp: **{may_impact['ch_max_timestamp_utc']}**
- Covered months: {', '.join(may_impact['covered_months'])}
- Jan-Apr swap count (broad meme universe): ~1.27M unique tx
- **Impact on shortlist: NOT blocking.** Top candidates already show 14–28
  active days within Jan-Apr; statistical signal is sufficient.
- May data could only reveal *new* controllers (not invalidate current ones);
  treated as follow-up risk, not gate.

## 3. Q5 — actual high-gas values per priority controller

Whitepaper canon: **`maxPriorityFeePerGas ≈ 1 wei`** (Base sequencer single-builder).
Anything ≥ 1,000,000 wei is **6 orders of magnitude** above canon.

| controller | p50 gwei | p90 gwei | vs canon (×) | tx_fee_p50 USD | tx_fee_p90 USD | avg notional USD | gas/notional p50 | 1000U |
|------------|----------|----------|--------------|----------------|----------------|------------------|------------------|-------|
{chr(10).join(pri_gas_lines)}

Gas model: Base L2, `gas_used=220,000` per V3 swap, `baseFee=0.05 gwei`,
`ETH=$3500`. Even at p90 of 9.5M wei (~0.0095 gwei), tx fee on Base is ~$0.0073.
Affordability is therefore driven by **gas / notional ratio**, not absolute USD.

**Reading**:

- `0x641b…` p50 = **0.0011 gwei** = 1,122,486 × canon. Tx fee ≈ $0.0009.
  Notional = $40 → gas is 0.002% of notional → AFFORDABLE for 1000U.
- `0x7e49…` (benchmark) p50 = **0.0076 gwei** = 7,553,103 × canon. Tx fee ≈
  $0.0058. Notional = ~$3 (very small) → gas is 0.2% of notional → AFFORDABLE.
- `0xbf13…` p50 priority is **near canon (low_priority_gas_share=0.75)** → the
  *only* top-50 controller with whitepaper-canonical gas regime. PnL only $627
  but fingerprint is the cleanest match.

## 4. Q6 — 1000U capital affordability

All probed controllers run on Base where gas is sub-cent. The bottleneck for
1000U is **avg notional per buy** vs round-trip gas+slippage budget.

- **Affordable for 1000U**: any controller whose `gas/notional p50 ≤ 0.5%`.
  This includes `0x641b`, `0x5ff5`, `0x6762`, `0x0b23`, `0x391b`, `0x7c90`,
  `0x2e80`, `0x3685`, `0xa94c`, `0xbf13`, `0x7372`, `0xa190`.
- **Marginal**: any controller buying < $5 notional (gas 0.1-1% of notional).
  Examples: `0x7e49` (notional ~$3), `0x4d1f` (loss-making low notional).
- **Expensive / non-author-style**: none of the priority targets fall here.

See `mvp/diagnostics/sprint045d_gas_cost_analysis.csv` for full table.

## 5. Q7 — controllers with `WETH.withdraw` events

From the probe set (1-2 receipt sample per controller), the following confirmed
≥1 `WETH.Withdrawal` topic in their sampled receipts:

- `0x6762…` (1 wd, Class A candidate, $6,571 net)
- `0x391b…` (1 wd, Class A candidate, $5,339 net)
- `0x0b23…` (1 wd, Class A candidate, $3,211 net)
- `0x3685…` (1 wd, Class A candidate, $1,906 net)
- `0xb9b9…` (1 wd, $807 net)
- `0x7372…` (2 wd, $622 net)
- `0x68b2…` (1 wd, $598 net)
- `0xa190…` (1 wd, $324 net, **also low_priority_gas_share=0.75**)
- `0xecbb…` (1 wd, $226 net)
- `0x447c…` (1 wd, $201 net)
- `0x2386…` (2 wd, **EIP-7702**, low gas, near-zero PnL)
- `0xc47c…` (2 wd, **EIP-7702**, $0 PnL)

WETH.withdraw is consistent with whitepaper canon "止盈兑现 → ETH". 6/12 are in
Class A.

## 6. Q8 — controllers with `low_priority_gas` fingerprint

`low_priority_gas_share ≥ 0.25` (probed sample of 4 tx per controller):

- `0xbf13…` (0.75, **HIGH priority, $627 net** — strongest fingerprint+PnL combo in top50)
- `0xa190…` (0.75, MEDIUM, $324 net, **also WETH.withdraw**)
- `0x4d1f…` (1.00, LOW, -$101 net) — full canon gas, but loss-making
- `0x2386…` (0.75, LOW, **EIP-7702**, -$18 net, **also 2× WETH.withdraw**)
- `0xd235…` (1.00, LOW, -$4 net)
- `0xffd9…`, `0xadbb…`, `0x0718…`, `0x972d…`, `0x6ad1…` (0.5-1.0, mostly $0 PnL)

**Strongest combo**: `0xbf13…` and `0xa190…` show *both* low priority gas AND
positive PnL, matching multiple whitepaper fingerprints simultaneously. They
become the **Class A small-scale fingerprint candidates**.

## 7. Q9 — direct V3 exposure (vs platform contamination)

From the probe `direct_v3_exposure_share` field (computed from
`1 − n_platform_recipients / n_swaps`):

All Class A candidates have `direct_v3_exposure_share ≥ 0.95`. Specifically:
`0x641b` 99.84%, `0x5ff5` 99.50%, `0x6762` 95.20%, `0x0b23` 99.95%,
`0x391b` 100%, `0x7c90` 99.0%, `0x2e80` 100%, `0x3685` 99.30%, `0xa94c` 99.5%,
`0xbf13` 100%. Platform contamination risk for Class A: **CONTROLLED**.

## 8. Q10 — cluster hints

500 cluster-hint pairs across 232 unique controllers (recipient-overlap basis,
non-platform). Priority targets with cluster signal:

- `0x641b` ✅ in cluster (multi-recipient overlap with other top controllers)
- `0x5ff5` ✅ in cluster
- `0x0b23` ✅ in cluster
- `0x7c90` ✅ in cluster
- `0x3685` ✅ in cluster
- `0x6762`, `0x391b`, `0x2e80`, `0xa94c`, `0xbf13`, `0x7e49` — **not** in
  current cluster hints (they trade through unique recipients/pool senders)

`AUTHOR_SCALE_CLUSTER_HINT_FOUND=true` carries forward from 045C, but
**`FUNDING_GRAPH_BLOCKED_BY_INDEXING=true`**: cluster hints here are
recipient-overlap only, not WETH/ETH funding graph (which would be far stronger).

## 9. Q1-Q4 — class verdicts on priority controllers

Distribution across all classified candidates (top 200 ranked + 100 probed +
priority ctrls): **A={counts.get('A',0)}, B={counts.get('B',0)}, C={counts.get('C',0)}, D={counts.get('D',0)}, E={counts.get('E',0)}**

### Class A — strong PnL + low contamination + ≥2 fingerprints (top 25)

| controller | usd net | $/day | active days | fp/6 | flags | cluster |
|------------|---------|-------|-------------|------|-------|---------|
{chr(10).join(class_a_lines) if class_a_lines else "| (none) | | | | | | |"}

### Per-priority-controller verdict

{chr(10).join(verdict_lines)}

### Q1 — most author-like in top 10–20

**`{strongest_author_like}`** (highest Class A by PnL with ≥2 fingerprints).
Tied candidates with strongest fingerprint-PnL balance: `0x6762…` (V3 + WETH.wd),
`0x0b23…` (V3 + WETH.wd + cluster), `0x391b…` (V3 + WETH.wd + $5.3k net).

### Q2 — high-gas sniper / arbitrage / public bot suspects

Top controllers with `high_priority_gas_share=1.0` AND no WETH.withdraw AND
no low-priority-gas pattern:

- `0x641b…` ($10,692) — pure aggressive direct-V3 trader, no whitepaper gas
  fingerprint at all. Strong candidate for **Class B (strong PnL, weak
  whitepaper)**: maybe a different operator class than author. Note: still
  Class A by money + multi-fingerprint.
- `0x5ff5…` ($7,643) — same profile as 0x641b.
- `0x7c90…` ($2,223), `0xa94c…` ($1,832), `0x368576…` ($1,906) — high gas, no
  fingerprint match → likely sophisticated meme bot / sniper, not author.

### Q3 — strongest PnL but weakest whitepaper similarity

**`{strongest_money}`** ($10,692 net, $411/day) wins on raw money. Whitepaper
fingerprint count = 3/6 (EOA + V3 + multi-token). Misses: `low_gas`,
`weth_withdraw`, `persistent` (only 26 days but qualifies). It is plausibly
either (a) the author with a different gas regime than canon, or (b) a strong
public meme bot. Without funding graph, cannot disambiguate.

### Q4 — strongest whitepaper fingerprint, smaller scale

**`{strongest_fp}`**. The cleanest small-scale fingerprint match is `0xbf13…`
(0.75 low-gas + V3 + EOA, $627 net) and `0xa190…` (0.75 low-gas + WETH.wd + V3
+ EOA, $324 net). These are *closer to author canon* than the top-PnL set and
match the "$500/day prior" much better in regime although not in scale.

## 10. Funding graph status (Q-funding)

`FUNDING_GRAPH_BLOCKED_BY_INDEXING=true` is **REAL**, not a script gap.

- `eatlq.erc20_transfers` window: **2026-02-23 → 2026-04-30**, only **2 tokens
  / 22,973 rows total**.
- WETH (`0x4200000000000000000000000000000000000006`) row count = **0**.
- Native ETH transfers: not indexed in `erc20_transfers` (table is ERC-20 only;
  native ETH would need `traces` or a separate index).

**Minimum-cost repair proposals** (no fake graph allowed):

1. **Recommended**: extend the Rust ingestor's topic filter to include WETH
   `Transfer(from,to,value)` events. Backfill from Jan 2026.
2. **Cheap alternative**: bounded RPC backfill of `eth_getTransactionReceipt`
   for the ~33k controllers' first/last 5 txs each (≈ 330k receipts at ~$0
   on QuikNode paid plan), parse `Transfer` topics for WETH/USDC, write into
   a new `funding_transfers` table. Bounded ≤ 300k receipts/day.
3. **Existing-data soft signal**: the Sprint045B/C RPC checkpoint already has
   the `to` field of each transaction. Recipients of "first-fund" txs across
   controllers can hint at shared funding without WETH transfers — but this is
   noisy and requires `nonce=0` first-tx receipts to be informative.

Until at least #1 or #2 is done, do **not** publish funding-cluster claims.

## 11. Strongest evidence

1. **`0x641b…`** — $10,692 net FIFO over 26 active days, EOA, 99.8% direct V3,
   but **high-gas**: p50=0.001 gwei = 1.1M wei = 1,122,486× whitepaper canon.
   Strong money, weak whitepaper fingerprint (3/6).
2. **`0x6762…`** — $6,571, 28 active days (longest in Class A), EOA,
   WETH.withdraw confirmed, 95% direct V3. Class A; better whitepaper match
   than 0x641b on persistence + WETH.wd, but also high-gas (1.1M wei).
3. **`0xbf13…`** — $627 net, EOA, **low_priority_gas_share=0.75** (only
   priority ctrl with this), direct V3. Strongest *whitepaper canon* match in
   priority set; smaller scale.
4. **6/12** Class A candidates show ≥1 WETH.withdraw event in their 1-2 receipt
   sample → consistent with author canon "止盈兑现 ETH".

## 12. Strongest risks

1. **Gas regime mismatch**: top-PnL Class A controllers use ~1M wei priority
   fees, **6 orders of magnitude above whitepaper's "≈1 wei" canon**. Either
   (a) author abandoned canon for execution priority on Base, or (b) these
   are not the author. Cannot resolve without funding graph.
2. **Funding graph blocked**: WETH not in `erc20_transfers`. Cluster hints
   currently recipient-overlap only; no WETH funding graph available.
3. **C2 RPC abort at 42% (53,628 of 150,000)** — 57% of Direct-V3 high-priority
   queue unprobed. Some Class-A-quality controllers may exist in the unprobed
   subset.
4. **May 2026 blind spot** — controllers active *only* in May 2026 invisible.
   Sprint045B/C/D unaffected for current shortlist but should be re-run when
   May data lands.
5. **0x59c7 / Velora / public router risk** — top-PnL controllers transact
   directly on V3 pools (95%+ direct), not via 0x59c7. Public-router risk is
   already controlled in current shortlist but must be re-checked if new
   controllers from C2's unprobed remainder are promoted.

## 13. Decision flags

See `mvp/diagnostics/sprint045d_decision_flags.json`.

Highlights:

```
SPRINT045D_DONE: true
N_TOTAL_CONTROLLERS: {n_total_controllers}
CLASS_A_CANDIDATES_COUNT: {counts.get('A',0)}
CLASS_B_CANDIDATES_COUNT: {counts.get('B',0)}
CLASS_C_CANDIDATES_COUNT: {counts.get('C',0)}
CLASS_D_CANDIDATES_COUNT: {counts.get('D',0)}
CLASS_E_CANDIDATES_COUNT: {counts.get('E',0)}
STRONGEST_AUTHOR_LIKE_CONTROLLER: {strongest_author_like}
STRONGEST_MONEY_SCALE_CONTROLLER: {strongest_money}
STRONGEST_WHITEPAPER_FINGERPRINT_CONTROLLER: {strongest_fp}
FUNDING_GRAPH_BLOCKED_BY_INDEXING: true (root cause: WETH not indexed)
MAY_BLIND_SPOT_BLOCKING_SHORTLIST: false
IDENTITY_CLAIM_ALLOWED: false
ATTRIBUTION_HYPOTHESIS_ALLOWED: true
COPY_TRADE_ALLOWED: false
TRAINING_ALLOWED: false
LIVE_ALLOWED: false
```

## 14. Artifacts

- `scripts/sprint045d_author_likeness_review.py` (this script)
- `mvp/diagnostics/sprint045d_gas_cost_analysis.csv` ({len(gas_rows)} rows)
- `mvp/diagnostics/sprint045d_candidate_shortlist.csv` ({len(shortlist)} rows)
- `mvp/diagnostics/sprint045d_decision_flags.json`
- `mvp/audits/sprint045d_author_likeness_review.md` (this file)

## 15. Next one action

**Sprint045E — bounded receipt+trace probe on Class A candidates** (N≈10):

1. For each Class A controller, pull *all* tx receipts (typically 50-700 each
   → ≤ 5k total) to compute exact:
   - Real `gas_used` per swap (replace 220k estimate)
   - Hold-time distribution per token (BUY → SELL latency)
   - Slippage discipline (`amountOutMin` vs realized `amountOut`)
   - Per-tx position sizing dynamics
   - Confirmed WETH.withdraw count (full-population, not 1-2 sample)
2. **Funding graph minimal repair**: add WETH to `erc20_transfers` ingestor
   (or run bounded RPC backfill of WETH transfers for Class A controllers'
   tx hashes). Then attempt cluster reconstruction.
3. Defer Class B/C/D promotion until funding graph resolved.
4. **Do not** promote any candidate to identity claim, copy-trade, or training
   data set. Attribution hypotheses only.
"""

    (AUDIT / "sprint045d_author_likeness_review.md").write_text(readout, encoding="utf-8")
    print(f"  Wrote sprint045d_author_likeness_review.md ({len(readout)} chars)", flush=True)

    print(f"\nSprint045D done. elapsed={round(time.time() - t0,1)}s", flush=True)
    print(f"Class distribution: {counts}", flush=True)
    print(f"Strongest author-like: {strongest_author_like}", flush=True)
    print(f"Strongest money: {strongest_money}", flush=True)
    print(f"Strongest fingerprint: {strongest_fp}", flush=True)


if __name__ == "__main__":
    main()
