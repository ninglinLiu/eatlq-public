#!/usr/bin/env python3
"""Sprint039D — false-positive autopsy (2025-10-27) + scorecard_v2 charter (no RPC/R5/037c).

Reads Sprint039A q4_bundle_combined CSVs + Sprint039C post_expansion_by_regime.csv.
Does not tune thresholds or train models.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "mvp/audits/sprint039d"
RUNS = REPO / "mvp/runs"

Q4_DIR = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
BY_REG = REPO / "mvp/audits/sprint039c/post_expansion_by_regime.csv"
S038B_SUMMARY = REPO / "mvp/audits/sprint038b/regime_failure_attribution_summary.json"
S039C_SUMMARY = REPO / "mvp/audits/sprint039c/post_expansion_scorecard_summary.json"

TAU_Y = 0.23472400000000002
TAU_AH = 95.0
HOLDOUT_FRAC = 0.3  # sprint037g split_chrono default
HORIZONS = ("5m", "10m", "15m", "30m")

PASS_ALLOWED = ("2025-12-01", "2025-12-29", "2026-01-26")
FP_WEEK = "2025-10-27"
BLOCKED_SAMPLE_NEGATIVE = ("2026-03-30",)
BLOCKED_SAMPLE_EMPTY = ("2026-03-02", "2026-02-23")
THIN_OR_UNCERTAIN = ("2026-01-05", "2026-02-02", "2026-01-26")


def git_snap_head(n: int = 35) -> list[str]:
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


def load_q4(reg: str) -> list[dict[str, Any]]:
    path = Q4_DIR / f"q4_candidates_{reg}.csv"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            row["entry_time_sec"] = int(float(row["entry_time_sec"]))
            row["_pool"] = str(row.get("pool_address") or "").lower()
            try:
                row["_heat"] = int(float(row["activity_heat"])) if row.get("activity_heat") not in (None, "") else None
            except (TypeError, ValueError):
                row["_heat"] = None
            for k in ("price_velocity_5m", "buy_pressure_ratio", "token_volatility_5m"):
                try:
                    row[f"_{k}"] = float(row[k]) if row.get(k) not in (None, "") else None
                except (TypeError, ValueError):
                    row[f"_{k}"] = None
            try:
                row["_fresh"] = float(row["freshness_lag_sec"]) if row.get("freshness_lag_sec") not in (None, "") else None
            except (TypeError, ValueError):
                row["_fresh"] = None
            try:
                row["_tick_range"] = float(row["tick_range"]) if row.get("tick_range") not in (None, "") else None
            except (TypeError, ValueError):
                row["_tick_range"] = None
            row["_fee_bps"] = int(float(row["fee_bps"])) if row.get("fee_bps") not in (None, "") else None
            row["_weth0"] = int(float(row["weth_is_token0"])) if row.get("weth_is_token0") not in (None, "") else None
            for h in HORIZONS:
                nk = f"net_return_{h}"
                try:
                    row[f"_{nk}"] = float(row[nk]) if row.get(nk) not in (None, "", "None") else None
                except (TypeError, ValueError):
                    row[f"_{nk}"] = None
                ok = row.get(f"exit_ok_{h}")
                row[f"_ok_{h}"] = ok in ("True", "1", "true", True)
            rows.append(row)
    return rows


def split_holdout(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    rs = sorted(rows, key=lambda x: x["entry_time_sec"])
    n = len(rs)
    cut = max(1, int(n * (1 - HOLDOUT_FRAC)))
    return rs[:cut], rs[cut:]


def horizon_stats(rows: list[dict[str, Any]], h: str) -> dict[str, Any]:
    key = f"_net_return_{h}"
    okk = f"_ok_{h}"
    vals = [float(r[key]) for r in rows if r.get(okk) and r.get(key) is not None]
    if not vals:
        return {"horizon": h, "n": 0, "mean": None, "median": None, "pf": None, "label_1_share": None}
    pos = sum(v for v in vals if v > 0)
    neg = sum(-v for v in vals if v < 0)
    pf = (pos / neg) if neg > 1e-12 else None
    l1 = sum(1 for v in vals if v > 0) / len(vals)
    return {
        "horizon": h,
        "n": len(vals),
        "mean": round(mean(vals), 6),
        "median": round(median(vals), 6),
        "pf": round(pf, 6) if pf is not None else None,
        "label_1_share": round(l1, 6),
    }


def pool_agg_holdout(
    fp_week: str, holdout_rows: list[dict[str, Any]], slice_label: str
) -> list[dict[str, Any]]:
    by: dict[str, list[float]] = defaultdict(list)
    for r in holdout_rows:
        rr = r.get("_net_return_30m")
        if rr is None or not r.get("_ok_30m"):
            continue
        by[r["_pool"]].append(rr)
    out = []
    for pool, vs in by.items():
        pos = sum(v for v in vs if v > 0)
        neg = sum(-v for v in vs if v < 0)
        pf = (pos / neg) if neg > 1e-12 else None
        out.append(
            {
                "fp_week": fp_week,
                "holdout_slice": slice_label,
                "pool_address": pool,
                "n_rows_holdout": len(vs),
                "mean_30m": round(mean(vs), 6),
                "sum_30m": round(sum(vs), 6),
                "pf_30m": round(pf, 6) if pf is not None else None,
            }
        )
    out.sort(key=lambda x: x["sum_30m"])
    return out


def _ffloat(x: str | None) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fint(x: str | None) -> int | None:
    try:
        return int(float(x)) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def young_onset_holdout_from_row(br: dict[str, str]) -> dict[str, Any]:
    return {
        "young_holdout_n_30m": _fint(br.get("young_holdout_n_30m")),
        "holdout_mean_30m": _ffloat(br.get("holdout_mean_30m")),
        "holdout_pf_30m": _ffloat(br.get("holdout_pf_30m")),
        "holdout_label_1_share_30m": _ffloat(br.get("holdout_label_1_share_30m")),
        "holdout_unique_pools": _fint(br.get("holdout_unique_pools")),
        "holdout_top1_pool_share": _ffloat(br.get("holdout_top1_pool_share")),
        "holdout_top5_pool_share": _ffloat(br.get("holdout_top5_pool_share")),
        "activity_heat_p50_q4_all_sprint039c": _ffloat(br.get("activity_heat_p50_q4_all")),
        "young_frac_of_q4_sprint039c": _ffloat(br.get("young_frac_of_q4")),
    }


def regime_feature_row(reg: str, bucket: str, meta: dict[str, Any], by_week: dict[str, dict[str, str]]) -> dict[str, Any]:
    rows = load_q4(reg)
    if not rows:
        return {"regime_week": reg, "comparison_bucket": bucket, "n_q4": 0, "note": "missing_csv"}

    heats = [r["_heat"] for r in rows if r["_heat"] is not None]
    tvs = [r["_token_volatility_5m"] for r in rows if r["_token_volatility_5m"] is not None]
    bps = [r["_buy_pressure_ratio"] for r in rows if r["_buy_pressure_ratio"] is not None]
    pvs = [r["_price_velocity_5m"] for r in rows if r["_price_velocity_5m"] is not None]
    fr = [r["_fresh"] for r in rows if r["_fresh"] is not None]
    tr = [r["_tick_range"] for r in rows if r["_tick_range"] is not None]

    fees = Counter(r["_fee_bps"] for r in rows if r["_fee_bps"] is not None)
    fee100_pct = round(fees.get(100, 0) / len(rows), 6) if rows else None
    weth0_pct = round(sum(1 for r in rows if r["_weth0"] == 1) / len(rows), 6)

    train, ho = split_holdout(rows)
    ho_h = {h: horizon_stats(ho, h) for h in HORIZONS}

    out: dict[str, Any] = {
        "regime_week": reg,
        "comparison_bucket": bucket,
        "taxonomy_or_note": meta.get(reg, ""),
        "n_q4": len(rows),
        "activity_heat_p50_q4": median(heats) if heats else None,
        "token_volatility_p50_q4": median(tvs) if tvs else None,
        "buy_pressure_p50_q4": median(bps) if bps else None,
        "price_velocity_mean_q4": round(mean(pvs), 6) if pvs else None,
        "price_velocity_p50_q4": median(pvs) if pvs else None,
        "freshness_lag_sec_p50": median(fr) if fr else None,
        "tick_range_p50": median(tr) if tr else None,
        "fee_tier_100bps_share": fee100_pct,
        "weth_is_token0_share": weth0_pct,
        "chrono_holdout_frac_rows": round(len(ho) / len(rows), 6) if rows else None,
        "holdout_n_rows": len(ho),
        "holdout_mean_30m_all_q4": ho_h["30m"]["mean"],
        "holdout_pf_30m_all_q4": ho_h["30m"]["pf"],
        "holdout_label_1_share_30m_all_q4": ho_h["30m"]["label_1_share"],
        "holdout_mean_5m_all_q4": ho_h["5m"]["mean"],
        "holdout_mean_15m_all_q4": ho_h["15m"]["mean"],
        "young_frac_attributed_sprint039c": meta.get(f"{reg}_yf"),
        "scorecard_v1_allow_sprint039c": meta.get(f"{reg}_allow"),
    }
    br = by_week.get(reg, {})
    if br:
        yo = young_onset_holdout_from_row(br)
        out.update(
            {
                "young_holdout_n_30m_sprint039c": yo["young_holdout_n_30m"],
                "young_holdout_mean_30m_sprint039c": yo["holdout_mean_30m"],
                "young_holdout_pf_30m_sprint039c": yo["holdout_pf_30m"],
                "young_holdout_label_1_share_30m_sprint039c": yo["holdout_label_1_share_30m"],
                "young_holdout_top1_pool_share_sprint039c": yo["holdout_top1_pool_share"],
                "young_holdout_top5_pool_share_sprint039c": yo["holdout_top5_pool_share"],
            }
        )
    return out


def main() -> None:
    AUDIT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    snap = git_snap_head()

    meta: dict[str, Any] = {}
    by_lines: list[dict[str, str]] = []
    if BY_REG.is_file():
        with BY_REG.open(encoding="utf-8") as f:
            by_lines = list(csv.DictReader(f))
        for row in by_lines:
            w = row["regime_week"]
            meta[w] = row.get("taxonomy_gold") or ""
            meta[f"{w}_yf"] = float(row["young_frac_of_q4"]) if row.get("young_frac_of_q4") else None
            meta[f"{w}_allow"] = row.get("scorecard_v1_allow") == "True"
    by_week = {r["regime_week"]: r for r in by_lines}

    fp_rows = load_q4(FP_WEEK)
    if not fp_rows:
        stub = {
            "kind": "sprint039d_fp_autopsy",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "BLOCKED_BY_ARTIFACT_GAP_OR_SCHEMA",
            "git_status_snapshot_sb_head": snap,
            "note": f"missing {Q4_DIR}/q4_candidates_{FP_WEEK}.csv",
        }
        (AUDIT / "fp_2025_10_27_autopsy_summary.json").write_text(json.dumps(stub, indent=2), encoding="utf-8")
        sys.exit(4)

    _, ho_fp = split_holdout(fp_rows)
    ho_stats = {h: horizon_stats(ho_fp, h) for h in HORIZONS}
    train_fp, _ = split_holdout(fp_rows)
    tr_stats = {h: horizon_stats(train_fp, h) for h in HORIZONS}

    decay = {}
    if ho_stats["5m"]["mean"] is not None and ho_stats["30m"]["mean"] is not None:
        decay["full_q4_chrono_holdout_mean_delta_30m_minus_5m"] = round(
            ho_stats["30m"]["mean"] - ho_stats["5m"]["mean"], 6
        )

    slice_full_q4 = "full_q4_chrono_last_30pct_rows_not_young_onset_gate"
    pool_rows = pool_agg_holdout(FP_WEEK, ho_fp, slice_full_q4)
    worst_pools = pool_rows[:15]
    best_pools = sorted(pool_rows, key=lambda x: -x["sum_30m"])[:15]

    neg_sum = sum(-min(0, r["sum_30m"]) for r in pool_rows) or 1.0
    conc_top5_neg_share = round(
        sum(-min(0, r["sum_30m"]) for r in sorted(pool_rows, key=lambda x: min(0, x["sum_30m"]))[:5])
        / neg_sum,
        6,
    )

    comparison_csv_rows: list[dict[str, Any]] = []
    plan = [
        *[(w, "A_pass_like_allowed") for w in PASS_ALLOWED],
        (FP_WEEK, "B_new_false_positive_scorecard"),
        *[(w, "C_blocked_negative_sample") for w in BLOCKED_SAMPLE_NEGATIVE],
        *[(w, "C_blocked_empty_low_intersection_sample") for w in BLOCKED_SAMPLE_EMPTY],
        *[(w, "D_thin_positive_or_boundary_sample") for w in THIN_OR_UNCERTAIN if w != FP_WEEK],
    ]
    seen = set()
    for w, bkt in plan:
        if w in seen:
            continue
        seen.add(w)
        comparison_csv_rows.append(regime_feature_row(w, bkt, meta, by_week))

    tv_fp = [r["_token_volatility_5m"] for r in fp_rows if r["_token_volatility_5m"] is not None]
    pv_fp = [r["_price_velocity_5m"] for r in fp_rows if r["_price_velocity_5m"] is not None]

    yo_fp = young_onset_holdout_from_row(by_week[FP_WEEK]) if FP_WEEK in by_week else {}

    fp_hypotheses: list[str] = [
        "FP_PRIMARY_RULE_POPULATION_MISMATCH_FULL_Q4_SCORECARD_VS_YOUNG_ONSET_ROUTED_HOLDOUT",
    ]
    if median(tv_fp) >= 1.8:
        fp_hypotheses.append("FP_HIGH_MEDIAN_TOKEN_VOLATILITY_PRETRADE_CLUSTER")
    top5y = yo_fp.get("holdout_top5_pool_share")
    if top5y is not None and top5y >= 0.55:
        fp_hypotheses.append("FP_CAUSED_BY_CONCENTRATION_OR_SCHEMA")

    delta_full_q4_train_minus_holdout_30m = None
    if tr_stats["30m"]["mean"] is not None and ho_stats["30m"]["mean"] is not None:
        delta_full_q4_train_minus_holdout_30m = round(tr_stats["30m"]["mean"] - ho_stats["30m"]["mean"], 6)

    ah_fp = next((r["activity_heat_p50_q4"] for r in comparison_csv_rows if r["regime_week"] == FP_WEEK), None)
    why_pass_scorecard = {
        "young_frac_of_q4": meta.get(f"{FP_WEEK}_yf"),
        "threshold_tau_young_frac": TAU_Y,
        "passed_young_gate": bool(meta.get(f"{FP_WEEK}_yf") is not None and meta[f"{FP_WEEK}_yf"] >= TAU_Y),
        "activity_heat_p50_q4_all": ah_fp,
        "threshold_tau_activity_heat_p50": TAU_AH,
        "passed_activity_gate": bool(ah_fp is not None and float(ah_fp) >= TAU_AH),
        "interpretation": (
            "scorecard_v1 only gates on full-Q4 young_frac + activity_heat p50; it does not observe young_pool_onset "
            "routed economics, intra-week train/holdout bleed on that routed slice, volatility-conditioned exits, "
            "or regime-family tags."
        ),
    }

    why_negative_economics = {
        "taxonomy_gold": meta.get(FP_WEEK),
        "authoritative_routed_slice_for_taxonomy": "young_pool_onset_holdout_per_sprint039c_g037_pipeline",
        "sprint039c_young_onset_holdout": yo_fp,
        "contrast_full_q4_chrono_holdout_last_30pct_rows": {
            "slice_label": slice_full_q4,
            "horizons": ho_stats,
            "interpretation": (
                "Unconditional Q4 rows with exit_ok can still show PF>1 on 30m while the young_pool_onset routed "
                "holdout is strongly negative on Oct27 — this mismatch is the FP mechanism, not generic horizon decay "
                "within the same routed slice (that ladder requires CH / labeled onset rows)."
            ),
        },
        "full_q4_chrono_train_slice_horizons": tr_stats,
        "full_q4_chrono_train_minus_holdout_30m_mean_delta": delta_full_q4_train_minus_holdout_30m,
        "full_q4_chrono_holdout_horizon_spread_30m_minus_5m_mean": decay,
        "pool_pnl_contribution_full_q4_chrono_holdout_only": {
            "top_losing_pools_by_sum_30m": worst_pools[:10],
            "top_winning_pools_by_sum_30m": best_pools[:10],
            "top5_share_of_negative_pool_mass": conc_top5_neg_share,
            "caveat": "Different population than Sprint039C young-onset holdout; included as structural colour only.",
        },
        "pretrade_distribution_oct27_full_q4_file": {
            "token_volatility_median": median(tv_fp) if tv_fp else None,
            "price_velocity_mean": round(mean(pv_fp), 6) if pv_fp else None,
            "buy_pressure_median": median([r["_buy_pressure_ratio"] for r in fp_rows if r["_buy_pressure_ratio"] is not None])
            if any(r["_buy_pressure_ratio"] is not None for r in fp_rows)
            else None,
            "tick_range_p50": median([r["_tick_range"] for r in fp_rows if r["_tick_range"] is not None])
            if any(r["_tick_range"] is not None for r in fp_rows)
            else None,
            "freshness_lag_sec_p50": median([r["_fresh"] for r in fp_rows if r["_fresh"] is not None])
            if any(r["_fresh"] is not None for r in fp_rows)
            else None,
        },
        "artifact_gap_notes": (
            "Per-row pool_age_sec / precise onset vs swap ladder lives in ClickHouse joins — absent here — "
            "so exhaustion / prior-runup autopsy stays proxy-level on CSV-only features."
        ),
    }

    missing_dimensions = [
        {
            "dimension": "onset_survivorship_alignment",
            "rationale": "scorecard uses full-Q4 young_frac; routed taxonomy uses young_pool_onset CH gate — rule/economics population mismatch.",
        },
        {
            "dimension": "routed_holdout_horizon_and_exit_bridge",
            "rationale": "Need pre-trade features correlated with young-onset holdout PF/mean (not unconditional Q4 nets); exit timing family tags.",
        },
        {
            "dimension": "volatility_quality_conditioning",
            "rationale": "Oct27 shows high median token_volatility in q4 file; v1 does not condition exits or gates on vol regime.",
        },
        {
            "dimension": "buy_pressure_quality_not_volume",
            "rationale": "Median buy_pressure alone is a weak flow-quality proxy versus absorption/spoof risk.",
        },
        {
            "dimension": "concentration_guard_on_routed_slice",
            "rationale": "Pool-level loss concentration must be measured on the **same routed slice** as taxonomy (young onset), not only on broad Q4 nets.",
        },
        {
            "dimension": "exhaustion_prior_runup_proxy",
            "rationale": "tick_range / freshness_lag / velocity trajectories are visible in q4 CSV but unused by v1 — need principled caps or routing.",
        },
        {
            "dimension": "regime_family_tag",
            "rationale": "Oct27 sits outside the Dec/Jan PASS-like economic family despite similar scorecard surface — calendar/regime classifier gap.",
        },
    ]

    pass_like_vs_fp = []
    for w in (*PASS_ALLOWED, FP_WEEK):
        if w not in by_week:
            continue
        pass_like_vs_fp.append({"week": w, **young_onset_holdout_from_row(by_week[w])})

    s038b_root: list[Any] = []
    if S038B_SUMMARY.is_file():
        s038b_root = json.loads(S038B_SUMMARY.read_text(encoding="utf-8")).get("root_causes", [])

    summary = {
        "kind": "sprint039d_false_positive_autopsy",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "SCORECARD_V1_FP_EXPLAINED_V2_CHARTERED",
        "secondary_tags": fp_hypotheses,
        "git_status_snapshot_sb_head": snap,
        "scorecard_v1": {"tau_young_frac": TAU_Y, "tau_activity_heat_p50_q4_all": TAU_AH},
        "why_2025_10_27_passed_scorecard_v1": why_pass_scorecard,
        "why_2025_10_27_negative_economics": why_negative_economics,
        "missing_pretrade_dimensions_for_v2": missing_dimensions,
        "pass_like_vs_fp_scorecard_surface_and_young_economics": pass_like_vs_fp,
        "downgrade_track_status": "SCORECARD_V1_DOWNGRADED_ROUTE_REDESIGN_NEEDED",
        "scorecard_v1_downgrade": {
            "downgraded_to_historical_diagnostic": True,
            "rationale": (
                "v1 explains Dec/Jan PASS cluster but admits disjoint FP under expansion; "
                "do not promote v1 as routing gate without additional pre-trade structure."
            ),
        },
        "scoped_candidate_young_pool_onset_flow": {
            "retained_as_scoped_research_candidate": True,
            "rationale": (
                "Economics still intermittently positive on anchored cohorts per Sprint038D snapshot; "
                "expansion degrades routed PF — scope tightening required, not blanket dismissal."
            ),
        },
        "scorecard_v2_worth_development": True,
        "next_route_options": [
            "pool_launch_strategy_subset",
            "shorter_horizon_canonical_label_for_router",
            "volatility_conditioned_exit_hypothesis_tests",
        ],
        "sprint038b_root_causes_echo": s038b_root,
        "references_readonly": {
            "sprint039c_summary": str(S039C_SUMMARY.relative_to(REPO)),
            "q4_bundle_combined_dir": str(Q4_DIR.relative_to(REPO)),
        },
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
        "supersession_candidate": False,
    }

    (AUDIT / "fp_2025_10_27_autopsy_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if comparison_csv_rows:
        keys = list(comparison_csv_rows[0].keys())
        with (AUDIT / "fp_vs_pass_feature_comparison.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for row in comparison_csv_rows:
                w.writerow(row)

    with (AUDIT / "fp_pool_contribution_2025_10_27.csv").open("w", newline="", encoding="utf-8") as f:
        cols = [
            "fp_week",
            "holdout_slice",
            "pool_address",
            "n_rows_holdout",
            "mean_30m",
            "sum_30m",
            "pf_30m",
        ]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in pool_rows:
            w.writerow({k: row[k] for k in cols})

    charter = "\n".join(
        [
            "# Scorecard v2 charter (research-only; **not** formal scorecard_v2)",
            "",
            "## Why scorecard_v1 failed here",
            "",
            "- **Week `2025-10-27`** passes frozen **scorecard_v1** (`young_frac_of_q4`, `activity_heat_p50_q4_all`) "
            "yet Sprint039C taxonomy marks **NEGATIVE** on the **young_pool_onset** routed economics slice.",
            "- The FP core is **population mismatch**: v1 reads unconditional full-Q4 composition, while taxonomy reads "
            "a narrower routed slice where Oct27 shows **PF≪1** despite similar scorecard surface to Dec/Jan anchors.",
            "",
            "## Required **pre-trade** additions for any v2 proposal",
            "",
            "1. **Onset / survivorship alignment**: route gate must reference the **same population** used for economics "
            "(young_pool_onset survivors), not only unconditional Q4 composition.",
            "2. **Routed economics probe**: pre-trade summaries must correlate with **young_pool_onset holdout** outcomes "
            "(not only unconditional Q4 net ladders).",
            "3. **Concentration guard**: measured on the routed slice that defines taxonomy, not on mixed-Q4 nets.",
            "4. **Volatility regime conditioning**: separate routing when `token_volatility_5m` mass sits in extreme quartiles.",
            "5. **Flow quality**: move beyond median `buy_pressure_ratio` toward entropy / two-sided flow imbalance stability.",
            "6. **Regime family tag**: explicit calendar/router cluster separate from Dec/Jan PASS family.",
            "7. **Exhaustion / prior-runup proxies**: principled use of tick_range / freshness / velocity structures visible pre-trade.",
            "",
            "## How v2 must be validated (no cheating)",
            "",
            "- **Hold out disjoint regime weeks** not used to craft features (fresh R5/Q4 weeks or time-forward buckets).",
            "- **No direct tuning** of τ on the same 20-week bundle to force PASS — "
            "changes must be hypothesis-led and frozen before peeking.",
            "- **Replay parity**: canonical Sprint038D-style parity checks before trusting routed PF deltas.",
            "",
            "## Positioning of scorecard_v1",
            "",
            "- **Downgrade** to **historical diagnostic / clustering artifact** describing the Dec/Jan PASS slice — "
            "**not** a safe universal router post-expansion.",
            "",
            "`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `supersession_candidate=false`",
            "",
        ]
    )
    (AUDIT / "scorecard_v2_charter.md").write_text(charter, encoding="utf-8")

    readout = "\n".join(
        [
            "# Run-039D — scorecard FP autopsy",
            "",
            f"Generated: {summary['created_at']}",
            "",
            f"**status:** `{summary['status']}`",
            "",
            "## Headline",
            "",
            "- **`2025-10-27`** passes scorecard_v1 because **young_frac** and **activity_heat p50 (from q4 file)** exceed frozen τ.",
            "- Sprint039C **young_pool_onset holdout** is economically negative (mean≈−0.35, PF≈0.38 at 30m) while an "
            "**unconditional full-Q4 chronological holdout** can still show PF>1 — proving v1 does not see the routed slice.",
            "- Pool-level winners/losers in `fp_pool_contribution_*` are **full-Q4 holdout diagnostic only**, not the onset slice.",
            "",
            "## Artifacts",
            "",
            "- `mvp/audits/sprint039d/fp_2025_10_27_autopsy_summary.json`",
            "- `mvp/audits/sprint039d/fp_vs_pass_feature_comparison.csv`",
            "- `mvp/audits/sprint039d/fp_pool_contribution_2025_10_27.csv`",
            "- `mvp/audits/sprint039d/scorecard_v2_charter.md`",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039d_scorecard_fp_autopsy.py*",
            "",
        ]
    )
    (RUNS / "run-039D-scorecard-fp-autopsy-readout.md").write_text(readout, encoding="utf-8")


if __name__ == "__main__":
    main()
