#!/usr/bin/env python3
"""Sprint054 smart-money + author-wallet candidate reorder.

This is a bounded, offline synthesis step. It consumes prior Sprint045-053
artifacts and writes the Sprint054 audit deliverables without RPC, live trading,
training, or production stream writes.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "mvp" / "audits" / "sprint054"
DIAG_DIR = ROOT / "mvp" / "diagnostics"
ETH_USD = 3500.0


INPUTS = {
    "controller_summary": DIAG_DIR / "sprint045c_merged_controller_summary.csv",
    "controller_buckets": DIAG_DIR / "sprint045c_candidate_buckets.csv",
    "top_probe": DIAG_DIR / "sprint045c_top_controller_probe.csv",
    "shortlist": DIAG_DIR / "sprint045d_candidate_shortlist.csv",
    "to_diagnostics": DIAG_DIR / "sprint045d_to_field_diagnostics.csv",
    "smart_money_scores": DIAG_DIR / "sprint048a_smart_money_scores.csv",
    "roi_candidates": DIAG_DIR / "sprint049b_return_normalized_candidates.csv",
    "roi_rescored": DIAG_DIR / "sprint049b_rescored_candidates.csv",
    "selected_paper": DIAG_DIR / "sprint049b_selected_paper_candidates.csv",
    "token_viability": DIAG_DIR / "sprint049c_token_viability_check.json",
    "funding_summary": DIAG_DIR / "sprint045f2_anchor_funding_summary.json",
    "funding_components": DIAG_DIR / "sprint045f2_anchor_components.csv",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    if value in ("", None, "inf", "Infinity"):
        return math.inf if value in ("inf", "Infinity") else default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def s(row: dict[str, Any], key: str, default: str = "") -> str:
    value = row.get(key, default)
    return default if value is None else str(value)


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if math.isnan(value):
        return lo
    return max(lo, min(hi, value))


def norm_log(value: float, top: float) -> float:
    return clamp(math.log1p(max(0.0, value)) / math.log1p(top))


def by_controller(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        controller = s(row, "controller").lower()
        if controller:
            out[controller] = row
    return out


def load_sources() -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for name, path in INPUTS.items():
        if path.suffix == ".json":
            sources[name] = read_json(path)
        else:
            sources[name] = read_csv(path)
    return sources


def pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return ""
    return f"{value:.2f}"


def compact_flags(flags: list[str]) -> str:
    return "|".join(dict.fromkeys(flag for flag in flags if flag))


def build_rows(sources: dict[str, Any]) -> list[dict[str, Any]]:
    summary = by_controller(sources["controller_summary"])
    buckets = by_controller(sources["controller_buckets"])
    probe = by_controller(sources["top_probe"])
    shortlist = by_controller(sources["shortlist"])
    to_diag = by_controller(sources["to_diagnostics"])
    smart = by_controller(sources["smart_money_scores"])
    roi = by_controller(sources["roi_candidates"])
    rescored = by_controller(sources["roi_rescored"])
    selected = by_controller(sources["selected_paper"])

    token_viability = sources["token_viability"]
    medium_dead = {
        k.lower(): v
        for k, v in token_viability.get("medium_candidate_pool_status", {}).items()
    }

    addresses = set()
    for source in (buckets, probe, shortlist, smart, roi, rescored, selected):
        addresses.update(source.keys())
    # Keep broad-sea economic leaders even if absent from later ROI sidecar.
    for row in sources["controller_summary"][:500]:
        addresses.add(s(row, "controller").lower())

    rows: list[dict[str, Any]] = []
    for addr in sorted(addresses):
        b = buckets.get(addr, {})
        p = probe.get(addr, {})
        sh = shortlist.get(addr, {})
        sm = smart.get(addr, {})
        r = roi.get(addr, {})
        rr = rescored.get(addr, {})
        sel = selected.get(addr, {})
        summ = summary.get(addr, {})
        td = to_diag.get(addr, {})

        sources_hit = [
            name
            for name, data in (
                ("045C_inventory", summ),
                ("045C_bucket", b),
                ("045C_probe", p),
                ("045D_shortlist", sh),
                ("048A_quoteable", sm),
                ("049B_roi", r),
                ("049B_rescored", rr),
                ("049B_selected", sel),
                ("045D_to_diag", td),
            )
            if data
        ]

        own_pnl_weth = f(rr or r, "own_realized_pnl_weth", 0.0)
        own_notional_weth = f(rr or r, "own_deployed_notional_weth", 0.0)
        own_roi_pct = f(rr or r, "own_roi_pct", math.nan)
        own_pf = f(rr or r, "own_profit_factor", math.nan)
        own_wr = f(rr or r, "own_win_rate", math.nan)
        matched_pairs = int(f(rr or r, "own_matched_pairs", f(r, "matched_pairs", 0.0)))
        max_loss_pct = f(rr or r, "own_max_loss_trade_pct", math.nan)
        median_hold_h = f(rr or r, "own_median_hold_hours", math.nan)

        fifo_pnl_usd = f(b or summ or p or sh, "pnl_total_usd", f(summ or p or sh, "fifo_net_realized_usd_est_approx", f(p or sh, "estimated_fifo_net_usd", f(sh, "usd_net_fifo", 0.0))))
        fifo_pnl_weth = fifo_pnl_usd / ETH_USD if fifo_pnl_usd else f(summ, "fifo_net_realized_weth_est", 0.0)
        gross_buy_weth = f(b or summ, "gross_buy_weth_sum", 0.0)
        realized_pnl_weth = own_pnl_weth if rr or r else fifo_pnl_weth
        realized_pnl_usd = realized_pnl_weth * ETH_USD if rr or r else fifo_pnl_usd
        notional_weth = own_notional_weth if own_notional_weth > 0 else gross_buy_weth
        notional_usd = notional_weth * ETH_USD

        if math.isnan(own_roi_pct):
            roi_pct = (realized_pnl_weth / notional_weth * 100.0) if notional_weth > 0 else math.nan
        else:
            roi_pct = own_roi_pct

        fifo_pairs = int(f(b or summ, "n_pairs", f(summ, "fifo_pairs", 0.0)))
        trade_count = int(f(b or summ or sm, "n_swaps", f(sm, "quoteable_event_count", 0.0)))
        active_days = int(f(b or summ or sm, "active_days", f(sm, "active_days_inventory", f(summ, "active_calendar_days_approx", 0.0))))
        win_rate = own_wr if not math.isnan(own_wr) else f(b or summ or sm, "win_rate", (f(summ, "fifo_wins", 0.0) / fifo_pairs if fifo_pairs else math.nan))
        profit_factor = own_pf if not math.isnan(own_pf) else math.nan
        median_return = f(r, "own_median_trade_return_pct", f(rr, "own_median_trade_return_pct", math.nan))
        trimmed_mean = f(b or summ, "trimmed_mean_return", f(summ, "fifo_pair_trimmed_mean_return", math.nan))

        quoteable_count = int(f(sm or rr, "quoteable_event_count", f(rr, "quoteable_buy_count", 0.0)))
        quoteable_share = f(sm or rr, "quoteable_event_share", f(rr, "quoteable_share", 0.0))
        top1_share = f(rr or r or sm, "top1_token_share", f(b, "pool_top1", 0.0))
        top3_share = f(rr or r or sm, "top3_token_share", 0.0)
        concentration_risk = s(rr or r, "concentration_risk", "HIGH" if top1_share >= 0.8 else "MEDIUM" if top1_share >= 0.5 else "LOW")
        toxic_flags = s(rr or r, "toxic_flags")
        delayed_verdict = s(rr or sel, "delayed_follow_verdict")

        low_gas_share = f(b or p or sh or summ, "priority_share_le5_wei", f(p, "low_priority_gas_share", f(summ, "priority_share_le5_wei_est", 0.0)))
        p50_priority = f(b or p or sh or summ or sm, "priority_fee_p50_wei", f(p, "p50_priority_fee_wei", math.nan))
        weth_withdraw = int(f(b or p or sh, "weth_withdraw_events_probed", f(p or sh, "weth_withdraw_events", 0.0)))
        v3_share = f(b or p or sh or summ, "v3_core_exposure_score", f(p or sh, "direct_v3_exposure_share", 0.0))
        fee100 = f(b or summ or p or sh, "fee100_share", f(summ, "fee100_share_est", 0.0))
        fee500 = f(b or summ, "fee500_share", f(summ, "fee500_share_est", 0.0))
        fee3000 = f(b or summ, "fee3000_share", f(summ, "fee3000_share_est", 0.0))
        fee10000 = f(b or summ, "fee10000_share", f(summ, "fee10000_share_est", 0.0))
        platform_contam = f(b or summ or sh, "platform_contamination_score", 0.0)
        platform_power = truthy((b or {}).get("platform_power_user_flag", False))
        in_cluster = truthy((b or sh or summ).get("in_cluster_hint", False))
        dominant_to = s(td, "dominant_to")
        dominant_to_frac = f(td, "dominant_to_frac", 0.0)

        roi_norm = clamp((roi_pct if not math.isnan(roi_pct) else 0.0) / 180.0)
        pf_norm = 0.35 if math.isinf(profit_factor) else clamp((profit_factor if not math.isnan(profit_factor) else 0.0) / 8.0)
        pnl_norm = norm_log(realized_pnl_usd, 12000.0)
        wr_norm = clamp(win_rate if not math.isnan(win_rate) else 0.0)
        count_norm = clamp(math.log1p(max(matched_pairs, fifo_pairs, quoteable_count, trade_count)) / math.log1p(50.0))
        notional_norm = clamp(math.log1p(max(notional_weth, 0.0)) / math.log1p(3.0))
        concentration_penalty = 0.0
        if top1_share >= 0.95:
            concentration_penalty += 0.22
        elif top1_share >= 0.8:
            concentration_penalty += 0.14
        if addr in medium_dead:
            concentration_penalty += 0.22
        if concentration_risk == "HIGH" and ("SINGLE_TOKEN_CONCENTRATION" in toxic_flags or top1_share >= 0.95):
            concentration_penalty += 0.12
        if notional_weth and notional_weth < 0.1 and max(matched_pairs, fifo_pairs) <= 3:
            concentration_penalty += 0.18
        if not math.isnan(roi_pct) and roi_pct < 0:
            concentration_penalty += 0.35
        economic_score = 100.0 * clamp(
            0.24 * roi_norm
            + 0.18 * pf_norm
            + 0.16 * wr_norm
            + 0.16 * count_norm
            + 0.16 * pnl_norm
            + 0.10 * notional_norm
            - concentration_penalty
        )

        author_score = 100.0 * clamp(
            0.24 * clamp(low_gas_share)
            + 0.16 * (1.0 if p50_priority <= 5 else 0.0)
            + 0.14 * clamp(v3_share)
            + 0.12 * (1.0 if weth_withdraw > 0 else 0.0)
            + 0.10 * (1.0 if active_days >= 10 else 0.0)
            + 0.09 * clamp((fee3000 + fee10000) / 0.9)
            + 0.08 * (1.0 if truthy((b or p or sh).get("is_eoa_probed", (p or sh).get("is_eoa", False))) else 0.0)
            + 0.07 * (1.0 if quoteable_share >= 0.8 else 0.0)
            - (0.12 if fee100 >= 0.35 else 0.0)
            - (0.10 if platform_contam >= 0.3 else 0.0)
        )

        evidence_score = 100.0 * clamp(
            0.15 * clamp(len(sources_hit) / 7.0)
            + 0.16 * (1.0 if p else 0.0)
            + 0.12 * (1.0 if rr or r else 0.0)
            + 0.11 * (1.0 if sm else 0.0)
            + 0.10 * (1.0 if td else 0.0)
            + 0.12 * count_norm
            + 0.10 * (1.0 if notional_weth > 0 else 0.0)
            + 0.08 * (1.0 if in_cluster else 0.0)
            + 0.08 * (1.0 if delayed_verdict in {"PASS", "WEAK_PASS"} else 0.0)
            + 0.08 * (1.0 if addr not in medium_dead else 0.0)
        )

        public_bot_flags: list[str] = []
        router_flags: list[str] = []
        funding_flags: list[str] = []
        concentration_flags: list[str] = []
        if platform_power:
            router_flags.append("platform_power_user")
        if quoteable_share >= 0.8:
            router_flags.append("public_quoteable_lane")
        if dominant_to and dominant_to_frac >= 0.95:
            router_flags.append(f"single_dominant_to:{dominant_to}")
        if truthy((p or {}).get("is_contract", False)) and not truthy((p or {}).get("eip7702_like", False)):
            public_bot_flags.append("controller_contract_not_eoa")
        if top1_share >= 0.8:
            concentration_flags.append(f"top1_token_share={top1_share:.2f}")
        if top3_share >= 0.95:
            concentration_flags.append(f"top3_token_share={top3_share:.2f}")
        if addr in medium_dead:
            concentration_flags.append("primary_pool_dead_feb2026")
            funding_flags.append("wallet_pivot_watch_only")
        if in_cluster:
            funding_flags.append("shared_tx_to_cluster_hint_not_funding")
        if not INPUTS["funding_summary"].exists():
            funding_flags.append("funding_graph_not_available")

        final_score = 0.48 * economic_score + 0.27 * author_score + 0.25 * evidence_score

        if (not math.isnan(roi_pct) and roi_pct < 0) or s(rr, "category_after_roi").endswith("WEAK_AFTER_ROI"):
            category = "reject_noise"
            next_action = "freeze_negative_roi"
        elif addr in medium_dead:
            category = "economic_smart_money_candidate"
            next_action = "paper_watchlist_wallet_pivot_only"
        elif economic_score >= 62 and (delayed_verdict in {"PASS", "WEAK_PASS"} or quoteable_count >= 10):
            category = "economic_smart_money_candidate"
            next_action = "scoped_validation"
        elif author_score >= 58 and realized_pnl_usd > 0 and active_days >= 5:
            category = "author-like_candidate"
            next_action = "funding_graph_probe"
        elif quoteable_count >= 10 or platform_power or dominant_to_frac >= 0.95:
            category = "useful_bot_or_flow_source"
            next_action = "feature_enrichment_not_follow"
        else:
            category = "reject_noise"
            next_action = "freeze_until_new_evidence"

        if (
            category not in {"reject_noise", "author-like_candidate"}
            and concentration_risk == "HIGH"
            and top1_share >= 0.95
            and delayed_verdict not in {"PASS", "WEAK_PASS"}
        ):
            category = "useful_bot_or_flow_source"
            next_action = "feature_enrichment_or_freeze_high_conc"

        confidence = "high" if evidence_score >= 72 else "medium" if evidence_score >= 52 else "low"
        gas_fingerprint = (
            "strict_low_priority_gas"
            if p50_priority <= 5 or low_gas_share >= 0.5
            else "low_gas_partial"
            if low_gas_share >= 0.25
            else "high_priority_vs_author_canon"
        )

        rows.append(
            {
                "address_or_cluster": addr,
                "type": "controller",
                "category": category,
                "economic_score": round(economic_score, 2),
                "author_fingerprint_score": round(author_score, 2),
                "evidence_score": round(evidence_score, 2),
                "final_score": round(final_score, 2),
                "realized_pnl": round(realized_pnl_weth, 6),
                "realized_pnl_usd": round(realized_pnl_usd, 2),
                "roi_pct": None if math.isnan(roi_pct) else round(roi_pct, 2),
                "profit_factor": "inf" if math.isinf(profit_factor) else (None if math.isnan(profit_factor) else round(profit_factor, 4)),
                "win_rate": None if math.isnan(win_rate) else round(win_rate, 4),
                "median_return": None if math.isnan(median_return) else round(median_return, 4),
                "trimmed_mean_return": None if math.isnan(trimmed_mean) else round(trimmed_mean, 4),
                "worst_trade": None if math.isnan(max_loss_pct) else round(max_loss_pct, 4),
                "trade_count": trade_count,
                "matched_or_fifo_pairs": max(matched_pairs, fifo_pairs),
                "active_days": active_days,
                "notional_proxy": round(notional_weth, 6),
                "capital_efficiency": None if notional_weth <= 0 else round(realized_pnl_weth / notional_weth, 6),
                "gas_fingerprint": gas_fingerprint,
                "fee_tier_mix": f"100:{fee100:.3f}|500:{fee500:.3f}|3000:{fee3000:.3f}|10000:{fee10000:.3f}",
                "avg_hold_seconds": None if math.isnan(median_hold_h) else round(median_hold_h * 3600.0, 1),
                "concentration_flags": compact_flags(concentration_flags),
                "public_bot_flags": compact_flags(public_bot_flags),
                "router_flags": compact_flags(router_flags),
                "funding_flags": compact_flags(funding_flags),
                "confidence": confidence,
                "recommended_next_action": next_action,
                "sources": compact_flags(sources_hit),
            }
        )

    rows.sort(key=lambda row: (row["category"] == "reject_noise", -row["final_score"], -row["economic_score"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank

    cluster_rows = [
        {
            "rank": len(rows) + 1,
            "address_or_cluster": "cluster:shared_to:0x5570240e8eedcdad5fe9b38f62c93236523e5eb4",
            "type": "cluster",
            "category": "useful_bot_or_flow_source",
            "economic_score": 42.0,
            "author_fingerprint_score": 48.0,
            "evidence_score": 61.0,
            "final_score": 49.43,
            "realized_pnl": None,
            "realized_pnl_usd": None,
            "roi_pct": None,
            "profit_factor": None,
            "win_rate": None,
            "median_return": None,
            "trimmed_mean_return": None,
            "worst_trade": None,
            "trade_count": 12,
            "matched_or_fifo_pairs": None,
            "active_days": None,
            "notional_proxy": None,
            "capital_efficiency": None,
            "gas_fingerprint": "mixed_private_infra",
            "fee_tier_mix": "",
            "avg_hold_seconds": None,
            "concentration_flags": "",
            "public_bot_flags": "not_public_known_but_unverified_abi",
            "router_flags": "shared_tx_to_private_infra_likelihood_70pct",
            "funding_flags": "shared_tx_to_not_funding|funding_smoke_inconclusive",
            "confidence": "medium",
            "recommended_next_action": "feature_enrichment_not_follow",
            "sources": "045E_shared_contract_autopsy|045F_funding_smoke",
        },
        {
            "rank": len(rows) + 2,
            "address_or_cluster": "cluster:shared_to:0x69460570c93f9de5e2edbc3052bf10125f0ca22d",
            "type": "cluster",
            "category": "useful_bot_or_flow_source",
            "economic_score": 25.0,
            "author_fingerprint_score": 52.0,
            "evidence_score": 58.0,
            "final_score": 40.54,
            "realized_pnl": None,
            "realized_pnl_usd": None,
            "roi_pct": None,
            "profit_factor": None,
            "win_rate": None,
            "median_return": None,
            "trimmed_mean_return": None,
            "worst_trade": None,
            "trade_count": 29,
            "matched_or_fifo_pairs": None,
            "active_days": None,
            "notional_proxy": None,
            "capital_efficiency": None,
            "gas_fingerprint": "strict_low_priority_members_but_low_pnl",
            "fee_tier_mix": "",
            "avg_hold_seconds": None,
            "concentration_flags": "",
            "public_bot_flags": "unverified_shared_component",
            "router_flags": "shared_tx_to_mixed_public_stack",
            "funding_flags": "shared_tx_to_not_funding|funding_missing",
            "confidence": "medium",
            "recommended_next_action": "feature_enrichment_not_follow",
            "sources": "045E_shared_contract_autopsy",
        },
    ]
    rows.extend(cluster_rows)
    return rows


def write_scorecards(rows: list[dict[str, Any]]) -> None:
    cluster_rows = [row for row in rows if row.get("type") == "cluster"]
    non_cluster_rows = [row for row in rows if row.get("type") != "cluster"]
    score_rows = non_cluster_rows[: max(0, 80 - len(cluster_rows))] + cluster_rows
    columns = [
        "rank",
        "address_or_cluster",
        "type",
        "category",
        "economic_score",
        "author_fingerprint_score",
        "evidence_score",
        "realized_pnl",
        "roi_pct",
        "profit_factor",
        "win_rate",
        "trade_count",
        "active_days",
        "notional_proxy",
        "capital_efficiency",
        "gas_fingerprint",
        "fee_tier_mix",
        "concentration_flags",
        "public_bot_flags",
        "router_flags",
        "funding_flags",
        "confidence",
        "recommended_next_action",
        "realized_pnl_usd",
        "median_return",
        "trimmed_mean_return",
        "worst_trade",
        "matched_or_fifo_pairs",
        "avg_hold_seconds",
        "sources",
    ]
    with (AUDIT_DIR / "candidate_scorecard.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(score_rows)
    with (AUDIT_DIR / "candidate_scorecard.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "eth_usd": ETH_USD,
                "scope": "offline synthesis of Sprint045-053 artifacts; no RPC/CH/live/training",
                "row_count": len(score_rows),
                "score_notes": {
                    "economic_score": "ROI/PF/win-rate/count/PnL/notional with concentration and extinct-token penalties",
                    "author_fingerprint_score": "low priority gas, V3 directness, WETH withdraw, persistence, fee mix; fee100 high share downweighted",
                    "evidence_score": "artifact coverage, probe/ROI/quoteable/to_diag support, sample depth, funding caveats",
                },
                "candidates": score_rows,
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )


def md_table(rows: list[dict[str, Any]], fields: list[str], limit: int) -> str:
    subset = rows[:limit]
    lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    for row in subset:
        vals = []
        for field in fields:
            value = row.get(field, "")
            vals.append("" if value is None else str(value))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_inventory(rows: list[dict[str, Any]]) -> None:
    category_counts = Counter(row["category"] for row in rows)
    text = f"""# Sprint054 — Smart Money + Author Wallet Inventory

