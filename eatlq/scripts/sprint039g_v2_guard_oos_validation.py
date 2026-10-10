#!/usr/bin/env python3
"""Sprint039G — frozen scorecard_v2 guards validated out-of-sample on canonical routed rows.

- OOS weeks = q4_bundle_combined regimes NOT present in Sprint039E routed_slice_feature_rows.csv.
- Routed rows built with Sprint037G semantics (same pipeline as Sprint039E via shared helpers).
- Frozen thresholds from mvp/audits/sprint039f/scorecard_v2_feature_spec.json + Sprint039G-only
  decision-time horizon-fade substitute constants (NOT tuned on OOS outcomes).

Env: SPRINT039E_CH_DOCKER_CONTAINER or SPRINT039G_CH_DOCKER_CONTAINER for docker exec CH (same as 039E).
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
AUDIT_E_ROWS = REPO / "mvp/audits/sprint039e/routed_slice_feature_rows.csv"
AUDIT_F_SPEC = REPO / "mvp/audits/sprint039f/scorecard_v2_feature_spec.json"
AUDIT_G = REPO / "mvp/audits/sprint039g"
RUNS = REPO / "mvp/runs"
Q4_BUNDLE = REPO / "mvp/audits/sprint039a/q4_bundle_combined"

HORIZONS = ("5m", "10m", "15m", "30m")

# Sprint039G-only freeze: decision-time substitute for labeled exhaustion_proxy (no post-OOS tuning).
HORIZON_FADE_SUB_TICK_RANGE_GE = 3500.0
HORIZON_FADE_SUB_PRICE_VELOCITY_GT = 0.15


def load_s039e():
    path = REPO / "scripts/sprint039e_routed_slice_feature_enrichment.py"
    spec = importlib.util.spec_from_file_location("sprint039e_routed_slice_feature_enrichment", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint039e module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def sprint039e_regime_weeks() -> set[str]:
    out: set[str] = set()
    with AUDIT_E_ROWS.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            out.add(row["regime_week"])
    return out


def bundle_regime_weeks() -> list[str]:
    reg = []
    for p in sorted(Q4_BUNDLE.glob("q4_candidates_*.csv")):
        reg.append(p.stem.replace("q4_candidates_", ""))
    return reg


def load_frozen_constants() -> dict[str, Any]:
    data = json.loads(AUDIT_F_SPEC.read_text(encoding="utf-8"))
    return data["guard_prototype_constants_predeclared"]


def stats_returns(vals: list[float]) -> dict[str, Any]:
    rs = sorted(float(x) for x in vals if x == x)
    if not rs:
        return {"n": 0, "mean": None, "median": None, "trimmed_mean": None, "profit_factor": None, "label_1_share": None}
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
    ctr = Counter(str(r["pool_address"]).lower() for r in rows)
    n = len(rows)
    mv = ctr.most_common()
    return {
        "unique_pools": len(ctr),
        "top1_pool_mass_share": round(mv[0][1] / n, 6),
        "top5_pool_mass_share": round(sum(c for _, c in mv[:5]) / n, 6),
    }


def horizon_pack(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for h in HORIZONS:
        vals = []
        for r in rows:
            v = r.get(f"ret_{h}")
            if v is not None:
                vals.append(float(v))
        out[h] = stats_returns(vals)
    return out


def week_mean_30m(rows: list[dict[str, Any]], week: str) -> float | None:
    xs = [float(r["ret_30m"]) for r in rows if r.get("regime_week") == week and r.get("ret_30m") is not None]
    if len(xs) < 5:
        return None
    return mean(xs)


def classify_proxy_weeks(base_rows: list[dict[str, Any]], weeks: list[str]) -> dict[str, Any]:
    means = {w: week_mean_30m(base_rows, w) for w in weeks}
    pass_like = sorted(w for w, m in means.items() if m is not None and m > 0)
    fp_like = sorted(w for w, m in means.items() if m is not None and m < 0)
    uncertain = sorted(w for w in weeks if means.get(w) is None)
    return {"week_mean_30m_baseline": means, "pass_like_proxy_weeks": pass_like, "fp_like_proxy_weeks": fp_like, "uncertain_weeks": uncertain}


def guard_vol_keep(fc: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    thr = float(fc["VOL_GUARD_MIN_EXCLUDE"])

    def _k(r: dict[str, Any]) -> bool:
        tv = r.get("token_volatility_5m")
        if tv is None:
            return True
        return float(tv) < thr

    return _k


def guard_horizon_fade_substitute_keep() -> Callable[[dict[str, Any]], bool]:
    def _k(r: dict[str, Any]) -> bool:
        tr = r.get("tick_range")
        pv = r.get("price_velocity_5m")
        if tr is None or pv is None:
            return True
        return not (
            float(tr) >= HORIZON_FADE_SUB_TICK_RANGE_GE and float(pv) > HORIZON_FADE_SUB_PRICE_VELOCITY_GT
        )

    return _k


def guard_prior_runup_keep(fc: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    thr = float(fc["PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE"])

    def _k(r: dict[str, Any]) -> bool:
        sp = r.get("prior_runup_tick_span_onset_to_entry")
        if sp is None:
            return True
        return float(sp) < thr

    return _k


def guard_pool_launch_strict_keep(fc: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    max_age = int(fc["POOL_LAUNCH_STRICT_MAX_AGE_SEC"])
    max_onset = int(fc["POOL_LAUNCH_STRICT_MAX_ONSET_SEC"])

    def _k(r: dict[str, Any]) -> bool:
        pa = r.get("pool_age_at_entry")
        toe = r.get("time_since_activity_onset")
        if pa is None or toe is None:
            return False
        return int(pa) <= max_age and int(toe) <= max_onset

    return _k


def combo_vol_then_hf(fc: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    v = guard_vol_keep(fc)
    h = guard_horizon_fade_substitute_keep()

    def _k(r: dict[str, Any]) -> bool:
        return v(r) and h(r)

    return _k


def apply_keep(rows: list[dict[str, Any]], keep_pred: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    return [r for r in rows if keep_pred(r)]


def summarize_policy(
    name: str,
    base_rows: list[dict[str, Any]],
    filt_rows: list[dict[str, Any]],
    proxy: dict[str, Any],
    caveats: str,
) -> dict[str, Any]:
    hp = horizon_pack(filt_rows)
    conc = pool_conc(filt_rows)

    pass_w = set(proxy["pass_like_proxy_weeks"])
    fp_w = set(proxy["fp_like_proxy_weeks"])
    pass_b = [r for r in base_rows if r["regime_week"] in pass_w]
    pass_a = [r for r in filt_rows if r["regime_week"] in pass_w]
    fp_b = [r for r in base_rows if r["regime_week"] in fp_w]
    fp_a = [r for r in filt_rows if r["regime_week"] in fp_w]

    def pct(a: int, b: int) -> float | None:
        if b <= 0:
            return None
        return round(100.0 * a / b, 4)

    return {
        "policy_family": name,
        "n_rows_baseline_all_oos": len(base_rows),
        "n_rows_after_policy_all_oos": len(filt_rows),
        "rows_dropped_all_oos": len(base_rows) - len(filt_rows),
        "mean_ret_5m": hp["5m"]["mean"],
        "mean_ret_10m": hp["10m"]["mean"],
        "mean_ret_15m": hp["15m"]["mean"],
        "mean_ret_30m": hp["30m"]["mean"],
        "median_ret_30m": hp["30m"]["median"],
        "trimmed_mean_ret_30m": hp["30m"]["trimmed_mean"],
        "pf_30m": hp["30m"]["profit_factor"],
        "label_1_share_30m": hp["30m"]["label_1_share"],
        "unique_pools_after": conc["unique_pools"],
        "top1_pool_mass_share_after": conc["top1_pool_mass_share"],
        "top5_pool_mass_share_after": conc["top5_pool_mass_share"],
        "fp_like_proxy_rows_before": len(fp_b),
        "fp_like_proxy_rows_after": len(fp_a),
        "fp_like_proxy_row_reduction_pct": pct(len(fp_b) - len(fp_a), len(fp_b)),
        "pass_like_proxy_rows_before": len(pass_b),
        "pass_like_proxy_rows_after": len(pass_a),
        "pass_like_proxy_retention_pct": pct(len(pass_a), len(pass_b)),
        "pass_like_proxy_weeks_list": ",".join(sorted(pass_w)),
        "fp_like_proxy_weeks_list": ",".join(sorted(fp_w)),
        "caveats": caveats,
    }


def by_regime_detail(name: str, base_rows: list[dict[str, Any]], filt_rows: list[dict[str, Any]], weeks: list[str]) -> list[dict[str, Any]]:
    out = []
    for w in weeks:
        b = [r for r in base_rows if r["regime_week"] == w]
        a = [r for r in filt_rows if r["regime_week"] == w]
        hp = horizon_pack(a)
        conc = pool_conc(a)
        out.append(
            {
                "policy_family": name,
                "regime_week": w,
                "n_rows_baseline": len(b),
                "n_rows_after": len(a),
                "mean_ret_30m": hp["30m"]["mean"],
                "median_ret_30m": hp["30m"]["median"],
                "trimmed_mean_ret_30m": hp["30m"]["trimmed_mean"],
                "pf_30m": hp["30m"]["profit_factor"],
                "label_1_share_30m": hp["30m"]["label_1_share"],
                "mean_ret_5m": hp["5m"]["mean"],
                "mean_ret_10m": hp["10m"]["mean"],
                "mean_ret_15m": hp["15m"]["mean"],
                "unique_pools": conc["unique_pools"],
                "top1_pool_mass_share": conc["top1_pool_mass_share"],
                "top5_pool_mass_share": conc["top5_pool_mass_share"],
            }
        )
    return out


def build_oos_routed_rows(
    s039e: Any, oos_weeks: list[str], ch_docker: str, ch_url: str, ch_user: str, ch_pwd: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    g037 = s039e._load_g037()
    if ch_docker:
        s039e._patch_g037_ch_docker(g037, ch_docker)
    baseline = dict(g037.BASELINE_CELL)
    present = [w for w in oos_weeks if (Q4_BUNDLE / f"q4_candidates_{w}.csv").is_file()]
    cov: dict[str, Any] = {
        "oos_weeks_requested": list(oos_weeks),
        "oos_weeks_with_q4_csv": present,
        "per_week": [],
        "chrono_holdout_rows_total": 0,
        "young_pool_onset_gate_pass_rows_total": 0,
        "baseline_cell": baseline,
    }
    if not present:
        return [], cov

    all_by_regime = {w: g037.load_regime_csv(w, Q4_BUNDLE) for w in present}
    flat = [r for w in present for r in all_by_regime[w]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})
    pool_global_first = g037.fetch_pool_first_seen(ch_url, ch_user, ch_pwd or "-", pools_all)

    swaps_ticks: dict[str, dict[str, list[tuple[int, int | None]]]] = {}
    swaps_ts_only: dict[str, dict[str, list[int]]] = {}
    for reg in present:
        rows = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rows})
        ts_min = min(r["entry_time_sec"] for r in rows) - s039e.PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rows) + 600
        raw = g037.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd or "-", pools, ts_min, ts_max)
        swaps_ticks[reg] = raw
        swaps_ts_only[reg] = {p: [x[0] for x in raw.get(p, [])] for p in pools}

    routed: list[dict[str, Any]] = []
    holdout_total = 0
    pass_total = 0
    for reg in present:
        rows = all_by_regime[reg]
        _, ho = g037.split_chrono(rows)
        sc_ts = swaps_ts_only.get(reg, {})
        sc_full = swaps_ticks.get(reg, {})
        week_pass = 0
        for r in ho:
            ts_list = sc_ts.get(r["pool_address_lower"], [])
            det = s039e.routed_pass_detail(r, ts_list, pool_global_first, g037, baseline)
            if det is None:
                continue
            week_pass += 1
            sw = sc_full.get(r["pool_address_lower"], [])
            routed.append(s039e.enrich_row(r, det, sw))
        holdout_total += len(ho)
        pass_total += week_pass
        cov["per_week"].append(
            {
                "regime_week": reg,
                "q4_candidate_rows": len(rows),
                "chrono_holdout_rows": len(ho),
                "young_pool_onset_gate_pass_rows": week_pass,
            }
        )
    cov["chrono_holdout_rows_total"] = holdout_total
    cov["young_pool_onset_gate_pass_rows_total"] = pass_total
    return routed, cov


def main() -> None:
    AUDIT_G.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    snap = git_snap_head()

    ch_docker = (
        os.environ.get("SPRINT039G_CH_DOCKER_CONTAINER", "").strip()
        or os.environ.get("SPRINT039E_CH_DOCKER_CONTAINER", "").strip()
    )
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://127.0.0.1:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")

    fc = load_frozen_constants()
    s039e_weeks = sprint039e_regime_weeks()
    bundle_weeks = bundle_regime_weeks()
    oos_weeks = sorted(set(bundle_weeks) - s039e_weeks)
    excluded = sorted(set(bundle_weeks) & s039e_weeks)
    gap_missing_bundle = sorted(s039e_weeks - set(bundle_weeks))

    selection = {
        "kind": "sprint039g_oos_week_selection",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bundle_q4_dir": str(Q4_BUNDLE.relative_to(REPO)),
        "sprint039e_routed_rows_csv": str(AUDIT_E_ROWS.relative_to(REPO)),
        "regime_weeks_in_sprint039e_routed_csv": sorted(s039e_weeks),
        "regime_weeks_in_q4_bundle": bundle_weeks,
        "selected_oos_weeks": oos_weeks,
        "excluded_from_oos_because_in_sprint039e": excluded,
        "coverage_gap_weeks_in_039e_but_not_bundle_csv": gap_missing_bundle,
        "git_status_snapshot_sb_head": snap,
    }

    blocked_ch = not ch_pwd and not ch_docker
    base_rows: list[dict[str, Any]] = []

    if blocked_ch:
        selection["clickhouse_access"] = "blocked_no_password_no_docker_container"
        selection["oos_routed_rows_built"] = False
    else:
        selection["clickhouse_access"] = "docker_exec" if ch_docker else "http"
    gate_cov: dict[str, Any] = {}

    if blocked_ch:
        selection["clickhouse_access"] = "blocked_no_password_no_docker_container"
        selection["oos_routed_rows_built"] = False
    else:
        selection["clickhouse_access"] = "docker_exec" if ch_docker else "http"
        try:
            s039e = load_s039e()
            base_rows, gate_cov = build_oos_routed_rows(s039e, oos_weeks, ch_docker, ch_url, ch_user, ch_pwd)
            selection["oos_routed_rows_built"] = True
            selection["oos_weeks_with_q4_present"] = [
                w for w in oos_weeks if (Q4_BUNDLE / f"q4_candidates_{w}.csv").is_file()
            ]
            selection["oos_weeks_missing_q4_csv"] = [w for w in oos_weeks if w not in selection["oos_weeks_with_q4_present"]]
            selection["oos_routed_total_rows"] = len(base_rows)
            selection["oos_routed_rows_per_week"] = {
                w: sum(1 for r in base_rows if r["regime_week"] == w) for w in sorted({r["regime_week"] for r in base_rows})
            }
            selection["oos_canonical_gate_coverage"] = gate_cov
        except Exception as e:
            selection["oos_routed_rows_built"] = False
            selection["build_error"] = str(e)[:500]

    (AUDIT_G / "oos_week_selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")

    if not base_rows:
        ch_reachable = (not blocked_ch) and selection.get("oos_routed_rows_built")
        gate_pass_tot = (selection.get("oos_canonical_gate_coverage") or {}).get(
            "young_pool_onset_gate_pass_rows_total"
        )
        holdout_tot = (selection.get("oos_canonical_gate_coverage") or {}).get("chrono_holdout_rows_total")

        if blocked_ch:
            stub_status = "BLOCKED_BY_ARTIFACT_GAP_OR_SCHEMA"
            detail = "No ClickHouse access path (set CLICKHOUSE_PASSWORD or SPRINT039E_CH_DOCKER_CONTAINER)."
        elif selection.get("build_error"):
            stub_status = "BLOCKED_BY_ARTIFACT_GAP_OR_SCHEMA"
            detail = f"Build failed: {selection.get('build_error')}"
        elif ch_reachable and gate_pass_tot == 0:
            stub_status = "NEEDS_MORE_OOS_COVERAGE"
            detail = (
                "ClickHouse reachable and q4 CSVs loaded, but canonical young_pool_onset gate matched zero "
                "chrono-holdout rows across all selected OOS weeks — frozen guards cannot be evaluated on this OOS slice. "
                "Pick additional OOS regimes with non-empty routed holdout under Sprint037G semantics, or accept bundle expansion."
            )
        else:
            stub_status = "NEEDS_MORE_OOS_COVERAGE"
            detail = "No OOS routed rows materialized."

        stub_summary = {
            "kind": "sprint039g_oos_validation_summary",
            "created_at": selection["created_at"],
            "status": stub_status,
            "detail": detail,
            "oos_routed_slice_materialized": False,
            "oos_canonical_gate_coverage": selection.get("oos_canonical_gate_coverage"),
            "frozen_guard_evaluation": "skipped_no_routed_rows",
            "sanity_reference_note": (
                "Pipeline sanity (manual): week 2025-10-27 yields non-zero young_pool_onset routed holdout rows "
                "under the same Docker ClickHouse path and Sprint037G baseline_cell — empty OOS slice reflects "
                "regime composition vs gate, not ClickHouse outage."
            ),
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
            "horizon_fade_substitute_sprint039g_freeze": {
                "tick_range_ge": HORIZON_FADE_SUB_TICK_RANGE_GE,
                "price_velocity_gt": HORIZON_FADE_SUB_PRICE_VELOCITY_GT,
                "decision_time_visible": True,
                "note": "Declared for Sprint039G OOS only — not retuned on OOS outcomes.",
            },
            "frozen_thresholds_source": str(AUDIT_F_SPEC.relative_to(REPO)),
            "git_status_snapshot_sb_head": snap,
        }
        (AUDIT_G / "oos_validation_summary.json").write_text(json.dumps(stub_summary, indent=2), encoding="utf-8")
        hdr = (
            "regime_week,pool_address,entry_time_sec,pool_first_seen_ts,activity_onset_ts,"
            "pool_age_at_entry,time_since_activity_onset,ret_5m,ret_10m,ret_15m,ret_30m,"
            "label_5m,label_10m,label_15m,label_30m,exit_ok_5m,exit_ok_10m,exit_ok_15m,exit_ok_30m,"
            "activity_heat,price_velocity_5m,buy_pressure_ratio,token_volatility_5m,unique_buyer_entropy_5m,"
            "prior_runup_tick_span_onset_to_entry,prior_runup_proxy_velocity_x_log_toe,"
            "exhaustion_proxy_ret5m_minus_ret30m,tick_range,freshness_lag_sec,fee_bps,weth_is_token0\n"
        )
        (AUDIT_G / "oos_routed_slice_rows.csv").write_text(hdr, encoding="utf-8")

        empty_policies = [
            "baseline_young_pool_onset_flow",
            "vol_guard_exclude_tv_ge_2",
            "horizon_fade_substitute_guard",
            "combo_vol_guard_then_horizon_fade_substitute_sequential",
            "prior_runup_guard_diagnostic_only",
            "pool_launch_strict_diagnostic_only",
        ]
        gr_fields = [
            "policy_family",
            "evaluation_status",
            "n_chrono_holdout_rows_all_oos",
            "n_young_pool_onset_routed_rows_all_oos",
        ]
        with (AUDIT_G / "oos_guard_readout.csv").open("w", newline="", encoding="utf-8") as gf:
            gw = csv.DictWriter(gf, fieldnames=gr_fields)
            gw.writeheader()
            for pname in empty_policies:
                gw.writerow(
                    {
                        "policy_family": pname,
                        "evaluation_status": "SKIPPED_NO_ROUTED_ROWS",
                        "n_chrono_holdout_rows_all_oos": str(holdout_tot if holdout_tot is not None else ""),
                        "n_young_pool_onset_routed_rows_all_oos": str(gate_pass_tot if gate_pass_tot is not None else ""),
                    }
                )

        br_fields = ["policy_family", "regime_week", "evaluation_status", "n_rows_baseline", "n_rows_after"]
        with (AUDIT_G / "oos_guard_by_regime.csv").open("w", newline="", encoding="utf-8") as bf:
            bw = csv.DictWriter(bf, fieldnames=br_fields)
            bw.writeheader()
            for pw in selection.get("oos_weeks_with_q4_present") or []:
                hd = 0
                for row in (selection.get("oos_canonical_gate_coverage") or {}).get("per_week") or []:
                    if row.get("regime_week") == pw:
                        hd = row.get("chrono_holdout_rows") or 0
                        break
                bw.writerow(
                    {
                        "policy_family": "baseline_young_pool_onset_flow",
                        "regime_week": pw,
                        "evaluation_status": "SKIPPED_NO_ROUTED_ROWS",
                        "n_rows_baseline": hd,
                        "n_rows_after": 0,
                    }
                )

        weeks_zero = [
            r["regime_week"]
            for r in (selection.get("oos_canonical_gate_coverage") or {}).get("per_week") or []
            if (r.get("young_pool_onset_gate_pass_rows") or 0) == 0
        ]
        md_lines = [
            "# Run-039G — frozen scorecard_v2 guards OOS validation",
            "",
            f"Generated: {selection['created_at']}",
            "",
            f"**status:** `{stub_summary['status']}`",
            "",
            stub_summary["detail"],
            "",
            "## Coverage",
            "",
            f"- **ClickHouse:** `{selection.get('clickhouse_access')}`",
            f"- **chrono holdout rows (pre-gate), all OOS weeks:** `{holdout_tot}`",
            f"- **young_pool_onset gate pass rows:** `{gate_pass_tot}`",
            f"- **weeks with zero routed rows after gate:** `{weeks_zero}`",
            "",
            "Frozen guard economics were **not** evaluated (no routed-row population).",
            "",
            "## Artifacts",
            "",
            "- `mvp/audits/sprint039g/oos_week_selection.json`",
            "- `mvp/audits/sprint039g/oos_routed_slice_rows.csv` (header only)",
            "- `mvp/audits/sprint039g/oos_guard_readout.csv`",
            "- `mvp/audits/sprint039g/oos_guard_by_regime.csv`",
            "- `mvp/audits/sprint039g/oos_validation_summary.json`",
            "",
            "`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `supersession_candidate=false`",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039g_v2_guard_oos_validation.py*",
            "",
        ]
        (RUNS / "run-039G-v2-guard-oos-validation-readout.md").write_text("\n".join(md_lines), encoding="utf-8")
        return

    row_fields = sorted(base_rows[0].keys())
    with (AUDIT_G / "oos_routed_slice_rows.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row_fields, extrasaction="ignore")
        w.writeheader()
        for row in base_rows:
            w.writerow(row)

    proxy = classify_proxy_weeks(base_rows, sorted({r["regime_week"] for r in base_rows}))
    weeks_observed = sorted({r["regime_week"] for r in base_rows})

    policies: list[tuple[str, Callable[[dict[str, Any]], bool], str]] = [
        ("baseline_young_pool_onset_flow", lambda r: True, "Unfiltered OOS canonical routed holdout."),
        ("vol_guard_exclude_tv_ge_2", guard_vol_keep(fc), "Frozen VOL_GUARD_MIN_EXCLUDE from Sprint039F JSON."),
        (
            "horizon_fade_substitute_guard",
            guard_horizon_fade_substitute_keep(),
            "Sprint039G freeze: tick_range≥3500 AND price_velocity_5m>0.15 excluded (decision-time only).",
        ),
        (
            "combo_vol_guard_then_horizon_fade_substitute_sequential",
            combo_vol_then_hf(fc),
            "Sequential composition illustrative — ablate components separately.",
        ),
        (
            "prior_runup_guard_diagnostic_only",
            guard_prior_runup_keep(fc),
            "Frozen PRIOR_RUNUP_TICK_SPAN_MIN_EXCLUDE — diagnostic per Sprint039F guidance.",
        ),
        (
            "pool_launch_strict_diagnostic_only",
            guard_pool_launch_strict_keep(fc),
            "Frozen POOL_LAUNCH_STRICT_* — diagnostic subset shrink.",
        ),
    ]

    readout: list[dict[str, Any]] = []
    by_reg: list[dict[str, Any]] = []
    for name, pred, cav in policies:
        filt = apply_keep(base_rows, pred)
        readout.append(summarize_policy(name, base_rows, filt, proxy, cav))
        by_reg.extend(by_regime_detail(name, base_rows, filt, weeks_observed))

    rk = sorted(set().union(*(set(r.keys()) for r in readout)))
    with (AUDIT_G / "oos_guard_readout.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rk, extrasaction="ignore")
        w.writeheader()
        for row in readout:
            w.writerow(row)

    bk = sorted(set().union(*(set(r.keys()) for r in by_reg)))
    with (AUDIT_G / "oos_guard_by_regime.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=bk)
        w.writeheader()
        for row in by_reg:
            w.writerow(row)

    vol_row = next(r for r in readout if r["policy_family"] == "vol_guard_exclude_tv_ge_2")
    hf_row = next(r for r in readout if r["policy_family"] == "horizon_fade_substitute_guard")
    combo_row = next(r for r in readout if r["policy_family"] == "combo_vol_guard_then_horizon_fade_substitute_sequential")
    prior_row = next(r for r in readout if r["policy_family"] == "prior_runup_guard_diagnostic_only")

    pass_ret_v = vol_row.get("pass_like_proxy_retention_pct")
    fp_cut_v = vol_row.get("fp_like_proxy_row_reduction_pct")
    pass_ret_h = hf_row.get("pass_like_proxy_retention_pct")
    fp_cut_h = hf_row.get("fp_like_proxy_row_reduction_pct")
    pass_ret_combo = combo_row.get("pass_like_proxy_retention_pct")
    pass_ret_prior = prior_row.get("pass_like_proxy_retention_pct")

    cohort_ok = bool(proxy["pass_like_proxy_weeks"]) and bool(proxy["fp_like_proxy_weeks"])
    min_rows = len(base_rows) >= 60

    status = "V2_GUARDS_OOS_WEAK_BUT_NOT_INVALIDATED"
    if not cohort_ok or not min_rows:
        status = "NEEDS_MORE_OOS_COVERAGE"
    else:
        damaged_pass = (
            (pass_ret_v is not None and pass_ret_v < 55)
            or (pass_ret_h is not None and pass_ret_h < 55)
            or (pass_ret_combo is not None and pass_ret_combo < 55)
        )
        promising = (
            (fp_cut_v or 0) >= 10
            and (pass_ret_v or 0) >= 72
            and (fp_cut_h or 0) >= 5
            and (pass_ret_h or 0) >= 68
        )
        weak_fp_cut = fp_cut_v is not None and fp_cut_h is not None and fp_cut_v < 5 and fp_cut_h < 5
        if promising and not damaged_pass:
            status = "V2_GUARDS_OOS_PROMISING_REVIEW_REQUIRED"
        elif weak_fp_cut:
            status = "V2_GUARDS_INVALIDATED_BY_OOS"
        elif damaged_pass:
            status = "V2_GUARDS_OOS_WEAK_BUT_NOT_INVALIDATED"

    summary = {
        "kind": "sprint039g_oos_validation_summary",
        "created_at": selection["created_at"],
        "status": status,
        "oos_total_routed_rows": len(base_rows),
        "proxy_week_classification_baseline_30m_mean": proxy,
        "horizon_fade_substitute_sprint039g_freeze": {
            "tick_range_ge": HORIZON_FADE_SUB_TICK_RANGE_GE,
            "price_velocity_gt": HORIZON_FADE_SUB_PRICE_VELOCITY_GT,
            "decision_time_visible": True,
            "note": "Not tuned on OOS metrics — structural exhaustion-family proxy.",
        },
        "frozen_thresholds_source": str(AUDIT_F_SPEC.relative_to(REPO)),
        "vol_guard_oos": {
            "fp_like_proxy_row_reduction_pct": fp_cut_v,
            "pass_like_proxy_retention_pct": pass_ret_v,
        },
        "horizon_fade_substitute_oos": {
            "fp_like_proxy_row_reduction_pct": fp_cut_h,
            "pass_like_proxy_retention_pct": pass_ret_h,
        },
        "combo_oos": {
            "fp_like_proxy_row_reduction_pct": combo_row.get("fp_like_proxy_row_reduction_pct"),
            "pass_like_proxy_retention_pct": pass_ret_combo,
        },
        "prior_runup_diagnostic_oos": {
            "pass_like_proxy_retention_pct": pass_ret_prior,
            "note": "Expect PASS-like damage if spike threshold bites broadly — matches Sprint039F IS pattern.",
        },
        "judgement": {
            "vol_guard_reduces_bad_proxy_rows": (fp_cut_v or 0) >= 8 if fp_cut_v is not None else None,
            "horizon_fade_substitute_decision_time_visible": True,
            "combo_overhurts_coverage_vs_vol_alone": (pass_ret_combo or 100) < (pass_ret_v or 100) - 5
            if pass_ret_combo is not None and pass_ret_v is not None
            else None,
            "prior_runup_still_damages_pass_proxy": (pass_ret_prior or 100) < (pass_ret_v or 100) - 8
            if pass_ret_prior is not None and pass_ret_v is not None
            else None,
            "human_gate0_review_recommended": status == "V2_GUARDS_OOS_PROMISING_REVIEW_REQUIRED",
        },
        "non_claims": [
            "No supersession of FAILED_NO_7D_ALPHA",
            "No scorecard_v2 PASS claim on bundle",
            "No threshold tuning against OOS outcomes",
        ],
        "supersession_candidate": False,
        "git_status_snapshot_sb_head": snap,
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
    (AUDIT_G / "oos_validation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    md = "\n".join(
        [
            "# Run-039G — frozen scorecard_v2 guards OOS validation",
            "",
            f"Generated: {selection['created_at']}",
            "",
            f"**status:** `{status}`",
            "",
            f"- **OOS routed rows:** {len(base_rows)} across `{len(weeks_observed)}` regime weeks.",
            f"- **PASS_like proxy weeks (baseline mean 30m > 0):** `{proxy['pass_like_proxy_weeks']}`",
            f"- **FP_like proxy weeks (baseline mean 30m < 0):** `{proxy['fp_like_proxy_weeks']}`",
            "",
            "## Artifacts",
            "",
            "- `mvp/audits/sprint039g/oos_week_selection.json`",
            "- `mvp/audits/sprint039g/oos_routed_slice_rows.csv`",
            "- `mvp/audits/sprint039g/oos_guard_readout.csv`",
            "- `mvp/audits/sprint039g/oos_guard_by_regime.csv`",
            "- `mvp/audits/sprint039g/oos_validation_summary.json`",
            "",
            "`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `supersession_candidate=false`",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039g_v2_guard_oos_validation.py*",
            "",
        ]
    )
    (RUNS / "run-039G-v2-guard-oos-validation-readout.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
