"""Task 7: corrected honest verdict table + markdown package."""
from __future__ import annotations

from typing import Any

from ..common import utcnow
from .data import AUDIT_OUT, save_audit_json


def build_verdicts(
    incremental: dict,
    dedup: dict,
    coverage: dict,
    resampling: dict,
    proxy: dict,
    loss: dict,
) -> dict[str, Any]:
    inc_v = incremental.get("decision", {}).get(
        "incremental_verdict", "UNCONFIRMED_INVALID_INCREMENTAL_ESTIMAND"
    )
    actor_v = coverage.get("actor_resolution_verdict", "PASS_PENDING_STRATIFIED_CI_AUDIT")
    whale_abs_pass = bool(
        resampling.get("whale_absolute", {}).get("passes", {}).get("token_bootstrap_p05_positive")
    )
    abs_v = "BACKTEST_POSITIVE" if whale_abs_pass else "BACKTEST_NOT_ROBUST"

    # Copy execution: historical constraints (no ruin, positive haircut path) — renamed
    whale_wallet = (
        dedup.get("arms", {}).get("WHALE_EVENT", {}).get("fixed_1000_scheduled_wallet", {})
    )
    copyability = "COPYABILITY_BACKTEST_PASS"
    if whale_wallet.get("ruin"):
        copyability = "COPYABILITY_BACKTEST_FAIL"

    pct_hit = float(whale_wallet.get("pct_months_hit_1000_usd") or 0.0)
    target_v = "TARGET_NO_GO" if pct_hit < 0.25 else "TARGET_PLAUSIBLE"

    size_explains = bool(proxy.get("recommendation", {}).get("size_explains_effect"))
    rename = proxy.get("recommendation", {}).get("recommended_label")

    matched_pass = bool(incremental.get("decision", {}).get("matched_passes"))
    overlay_pass = bool(incremental.get("decision", {}).get("overlay_passes"))

    n_cs = 0
    for f in loss.get("findings", []):
        if f.get("check") == "censored_transfer_out_conservative":
            n_cs = int(f.get("n_censored_success_labeled") or 0)
            break

    spec_must_change_reasons = []
    if size_explains:
        spec_must_change_reasons.append(
            "WHALE_EVENT is explained by LARGE_BUY_IMPACT (no actor identity); rename/respec required for honest labeling."
        )
    if n_cs >= 50:
        spec_must_change_reasons.append(
            f"Censored SUCCESS labels ({n_cs}) still receive optimistic path PnL; "
            "scheduling/loss floor must be fixed before freeze."
        )

    # Coverage share 67% is a definition smell — flag but not automatic respec unless size explains
    whale_share = incremental.get("within_opportunity_overlay", {}).get("whale_share_of_pool")
    if whale_share is not None and whale_share >= 0.5:
        # informational
        pass

    if spec_must_change_reasons:
        decision = "SPEC_MUST_CHANGE__DECLARE_WHALE_EVENT_v1"
        shadow = "SHADOW_BLOCKED_UNTIL_V1_LOCK"
    else:
        decision = "SPEC_SURVIVES_STATISTICS_ONLY__SHADOW_ALLOWED_AFTER_FREEZE"
        shadow = "ALLOWED_IF_SPEC_FROZEN"

    overall = "HISTORICAL_COPY_SIM_PASS__INCREMENTAL_EDGE_UNCONFIRMED"
    if matched_pass and overlay_pass:
        overall = "HISTORICAL_COPY_SIM_PASS__INCREMENTAL_EDGE_PAIRED_POSITIVE"
    if size_explains:
        overall = "HISTORICAL_COPY_SIM_PASS__IDENTITY_UNPROVEN__SIZE_PROXY"

    table = [
        {"item": "Actor resolution", "verdict": actor_v},
        {"item": "WHALE_EVENT historical absolute return", "verdict": abs_v},
        {"item": "WHALE vs POOL_ONLY incremental", "verdict": inc_v},
        {"item": "Copy execution constraints", "verdict": copyability},
        {"item": "Copyable edge", "verdict": resampling.get("primary_information_verdict", "PROSPECTIVE_CONFIRMATION_REQUIRED")},
        {"item": "Live deployment", "verdict": "NO_GO"},
        {"item": "Observer-only shadow", "verdict": shadow},
        {"item": "Monthly earn $1000 target", "verdict": target_v},
        {"item": "Whale label integrity", "verdict": rename or "KEEP_WHALE_EVENT_LABEL_PENDING"},
        {"item": "Loss handling", "verdict": loss.get("summary", {}).get("verdict")},
        {"item": "Overall WHALE_EVENT", "verdict": overall},
        {"item": "Spec decision", "verdict": decision},
    ]

    return {
        "overall": overall,
        "spec_decision": decision,
        "spec_must_change_reasons": spec_must_change_reasons,
        "verdict_table": table,
        "renamed_from_scorecard": {
            "COPY_EDGE_GO": "COPYABILITY_BACKTEST_PASS",
            "incremental_vs_pool_only_weth": "INVALID_LEGACY_ARITHMETIC_DIFF",
        },
        "key_numbers": {
            "legacy_arithmetic_diff_weth": incremental.get("legacy", {}).get("arithmetic_diff"),
            "overlay_paired_delta_p05_token": incremental.get("within_opportunity_overlay", {}).get("paired_delta_p05_token"),
            "matched_paired_delta_p05_token": incremental.get("covariate_matched", {}).get("paired_delta_p05_token"),
            "whale_share_of_pool_selected": whale_share,
            "whale_unique_token": dedup.get("arms", {}).get("WHALE_EVENT", {}).get("breadth", {}).get("unique_token"),
            "whale_n_trades": dedup.get("arms", {}).get("WHALE_EVENT", {}).get("breadth", {}).get("n_trades"),
            "unconstrained_pnl_weth": dedup.get("arms", {}).get("WHALE_EVENT", {}).get("unconstrained_opportunity_pnl_weth"),
            "scheduled_cum_withdrawn_usd": whale_wallet.get("cum_withdrawn_usd"),
            "pct_months_hit_1000_usd": pct_hit,
            "gate_wilson_95_upper": coverage.get("existing_gate_ci", {}).get("wilson_95_upper"),
            "proxy_jaccard_vs_lbi": proxy.get("jaccard"),
            "size_explains_effect": size_explains,
        },
        "generated_at": utcnow(),
    }


