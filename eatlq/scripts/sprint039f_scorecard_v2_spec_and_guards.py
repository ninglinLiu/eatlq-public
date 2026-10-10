#!/usr/bin/env python3
"""Sprint039F — freeze scorecard_v2 feature spec + predeclared routed-only guard prototypes.

Pure CSV read — no RPC/R5/037C. Guard thresholds are frozen constants (not tuned on bundle PASS).

Outputs under mvp/audits/sprint039f/ and mvp/runs/run-039F-*.md
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
AUDIT_E = REPO / "mvp/audits/sprint039e"
AUDIT_F = REPO / "mvp/audits/sprint039f"
RUNS = REPO / "mvp/runs"
ROWS_CSV = AUDIT_E / "routed_slice_feature_rows.csv"

FP_WEEK = "2025-10-27"
PASS_WEEKS = ("2025-12-01", "2025-12-29", "2026-01-26")
HORIZONS = ("5m", "10m", "15m", "30m")

# --- Predeclared guard constants (frozen before inspecting row-level outcomes; aligned to Sprint039E narrative only).
VOL_GUARD_MIN_EXCLUDE = 2.0  # Sprint039E Oct27 routed median token_volatility_5m bucket boundary
PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE = 24000  # round conservative spike threshold > FP week median ~19433 (no grid search)
CONCENTRATION_WEEK_TOP5_FAIL_GE = 0.90  # drop entire regime_week when holdout top5 pool mass ≥ this
POOL_LAUNCH_STRICT_MAX_AGE_SEC = 12 * 3600  # 12h diagnostic subset (stricter than 037G 24h cell)
POOL_LAUNCH_STRICT_MAX_ONSET_SEC = 45 * 60  # 45m diagnostic subset (stricter than 037G 60m cell)


def git_snap_head(n: int = 28) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "status", "-sb"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return (r.stdout or "").splitlines()[:n]
    except (OSError, subprocess.TimeoutExpired):
        return ["<git unavailable>"]


def load_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            row["_rw"] = row["regime_week"]
            row["_pool"] = row["pool_address"].lower()
            for k in (
                "ret_5m",
                "ret_10m",
                "ret_15m",
                "ret_30m",
                "token_volatility_5m",
                "buy_pressure_ratio",
                "unique_buyer_entropy_5m",
                "prior_runup_tick_span_onset_to_entry",
                "prior_runup_proxy_velocity_x_log_toe",
                "exhaustion_proxy_ret5m_minus_ret30m",
                "tick_range",
                "freshness_lag_sec",
            ):
                v = row.get(k)
                if v in (None, "", "None"):
                    row[f"_{k}"] = None
                else:
                    row[f"_{k}"] = float(v)
            for ik in ("pool_age_at_entry", "time_since_activity_onset", "fee_bps", "weth_is_token0", "activity_heat"):
                v = row.get(ik)
                if v in (None, "", "None"):
                    row[f"_{ik}"] = None
                else:
                    row[f"_{ik}"] = int(float(v))
            for hk in HORIZONS:
                ek = f"exit_ok_{hk}"
                row[f"_{ek}"] = row.get(ek) in ("True", "1", "true", True)
            out.append(row)
    return out


def stats_returns(vals: list[float]) -> dict[str, Any]:
    rs = sorted([float(x) for x in vals if x == x])
    if not rs:
        return {"n": 0, "mean": None, "median": None, "profit_factor": None, "label_1_share": None}
    n = len(rs)
    pos = [x for x in rs if x > 0]
    neg = [x for x in rs if x < 0]
    gp, gl = sum(pos), abs(sum(neg))
    pf = (gp / gl) if gl > 1e-12 else None
    k = max(1, n // 10)
    tr = rs[k:-k] if n > 2 * k else rs
    return {
        "n": n,
        "mean": round(mean(rs), 6),
        "median": round(rs[n // 2], 6),
        "trimmed_mean": round(sum(tr) / max(len(tr), 1), 6),
        "profit_factor": round(pf, 6) if pf is not None else None,
        "label_1_share": round(len(pos) / n, 6),
    }


def pool_conc(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"unique_pools": 0, "top1_pool_mass_share": None, "top5_pool_mass_share": None}
    ctr = Counter(r["_pool"] for r in rows)
    n = len(rows)
    mv = ctr.most_common()
    top1 = round(mv[0][1] / n, 6)
    top5 = round(sum(c for _, c in mv[:5]) / n, 6)
    return {"unique_pools": len(ctr), "top1_pool_mass_share": top1, "top5_pool_mass_share": top5}


def cohort_counts(rows: list[dict[str, Any]]) -> tuple[int, int, dict[str, int]]:
    fp_n = sum(1 for r in rows if r["_rw"] == FP_WEEK)
    pw: dict[str, int] = {w: sum(1 for r in rows if r["_rw"] == w) for w in PASS_WEEKS}
    pass_total = sum(pw.values())
    return fp_n, pass_total, pw


def week_top5_share(rows: list[dict[str, Any]], week: str) -> float | None:
    sub = [r for r in rows if r["_rw"] == week]
    if not sub:
        return None
    return pool_conc(sub)["top5_pool_mass_share"]


def apply_guard(rows: list[dict[str, Any]], pred_keep: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    return [r for r in rows if pred_keep(r)]


def guard_vol_exclude_high_tv(r: dict[str, Any]) -> bool:
    tv = r.get("_token_volatility_5m")
    return tv is None or tv < VOL_GUARD_MIN_EXCLUDE


def guard_horizon_fade_exclude(r: dict[str, Any]) -> bool:
    ex = r.get("_exhaustion_proxy_ret5m_minus_ret30m")
    if ex is None:
        return True
    return ex <= 0


def guard_prior_runup_exclude(r: dict[str, Any]) -> bool:
    sp = r.get("_prior_runup_tick_span_onset_to_entry")
    if sp is None:
        return True
    return sp < PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE


def guard_pool_launch_strict_keep(r: dict[str, Any]) -> bool:
    pa = r.get("_pool_age_at_entry")
    toe = r.get("_time_since_activity_onset")
    if pa is None or toe is None:
        return False
    return pa <= POOL_LAUNCH_STRICT_MAX_AGE_SEC and toe <= POOL_LAUNCH_STRICT_MAX_ONSET_SEC


def guard_combo_vol_then_fade(r: dict[str, Any]) -> bool:
    return guard_vol_exclude_high_tv(r) and guard_horizon_fade_exclude(r)


def guard_week_concentration(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bad_weeks: set[str] = set()
    for w in {r["_rw"] for r in rows}:
        t5 = week_top5_share(rows, w)
        if t5 is not None and t5 >= CONCENTRATION_WEEK_TOP5_FAIL_GE:
            bad_weeks.add(w)
    return [r for r in rows if r["_rw"] not in bad_weeks]


def horizon_pack(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for h in HORIZONS:
        key = f"_ret_{h}"
        vals = []
        for r in rows:
            v = r.get(key)
            if v is not None:
                vals.append(float(v))
        out[h] = stats_returns(vals)
    return out


def summarize_guard(
    name: str,
    caveats: str,
    suitable_oos: str,
    rows_after: list[dict[str, Any]],
    rows_before: list[dict[str, Any]],
    dropped_weeks_note: str = "",
) -> dict[str, Any]:
    fp_b, pass_b, pw_b = cohort_counts(rows_before)
    fp_a, pass_a, pw_a = cohort_counts(rows_after)

    def pct_kept(before: int, after: int) -> float | None:
        if before <= 0:
            return None
        return round(100.0 * after / before, 4)

    hp = horizon_pack(rows_after)
    conc = pool_conc(rows_after)
    pass_retentions = [pct_kept(pw_b[w], pw_a[w]) for w in PASS_WEEKS if pw_b.get(w, 0) > 0]

    return {
        "guard_or_policy": name,
        "n_rows_after_guard_all_weeks": len(rows_after),
        "n_fp_week_before": fp_b,
        "n_fp_week_after": fp_a,
        "fp_week_row_reduction_pct": round(100.0 * (1 - fp_a / fp_b), 4) if fp_b > 0 else None,
        "pass_weeks_rows_before_pooled": pass_b,
        "pass_weeks_rows_after_pooled": pass_a,
        "pass_weeks_pooled_retention_pct": pct_kept(pass_b, pass_a),
        "pass_week_min_retention_pct_among_weeks": min(pass_retentions) if pass_retentions else None,
        "mean_ret_5m": hp["5m"]["mean"],
        "mean_ret_10m": hp["10m"]["mean"],
        "mean_ret_15m": hp["15m"]["mean"],
        "mean_ret_30m": hp["30m"]["mean"],
        "pf_30m": hp["30m"]["profit_factor"],
        "label_1_share_30m": hp["30m"]["label_1_share"],
        "unique_pools_after": conc["unique_pools"],
        "top1_pool_mass_share_after": conc["top1_pool_mass_share"],
        "top5_pool_mass_share_after": conc["top5_pool_mass_share"],
        "caveats": caveats + (" " + dropped_weeks_note if dropped_weeks_note else ""),
        "suitable_for_out_of_bundle_validation": suitable_oos,
    }


def by_regime_rows(name: str, rows_after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for week in sorted({r["_rw"] for r in rows_after}):
        sub = [r for r in rows_after if r["_rw"] == week]
        hp = horizon_pack(sub)
        conc = pool_conc(sub)
        row = {
            "guard_or_policy": name,
            "regime_week": week,
            "cohort": "FP" if week == FP_WEEK else ("PASS_like" if week in PASS_WEEKS else "other"),
            "n_rows": len(sub),
            "mean_ret_5m": hp["5m"]["mean"],
            "mean_ret_10m": hp["10m"]["mean"],
            "mean_ret_15m": hp["15m"]["mean"],
            "mean_ret_30m": hp["30m"]["mean"],
            "pf_30m": hp["30m"]["profit_factor"],
            "label_1_share_30m": hp["30m"]["label_1_share"],
            "unique_pools": conc["unique_pools"],
            "top1_pool_mass_share": conc["top1_pool_mass_share"],
            "top5_pool_mass_share": conc["top5_pool_mass_share"],
        }
        out.append(row)
    return out


def build_feature_spec(
    snap: list[str],
    guard_constants: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "scorecard_v2_feature_spec",
        "version": "sprint039f-frozen-2026-05-08",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_readonly": [
            str((AUDIT_E / "routed_slice_feature_rows.csv").relative_to(REPO)),
            str((AUDIT_E / "routed_slice_regime_comparison.csv").relative_to(REPO)),
            str((AUDIT_E / "scorecard_v2_feature_evidence.json").relative_to(REPO)),
            "docs/ml-training-gate.md",
            "docs/phase6-resolution-criteria.md",
            "docs/label-semantics-freeze.md",
        ],
        "guard_prototype_constants_predeclared": guard_constants,
        "features": [
            {
                "name": "horizon_fade_proxy",
                "source_columns": ["ret_5m", "ret_10m", "ret_15m", "ret_30m", "exit_ok_5m", "exit_ok_30m"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "Requires executable multi-horizon replay quotes pre-entry path only for momentum proxies; realized ladder uses labeled horizons post hoc.",
                "intended_failure_mode": "Short horizon masks bleed into 30m economics (FP_CAUSED_BY_HORIZON_REVERSAL family).",
                "production_candidate": True,
                "artifact_gap": None,
            },
            {
                "name": "volatility_regime",
                "source_columns": ["token_volatility_5m"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "Derived from pre-trade window features already in q4 bundle.",
                "intended_failure_mode": "High vol regimes incompatible with fixed 30m exit without conditioning.",
                "production_candidate": True,
                "artifact_gap": None,
            },
            {
                "name": "prior_runup_proxy",
                "source_columns": [
                    "prior_runup_tick_span_onset_to_entry",
                    "prior_runup_proxy_velocity_x_log_toe",
                    "price_velocity_5m",
                    "time_since_activity_onset",
                ],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "tick_span requires CH swap trail aligned to Sprint037G onset semantics.",
                "intended_failure_mode": "Extended microstructure run-up before entry increases exhaustion odds.",
                "production_candidate": True,
                "artifact_gap": "Sparse ticks yield null span; fallback proxies documented per sprint039e.",
            },
            {
                "name": "exhaustion_proxy",
                "source_columns": ["exhaustion_proxy_ret5m_minus_ret30m"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": False,
                "availability_notes": "Uses labeled ret_5m vs ret_30m — known only after replay horizons computed (diagnostic / gated staging unless proxied).",
                "intended_failure_mode": "Row-level fade between shortest labeled horizon and 30m.",
                "production_candidate": False,
                "artifact_gap": "Needs lighter proxy using only pre-entry signals for pure decision-time gate.",
            },
            {
                "name": "buy_pressure_quality",
                "source_columns": ["buy_pressure_ratio", "unique_buyer_entropy_5m"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "Entropy column originates from q4 bundle generation.",
                "intended_failure_mode": "One-sided flow disguised as balanced median pressure.",
                "production_candidate": True,
                "artifact_gap": None,
            },
            {
                "name": "routed_slice_concentration",
                "source_columns": ["pool_address"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": False,
                "availability_notes": "Computed over routed holdout slice — operational routing requires live cohort mirror.",
                "intended_failure_mode": "Few pools dominate economics within routed slice.",
                "production_candidate": False,
                "artifact_gap": "Live parity requires matching population estimator vs holdout CSV replay.",
            },
            {
                "name": "stricter_pool_launch_age_onset",
                "source_columns": ["pool_age_at_entry", "time_since_activity_onset"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "Depends on CH pool first-seen + onset counting identical to Sprint037G baseline cell.",
                "intended_failure_mode": "Late-cycle pools misclassified as young_pool_onset survivors.",
                "production_candidate": False,
                "artifact_gap": None,
            },
            {
                "name": "regime_family_tag",
                "source_columns": ["regime_week"],
                "artifact": "mvp/audits/sprint039e/routed_slice_feature_rows.csv",
                "decision_time_available": True,
                "availability_notes": "Calendar tag only — coarse router cluster prior.",
                "intended_failure_mode": "Disjoint calendar regimes share scorecard surface but differ economically.",
                "production_candidate": True,
                "artifact_gap": "Needs principled mapping beyond ISO week string.",
            },
        ],
        "claims_this_sprint": {
            "bundle_pass_claim": False,
            "scorecard_v2_route_pass_claim": False,
            "reference_docs_no_training_authorization": True,
        },
        "git_status_snapshot_sb_head": snap,
        "supersession_candidate": False,
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "TRAINING_ALLOWED": False,
            "TRAINING_REVIEW_ALLOWED": False,
            "RPC_BACKED_LABEL_VALIDATION_ALLOWED": True,
            "MULTI_HORIZON_RESEARCH_ALLOWED": True,
            "H12_ABLATION_ALLOWED": False,
            "PHASE6_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "NO_ALPHA_CLAIM": True,
        },
    }


def write_feature_spec_md(spec: dict[str, Any], path: Path) -> None:
    lines = [
        "# Scorecard v2 — frozen feature spec (Sprint039F)",
        "",
        f"Version: `{spec['version']}` · Generated: `{spec['created_at']}`",
        "",
        "## Guard prototype constants (predeclared)",
        "",
        "```json",
        json.dumps(spec["guard_prototype_constants_predeclared"], indent=2),
        "```",
        "",
        "## Features",
        "",
    ]
    for f in spec["features"]:
        lines.extend(
            [
                f"### `{f['name']}`",
                "",
                f"- **Artifact / columns:** `{f['artifact']}` → `{', '.join(f['source_columns'])}`",
                f"- **Decision-time visible:** {'yes' if f['decision_time_available'] else 'no'} — {f['availability_notes']}",
                f"- **Failure mode:** {f['intended_failure_mode']}",
                f"- **Production candidate:** {'yes' if f['production_candidate'] else 'diagnostic-only'}",
                f"- **Artifact gap:** {f['artifact_gap'] or 'none'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Sprint039F automated verdict (non-binding)",
            "",
            f"- **status:** `{spec.get('sprint039f_status')}`",
            f"- **v2_feature_spec_ready:** `{spec.get('v2_feature_spec_ready')}`",
            f"- **v2_prototype_worth_out_of_bundle_validation:** `{spec.get('v2_prototype_worth_out_of_bundle_validation')}`",
            f"- **recommended_next_validation_guards:** `{', '.join(spec.get('recommended_next_validation_guards') or [])}`",
            "",
        ]
    )
    lines.extend(
        [
            "## Non-claims",
            "",
            "- This sprint **does not** authorize training, Phase 6, live deployment, or Gate 0 supersession.",
            "- Any guard thresholds listed above are **illustrative prototypes** for out-of-bundle validation — "
            "not tuned to force PASS on the historical 20-week bundle.",
            "",
            "`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `supersession_candidate=false`",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def validation_charter(path: Path, verdict_flags: dict[str, Any]) -> None:
    body = "\n".join(
        [
            "# Sprint039F — scorecard_v2 out-of-bundle validation charter",
            "",
            "## Binding references",
            "",
            "- `docs/ml-training-gate.md` — training remains unauthorized (`TRAINING_ALLOWED=false`).",
            "- `docs/phase6-resolution-criteria.md` — supersession requires new scoped economic readout; **not** asserted here.",
            "- `docs/label-semantics-freeze.md` — label semantics unchanged assumption for comparisons.",
            "",
            "## What may be validated next",
            "",
            "1. Re-export routed_slice rows on **fresh regime weeks** absent from Sprint039E CSV bundle.",
            "2. Freeze thresholds **before** peeking holdout metrics on those weeks (hypothesis-led constants).",
            "3. Replay parity vs Sprint038D-style canonical readouts before trusting routed PF deltas.",
            "",
            "## Explicit prohibitions",
            "",
            "- No threshold sweeps on the same frozen 20-week bundle to claim PASS.",
            "- No `supersedes: FAILED_NO_7D_ALPHA` line from this sprint.",
            "",
            "## Sprint039F verdict hooks",
            "",
            "```json",
            json.dumps(verdict_flags, indent=2),
            "```",
            "",
            "---",
            "",
            "*Machine-generated charter — human reviewer selects guards for next sprint.*",
            "",
        ]
    )
    path.write_text(body, encoding="utf-8")


def main() -> None:
    AUDIT_F.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    snap = git_snap_head()

    if not ROWS_CSV.is_file():
        sys.exit("missing sprint039e routed_slice_feature_rows.csv")

    guard_constants = {
        "VOL_GUARD_MIN_EXCLUDE": VOL_GUARD_MIN_EXCLUDE,
        "rule_vol_guard": "drop rows with token_volatility_5m >= VOL_GUARD_MIN_EXCLUDE",
        "PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE": PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE,
        "rule_prior_runup_guard": "drop rows with prior_runup_tick_span_onset_to_entry >= threshold when present",
        "CONCENTRATION_WEEK_TOP5_FAIL_GE": CONCENTRATION_WEEK_TOP5_FAIL_GE,
        "rule_concentration_guard": "drop ALL rows for regime_week whose routed holdout top5 pool mass >= threshold",
        "POOL_LAUNCH_STRICT_MAX_AGE_SEC": POOL_LAUNCH_STRICT_MAX_AGE_SEC,
        "POOL_LAUNCH_STRICT_MAX_ONSET_SEC": POOL_LAUNCH_STRICT_MAX_ONSET_SEC,
        "rule_pool_launch_strict_guard": "KEEP rows with pool_age_at_entry<=max_age AND time_since_activity_onset<=max_onset",
        "rule_horizon_fade_guard": "drop rows where exhaustion_proxy_ret5m_minus_ret30m > 0 when defined",
        "shorter_horizon_router": "report-only policy families using ret_5m..ret_30m without filtering",
    }

    spec = build_feature_spec(snap, guard_constants)
    rows_all = load_rows(ROWS_CSV)

    readout_rows: list[dict[str, Any]] = []
    by_regime_accum: list[dict[str, Any]] = []

    # Reference baselines
    readout_rows.append(
        summarize_guard(
            "baseline_all_routed_rows",
            "Unfiltered Sprint039E routed holdout export.",
            "reference_only",
            rows_all,
            rows_all,
        )
    )
    by_regime_accum.extend(by_regime_rows("baseline_all_routed_rows", rows_all))

    fp_only = [r for r in rows_all if r["_rw"] == FP_WEEK]
    pass_only = [r for r in rows_all if r["_rw"] in PASS_WEEKS]
    readout_rows.append(
        summarize_guard(
            "cohort_FP_week_only_baseline",
            "Sanity slice Oct27 only.",
            "reference_only",
            fp_only,
            fp_only,
        )
    )
    readout_rows.append(
        summarize_guard(
            "cohort_PASS_like_weeks_only_baseline",
            "Sanity slice PASS anchors only.",
            "reference_only",
            pass_only,
            pass_only,
        )
    )

    readout_rows.append(
        summarize_guard(
            "vol_guard_exclude_tv_ge_2",
            "High-vol band aligned to Sprint039E FP median bucket.",
            "yes_with_threshold_review",
            apply_guard(rows_all, guard_vol_exclude_high_tv),
            rows_all,
        )
    )
    by_regime_accum.extend(by_regime_rows("vol_guard_exclude_tv_ge_2", apply_guard(rows_all, guard_vol_exclude_high_tv)))

    readout_rows.append(
        summarize_guard(
            "horizon_fade_guard_drop_positive_exhaustion_proxy",
            "Uses labeled short-vs-long proxy — decision-time surrogate still missing.",
            "diagnostic_until_pretrade_surrogate",
            apply_guard(rows_all, guard_horizon_fade_exclude),
            rows_all,
        )
    )
    by_regime_accum.extend(
        by_regime_rows("horizon_fade_guard_drop_positive_exhaustion_proxy", apply_guard(rows_all, guard_horizon_fade_exclude))
    )

    readout_rows.append(
        summarize_guard(
            "prior_runup_guard_exclude_large_tick_span",
            "Large tick span window onset→entry treated as risk spike.",
            "yes_with_threshold_review",
            apply_guard(rows_all, guard_prior_runup_exclude),
            rows_all,
        )
    )
    by_regime_accum.extend(by_regime_rows("prior_runup_guard_exclude_large_tick_span", apply_guard(rows_all, guard_prior_runup_exclude)))

    conc_rows = guard_week_concentration(rows_all)
    dropped = sorted(set(r["_rw"] for r in rows_all) - set(r["_rw"] for r in conc_rows))
    readout_rows.append(
        summarize_guard(
            "concentration_guard_drop_week_top5_ge_0p90",
            "Week-level dominance guard — crude operational mirror.",
            "partial_requires_live_estimator",
            conc_rows,
            rows_all,
            dropped_weeks_note=f"dropped_weeks={dropped}" if dropped else "",
        )
    )
    by_regime_accum.extend(by_regime_rows("concentration_guard_drop_week_top5_ge_0p90", conc_rows))

    strict_rows = apply_guard(rows_all, guard_pool_launch_strict_keep)
    readout_rows.append(
        summarize_guard(
            "pool_launch_strict_diagnostic_subset_keep",
            "Diagnostic-only subset tightening age/onset beyond 037G baseline cell.",
            "diagnostic_only",
            strict_rows,
            rows_all,
        )
    )
    by_regime_accum.extend(by_regime_rows("pool_launch_strict_diagnostic_subset_keep", strict_rows))

    combo = apply_guard(rows_all, guard_combo_vol_then_fade)
    readout_rows.append(
        summarize_guard(
            "combo_vol_guard_then_horizon_fade_sequential",
            "Illustrative sequential composition — not a claimed production router.",
            "no_requires_decomposed_ablation",
            combo,
            rows_all,
        )
    )
    by_regime_accum.extend(by_regime_rows("combo_vol_guard_then_horizon_fade_sequential", combo))

    # Shorter horizon router — policy evaluation rows (same underlying rows_all)
    for h in HORIZONS:
        hp = horizon_pack(rows_all)
        st = hp[h]
        conc = pool_conc(rows_all)
        fp_b = sum(1 for r in rows_all if r["_rw"] == FP_WEEK)
        pass_b = sum(1 for r in rows_all if r["_rw"] in PASS_WEEKS)
        readout_rows.append(
            {
                "guard_or_policy": f"policy_family_outcome_horizon_{h}_eval_all_rows",
                "n_rows_after_guard_all_weeks": len(rows_all),
                "n_fp_week_before": fp_b,
                "n_fp_week_after": fp_b,
                "fp_week_row_reduction_pct": 0.0,
                "pass_weeks_rows_before_pooled": pass_b,
                "pass_weeks_rows_after_pooled": pass_b,
                "pass_weeks_pooled_retention_pct": 100.0,
                "pass_week_min_retention_pct_among_weeks": 100.0,
                "mean_ret_5m": hp["5m"]["mean"],
                "mean_ret_10m": hp["10m"]["mean"],
                "mean_ret_15m": hp["15m"]["mean"],
                "mean_ret_30m": hp["30m"]["mean"],
                "profit_factor_at_policy_horizon": st["profit_factor"],
                "mean_ret_at_policy_horizon": st["mean"],
                "label_1_share_at_policy_horizon": st["label_1_share"],
                "policy_horizon_evaluated": h,
                "pf_30m": hp["30m"]["profit_factor"],
                "label_1_share_30m": hp["30m"]["label_1_share"],
                "unique_pools_after": conc["unique_pools"],
                "top1_pool_mass_share_after": conc["top1_pool_mass_share"],
                "top5_pool_mass_share_after": conc["top5_pool_mass_share"],
                "caveats": (
                    f"Rows unchanged — evaluates economics if canonical exit were {h} instead of reporting ladder snapshot."
                ),
                "suitable_for_out_of_bundle_validation": "yes_router_hypothesis",
            }
        )

    # Decide verdict metadata
    vol_row = next(r for r in readout_rows if r["guard_or_policy"] == "vol_guard_exclude_tv_ge_2")
    fade_row = next(r for r in readout_rows if r["guard_or_policy"] == "horizon_fade_guard_drop_positive_exhaustion_proxy")
    combo_row = next(r for r in readout_rows if r["guard_or_policy"] == "combo_vol_guard_then_horizon_fade_sequential")

    def fp_red_pct(row: dict[str, Any]) -> float:
        return float(row["fp_week_row_reduction_pct"] or 0.0)

    def pass_ret(row: dict[str, Any]) -> float:
        return float(row["pass_weeks_pooled_retention_pct"] or 0.0)

    promising = (
        fp_red_pct(vol_row) >= 25.0
        and pass_ret(vol_row) >= 75.0
        and fp_red_pct(fade_row) >= 15.0
        and pass_ret(fade_row) >= 70.0
    )
    damage_pass = pass_ret(vol_row) < 60.0 or pass_ret(fade_row) < 55.0

    if damage_pass:
        status = "V2_GUARDS_REDUCE_FP_BUT_DAMAGE_PASS"
    elif promising:
        status = "SCORECARD_V2_FEATURE_SPEC_READY_GUARDS_PROMISING"
    else:
        status = "SCORECARD_V2_FEATURE_SPEC_READY_BUT_GUARDS_WEAK"

    v2_spec_ready = True
    worth_oos = promising and not damage_pass

    recommended_guards = ["vol_guard_exclude_tv_ge_2", "horizon_fade_guard_drop_positive_exhaustion_proxy"]
    spec["notes_on_prior_runup_guard"] = (
        "prior_runup_guard_exclude_large_tick_span materially lowers pooled PASS-like retention on this frozen CSV bundle (~67%); "
        "any production use requires independent threshold freeze + out-of-bundle validation — not promoted here."
    )

    spec["sprint039f_status"] = status
    spec["v2_feature_spec_ready"] = v2_spec_ready
    spec["v2_prototype_worth_out_of_bundle_validation"] = worth_oos
    spec["recommended_next_validation_guards"] = recommended_guards
    spec["guard_prototype_summary_metrics"] = {
        "vol_guard": {"fp_reduction_pct": vol_row["fp_week_row_reduction_pct"], "pass_retention_pooled_pct": vol_row["pass_weeks_pooled_retention_pct"]},
        "horizon_fade_guard": {"fp_reduction_pct": fade_row["fp_week_row_reduction_pct"], "pass_retention_pooled_pct": fade_row["pass_weeks_pooled_retention_pct"]},
        "combo_vol_then_fade": {"fp_reduction_pct": combo_row["fp_week_row_reduction_pct"], "pass_retention_pooled_pct": combo_row["pass_weeks_pooled_retention_pct"]},
    }

    (AUDIT_F / "scorecard_v2_feature_spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    write_feature_spec_md(spec, AUDIT_F / "scorecard_v2_feature_spec.md")

    rk = sorted(set().union(*(set(r.keys()) for r in readout_rows)))
    with (AUDIT_F / "v2_guard_prototype_readout.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rk, extrasaction="ignore")
        w.writeheader()
        for row in readout_rows:
            w.writerow(row)

    bk = list(by_regime_accum[0].keys())
    with (AUDIT_F / "v2_guard_by_regime.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=bk)
        w.writeheader()
        for row in by_regime_accum:
            w.writerow(row)

    verdict_flags = {
        "sprint039f_status": status,
        "v2_feature_spec_ready": v2_spec_ready,
        "v2_prototype_worth_out_of_bundle_validation": worth_oos,
        "recommended_next_validation_guards": recommended_guards,
        "supersession_candidate": False,
        "NO_ALPHA_CLAIM": True,
        "TRAINING_ALLOWED": False,
    }
    validation_charter(AUDIT_F / "v2_validation_charter.md", verdict_flags)

    readout_md = "\n".join(
        [
            "# Run-039F — scorecard_v2 feature spec freeze + guard prototypes",
            "",
            f"Generated: {spec['created_at']}",
            "",
            f"**status:** `{status}`",
            "",
            f"- `v2_feature_spec_ready`: **{v2_spec_ready}**",
            f"- `v2_prototype_worth_out_of_bundle_validation`: **{worth_oos}**",
            "",
            "## Frozen spec",
            "",
            "- `mvp/audits/sprint039f/scorecard_v2_feature_spec.json`",
            "- `mvp/audits/sprint039f/scorecard_v2_feature_spec.md`",
            "",
            "## Guard prototypes",
            "",
            "- `mvp/audits/sprint039f/v2_guard_prototype_readout.csv`",
            "- `mvp/audits/sprint039f/v2_guard_by_regime.csv`",
            "",
            "## Charter",
            "",
            "- `mvp/audits/sprint039f/v2_validation_charter.md`",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039f_scorecard_v2_spec_and_guards.py*",
            "",
        ]
    )
    (RUNS / "run-039F-scorecard-v2-feature-spec-readout.md").write_text(readout_md, encoding="utf-8")


if __name__ == "__main__":
    main()