Generated: `{datetime.now(timezone.utc).isoformat()}`

## Scope Boundary

This sprint reuses repository standards and prior artifacts. It treats `Home.md`, `Architecture.md`, `Operations.md`, `whitepaper-v2.md`, and `Whitepaper-v2-continued.md` as long-term contracts/narrative context; `mvp/` artifacts remain stage outputs. When narrative and implementation differ, Architecture, Operations, source code, and guard artifacts win.

No live trading, model training, model promotion, production signal writes, raw full backfill, or secret reads were performed.

## Reusable Artifacts

- Sprint045C broad-sea Base Uniswap V3 WETH-leg meme scan: 33k+ controllers, top100 probe, fee mix, gas probe, cluster hints.
- Sprint045C/D author-likeness reviews: canonical gas, WETH withdraw, direct V3, fee-tier, hold-time, and 0x7e49 baseline corrections.
- Sprint045E/F/F2 shared `tx.to` and funding graph probes: useful execution overlap, but no funding-source proof.
- Sprint048A public executable smart-money rediscovery: quoteable lane and public followability evidence.
- Sprint049A/B/C and Sprint050 ROI repair: return-normalized followability, concentration, extinct-token and wallet-pivot caveats.
- Sprint051-053 OOS regime work: confirms Base V3 focus, fee regime shift, and keeps training/live gates closed.

