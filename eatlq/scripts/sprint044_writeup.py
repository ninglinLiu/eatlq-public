"""Sprint044 — Final writeup: build audit MD + final summary JSON + cluster CSV.

Reads:
  mvp/diagnostics/sprint044_global_signer_candidates.csv
  mvp/diagnostics/sprint044_phase5_summary.json

Writes:
  mvp/audits/sprint044_global_author_scale_operator_rediscovery.md
  mvp/diagnostics/sprint044_author_scale_summary.json  (overwrites)
  mvp/diagnostics/sprint044_operator_clusters.csv       (overwrites)
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"
AUDIT_DIR = REPO_ROOT / "mvp" / "audits"

ETH_USD = 3500.0


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


def main() -> None:
    cands = load_csv(DIAG_DIR / "sprint044_global_signer_candidates.csv")
    cands.sort(key=lambda r: float(r["fifo_net_realized_weth"]), reverse=True)

    p5 = json.loads((DIAG_DIR / "sprint044_phase5_summary.json").read_text())

    pos_ctrl = {
        "address": "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a",
        "lane": "B_velora_routed",
        "etype": "EIP-7702",
        "fifo_net_realized_weth_2026_02_window": 0.01281,
        "fifo_net_realized_usd_est_2026_02_window": 44.83,
        "fifo_pairs": 23,
        "fifo_win_rate": 0.8696,
        "fifo_trimmed_mean_pct_window": 12.742,
        "max_priority_fee_share_zero_wei": 1.0,
        "is_self_caller_in_sample": True,
        "n_distinct_controllers": 1,
        "active_window": "2026-02-14 → 2026-02-28",
        "scale_match_500_usd_per_day": False,
        "interpretation": (
            "best LASZLO-canon match by fingerprint (0 wei gas, EIP-7702 self "
            "signer, direct Velora) but bounded sample PnL is far below "
            "USER_PRIOR $500/day; remains attribution-hypothesis only."
        ),
    }

    top20 = cands[:20]
    summary: dict[str, Any] = {
        "sprint": "044_global_author_scale_operator_rediscovery",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": (
            "global rediscovery of author-scale operators on Base V3 WETH-leg "
            "meme universe Jan-Apr 2026, NOT a re-validation of 0x7e49"
        ),
        "data_coverage_caveats": {
            "uniswap_v3_swaps_window": "2025-10-01 → 2026-04-30 02:18:05",
            "may_2026_v3_data_present": False,
            "erc20_transfers_indexed_tokens": 2,
            "weth_transfers_indexed": False,
            "funding_profit_graph_available": False,
            "aerodrome_vamm_swaps_window": "2026-02-23 → 2026-04-30",
        },
        "user_priors_AUDIT_ONLY": {
            "author_dev_start": "2026-01 (USER_PRIOR)",
            "author_active_window": "2026-01/02 → 2026-05 (USER_PRIOR)",
            "claimed_usd_per_day": 500,
            "stealth_anti_tracking_mentioned": True,
            "current_balance_low_not_disqualifying": True,
        },
        "phase_outcomes": {
            "phase1_preflight": {
                "v3_swaps_jan_apr_2026_meme_weth": 1041045,
                "distinct_recipients": 211359,
                "recipients_with_min4_swaps": 38986,
                "abort_breached": False,
            },
            "phase2_lane_a_direct_v3": {
                "n_returned": 5000,
                "router_exclusions_applied": 18,
                "elapsed_seconds": 6.7,
            },
            "phase2_lane_bc_velora_weth": {
                "status": "EMPTY",
                "reason": "erc20_transfers indexes only 2 tokens; WETH absent",
            },
            "phase3_authorstyle_filter_and_approx_fifo": {
                "after_filter": 32,
                "fifo_top_N_evaluated": 32,
                "elapsed_seconds": 1.4,
            },
            "phase4_true_fifo_and_rpc_classification": {
                "n_candidates": 31,
                "all_smart_contracts": True,
                "all_is_self_caller_false": True,
                "rpc_calls": 62,
            },
            "phase5_multi_tx_gas_and_controllers": {
                "rpc_calls": p5.get("rpc_calls_used_phase5"),
                "shared_controllers_count": len(p5.get("shared_controllers", {})),
                "candidates_with_priority_zero_dominant": 0,
                "candidates_with_priority_le5_dominant": 0,
                "candidates_with_priority_gt_1e5_dominant": 12,
                "n_with_n_controllers_ge_3": sum(
                    1
                    for r in cands[:12]
                    if int(r.get("multi_tx_n_controllers", "0")) >= 3
                ),
            },
        },
        "primary_finding": (
            "All top-12 author-style filter-passing recipients are multi-user "
            "public bot platforms (3-8 distinct controllers per 8 sample tx, "
            "tx.to == recipient bot contract, priority gas 1M-20G wei). None "
            "match LASZLO-canon (0/1/<=5 wei priority) at sample scale. "
            "Aggregate PnL belongs to many users, not a single author. "
            "Cross-recipient controller overlap is zero, so no cluster yet."
        ),
        "answer_is_author_scale_candidate_found": False,
        "answer_explanation": (
            "Within CH-only and 200-RPC budget, NO new single-EOA / single-"
            "smart-account candidate dominates a author-style fingerprint "
            "with realized PnL approaching USER_PRIOR $500/day. The Lane A "
            "high-PnL recipients are public bot platforms; the Lane B Velora "
            "WETH-receiver lane is unavailable due to indexing gap. 0x7e49 "
            "remains the closest fingerprint match but is under-scale ($45 "
            "in 14 days). May 2026 is outside CH coverage entirely."
        ),
        "is_new_address_stronger_than_0x7e49": False,
        "positive_control": pos_ctrl,
        "top20_by_fifo_net_realized_weth_AUDIT_ONLY": [
            {k: r[k] for k in r if k not in ("multi_tx_top_to_dist",)}
            for r in top20
        ],
        "decision_flags": {
            "ROUTER_ARTIFACT_FIXED": True,
            "AUTHOR_WALLET_FOUND": False,
            "STRONG_OPERATOR_CANDIDATE_NEW": False,
            "MULTI_WALLET_CLUSTER_SUSPECTED": False,
            "VELORA_ROUTE_COMPATIBLE_WITH_AUTHOR": True,
            "PROFIT_SCALE_MATCHES_500D": False,
            "IDENTITY_CLAIM_ALLOWED": False,
            "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
            "COPY_TRADE_ALLOWED": False,
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "NEXT_QUERY_REQUIRED": True,
            "MAY_2026_BLIND_SPOT": True,
            "FUNDING_GRAPH_BLOCKED_BY_INDEXING": True,
            "BOT_PLATFORM_CONTAMINATION_CONFIRMED": True,
            "SINGLE_ACCOUNT_AUTHOR_RULED_OUT_IN_LANE_A": True,
        },
        "next_query_plan_phase_b": {
            "kind": "controller-keyed RPC enumeration",
            "rationale": (
                "All top recipients are bot contracts with 3-8 distinct tx.from "
                "controllers. Author may be one of those controllers. We need "
                "to enumerate ALL tx.from across top-12 bot trades, build a "
                "by-controller PnL table, and apply LASZLO gas/notional/hold "
                "filter at controller level."
            ),
            "preflight_estimate": {
                "tx_to_pull_from_ch": "<= 30,000 unique tx_hashes (top-12 swap rows)",
                "rpc_eth_getTransactionByHash_calls": "~30,000",
                "expected_walltime_minutes": "30 - 90 depending on RPC RPS",
                "abort_thresholds": {
                    "row_cap_per_query": 500_000,
                    "rpc_call_cap": 50_000,
                    "controller_fanout_cap": 1000,
                },
            },
            "lane_b_workaround": (
                "If RPC budget allows, also enumerate tx_hashes where v3 "
                "recipient ∈ {Velora, Universal Router, 1inch} during "
                "Jan-Apr 2026 (estimated ~80,000 tx) and resolve tx.from to "
                "find Velora end-users, replicating Sprint042 across the "
                "broader window."
            ),
            "lane_d_aero": (
                "Aerodrome vAMM swaps (278k rows Feb-Apr) currently NOT "
                "evaluated; same recipient-keyed approach available, with "
                "the same router caveat."
            ),
            "may_2026_recovery": (
                "If user requires May 2026 coverage, repair ingestor backfill "
                "or load May data via a separate RPC scan window."
            ),
        },
        "what_could_falsify_or_confirm": {
            "confirm_author": [
                "controller-keyed PnL ranking surfaces a single tx.from EOA "
                "whose WETH realized PnL >= $5k with LASZLO gas fingerprint",
                "WETH transfer index repaired and a stable profit sink "
                "ties together multiple candidate signers",
                "May 2026 data added and a new bot platform / signer matches "
                "scale + fingerprint",
                "trace recovery confirms 0x7e49 sustained activity beyond "
                "Feb 2026 and per-day PnL approaches $500",
            ],
            "falsify_author": [
                "every top controller tx.from belongs to a public bot platform "
                "user (no single author dominance)",
                "no signer with both LASZLO gas fingerprint AND scale ever "
                "appears in any month",
                "FAILED_NO_7D_ALPHA never lifted in author repo and Sprint042 "
                "0x7e49 sample remains the only LASZLO-canon trace",
            ],
        },
    }

    out_summary = DIAG_DIR / "sprint044_author_scale_summary.json"
    out_summary.write_text(json.dumps(summary, indent=2, default=str))

    cluster_cols = [
        "cluster_id",
        "members",
        "rationale",
        "shared_evidence",
        "members_total_net_realized_weth",
        "members_total_usd_per_day_est",
        "status",
    ]
    clusters = [
        {
            "cluster_id": "C0_no_cross_recipient_controller_overlap",
            "members": "n/a",
            "rationale": (
                "Phase 5 RPC over top-12 recipients found 0 controllers shared "
                "between any pair of recipients."
            ),
            "shared_evidence": "0",
            "members_total_net_realized_weth": 0,
            "members_total_usd_per_day_est": 0,
            "status": "NO_CLUSTER_FOUND_AT_RECIPIENT_LEVEL",
        },
        {
            "cluster_id": "C1_funding_profit_graph_DEFERRED",
            "members": "n/a",
            "rationale": (
                "erc20_transfers indexes only 2 tokens; WETH transfers absent. "
                "Funding-source and profit-sink clustering require either "
                "(a) WETH transfer backfill, or (b) RPC trace_block enumeration "
                "well above the 200-counterparty cap."
            ),
            "shared_evidence": "0",
            "members_total_net_realized_weth": 0,
            "members_total_usd_per_day_est": 0,
            "status": "DEFERRED_pending_indexing_or_trace_budget",
        },
        {
            "cluster_id": "C2_per_recipient_internal_controller_concentration",
            "members": (
                "see Phase 5 fingerprints; e.g. 0x5570240e top-controller share "
                "0.75; 0xa9c0cded top-controller share 0.57; 0xf4cd8295 top-"
                "controller share 0.75"
            ),
            "rationale": (
                "Some bot recipients are dominated by 1-3 controller EOAs. "
                "These are within-bot single-user concentrations, not cross-bot "
                "cluster evidence; could be candidate authors using a single "
                "public bot, deferred to controller-keyed Phase B."
            ),
            "shared_evidence": "intra-bot; not cross-bot",
            "members_total_net_realized_weth": 0,
            "members_total_usd_per_day_est": 0,
            "status": "FOLLOW_UP_AT_CONTROLLER_LEVEL",
        },
    ]
    out_clusters = DIAG_DIR / "sprint044_operator_clusters.csv"
    with out_clusters.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cluster_cols)
        w.writeheader()
        for r in clusters:
            w.writerow(r)

    md = generate_md(cands, p5, summary)
    (AUDIT_DIR / "sprint044_global_author_scale_operator_rediscovery.md").write_text(md)
    print("wrote sprint044 audit + summary + clusters")


def generate_md(
    cands: list[dict[str, str]],
    p5: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    fp = p5.get("fingerprints_top12", {})

    lines: list[str] = []
    lines.append("# Sprint044 — Global Author-Scale Operator Rediscovery")
    lines.append("")
    lines.append(f"Generated UTC: `{summary['generated_at_utc']}`")
    lines.append("")
    lines.append("Diagnostics-only review.  No identity claim.  No live trading.")
    lines.append("No copy trading.  All addresses use `_AUDIT_ONLY` semantics.")
    lines.append("")
    lines.append("## 1. Verdict")
    lines.append("")
    lines.append(
        "- **Is an author-scale candidate found?** No. Within the CH-only + "
        "bounded RPC budget, no new single-account / single-cluster operator "
        "matches both the LASZLO whitepaper fingerprint and the "
        "`USER_PRIOR $500/day` scale."
    )
    lines.append(
        "- **Is any new candidate stronger than `0x7e49`?** No. "
        "`0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a` remains the only "
        "candidate that simultaneously satisfies (a) zero priority gas, "
        "(b) `tx.from = recipient` self-signing, (c) EIP-7702 delegation, "
        "and (d) direct Velora call. It is still under-scale at ~$45 "
        "realized over 14 days."
    )
    lines.append(
        "- **Most likely scenario.** Either (a) the author runs at a scale "
        "smaller than $500/day, (b) operates outside the indexed window "
        "(May 2026 not in CH), (c) operates as a controller `tx.from` of an "
        "existing public bot platform and is invisible to recipient-keyed "
        "search, or (d) `FAILED_NO_7D_ALPHA` was never lifted and there is no "
        "live author bot."
    )
    lines.append("")
    lines.append("## 2. Data Coverage and Constraints")
    lines.append("")
    lines.append("| Constraint | Value |")
    lines.append("|---|---|")
    lines.append(
        "| `uniswap_v3_swaps` block_time range | 2025-10-01 → 2026-04-30 02:18:05 |"
    )
    lines.append("| May 2026 V3 data | absent |")
    lines.append("| `erc20_transfers` distinct token contracts | 2 (WETH absent) |")
    lines.append("| WETH funding/profit graph available | no |")
    lines.append(
        "| `aerodrome_vamm_swaps_mv` | 278,821 rows, 2026-02-23 → 2026-04-30 |"
    )
    lines.append(
        "| Jan-Apr V3 WETH-leg meme swaps | 1,041,045 rows |"
    )
    lines.append(
        "| Distinct V3 recipients in window | 211,359 (38,986 with ≥4 swaps) |"
    )
    lines.append(
        "| `sender_*` aggregate views | sender = pool msg.sender (router), NOT tx.from |"
    )
    lines.append("")
    lines.append(
        "Implication: tx.from (signer) is **not** stored in any CH table.  Real "
        "signer attribution at scale would require ~30k+ RPC "
        "`eth_getTransactionByHash` calls; this is the **Phase B** preflight "
        "described below, not executed in this sprint."
    )
    lines.append("")
    lines.append("## 3. Lane Architecture")
    lines.append("")
    lines.append("| Lane | Definition | Status |")
    lines.append("|---|---|---|")
    lines.append(
        "| A | Direct V3 recipient (`uniswap_v3_swaps.recipient`) excluding "
        "known routers | top-5000 captured; top-32 author-style filter pass; "
        "top-31 after known-bot exclusion |"
    )
    lines.append(
        "| B | Velora WETH-recipient end users via `erc20_transfers` | "
        "**unavailable**: WETH not indexed |"
    )
    lines.append(
        "| C | Velora WETH-sender end users via `erc20_transfers` | "
        "**unavailable** for the same reason |"
    )
    lines.append(
        "| D | Aerodrome vAMM recipients | available but not evaluated this "
        "sprint; recommended for Phase B |"
    )
    lines.append("")
    lines.append("## 4. Author-style Filter Used in Phase 3")
    lines.append("")
    lines.append("Required (per whitepaper canon):")
    lines.append("")
    lines.append("- `n_swaps` in [8, 5000]")
    lines.append("- `n_meme_tokens >= 3`")
    lines.append("- `n_pools >= 3`")
    lines.append("- `active_days >= 5`")
    lines.append("- `n_buys > 0` and `n_sells > 0`")
    lines.append("- `min(n_buys, n_sells) / max(...) >= 0.25`")
    lines.append("- `1e-6 <= p50_weth <= 0.3`")
    lines.append("- at least one of fee 500 / 3000 / 10000 present")
    lines.append("")
    lines.append(
        "Filter dropped 5,000 → 32 candidates.  Known-bot exclusion (e.g. CoW "
        "Settlement) dropped 32 → 31."
    )
    lines.append("")
    lines.append(
        "Note: this is *recipient-level* evidence.  Recipient = bot contract "
        "in nearly all cases.  All 31 are smart contracts; none satisfy "
        "`is_self_caller=true` in their RPC sample."
    )
    lines.append("")
    lines.append("## 5. Top 20 Recipient Candidates (FIFO realized WETH)")
    lines.append("")
    lines.append(
        "| # | recipient | tokens | RT_tokens | FIFO pairs | WR | net_WETH | "
        "net_USD | days | $/day | n_ctrls | top_ctrl_share | gas_le5_share | "
        "gas_>1e5_share |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for r in cands[:20]:
        lines.append(
            "| {n} | `{a}` | {t} | {rt} | {p} | {wr} | {nw} | ${nu} | {d} | "
            "${dpd} | {nc} | {tcs} | {le5} | {gt1e5} |".format(
                n=cands.index(r) + 1,
                a=r["recipient"],
                t=r["tokens_traded"],
                rt=r["tokens_with_round_trip"],
                p=r["fifo_pairs"],
                wr=r["fifo_win_rate"],
                nw=r["fifo_net_realized_weth"],
                nu=r["fifo_net_realized_usd_est"],
                d=r["active_span_days"],
                dpd=r["usd_per_active_day"],
                nc=r.get("multi_tx_n_controllers", "0"),
                tcs=r.get("multi_tx_top_controller_share", "0"),
                le5=r.get("multi_tx_share_priority_le5", "0"),
                gt1e5=r.get("multi_tx_share_priority_gt_1e5", "0"),
            )
        )
    lines.append("")
    lines.append(
        "Reading: rows 1-12 had multi-tx RPC fingerprinting (Phase 5).  "
        "Rows 13-20 only had a single sample tx and are listed for completeness."
    )
    lines.append("")
    lines.append("## 6. Why None of Top 12 Are LASZLO-canon Authors")
    lines.append("")
    lines.append(
        "Multi-tx fingerprint over 6-8 sample tx per recipient (Phase 5):"
    )
    lines.append("")
    lines.append(
        "| recipient | n_sampled | gas_le5_share | gas_>1e5_share | "
        "n_distinct_controllers | top_ctrl_share | tx_to dominant |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|"
    )
    for addr, info in fp.items():
        tx_to_top = list(info.get("tx_to_dist_top", {}).keys())
        tx_to_first = tx_to_top[0] if tx_to_top else ""
        lines.append(
            "| `{a}` | {n} | {le5} | {mev} | {nc} | {tcs} | `{txto}` |".format(
                a=addr,
                n=info["n_sampled"],
                le5=info["share_priority_le_5_wei"],
                mev=info["share_priority_gt_1e5"],
                nc=info["n_distinct_controllers"],
                tcs=info["top_controller_share"],
                txto=tx_to_first,
            )
        )
    lines.append("")
    lines.append(
        "Conclusions (from the table above):"
    )
    lines.append("")
    lines.append(
        "- **Gas fingerprint failure.** All 12 have `gas_>1e5_share` ≈ 1.0.  "
        "LASZLO whitepaper canon is ≤ 5 wei priority gas.  Most candidates "
        "use 1M-20G wei priority, the opposite of canon."
    )
    lines.append(
        "- **Multi-controller failure.** All 12 have at least 3 distinct "
        "`tx.from` controllers across only 6-8 sample tx; several have 8 "
        "distinct controllers in 8 samples.  This is the signature of a "
        "*public bot platform*, not a single-author bot."
    )
    lines.append(
        "- **`tx.to == recipient` is the bot pattern.** Most candidates have "
        "all 8 sample tx.to equal the recipient itself (= a published bot "
        "router contract that any user can call).  This is the Banana Gun / "
        "Maestro / Photon retail-bot architecture."
    )
    lines.append(
        "- `0xe89aab725a2b...` had a single 0-wei sample, but multi-tx "
        "fingerprint shows `tx.to` includes ERC-4337 EntryPoint v0.7 "
        "(`0x0000000071727de22e5e9d8baf0edac6f37da032`) and a custom paymaster "
        "router; this is account-abstraction infra, not a LASZLO bot."
    )
    lines.append(
        "- **No cross-recipient controller overlap.** Phase 5 reported "
        f"`shared_controllers = {len(p5.get('shared_controllers', {}))}` "
        "across the 12 recipients.  No author cluster spans multiple bot "
        "platforms in this sample."
    )
    lines.append("")
    lines.append("## 7. Positive Control: 0x7e49")
    lines.append("")
    pc = summary["positive_control"]
    lines.append(f"- Address: `{pc['address']}`")
    lines.append(f"- Type: {pc['etype']}")
    lines.append(
        f"- LASZLO-canon fingerprint matches: zero-priority-gas share = "
        f"{pc['max_priority_fee_share_zero_wei']}; self-caller = "
        f"{pc['is_self_caller_in_sample']}; n_distinct_controllers = "
        f"{pc['n_distinct_controllers']}."
    )
    lines.append(
        f"- Bounded FIFO PnL (Sprint042 window): "
        f"net = {pc['fifo_net_realized_weth_2026_02_window']} WETH ≈ "
        f"${pc['fifo_net_realized_usd_est_2026_02_window']}; "
        f"WR = {pc['fifo_win_rate']}; trimmed mean = "
        f"{pc['fifo_trimmed_mean_pct_window']}%."
    )
    lines.append(
        f"- Scale match to USER_PRIOR $500/day: **{pc['scale_match_500_usd_per_day']}**."
    )
    lines.append(f"- Interpretation: {pc['interpretation']}")
    lines.append("")
    lines.append("## 8. $500/day Sanity Check Across Top Candidates")
    lines.append("")
    lines.append(
        "Top-12 recipients have aggregate USD/day across **all controllers**, "
        "not per-controller.  So even rows that look like author-scale "
        "(e.g. `0x12be7ac0` at $769/day; `0x34f5937f` at $846/day; "
        "`0xe89aab72` at $466/day) are platform-level numbers shared among "
        "3-8 distinct EOAs.  Per-controller estimates would be smaller "
        "by a factor of `n_distinct_controllers`, breaking the $500/day "
        "match for every row that does not also concentrate ≥80% of "
        "transactions in a single controller — which none do."
    )
    lines.append("")
    lines.append("## 9. Cluster Reconstruction Status")
    lines.append("")
    lines.append(
        "Sprint044 cluster status is in "
        "`mvp/diagnostics/sprint044_operator_clusters.csv`:"
    )
    lines.append("")
    lines.append(
        "- `C0_no_cross_recipient_controller_overlap`: zero shared controllers "
        "across the top 12 recipients."
    )
    lines.append(
        "- `C1_funding_profit_graph_DEFERRED`: blocked by missing WETH "
        "transfer index in `eatlq.erc20_transfers` (only 2 token contracts "
        "indexed)."
    )
    lines.append(
        "- `C2_per_recipient_internal_controller_concentration`: a few "
        "recipients have one controller dominant at 0.57-0.75 share "
        "(`0x5570240e`, `0xa9c0cded`, `0xf4cd8295`).  These are the strongest "
        "intra-bot single-user candidates and should be enumerated and "
        "re-PnL'd at controller level in Phase B."
    )
    lines.append("")
    lines.append("## 10. False-Positive Exclusions")
    lines.append("")
    lines.append("Excluded explicitly by router / settlement / paymaster lookup:")
    lines.append("")
    lines.append(
        "- `0x9008d19f58aabd9ed0d60971565aa8510560ab41` = CoW Protocol "
        "Settlement V2 (would have ranked top-3 by approximate FIFO)"
    )
    lines.append(
        "- 18 known router/aggregator/paymaster addresses excluded at Lane A "
        "filtering."
    )
    lines.append("")
    lines.append("Excluded by Phase 5 fingerprint (kept in CSV but flagged):")
    lines.append("")
    lines.append(
        "- All multi-controller bot platforms with `n_distinct_controllers ≥ 3` "
        "AND `gas_>1e5_share = 1.0` are flagged as bot-platform contamination, "
        "not author candidates.  This is all 12 fingerprinted recipients."
    )
    lines.append("")
    lines.append("## 11. Why Direct Copy-Trade is Still Wrong")
    lines.append("")
    lines.append(
        "- Recipient is a bot contract; copying its swaps means subscribing to "
        "an existing bot service, not following the author."
    )
    lines.append(
        "- Bot platforms eat the alpha through their own tipping/fees and "
        "their gas patterns (1M-20G priority) are not transferable to a "
        "low-priority Base strategy."
    )
    lines.append(
        "- Public bot users have heterogeneous strategies; aggregate platform "
        "PnL is meaningless for individual replication."
    )
    lines.append("")
    lines.append("## 12. Decision Flags")
    lines.append("")
    lines.append("```text")
    for k, v in summary["decision_flags"].items():
        lines.append(f"{k}={v}")
    lines.append("```")
    lines.append("")
    lines.append("## 13. Next Query Plan (Phase B)")
    lines.append("")
    plan = summary["next_query_plan_phase_b"]
    lines.append(f"- **Kind**: {plan['kind']}")
    lines.append(f"- **Rationale**: {plan['rationale']}")
    lines.append("")
    lines.append("Preflight:")
    lines.append("")
    pf = plan["preflight_estimate"]
    lines.append(f"- tx_hashes to pull from CH: `{pf['tx_to_pull_from_ch']}`")
    lines.append(
        f"- RPC `eth_getTransactionByHash` calls: `{pf['rpc_eth_getTransactionByHash_calls']}`"
    )
    lines.append(f"- Expected walltime: `{pf['expected_walltime_minutes']} min`")
    lines.append(f"- Abort thresholds: `{pf['abort_thresholds']}`")
    lines.append("")
    lines.append(f"- Lane B workaround: {plan['lane_b_workaround']}")
    lines.append(f"- Lane D Aerodrome: {plan['lane_d_aero']}")
    lines.append(f"- May 2026 recovery: {plan['may_2026_recovery']}")
    lines.append("")
    lines.append("## 14. Verification Gate Summary")
    lines.append("")
    lines.append("| # | Question | Answer |")
    lines.append("|---|---|---|")
    lines.append("| 1 | Author-scale candidate found? | No |")
    lines.append("| 2 | Top 20 signer ranking | provided in §5 and CSV |")
    lines.append(
        "| 3 | Top 10 cluster ranking | none confirmed; deferred (`C0/C1/C2`) "
        "in clusters CSV |"
    )
    lines.append(
        "| 4 | Jan-May realized PnL per top candidate | provided per recipient "
        "in CSV; recipient-level only |"
    )
    lines.append(
        "| 5 | Profit/day per top candidate | provided per recipient in CSV |"
    )
    lines.append("| 6 | Close to $500/day? | only at platform-level; not per-author |")
    lines.append("| 7 | Match whitepaper behavior? | none of the top 12 |")
    lines.append("| 8 | Velora / direct V3 / Aero use? | top 12 are direct V3; Lane B/C unavailable; Aero deferred |")
    lines.append("| 9 | Funding/profit sink evidence? | unavailable due to indexing gap |")
    lines.append("| 10 | Small-wallet high-WR noise? | yes for several, e.g. `0x12be7ac0` 1.0 WR on 2 tokens |")
    lines.append("| 11 | New address stronger than 0x7e49? | No |")
    lines.append("| 12 | Need debug_traceTransaction? | optional but lower priority than tx.from enumeration |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "*Diagnostics-only. No identity claim. No copy trading. No live "
        "trading. No training. `IDENTITY_CLAIM_ALLOWED=false`, "
        "`ATTRIBUTION_HYPOTHESIS_ALLOWED=true`.*"
    )

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
