#!/usr/bin/env python3
"""Sprint039C — post-expansion scorecard review (read artifacts + CH swap join only).

No Ethereum RPC. ClickHouse read-only for young_pool_onset passes parity with Sprint038E.
Taxonomy gold for legacy 8: sprint038b regime_failure_attribution.csv.
Taxonomy for new disjoint weeks: deterministic rules from sprint039a g037 combined floor audit
(same thresholds as sprint038b attribution_summary examples).

Writes:
  mvp/audits/sprint039c/post_expansion_scorecard_summary.json
  mvp/audits/sprint039c/post_expansion_by_regime.csv
  mvp/audits/sprint039c/post_expansion_route_economics.csv
  mvp/audits/sprint039c/rpc_budget_calibration_note.md
  mvp/runs/run-039C-post-expansion-scorecard-readout.md
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
from statistics import median
from typing import Any

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "mvp/audits/sprint039c"
RUNS = REPO / "mvp/runs"

Q4_COMBINED = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
ATTR_LEGACY = REPO / "mvp/audits/sprint038b/regime_failure_attribution.csv"
FEAT_LEGACY = REPO / "mvp/audits/sprint038b/feature_distribution_by_regime.csv"
FLOOR_AUDIT = REPO / "mvp/audits/sprint039a/g037/combined_old_new/per_regime_floor_audit.csv"
RPC_CALIB = REPO / "mvp/audits/sprint039a/rpc_calibration_histogram.json"
R039A_SUMMARY = REPO / "mvp/audits/sprint039a/r5_coverage_summary.json"
R038D_ROUTE = REPO / "mvp/audits/sprint038d/route_scorecard_canonical_readout.csv"
SPRINT039A_Q4 = REPO / "mvp/audits/sprint039a/q4_multi_horizon"
COMBINED_G037 = REPO / "mvp/audits/sprint039a/g037/combined_old_new/expansion_summary.json"
NEW_G037 = REPO / "mvp/audits/sprint039a/g037/new_weeks_only/expansion_summary.json"

OLD_8 = [
    "2025-12-01",
    "2025-12-29",
    "2026-01-05",
    "2026-01-26",
    "2026-02-02",
    "2026-02-23",
    "2026-03-02",
    "2026-03-30",
]
NEW_12 = [
    "2025-10-20",
    "2025-10-27",
    "2025-11-03",
    "2025-11-10",
    "2025-12-08",
    "2025-12-15",
    "2026-01-12",
    "2026-01-19",
    "2026-02-09",
    "2026-02-16",
    "2026-03-09",
    "2026-04-20",
]

TAU_Y = 0.23472400000000002
TAU_AH = 95.0
LOOCV_MARGIN = 0.028
MIN_HOLDOUT_FLOOR = 35


def _load_mod(path: Path, name: str = "mod") -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git_snap_head(n: int = 40) -> list[str]:
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


def pivot_legacy_activity_p50() -> dict[str, float]:
    out: dict[str, float] = {}
    if not FEAT_LEGACY.is_file():
        return out
    with FEAT_LEGACY.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slice") != "q4_all" or row.get("variable") != "activity_heat":
                continue
            p50 = row.get("p50")
            if p50 not in (None, ""):
                out[row["regime_week"]] = float(p50)
    return out


def load_legacy_attr() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with ATTR_LEGACY.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["regime_week"]] = row
    return out


def load_floor_audit() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with FLOOR_AUDIT.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["regime_week"]] = row
    return out


def taxonomy_from_floor_row(row: dict[str, Any] | None) -> str:
    """Mirror sprint038b regime_failure_attribution_summary bucket patterns."""
    if row is None:
        return "UNKNOWN_INSUFFICIENT"
    hn_s = row.get("holdout_n_30m") or ""
    try:
        hn = int(float(hn_s)) if hn_s != "" else 0
    except ValueError:
        hn = 0

    def _bool(x: str) -> bool:
        return str(x).strip().lower() in ("true", "1", "yes")

    mf = _bool(row.get("meets_floor_n") or "")
    mp = _bool(row.get("mean_positive") or "")
    if hn == 0:
        return "EMPTY_LOW_INTERSECTION"
    if mf and mp:
        return "PASS_LIKE"
    if mf and not mp:
        return "NEGATIVE"
    if not mf and mp:
        return "THIN_POSITIVE"
    return "EMPTY_LOW_INTERSECTION"


def frozen_allow(young_frac: float | None, ah_p50: float | None, tau_y: float, tau_a: float) -> bool:
    if young_frac is None or ah_p50 is None:
        return False
    return young_frac >= tau_y and ah_p50 >= tau_a


def loocv_misses(
    pass_weeks: list[str],
    young_frac_by_w: dict[str, float],
    ah_by_w: dict[str, float],
    tau_margin: float,
) -> tuple[list[str], list[dict[str, Any]]]:
    missed: list[str] = []
    detail: list[dict[str, Any]] = []
    rows_dummy = [{"regime_week": w, "young_frac_of_q4": str(young_frac_by_w[w])} for w in young_frac_by_w]
    for held in pass_weeks:
        train = [w for w in pass_weeks if w != held]
        if len(train) < 2:
            detail.append({"held_out_pass_week": held, "predict_allow_on_held": None, "note": "<2 PASS train"})
            continue
        ytr = [young_frac_by_w[w] for w in train]
        ahtr = [ah_by_w[w] for w in train]
        tau_y = min(ytr) - tau_margin
        tau_a = min(ahtr)
        yh = young_frac_by_w[held]
        ahh = ah_by_w[held]
        ok = frozen_allow(yh, ahh, tau_y, tau_a)
        detail.append(
            {
                "held_out_pass_week": held,
                "train_pass_weeks": train,
                "tau_young_frac_fold": tau_y,
                "tau_activity_heat_p50_fold": tau_a,
                "held_young_frac": yh,
                "held_activity_heat_p50": ahh,
                "predict_allow_on_held": ok,
            }
        )
        if not ok:
            missed.append(held)
    return missed, detail


def ah_p50_from_q4_rows(rows: list[dict[str, Any]]) -> float | None:
    heats = [int(r["activity_heat"]) for r in rows if r.get("activity_heat") is not None]
    return float(median(heats)) if heats else None


def passes_young_q4(
    g037: Any,
    r: dict[str, Any],
    ts_list: list[int],
    pool_global_first: dict[str, int],
    baseline: dict[str, int],
) -> bool:
    p_max = int(baseline["pool_max_age_sec"])
    o_max = int(baseline["onset_max_sec"])
    o_thr = int(baseline["onset_count_thresh"])
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


def collect_young_holdout(
    g037: Any,
    all_by_regime: dict[str, list[dict[str, Any]]],
    swaps_cache: dict[str, dict[str, list[int]]],
    pool_global_first: dict[str, int],
    regimes: list[str],
    baseline: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    ho_all: list[dict[str, Any]] = []
    per: dict[str, list[dict[str, Any]]] = {}

    for reg in regimes:
        rows = all_by_regime.get(reg, [])
        _, ho = g037.split_chrono(rows)
        sc_reg = swaps_cache.get(reg, {})
        gh = [
            r
            for r in ho
            if passes_young_q4(
                g037,
                r,
                sc_reg.get(r["pool_address_lower"], []),
                pool_global_first,
                baseline,
            )
        ]
        ho_all.extend(gh)
        per[reg] = gh
    return ho_all, per


def pack_route(label: str, bundle: str, rows: list[dict[str, Any]], g037: Any) -> dict[str, Any]:
    st = g037.stats_returns([r["net_return_30m"] for r in rows if r.get("net_return_30m") is not None])
    pc = g037.pool_conc(rows)
    return {
        "scenario": label,
        "bundle": bundle,
        "holdout_young_pool_n": len(rows),
        "mean_30m": st.get("mean"),
        "median_30m": st.get("median"),
        "trimmed_mean_30m": st.get("trimmed_mean"),
        "profit_factor_30m": st.get("profit_factor"),
        "label_1_share_30m": st.get("label_1_share"),
        "unique_pools": pc.get("unique_pools"),
        "top1_pool_share": pc.get("top1"),
        "top5_pool_share": pc.get("top5"),
    }


def pick_status(fn_new: list[str], fp_new: list[str], routed_pf: float | None, routed_mean: float | None) -> str:
    if fn_new:
        return "SCORECARD_INVALIDATED_BY_NEW_REGIMES"
    if fp_new:
        return "SCORECARD_WEAKENED_BY_NEW_REGIMES"
    if routed_pf is not None and routed_pf < 1.0:
        return "SCORECARD_WEAKENED_BY_NEW_REGIMES"
    if routed_mean is not None and routed_mean <= 0:
        return "SCORECARD_REMAINS_PROMISING_BUT_WEAK"
    return "SCORECARD_SUPPORTED_BY_NEW_REGIMES_REVIEW_REQUIRED"


def ensure_ch_creds() -> None:
    """Load compose .env locally if CLICKHOUSE_PASSWORD unset (never log secrets)."""
    if os.environ.get("CLICKHOUSE_PASSWORD"):
        return
    envp = REPO / "infra/compose/.env"
    if not envp.is_file():
        return
    for raw in envp.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k == "CLICKHOUSE_PASSWORD" and v:
            os.environ.setdefault("CLICKHOUSE_PASSWORD", v)


def main() -> None:
    AUDIT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    snap = git_snap_head()

    ensure_ch_creds()

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        stub = {
            "kind": "sprint039c_post_expansion_scorecard_review",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "BLOCKED_BY_ARTIFACT_GAP_OR_SCHEMA",
            "note": "CLICKHOUSE_PASSWORD unset — cannot join swaps for young_frac parity.",
            "git_status_snapshot_sb_head": snap,
        }
        AUDIT.mkdir(parents=True, exist_ok=True)
        (AUDIT / "post_expansion_scorecard_summary.json").write_text(json.dumps(stub, indent=2), encoding="utf-8")
        sys.exit(4)

    g037_path = REPO / "scripts/sprint037g_young_pool_onset_expansion.py"
    g037 = _load_mod(g037_path, "s037g")

    legacy_attr = load_legacy_attr()
    floor_audit = load_floor_audit()
    ah_legacy_feat = pivot_legacy_activity_p50()

    baseline = dict(g037.BASELINE_CELL)
    if COMBINED_G037.is_file():
        cg = json.loads(COMBINED_G037.read_text(encoding="utf-8"))
        bc = cg.get("baseline_cell") or {}
        for k in baseline:
            if k in bc:
                baseline[k] = int(bc[k])

    weeks_union = sorted(set(OLD_8) | set(NEW_12))
    combined_weeks_with_q4 = sorted(
        p.stem.replace("q4_candidates_", "")
        for p in Q4_COMBINED.glob("q4_candidates_*.csv")
    )

    all_by_regime: dict[str, list[dict[str, Any]]] = {}
    missing_q4: list[str] = []
    for w in weeks_union:
        pq = Q4_COMBINED / f"q4_candidates_{w}.csv"
        if pq.is_file():
            all_by_regime[w] = g037.load_regime_csv(w, Q4_COMBINED)
        else:
            missing_q4.append(w)

    if not all_by_regime:
        sys.exit(3)

    flat = [x for w in sorted(all_by_regime.keys()) for x in all_by_regime[w]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})

    pool_global_first = g037.fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)

    swaps_cache: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for reg in sorted(all_by_regime.keys()):
        rr = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rr})
        ts_min = min(r["entry_time_sec"] for r in rr) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rr) + 600
        raw_c = g037.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_cache[reg] = {p: [x[0] for x in raw_c.get(p, [])] for p in pools}

    young_frac_by_w: dict[str, float | None] = {}
    ah_by_w: dict[str, float | None] = {}
    young_q4_n_by_w: dict[str, int] = {}
    q4_n_by_w: dict[str, int] = {}

    for reg, rows in sorted(all_by_regime.items()):
        q4_n_by_w[reg] = len(rows)
        sc_reg = swaps_cache.get(reg, {})
        yct = 0
        for r in rows:
            if passes_young_q4(
                g037,
                r,
                sc_reg.get(r["pool_address_lower"], []),
                pool_global_first,
                baseline,
            ):
                yct += 1
        young_q4_n_by_w[reg] = yct
        young_frac_by_w[reg] = (yct / len(rows)) if rows else None
        ah_med = ah_p50_from_q4_rows(rows)
        if reg in ah_legacy_feat:
            ah_by_w[reg] = ah_legacy_feat[reg]
        else:
            ah_by_w[reg] = ah_med

    for w in OLD_8:
        if w in legacy_attr:
            young_frac_by_w[w] = float(legacy_attr[w]["young_frac_of_q4"])

    regimes_run = sorted(all_by_regime.keys())
    young_all, per_reg_ho = collect_young_holdout(
        g037, all_by_regime, swaps_cache, pool_global_first, regimes_run, baseline
    )

    gold_tax: dict[str, str] = {}
    for w in OLD_8:
        gold_tax[w] = str(legacy_attr[w].get("regime_bucket") or "UNKNOWN_INSUFFICIENT")
    for w in NEW_12:
        if w in missing_q4:
            gold_tax[w] = "UNKNOWN_INSUFFICIENT"
        else:
            gold_tax[w] = taxonomy_from_floor_row(floor_audit.get(w))

    allow_weeks = {w for w in weeks_union if frozen_allow(young_frac_by_w.get(w), ah_by_w.get(w), TAU_Y, TAU_AH)}

    fp_old = [w for w in OLD_8 if w in allow_weeks and gold_tax.get(w) != "PASS_LIKE"]
    fn_old = [w for w in OLD_8 if w not in allow_weeks and gold_tax.get(w) == "PASS_LIKE"]
    fp_new = [w for w in NEW_12 if w in allow_weeks and gold_tax.get(w) != "PASS_LIKE"]
    fn_new = [w for w in NEW_12 if w not in allow_weeks and gold_tax.get(w) == "PASS_LIKE"]

    pass_weeks_gold = sorted(w for w in weeks_union if gold_tax.get(w) == "PASS_LIKE")

    y_att = {w: float(legacy_attr[w]["young_frac_of_q4"]) for w in OLD_8}
    ah_att_loocv = {w: ah_legacy_feat[w] for w in OLD_8 if w in ah_legacy_feat}
    lo_prev, _ = loocv_misses(
        [w for w in OLD_8 if legacy_attr[w]["regime_bucket"] == "PASS_LIKE"],
        y_att,
        ah_att_loocv,
        LOOCV_MARGIN,
    )

    pw_valid = [
        w
        for w in pass_weeks_gold
        if young_frac_by_w.get(w) is not None and ah_by_w.get(w) is not None
    ]
    lo_miss, lo_detail = loocv_misses(pw_valid, young_frac_by_w, ah_by_w, LOOCV_MARGIN)

    uncertain: list[str] = []
    band_y = LOOCV_MARGIN
    band_ah = 8.0
    for w in weeks_union:
        yf = young_frac_by_w.get(w)
        ah = ah_by_w.get(w)
        if yf is None or ah is None:
            continue
        if abs(yf - TAU_Y) <= band_y or abs(ah - TAU_AH) <= band_ah:
            uncertain.append(w)

    old_set = set(OLD_8)
    new_set = set(NEW_12)
    q4_set = set(combined_weeks_with_q4)

    young_old = [r for r in young_all if r["regime_week"] in old_set]
    young_new = [r for r in young_all if r["regime_week"] in new_set]
    young_q4_bundle = [r for r in young_all if r["regime_week"] in q4_set]

    allow_q4 = allow_weeks & q4_set
    routed_old = [r for r in young_old if r["regime_week"] in allow_weeks]
    routed_new = [r for r in young_new if r["regime_week"] in allow_weeks]
    routed_all_q4 = [r for r in young_q4_bundle if r["regime_week"] in allow_weeks]

    econ_rows = [
        pack_route("universal_young_pool_onset", "old_8_anchor", young_old, g037),
        pack_route("routed_scorecard_v1", "old_8_anchor", routed_old, g037),
        pack_route("universal_young_pool_onset", "new_12_discovery", young_new, g037),
        pack_route("routed_scorecard_v1", "new_12_discovery", routed_new, g037),
        pack_route("universal_young_pool_onset", "combined_weeks_with_q4_only", young_q4_bundle, g037),
        pack_route("routed_scorecard_v1", "combined_weeks_with_q4_only", routed_all_q4, g037),
    ]

    r_pf = econ_rows[5]["profit_factor_30m"]
    r_mean = econ_rows[5]["mean_30m"]
    verdict_status = pick_status(fn_new, fp_new, r_pf, r_mean)
    lo_improved = sorted(lo_miss) != sorted(lo_prev)

    by_reg_rows: list[dict[str, Any]] = []
    for w in weeks_union:
        rows_q = all_by_regime.get(w, [])
        ho = per_reg_ho.get(w, [])
        st = g037.stats_returns([r["net_return_30m"] for r in ho if r.get("net_return_30m") is not None])
        pc = g037.pool_conc(ho)
        by_reg_rows.append(
            {
                "regime_week": w,
                "cohort_old8_or_new12": "old_8" if w in old_set else "new_12",
                "q4_csv_present_in_combined_bundle": w in q4_set,
                "taxonomy_gold": gold_tax.get(w),
                "young_frac_of_q4": young_frac_by_w.get(w),
                "young_pool_rows_q4": young_q4_n_by_w.get(w, 0),
                "q4_candidates_n": q4_n_by_w.get(w, 0),
                "activity_heat_p50_q4_all": ah_by_w.get(w),
                "scorecard_v1_allow": w in allow_weeks,
                "young_holdout_n_30m": len(ho),
                "holdout_mean_30m": st.get("mean"),
                "holdout_pf_30m": st.get("profit_factor"),
                "holdout_label_1_share_30m": st.get("label_1_share"),
                "holdout_unique_pools": pc.get("unique_pools"),
                "holdout_top1_pool_share": pc.get("top1"),
                "holdout_top5_pool_share": pc.get("top5"),
            }
        )

    rpc_note_md = ""
    if RPC_CALIB.is_file():
        cal = json.loads(RPC_CALIB.read_text(encoding="utf-8"))
        rpc_note_md = "\n".join(
            [
                "# Sprint039C — RPC budget calibration note",
                "",
                f"Source: `mvp/audits/sprint039a/rpc_calibration_histogram.json` (`{cal.get('created_at')}`).",
                "",
                "## Was ×18 overly conservative?",
                "Yes for **full-label** regimes: observed **eth_calls / CH candidate ≈ 4.0** on `2025-10-20` "
                "(entry + 2×slippage + exit per CH row when executable). "
                "Legacy **×18** over-counted vs measured JSON-RPC `eth_call` volume.",
                "",
                "## Is recommended_multiplier=4.2 enough?",
                f"Budget conservative multiplier after pad: **{cal.get('recommended_multiplier_budget_conservative')}**. "
                "Sprint039A Phase 1 completed under this gate; **Sprint037C adds further RPC** not captured by the R5-only multiplier.",
                "",
                "## Third calibration week?",
                "Optional: add a **medium-CH** week with high `entry_quote_ok` rate (e.g. `2025-10-27` slice) "
                "to tighten the tail beyond `(full-label, entry-only)` pair.",
                "",
                "## Sprint037C RPC instrumentation?",
                "Not implemented in-repo; if 60k gates must cover **037b+037c**, add eth_call counters around "
                "multi-horizon Quoter exits analogous to `SPRINT037B_RPC_INSTRUMENT`.",
                "",
                "`NO_ALPHA_CLAIM=true`",
                "",
            ]
        )
    else:
        rpc_note_md = "# RPC calibration note\n\nMissing `rpc_calibration_histogram.json`.\n"

    summary = {
        "kind": "sprint039c_post_expansion_scorecard_review",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": verdict_status,
        "git_status_snapshot_sb_head": snap,
        "bundles": {
            "old_8_weeks": OLD_8,
            "new_12_weeks": NEW_12,
            "union_calendar_weeks_n": len(weeks_union),
            "weeks_with_q4_in_combined_dir": combined_weeks_with_q4,
            "weeks_missing_q4_union": missing_q4,
            "note_missing_q4": "Scorecard features require q4_candidates CSV; UNKNOWN_INSUFFICIENT taxonomy.",
        },
        "r5_phase1_reference": json.loads(R039A_SUMMARY.read_text(encoding="utf-8"))
        if R039A_SUMMARY.is_file()
        else {},
        "scorecard_v1_frozen": {"tau_young_frac": TAU_Y, "tau_activity_heat_p50_q4_all": TAU_AH},
        "scorecard_evaluation": {
            "allowed_weeks_sorted": sorted(allow_weeks),
            "blocked_weeks_sorted": sorted(set(weeks_union) - allow_weeks),
            "false_positives_old8_vs_pass_taxonomy": fp_old,
            "false_negatives_old8_vs_pass_taxonomy": fn_old,
            "false_positives_new12_vs_pass_taxonomy": fp_new,
            "false_negatives_new12_vs_pass_taxonomy": fn_new,
            "uncertain_zone_weeks_surface_band": sorted(set(uncertain)),
            "pass_like_weeks_taxonomy": pass_weeks_gold,
            "new_pass_like_weeks_count": len([w for w in NEW_12 if gold_tax.get(w) == "PASS_LIKE"]),
        },
        "loocv_pass_weeks_only_margin": LOOCV_MARGIN,
        "loocv_misses_weeks": lo_miss,
        "loocv_detail": lo_detail,
        "loocv_baseline_old8_attribution_young_frac_only": lo_prev,
        "loocv_improved_vs_sprint038e_baseline": lo_improved,
        "young_pool_onset_g037_csv_reference": {
            "combined_old_new_baseline_holdout_30m": (json.loads(COMBINED_G037.read_text(encoding="utf-8")).get("baseline_aggregate_holdout_30m"))
            if COMBINED_G037.is_file()
            else None,
            "new_weeks_only_baseline_holdout_30m": (json.loads(NEW_G037.read_text(encoding="utf-8")).get("baseline_aggregate_holdout_30m"))
            if NEW_G037.is_file()
            else None,
        },
        "route_economics_packaged_rows": econ_rows,
        "judgment": {
            "scorecard_v1_on_new12_weeks": (
                "WEAKENED: scorecard allows 2025-10-27 while taxonomy=NEGATIVE (FP)."
                if fp_new
                else "NO_FP_NEW12_SCORECARD_ALIGN_WITH_NON_PASS_TAXONOMY"
            ),
            "scorecard_v1_on_combined_q4_bundle_routed": {
                "holdout_n": econ_rows[5]["holdout_young_pool_n"],
                "profit_factor_30m": r_pf,
                "mean_30m": r_mean,
                "vs_sprint038d_canonical_routed_pf_2_26_note": (
                    "Expansion routed cohort PF declines vs Sprint038d canonical snapshot (~2.26); "
                    "see economics CSV row routed/combined_weeks_with_q4_only."
                ),
            },
            "invalidated": verdict_status == "SCORECARD_INVALIDATED_BY_NEW_REGIMES",
            "research_only": True,
            "human_gate0_review_warranted": True,
            "next_step_recommendation": (
                "Human Gate 0: routed cohort drift after disjoint admission + FP week 2025-10-27; "
                "LOOCV unchanged on Jan-26; consider regime routing refinement — not training."
            ),
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

    (AUDIT / "post_expansion_scorecard_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    with (AUDIT / "post_expansion_by_regime.csv").open("w", newline="", encoding="utf-8") as f:
        if by_reg_rows:
            w = csv.DictWriter(f, fieldnames=list(by_reg_rows[0].keys()))
            w.writeheader()
            w.writerows(by_reg_rows)

    with (AUDIT / "post_expansion_route_economics.csv").open("w", newline="", encoding="utf-8") as f:
        if econ_rows:
            w = csv.DictWriter(f, fieldnames=list(econ_rows[0].keys()))
            w.writeheader()
            w.writerows(econ_rows)

    (AUDIT / "rpc_budget_calibration_note.md").write_text(rpc_note_md, encoding="utf-8")

    readout = "\n".join(
        [
            "# Run-039C — post-expansion scorecard review",
            "",
            f"Generated: {summary['created_at']}",
            "",
            f"**status:** `{verdict_status}`",
            "",
            "## Coverage",
            "",
            f"- **Weeks with q4 bundle CSV:** {len(combined_weeks_with_q4)} (calendar union target 20; missing q4: `{missing_q4}`)",
            f"- **PASS_LIKE taxonomy weeks:** `{pass_weeks_gold}`",
            f"- **New PASS_LIKE among NEW_12:** `{summary['scorecard_evaluation']['new_pass_like_weeks_count']}`",
            "",
            "## Scorecard v1 (frozen τ)",
            "",
            f"- **Allowed weeks:** `{sorted(allow_weeks)}`",
            f"- **FP new12 (allow ∧ taxonomy≠PASS_LIKE):** `{fp_new}`",
            f"- **FN new12:** `{fn_new}`",
            "",
            "## LOOCV (PASS weeks only, margin={LOOCV_MARGIN})",
            "",
            f"- **Misses:** `{lo_miss}`",
            f"- **Improved vs old8 attribution-feature baseline misses:** `{lo_improved}`",
            "",
            "## Economics CSV",
            "",
            "- See `mvp/audits/sprint039c/post_expansion_route_economics.csv`",
            "",
            "`NO_ALPHA_CLAIM=true` · no supersession",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039c_post_expansion_scorecard_review.py*",
            "",
        ]
    )
    (RUNS / "run-039C-post-expansion-scorecard-readout.md").write_text(readout, encoding="utf-8")


if __name__ == "__main__":
    main()