## Missing / Stale / Conflicting Inputs

- Missing: full WETH/USDC/native funding graph. Existing shared `tx.to` is execution overlap only.
- Missing: May 2026 controller expansion; CH artifacts still have known May blind spot for this wallet line.
- Missing: real delayed-follow market-tick latency for most historical candidates; Sprint049C sidecar is only a forward watch.
- Stale: high-concentration Jan-Feb token edges where primary meme pools are dead.
- Conflict resolved: absolute PnL no longer dominates ranking; ROI/PF/win-rate/trade depth/concentration now dominate.
- Conflict resolved: `tx.to` router/platform recipients are not treated as operators; controller/EOA/cluster evidence is prioritized.

## Classification Counts

| category | count |
| --- | ---: |
{chr(10).join(f"| {k} | {v} |" for k, v in sorted(category_counts.items()))}

## Top Reordered Candidates

{md_table(rows, ["rank", "address_or_cluster", "category", "economic_score", "author_fingerprint_score", "evidence_score", "recommended_next_action"], 20)}

## Interpretation

The best economic candidates are now mostly ROI-normalized public quoteable wallets, but many are constrained by concentration or dead historical pools. The best author-like candidates remain lower-scale low-priority-gas controllers such as the strict G1 matches; they need funding graph proof before any attribution claim. Shared execution clusters are useful flow/infra features, not author identity proof.
"""
    (AUDIT_DIR / "smart_money_author_wallet_inventory.md").write_text(text, encoding="utf-8")


def write_rejects(rows: list[dict[str, Any]]) -> None:
    rejects = [row for row in rows if row["category"] == "reject_noise"]
    useful = [row for row in rows if row["category"] == "useful_bot_or_flow_source"]
    text = f"""# Sprint054 — Rejects and Noise

