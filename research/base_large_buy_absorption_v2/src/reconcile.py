"""Clear denominators for v0 number discrepancies + v1 counterparts."""
from __future__ import annotations

from typing import Any

import pandas as pd

from .common import OUT, save_json, utcnow


def run_reconcile(
    oof: dict[str, pd.DataFrame],
    scorecard: pd.DataFrame,
    calendar_report: dict,
) -> dict[str, Any]:
    """Document denominators. Include frozen v0 figures for audit trail."""
    pool = oof.get("POOL_ONLY")
    lba = oof.get("LARGE_BUY_ABSORPTION")
    lbo = oof.get("LARGE_BUY_ONLY")

    def n_sel(df):
        if df is None or "policy_selected" not in df.columns:
            return 0
        return int(df["policy_selected"].astype(bool).sum())

    pool_n = n_sel(pool)
    lba_n = n_sel(lba)
    # within-pool overlay flag
    overlay_share = None
    if pool is not None and pool_n:
        ps = pool[pool["policy_selected"].astype(bool)]
        flag = (
            (pd.to_numeric(ps.get("lba_score", 0), errors="coerce").fillna(0) > 0)
            & (pd.to_numeric(ps.get("absorbed", 0), errors="coerce").fillna(0) > 0)
        )
        overlay_share = float(flag.mean())

    sc = scorecard.set_index("arm") if len(scorecard) and "arm" in scorecard.columns else None

    def skip(arm):
        if sc is None or arm not in sc.index:
            return None
        return float(sc.loc[arm, "skip_rate"]) if "skip_rate" in sc.columns else None

    report = {
        "task": "denominator_reconciliation",
        "generated_at": utcnow(),
        "v0_frozen_figures": {
            "arm_vs_arm_share": {
                "value": 19684 / 29194,
                "formula": "WHALE_EVENT selected / POOL_ONLY selected",
                "nums": "19684 / 29194 = 67.42%",
            },
            "within_pool_overlay_share": {
                "value": 0.6198533945331233,
                "formula": "whale-flagged rows among POOL_ONLY selected",
                "nums": "18096 / 29194 = 61.99%",
                "note": "Different denominator than arm-vs-arm; both correct.",
            },
            "skip_rate_whale": {
                "value": 0.975,
                "formula": "WHALE arm scheduled skips / (fills+skips) under $1000 max_concurrent=3",
            },
            "skip_rate_pool": {
                "value": 0.99198,
                "formula": "POOL_ONLY arm same scheduler",
                "note": "~99% was POOL, ~97.5% was WHALE — not a contradiction.",
            },
            "unique_days_whale_selected": {
                "value": 43,
                "explanation": calendar_report.get("v0_explanation_43_days"),
            },
        },
        "v1_figures": {
            "POOL_ONLY_selected": pool_n,
            "LARGE_BUY_ABSORPTION_selected": lba_n,
            "LARGE_BUY_ONLY_selected": n_sel(lbo),
            "arm_vs_arm_share_lba_over_pool": (lba_n / pool_n) if pool_n else None,
            "within_pool_lba_flag_share": overlay_share,
            "skip_rate_POOL_ONLY": skip("POOL_ONLY"),
            "skip_rate_LARGE_BUY_ABSORPTION": skip("LARGE_BUY_ABSORPTION"),
            "skip_rate_LARGE_BUY_ONLY": skip("LARGE_BUY_ONLY"),
            "calendar_coverage_frac_scoped": calendar_report.get("coverage_frac_scoped"),
            "calendar_missing_days_n": calendar_report.get("missing_data_days_n"),
            "full_universe_no_downsample": True,
        },
        "rules": [
            "Always state numerator and denominator explicitly.",
            "Never mix arm-vs-arm share with within-pool overlay share.",
            "Always label which arm a skip_rate belongs to.",
            "Monthly $1000 target evaluated only over months with covered days.",
        ],
    }
    save_json(OUT / "reconcile.json", report)
    # human markdown
    lines = [
        "# Denominator reconciliation (v0 → v1)",
        "",
        "## Why 67.4% and 61.99% both appeared",
        "",
        "| Metric | Formula | Value |",
        "| --- | --- | --- |",
        "| Arm-vs-arm | WHALE selected / POOL selected | 19684/29194 = **67.4%** |",
        "| Within-pool overlay | whale-flagged inside POOL selected | 18096/29194 = **61.99%** |",
        "",
        "Different questions, both true.",
        "",
        "## Why skip ~97.5% vs ~99%",
        "",
        "- **97.5%**: WHALE arm under $1000 / max_concurrent=3",
        "- **~99%**: POOL_ONLY arm under the same scheduler (more candidates → more skips)",
        "",
        "## Why 43 unique days across 6 months",
        "",
        calendar_report.get("v0_explanation_43_days", ""),
        "",
        "## v1 current figures",
        "",
        f"- POOL_ONLY selected: {pool_n}",
        f"- LARGE_BUY_ABSORPTION selected: {lba_n}",
        f"- arm share LBA/POOL: {report['v1_figures']['arm_vs_arm_share_lba_over_pool']}",
        f"- LBA skip_rate: {skip('LARGE_BUY_ABSORPTION')}",
        f"- calendar scoped coverage: {calendar_report.get('coverage_frac_scoped')}",
        "",
    ]
    (OUT / "RECONCILE.md").write_text("\n".join(lines) + "\n")
    print("[reconcile] wrote reconcile.json + RECONCILE.md", flush=True)
    return report
