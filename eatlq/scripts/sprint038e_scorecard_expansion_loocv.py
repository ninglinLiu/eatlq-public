#!/usr/bin/env python3
"""Sprint038E — canonical scorecard expansion scan + LOOCV / threshold-neighbor diagnostics.

Uses Sprint037G replay only (eatlq.uniswap_v3_swaps). Sprint038C inline replay is not evidence.

No training, no supersession, read-only ClickHouse.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mvp/audits/sprint038e"
RUNS = REPO / "mvp/runs"

Q4_PRIMARY = REPO / "mvp/audits/sprint038a/q4_multi_horizon"
Q4_SCAN_DIRS = [
    REPO / "mvp/audits/sprint037c",
    REPO / "mvp/audits/sprint037a",
]
R5_SUMMARY = REPO / "mvp/audits/sprint038a/r5_candidate_expansion_summary.json"
SPRINT038D_SUMMARY = REPO / "mvp/audits/sprint038d/canonical_replay_parity_summary.json"
SPRINT038C_SUMMARY = REPO / "mvp/audits/sprint038c/regime_family_routing_summary.json"
ATTR_CSV = REPO / "mvp/audits/sprint038b/regime_failure_attribution.csv"
FEAT_DIST = REPO / "mvp/audits/sprint038b/feature_distribution_by_regime.csv"
G037_SUMMARY = REPO / "mvp/audits/sprint038a/g037/expansion_summary.json"


def _load_script_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[038e {ts}] {msg}", flush=True)


def git_status_snapshot(max_lines: int = 60) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "status", "-sb"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        lines = (r.stdout or "").splitlines()
        return lines[:max_lines]
    except (OSError, subprocess.TimeoutExpired):
        return ["<git status unavailable>"]


def discover_q4_weeks(q4_dir: Path) -> list[str]:
    if not q4_dir.is_dir():
        return []
    return sorted(p.stem.replace("q4_candidates_", "") for p in q4_dir.glob("q4_candidates_*.csv"))


def expansion_candidates_disjoint(primary_weeks: set[str]) -> dict[str, Any]:
    """Find regime weeks present in other audit dirs but not in primary 8-week bundle."""
    found: dict[str, list[str]] = {}
    disjoint: list[str] = []
    for d in Q4_SCAN_DIRS:
        if not d.is_dir():
            continue
        key = str(d.relative_to(REPO))
        wks = discover_q4_weeks(d)
        found[key] = wks
        for w in wks:
            if w not in primary_weeks and w not in disjoint:
                disjoint.append(w)
    disjoint.sort()
    return {"scanned_dirs": found, "disjoint_weeks_with_csv": disjoint}


def pivot_q4_all_activity_p50() -> dict[str, float]:
    out: dict[str, float] = {}
    with FEAT_DIST.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slice") != "q4_all" or row.get("variable") != "activity_heat":
                continue
            p50 = row.get("p50")
            if p50 not in (None, ""):
                out[row["regime_week"]] = float(p50)
    return out


def load_attr_rows() -> list[dict[str, Any]]:
    with ATTR_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def frozen_allow(young_frac: float, ah_p50: float | None, tau_y: float, tau_a: float) -> bool:
    if ah_p50 is None:
        return False
    return young_frac >= tau_y and ah_p50 >= tau_a


def loocv_misses(
    pass_weeks: list[str],
    rows_attr: list[dict[str, Any]],
    ah_by_reg: dict[str, float],
    tau_margin: float,
) -> tuple[list[str], list[dict[str, Any]]]:
    missed: list[str] = []
    detail: list[dict[str, Any]] = []
    for held in pass_weeks:
        train = [w for w in pass_weeks if w != held]
        if len(train) < 2:
            detail.append({"held_out_pass_week": held, "predict_allow_on_held": None, "note": "<2 PASS train"})
            continue
        ytr = [float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == w)) for w in train]
        ahtr = [ah_by_reg[w] for w in train]
        tau_y = min(ytr) - tau_margin
        tau_a = min(ahtr)
        yf_h = float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == held))
        ah_h = ah_by_reg[held]
        ok = frozen_allow(yf_h, ah_h, tau_y, tau_a)
        detail.append(
            {
                "held_out_pass_week": held,
                "train_pass_weeks": train,
                "tau_young_frac_fold": tau_y,
                "tau_activity_heat_p50_fold": tau_a,
                "held_young_frac": yf_h,
                "held_activity_heat_p50": ah_h,
                "predict_allow_on_held": ok,
            }
        )
        if not ok:
            missed.append(held)
    return missed, detail


def collect_young_holdout_g037(
    g037: Any,
    all_by_regime: dict[str, list[dict[str, Any]]],
    swaps_cache: dict[str, dict[str, list[int]]],
    pool_global_first: dict[str, int],
    regimes: list[str],
    baseline: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    p_max = int(baseline["pool_max_age_sec"])
    o_max = int(baseline["onset_max_sec"])
    o_thr = int(baseline["onset_count_thresh"])
    ho_all: list[dict[str, Any]] = []
    per: dict[str, list[dict[str, Any]]] = {}

    def passes(r: dict[str, Any], ts_list: list[int]) -> bool:
        gfs = pool_global_first.get(r["pool_address_lower"])
        if gfs is None:
            gfs = ts_list[0] if ts_list else int(r["entry_time_sec"])
        age = max(0, int(r["entry_time_sec"]) - gfs)
        if age > p_max:
            return False
        onset = g037.compute_activity_onset(ts_list, int(r["entry_time_sec"]), gfs, o_thr)
        if onset is None:
            return False
        if int(r["entry_time_sec"]) - onset > o_max:
            return False
        pv = r.get("price_velocity_5m")
        if pv is None or float(pv) <= g037.FLOW_EPS:
            return False
        return True

    for reg in regimes:
        rows = all_by_regime.get(reg, [])
        _, ho = g037.split_chrono(rows)
        sc_reg = swaps_cache.get(reg, {})
        gh = [r for r in ho if passes(r, sc_reg.get(r["pool_address_lower"], []))]
        ho_all.extend(gh)
        per[reg] = gh
    return ho_all, per


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    snap = git_status_snapshot()

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        sys.exit(2)

    g037_path = REPO / "scripts/sprint037g_young_pool_onset_expansion.py"
    if not g037_path.is_file():
        _log("ERROR: sprint037g missing")
        sys.exit(4)

    g037 = _load_script_module("sprint037g_young_pool_onset_expansion", g037_path)

    primary_weeks_list = discover_q4_weeks(Q4_PRIMARY)
    primary_weeks = set(primary_weeks_list)
    expansion_scan = expansion_candidates_disjoint(primary_weeks)
    extra_weeks = expansion_scan["disjoint_weeks_with_csv"]

    r5_regimes: list[str] = []
    if R5_SUMMARY.is_file():
        r5 = json.loads(R5_SUMMARY.read_text(encoding="utf-8"))
        r5_regimes = list((r5.get("regimes_order") or []))

    regimes_full = sorted(primary_weeks | set(extra_weeks))
    canonical_evidence_expanded = bool(extra_weeks)

    all_by_regime: dict[str, list[dict[str, Any]]] = {}
    for reg in regimes_full:
        path_pri = Q4_PRIMARY / f"q4_candidates_{reg}.csv"
        path_alt = None
        for d in Q4_SCAN_DIRS:
            p = d / f"q4_candidates_{reg}.csv"
            if p.is_file():
                path_alt = p
                break
        chosen = path_pri if path_pri.is_file() else path_alt
        if chosen is None:
            _log(f"WARN: missing CSV for regime {reg}")
            continue
        base_dir = chosen.parent
        all_by_regime[reg] = g037.load_regime_csv(reg, base_dir)

    regimes_run = sorted(all_by_regime.keys())
    if not regimes_run:
        _log("ERROR: no regimes loaded")
        sys.exit(3)

    flat = [x for r in regimes_run for x in all_by_regime[r]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})

    _log(f"Canonical replay regimes={len(regimes_run)} pools={len(pools_all)} …")
    pool_global_first = g037.fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)
    if pools_all and not pool_global_first:
        _log("ERROR: CH first-seen empty")
        sys.exit(3)

    swaps_cache: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for reg in regimes_run:
        rr = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rr})
        ts_min = min(r["entry_time_sec"] for r in rr) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rr) + 600
        raw_c = g037.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_cache[reg] = {p: [x[0] for x in raw_c.get(p, [])] for p in pools}

    arch = json.loads(G037_SUMMARY.read_text(encoding="utf-8"))
    baseline = dict(g037.BASELINE_CELL)
    ac = arch.get("baseline_cell") or {}
    for k in baseline:
        if k in ac:
            baseline[k] = int(ac[k])

    young_all, per_reg = collect_young_holdout_g037(g037, all_by_regime, swaps_cache, pool_global_first, regimes_run, baseline)

    s38c_sum = json.loads(SPRINT038C_SUMMARY.read_text(encoding="utf-8"))
    pr = s38c_sum.get("proposed_route_rule") or {}
    tau_y_base = float(pr.get("tau_young_frac", 0.234724))
    tau_a_base = float(pr.get("tau_activity_heat_p50_q4_all", 95.0))
    young_margin_calibration = 0.028

    ah_by_reg = pivot_q4_all_activity_p50()
    rows_attr = load_attr_rows()
    attr_by_week = {r["regime_week"]: r for r in rows_attr}

    pass_weeks_tax = [r["regime_week"] for r in rows_attr if r["regime_bucket"] == "PASS_LIKE"]

    def scorecard_weeks(tau_y: float, tau_a: float) -> set[str]:
        allow: set[str] = set()
        for r in rows_attr:
            w = r["regime_week"]
            if w not in ah_by_reg:
                continue
            yf = float(r["young_frac_of_q4"])
            if frozen_allow(yf, ah_by_reg[w], tau_y, tau_a):
                allow.add(w)
        return allow

    def fp_fn(tau_y: float, tau_a: float) -> tuple[list[str], list[str]]:
        fp: list[str] = []
        fn: list[str] = []
        allow = scorecard_weeks(tau_y, tau_a)
        for r in rows_attr:
            w = r["regime_week"]
            gold = r["regime_bucket"] == "PASS_LIKE"
            pred = w in allow
            if pred and not gold:
                fp.append(w)
            if not pred and gold:
                fn.append(w)
        return fp, fn

    lo_miss_base, lo_detail_base = loocv_misses(pass_weeks_tax, rows_attr, ah_by_reg, young_margin_calibration)

    neighbor_specs = [
        ("frozen_baseline_scorecard_v1", tau_y_base, tau_a_base),
        ("frozen_slightly_stricter_young_+0.02", tau_y_base + 0.02, tau_a_base),
        ("frozen_slightly_stricter_ah_+5", tau_y_base, tau_a_base + 5.0),
        ("frozen_slightly_stricter_both", tau_y_base + 0.02, tau_a_base + 5.0),
        ("frozen_slightly_looser_young_-0.02", tau_y_base - 0.02, tau_a_base),
        ("frozen_slightly_looser_ah_-5", tau_y_base, tau_a_base - 5.0),
        ("frozen_slightly_looser_both", tau_y_base - 0.02, tau_a_base - 5.0),
    ]

    loocv_rows: list[dict[str, Any]] = []
    for name, ty, ta in neighbor_specs:
        allow = scorecard_weeks(ty, ta)
        fp, fn = fp_fn(ty, ta)
        loocv_rows.append(
            {
                "variant_kind": "frozen_threshold_neighbor",
                "variant_name": name,
                "tau_young_frac": ty,
                "tau_activity_heat_p50": ta,
                "n_weeks_allow": len(allow),
                "weeks_allow_sorted": ",".join(sorted(allow)),
                "fp_weeks": ";".join(fp),
                "fn_weeks": ";".join(fn),
                "loocv_pass_only_refit_misses_base_margin_0_028": ";".join(lo_miss_base),
                "notes": "LOOCV column identical across frozen neighbors — refit uses PASS subset only, not frozen τ.",
            }
        )

    for m in (0.020, 0.028, 0.036):
        miss, _det = loocv_misses(pass_weeks_tax, rows_attr, ah_by_reg, m)
        loocv_rows.append(
            {
                "variant_kind": "loocv_margin_sweep",
                "variant_name": f"pass_only_loocv_young_margin_{m}",
                "tau_young_frac": "",
                "tau_activity_heat_p50": "",
                "n_weeks_allow": "",
                "weeks_allow_sorted": "",
                "fp_weeks": "",
                "fn_weeks": "",
                "loocv_pass_only_refit_misses_base_margin_0_028": ";".join(miss),
                "notes": "Structural LOOCV — τ_y_fold=min(train young_frac)−margin; τ_ah_fold=min(train ah p50).",
            }
        )

    uncertain_weeks: list[str] = []
    band_y = young_margin_calibration
    band_ah = 8.0
    for r in rows_attr:
        w = r["regime_week"]
        if w not in ah_by_reg:
            continue
        yf = float(r["young_frac_of_q4"])
        ah = ah_by_reg[w]
        near_y = abs(yf - tau_y_base) <= band_y
        near_a = abs(ah - tau_a_base) <= band_ah
        if near_y or near_a:
            uncertain_weeks.append(w)

    loocv_rows.append(
        {
            "variant_kind": "uncertain_zone_diag",
            "variant_name": "margin_band_young<=0.028_or_ah<=8_vs_baseline_tau",
            "tau_young_frac": tau_y_base,
            "tau_activity_heat_p50": tau_a_base,
            "n_weeks_allow": len(uncertain_weeks),
            "weeks_allow_sorted": ",".join(sorted(set(uncertain_weeks))),
            "fp_weeks": "",
            "fn_weeks": "",
            "loocv_pass_only_refit_misses_base_margin_0_028": ";".join(lo_miss_base),
            "notes": "Weeks near scorecard surface (not a prediction set).",
        }
    )

    h26 = ah_by_reg.get("2026-01-26"), float(attr_by_week["2026-01-26"]["young_frac_of_q4"]) if "2026-01-26" in attr_by_week else None
    boundary_2026_01_26 = {
        "young_frac_of_q4": h26[1],
        "activity_heat_p50_q4_all": h26[0],
        "equals_baseline_tau_activity_band": h26[0] is not None and abs(h26[0] - tau_a_base) <= 1e-9,
        "loocv_failure_when_train_is_Dec_weeks": (
            "min(train PASS activity_heat p50) can rise to 103 when 2026-01-26 held out and train={2025-12-01,2025-12-29}; "
            "held ah=95 fails τ_ah_fold=103."
        ),
        "still_boundary_case": True,
    }

    allow_base = scorecard_weeks(tau_y_base, tau_a_base)
    routed = [r for r in young_all if r.get("regime_week") in allow_base]

    def pack(label: str, rows_slice: list[dict[str, Any]]) -> dict[str, Any]:
        st = g037.stats_returns([r["net_return_30m"] for r in rows_slice if r.get("net_return_30m") is not None])
        pc = g037.pool_conc(rows_slice)
        return {
            "scenario": label,
            "holdout_young_pool_n": len(rows_slice),
            "mean_30m": st.get("mean"),
            "median_30m": st.get("median"),
            "profit_factor_30m": st.get("profit_factor"),
            "label_1_share_30m": st.get("label_1_share"),
            "pools_unique_pools": pc.get("unique_pools"),
            "pools_top1": pc.get("top1"),
            "pools_top5": pc.get("top5"),
        }

    sim_uni = pack("canonical_universal", young_all)
    sim_rt = pack("canonical_routed_scorecard_v1_baseline_tau", routed)

    by_reg_rows: list[dict[str, Any]] = []
    for reg in regimes_run:
        sub = per_reg.get(reg, [])
        st = g037.stats_returns([r["net_return_30m"] for r in sub if r.get("net_return_30m") is not None])
        pc = g037.pool_conc(sub)
        tax = attr_by_week.get(reg, {}).get("regime_bucket", "")
        yf = float(attr_by_week[reg]["young_frac_of_q4"]) if reg in attr_by_week else None
        ah50 = ah_by_reg.get(reg)
        by_reg_rows.append(
            {
                "regime_week": reg,
                "taxonomy_sprint038b": tax if reg in attr_by_week else "NO_ATTRIBUTION_ROW",
                "in_primary_q4_bundle": reg in primary_weeks,
                "young_frac_of_q4": yf,
                "activity_heat_p50_q4_all": ah50,
                "scorecard_allow_baseline_v1": reg in allow_base,
                "young_holdout_n": len(sub),
                "mean_30m": st.get("mean"),
                "median_30m": st.get("median"),
                "profit_factor_30m": st.get("profit_factor"),
                "label_1_share_30m": st.get("label_1_share"),
                "pools_unique_pools": pc.get("unique_pools"),
                "pools_top1": pc.get("top1"),
                "pools_top5": pc.get("top5"),
            }
        )

    s38d_ref: dict[str, Any] = {}
    if SPRINT038D_SUMMARY.is_file():
        s38d_ref = json.loads(SPRINT038D_SUMMARY.read_text(encoding="utf-8"))

    fp_b, fn_b = fp_fn(tau_y_base, tau_a_base)
    s38d_prev = (s38d_ref.get("scorecard_v1_post_canonical") or {}) if s38d_ref else {}
    prev_miss = list(s38d_prev.get("loocv_pass_weeks_missed") or [])
    loocv_improved_vs_038d = len(lo_miss_base) < len(prev_miss)

    gap_notes = []
    if not canonical_evidence_expanded:
        gap_notes.append(
            "No disjoint q4_candidates_*.csv found beyond mvp/audits/sprint038a/q4_multi_horizon eight weeks; "
            "sprint037c weeks are a subset. Need new R5/Q4 artifact weeks for out-of-sample regime admission testing."
        )

    if not canonical_evidence_expanded:
        status = "NEEDS_MORE_R5_COVERAGE"
    elif fp_b or fn_b:
        status = "SCORECARD_INVALIDATED_BY_NEW_REGIMES"
    elif lo_miss_base:
        status = "SCORECARD_REMAINS_PROMISING_LOOCV_FRAGILE"
    else:
        status = "SCORECARD_EXPANSION_SUPPORTS_REVIEW_BUT_NO_SUPERSESSION"

    summary = {
        "kind": "sprint038e_scorecard_expansion_loocv",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "git_status_snapshot_sb_head": snap,
        "sprint038c_inline_route_simulation": {
            "evidence_class": "diagnostic_only",
            "reference_audit": "mvp/audits/sprint038d/canonical_replay_parity_summary.json",
            "canonical_replay_required_for_route_metrics": True,
        },
        "sprint038d_canonical_snapshot_ref": {
            "parity_pass": s38d_ref.get("parity_canonical_vs_archived_pass"),
            "inline_diverged": (
                (s38d_ref.get("difference_attribution") or {}).get("young_holdout_jaccard_keys_pool_entry")
            ),
        },
        "canonical_evidence_expanded": canonical_evidence_expanded,
        "additional_regime_weeks_loaded": extra_weeks,
        "primary_bundle_weeks": primary_weeks_list,
        "regimes_replayed_canonical": regimes_run,
        "expansion_scan": expansion_scan,
        "r5_summary_regimes_order": r5_regimes,
        "expansion_gap_notes": gap_notes,
        "new_pass_like_weeks_added": 0,
        "scorecard_v1_baseline": {
            "tau_young_frac": tau_y_base,
            "tau_activity_heat_p50_q4_all": tau_a_base,
            "young_margin_used_in_loocv_refit": young_margin_calibration,
        },
        "taxonomy_fp_fn_baseline": {"fp_weeks": fp_b, "fn_weeks": fn_b},
        "loocv_fragility": {
            "missed_pass_weeks_margin_0_028": lo_miss_base,
            "improved_vs_sprint038d": loocv_improved_vs_038d,
            "sprint038d_prior_misses": prev_miss,
            "detail": lo_detail_base,
        },
        "boundary_week_2026_01_26": boundary_2026_01_26,
        "uncertain_zone_weeks_near_surface": sorted(set(uncertain_weeks)),
        "canonical_aggregate": {"universal": sim_uni, "routed_baseline_scorecard": sim_rt},
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

    if status == "NEEDS_MORE_R5_COVERAGE" and lo_miss_base:
        summary["secondary_assessment"] = "SCORECARD_REMAINS_PROMISING_LOOCV_FRAGILE_ON_BASE_8_WEEKS"

    (OUT / "scorecard_expansion_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if by_reg_rows:
        keys = list(by_reg_rows[0].keys())
        with (OUT / "scorecard_expansion_by_regime.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(by_reg_rows)

    if loocv_rows:
        keys = list(loocv_rows[0].keys())
        with (OUT / "scorecard_loocv_hardening.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(loocv_rows)

    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "run-038E-scorecard-expansion-readout.md").write_text(
        "\n".join(
            [
                "# Run-038E — canonical scorecard expansion + LOOCV hardening",
                "",
                f"Generated: {summary['created_at']}",
                "",
                f"**status:** `{status}`",
                "",
                f"- **canonical_evidence_expanded:** `{canonical_evidence_expanded}`",
                f"- **additional regimes:** `{extra_weeks}`",
                f"- **LOOCV misses (margin 0.028):** `{lo_miss_base}`",
                f"- **FP/FN baseline:** fp=`{fp_b}` fn=`{fn_b}`",
                "",
                "## Canonical aggregates (Sprint037G replay)",
                "",
                f"- Universal n={sim_uni.get('holdout_young_pool_n')} mean={sim_uni.get('mean_30m')} PF={sim_uni.get('profit_factor_30m')}",
                f"- Routed n={sim_rt.get('holdout_young_pool_n')} mean={sim_rt.get('mean_30m')} PF={sim_rt.get('profit_factor_30m')}",
                "",
                "`NO_ALPHA_CLAIM=true` · inline Sprint038C replay **not** primary evidence — see Sprint038D.",
                "",
                "---",
                "",
                "*Generated by scripts/sprint038e_scorecard_expansion_loocv.py*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _log(f"done status={status} expanded={canonical_evidence_expanded} loocv_miss={lo_miss_base}")


if __name__ == "__main__":
    main()