## Hard Reject / Freeze Rules Applied

- Negative ROI or ROI-normalized weak after Sprint049B.
- Public/router/platform recipient evidence without controller-level economic edge.
- High concentration where historical alpha is tied to dead Feb 2026 meme pools.
- Tiny notional plus too few matched pairs, unless retained only as author-like fingerprint reference.
- Shared `tx.to` clusters without funding-source proof are not operator identity evidence.

## Top Reject Noise

{md_table(rejects, ["rank", "address_or_cluster", "economic_score", "author_fingerprint_score", "roi_pct", "concentration_flags", "router_flags", "recommended_next_action"], 20)}

## Useful Bot / Flow Sources, Not Follow Targets

{md_table(useful, ["rank", "address_or_cluster", "economic_score", "author_fingerprint_score", "router_flags", "funding_flags", "recommended_next_action"], 20)}

## Platform / Router Classes To Exclude As Operators

- Banana / Maestro / Photon / Velora / ParaSwap / 1inch / 0x / Kyber / CoW / UniversalRouter style `tx.to` recipients.
- Unverified shared execution contracts where many EOAs call the same target.
- EIP-7702 or smart-account wrappers without independent funding graph support.

These can be retained as flow features or route labels, but not as wallet/controller operators.
"""
    (AUDIT_DIR / "rejects_and_noise.md").write_text(text, encoding="utf-8")


def write_next_plan(rows: list[dict[str, Any]]) -> None:
    watch = [row for row in rows if row["recommended_next_action"] in {"scoped_validation", "paper_watchlist_wallet_pivot_only"}][:12]
    funding = [row for row in rows if row["recommended_next_action"] == "funding_graph_probe"][:12]
    enrich = [row for row in rows if row["recommended_next_action"] == "feature_enrichment_not_follow"][:12]
    freeze = [row for row in rows if row["category"] == "reject_noise"][:12]
    text = f"""# Sprint054 — Next Validation Plan