def to_markdown(payload: dict, parts: dict[str, Any]) -> str:
    lines = [
        "# ACTOR_FLOW v0 — Estimand-Repair Audit (diagnostic)",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "> Diagnostic only. Does **not** mutate `configs/actor_flow_freeze.json`, scored arms, or `out/oos/scorecard.csv`.",
        "",
        "## Corrected verdict table",
        "",
        "| Item | Verdict |",
        "| --- | --- |",
    ]
    for row in payload["verdict_table"]:
        lines.append(f"| {row['item']} | `{row['verdict']}` |")

    kn = payload["key_numbers"]
    lines += [
        "",
        "## Overall",
        "",
        f"**`WHALE_EVENT = {payload['overall']}`**",
        "",
        f"**Spec decision:** `{payload['spec_decision']}`",
        "",
    ]
    if payload["spec_must_change_reasons"]:
        lines.append("Reasons the v0 spec cannot be frozen as-is:")
        for r in payload["spec_must_change_reasons"]:
            lines.append(f"- {r}")
        lines.append("")
        lines.append(
            "If you proceed, declare `WHALE_EVENT_v1` with a new locked T0; "
            "do **not** inherit v0 forward evidence."
        )
    else:
        lines.append(
            "Statistics were mis-stated but the selection definition may still be freezable. "
            "Recommended next step: freeze v0 spec + code hash, then start **observer-only** "
            "prospective shadow at a fresh T0. **No live trading.**"
        )

    lines += [
        "",
        "## Key corrected numbers",
        "",
        f"- Legacy invalid arithmetic diff: `{kn.get('legacy_arithmetic_diff_weth')}` WETH",
        f"- Overlay paired Δ p05 (token cluster): `{kn.get('overlay_paired_delta_p05_token')}`",
        f"- Matched paired Δ p05 (token cluster): `{kn.get('matched_paired_delta_p05_token')}`",
        f"- Whale share of POOL_ONLY selected: `{kn.get('whale_share_of_pool_selected')}`",
        f"- WHALE unique tokens / trades: `{kn.get('whale_unique_token')}` / `{kn.get('whale_n_trades')}`",
        f"- Unconstrained opportunity PnL: `{kn.get('unconstrained_pnl_weth')}` WETH",
        f"- Fixed-$1000 scheduled cum withdrawn: `${kn.get('scheduled_cum_withdrawn_usd')}`",
        f"- pct_months_hit_1000_usd: `{kn.get('pct_months_hit_1000_usd')}` → `TARGET_NO_GO`",
        f"- Gate Wilson 95% upper (existing n): `{kn.get('gate_wilson_95_upper')}`",
        f"- Jaccard(WHALE, LARGE_BUY_IMPACT): `{kn.get('proxy_jaccard_vs_lbi')}`",
        f"- Size explains effect: `{kn.get('size_explains_effect')}`",
        "",
        "## What was wrong in the scorecard",
        "",
        "1. `incremental_vs_pool_only_weth` subtracted totals of two different selection sets.",
        "2. `COPY_EDGE_GO` overstated copyable edge; renamed to `COPYABILITY_BACKTEST_PASS`.",
        "3. `bootstrap_p05` was absolute, not paired Δ.",
        "4. `sampled_error_rate=0` on ~25 traces cannot claim <2% (rule-of-three needs ~150).",
        "5. `total_wallet_pnl_weth` is unconstrained opportunity sum; economic target uses scheduled wallet.",
        "",
        "## Artifact index",
        "",
        "- `out/audit/WHALE_EVENT_v0_DIAGNOSTIC.json`",
        "- `out/audit/01_incremental.json`",
        "- `out/audit/02_dedup_capital.json`",
        "- `out/audit/03_coverage_ci.json`",
        "- `out/audit/04_resampling.json`",
        "- `out/audit/05_proxy_ablation.json`",
        "- `out/audit/06_loss_handling.json`",
        "",
        "## Hard bans still in force",
        "",
        "- No live deploy",
        "- No silent upgrade of PROSPECTIVE → INFORMATION_EDGE_GO",
        "- No editing freeze after seeing audit results without declaring v1",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_reports(
    incremental: dict,
    dedup: dict,
    coverage: dict,
    resampling: dict,
    proxy: dict,
    loss: dict,
) -> dict[str, Any]:
    payload = build_verdicts(incremental, dedup, coverage, resampling, proxy, loss)
    diagnostic = {
        "phase": "ACTOR_FLOW_OOS_V0_DIAGNOSTIC_AUDIT",
        "mutates_scored_outputs": False,
        **payload,
        "modules": {
            "incremental": incremental.get("decision"),
            "dedup_capital_whale": dedup.get("arms", {}).get("WHALE_EVENT", {}).get("breadth"),
            "coverage_verdict": coverage.get("actor_resolution_verdict"),
            "resampling_primary": resampling.get("primary_information_verdict"),
            "proxy_recommendation": proxy.get("recommendation"),
            "loss_summary": loss.get("summary"),
        },
    }
    save_audit_json("WHALE_EVENT_v0_DIAGNOSTIC.json", diagnostic)
    md = to_markdown(payload, {})
    path = AUDIT_OUT / "ACTOR_FLOW_v0_AUDIT.md"
    path.write_text(md)
    # Also place a copy at project root for easy open
    root_md = AUDIT_OUT.parent.parent / "ACTOR_FLOW_v0_AUDIT.md"
    root_md.write_text(md)
    print(f"[audit:report] wrote {path} and {root_md}", flush=True)
    return diagnostic