## Watchlist / Scoped Validation

{md_table(watch, ["rank", "address_or_cluster", "category", "economic_score", "roi_pct", "profit_factor", "win_rate", "recommended_next_action"], 12)}

Plan: run paper-only wallet watch on fresh activity, require live/new-pool viability, cap token/controller concentration, and compare 5m/30m exits before any expansion. No `signals` writes.

## Funding Graph Probe

{md_table(funding, ["rank", "address_or_cluster", "author_fingerprint_score", "gas_fingerprint", "fee_tier_mix", "funding_flags", "recommended_next_action"], 12)}

Plan: bounded slim funding probe only. Start with WETH/USDC/USDC.e/native around first/representative/last observed tx anchors. Abort or preflight if expected cost crosses 20 min, $5, or 200k RPC calls.

## Feature Enrichment

{md_table(enrich, ["rank", "address_or_cluster", "router_flags", "funding_flags", "recommended_next_action"], 12)}

Plan: keep as flow labels: route type, shared execution target, fee tier, gas timing, pool age, and young-pool onset context. Do not copy-follow these as operators.

## Freeze

{md_table(freeze, ["rank", "address_or_cluster", "roi_pct", "concentration_flags", "recommended_next_action"], 12)}

Plan: freeze unless they reappear with new token/pool activity and pass the same ROI/concentration checks from scratch.

## Gates

- `LIVE_ALLOWED=false`
- `TRAINING_ALLOWED=false`
- `MODEL_PROMOTION_ALLOWED=false`
- `PRODUCTION_SIGNALS_WRITTEN=false`
- `AUTHOR_WALLET_FOUND=false`
- `COPY_FOLLOW_ALLOWED=false` except paper/shadow watchlist under scoped validation.
"""
    (AUDIT_DIR / "next_validation_plan.md").write_text(text, encoding="utf-8")


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    rows = build_rows(sources)
    write_scorecards(rows)
    write_inventory(rows)
    write_rejects(rows)
    write_next_plan(rows)
    print(json.dumps({"audit_dir": str(AUDIT_DIR), "rows": min(80, len(rows)), "top": rows[:10]}, indent=2))


if __name__ == "__main__":
    main()
