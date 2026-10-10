#!/usr/bin/env python3
"""Sprint038D — canonical Sprint037G replay parity vs Sprint038C inline + scorecard hardening.

Restores comparison against archived g037 artifacts; re-evaluates scorecard_v1 using
canonical replay only when parity checks pass.

No training, no RPC, read-only ClickHouse.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mvp/audits/sprint038d"
RUNS = REPO / "mvp/runs"

Q4_DIR = REPO / "mvp/audits/sprint038a/q4_multi_horizon"
G037_SUMMARY = REPO / "mvp/audits/sprint038a/g037/expansion_summary.json"
G037_PER_REGIME = REPO / "mvp/audits/sprint038a/g037/per_regime_floor_audit.csv"
SPRINT038C_SUMMARY = REPO / "mvp/audits/sprint038c/regime_family_routing_summary.json"
SPRINT038C_FEATURES = REPO / "mvp/audits/sprint038c/regime_family_features.csv"
ATTR_CSV = REPO / "mvp/audits/sprint038b/regime_failure_attribution.csv"
FEAT_DIST = REPO / "mvp/audits/sprint038b/feature_distribution_by_regime.csv"


def _load_script_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[038d {ts}] {msg}", flush=True)


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


def loocv_pass_weeks(pass_weeks: list[str], rows_attr: list[dict[str, Any]], ah_by_reg: dict[str, float], tau_margin: float) -> tuple[list[str], list[dict[str, Any]]]:
    """PASS-only LOOCV refit on young_frac min − margin and min activity p50 on train PASS subset."""
    details: list[dict[str, Any]] = []
    missed: list[str] = []
    for held in pass_weeks:
        train = [w for w in pass_weeks if w != held]
        if len(train) < 2:
            details.append({"held": held, "note": "<2 train PASS", "predict_allow_on_held": None})
            continue
        ytr = [float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == w)) for w in train]
        ahtr = [ah_by_reg[w] for w in train]
        tau_y = min(ytr) - tau_margin
        tau_a = min(ahtr)
        yf_h = float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == held))
        ah_h = ah_by_reg[held]
        ok = frozen_allow(yf_h, ah_h, tau_y, tau_a)
        details.append(
            {
                "held_out_pass_week": held,
                "tau_young_frac_fold": tau_y,
                "tau_activity_heat_p50_fold": tau_a,
                "predict_allow_on_held": ok,
            }
        )
        if not ok:
            missed.append(held)
    return missed, details


def row_key(r: dict[str, Any]) -> tuple[str, int]:
    return (str(r.get("pool_address_lower") or ""), int(r["entry_time_sec"]))


def jaccard(a: set[tuple[str, int]], b: set[tuple[str, int]]) -> float | None:
    if not a and not b:
        return 1.0
    u = len(a | b)
    if u == 0:
        return None
    return len(a & b) / u


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


def collect_young_holdout_inline(
    s38c: Any,
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
        onset = s38c.compute_activity_onset(ts_list, int(r["entry_time_sec"]), gfs, o_thr)
        if onset is None:
            return False
        if int(r["entry_time_sec"]) - onset > o_max:
            return False
        pv = r.get("price_velocity_5m")
        if pv is None or float(pv) <= s38c.FLOW_EPS:
            return False
        return True

    for reg in regimes:
        rows = all_by_regime.get(reg, [])
        _, ho = s38c.split_chrono_rows(rows)
        sc_reg = swaps_cache.get(reg, {})
        gh = [r for r in ho if passes(r, sc_reg.get(r["pool_address_lower"], []))]
        ho_all.extend(gh)
        per[reg] = gh
    return ho_all, per


def pool_conc_g037(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    if not rows:
        return {"unique_pools": 0, "top1": None, "top5": None}
    ctr = Counter(r["pool_address_lower"] for r in rows)
    n = len(rows)
    mv = ctr.most_common()
    return {
        "unique_pools": len(ctr),
        "top1": round(mv[0][1] / n, 6) if mv else None,
        "top5": round(sum(c for _, c in mv[:5]) / n, 6) if mv else None,
    }


def scan_scorecard_allow_from_csv() -> dict[str, bool]:
    out: dict[str, bool] = {}
    with SPRINT038C_FEATURES.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rw = row["regime_week"]
            v = row.get("scorecard_predict_allow_young_pool_route", "").strip().lower()
            out[rw] = v in ("true", "1", "yes")
    return out


def supplement_fee_weth_q4all() -> list[dict[str, Any]]:
    """Parse stable fee/weth fields from feature_distribution_by_regime (q4_all)."""
    fee_by_reg: dict[str, str] = {}
    weth_by_reg: dict[str, str] = {}
    with FEAT_DIST.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slice") != "q4_all":
                continue
            rw = row["regime_week"]
            vn = row.get("variable") or ""
            note = row.get("value_note") or ""
            if vn == "fee_bps_top3":
                fee_by_reg[rw] = note
            elif vn == "weth_is_token0_counts":
                weth_by_reg[rw] = note
    rows_out: list[dict[str, Any]] = []
    regimes = sorted(set(fee_by_reg) | set(weth_by_reg))
    for rw in regimes:
        rows_out.append(
            {
                "regime_week": rw,
                "fee_bps_top3_note_q4_all": fee_by_reg.get(rw, ""),
                "weth_is_token0_counts_note_q4_all": weth_by_reg.get(rw, ""),
            }
        )
    return rows_out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        sys.exit(2)

    g037_path = REPO / "scripts/sprint037g_young_pool_onset_expansion.py"
    if not g037_path.is_file():
        summary = {
            "kind": "sprint038d_canonical_route_parity",
            "status": "CANONICAL_RESTORE_BLOCKED",
            "parity_canonical_vs_archived_pass": False,
            "notes": "scripts/sprint037g_young_pool_onset_expansion.py missing",
            "decision_flags": {"NO_ALPHA_CLAIM": True, "TRAINING_ALLOWED": False},
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "canonical_replay_parity_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        sys.exit(4)

    g037 = _load_script_module("sprint037g_young_pool_onset_expansion", g037_path)
    s38c = _load_script_module("sprint038c_regime_family_routing", REPO / "scripts/sprint038c_regime_family_routing.py")

    regimes = g037.discover_regimes(Q4_DIR)
    if not regimes:
        _log(f"ERROR: no CSV under {Q4_DIR}")
        sys.exit(3)

    all_by_regime = {r: g037.load_regime_csv(r, Q4_DIR) for r in regimes}
    flat = [x for r in regimes for x in all_by_regime[r]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})

    _log(f"fetch canonical first-seen (uniswap_v3_swaps) pools={len(pools_all)} …")
    pgf_can = g037.fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)
    _log("fetch inline first-seen (uniswap_v3_swaps_enriched) …")
    pgf_inl = s38c.fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)

    swaps_can: dict[str, dict[str, list[int]]] = {}
    swaps_inl: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for reg in regimes:
        rr = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rr})
        ts_min = min(r["entry_time_sec"] for r in rr) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rr) + 600
        _log(f"  swaps canonical {reg} …")
        raw_c = g037.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_can[reg] = {p: [x[0] for x in raw_c.get(p, [])] for p in pools}
        _log(f"  swaps inline {reg} …")
        raw_i = s38c.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_inl[reg] = {p: raw_i.get(p, []) for p in pools}

    arch = json.loads(G037_SUMMARY.read_text(encoding="utf-8"))
    baseline = dict(g037.BASELINE_CELL)
    archived_cell = arch.get("baseline_cell") or {}
    for k in baseline:
        if k in archived_cell:
            baseline[k] = int(archived_cell[k])

    young_can, per_can = collect_young_holdout_g037(g037, all_by_regime, swaps_can, pgf_can, regimes, baseline)
    young_inl, per_inl = collect_young_holdout_inline(s38c, all_by_regime, swaps_inl, pgf_inl, regimes, baseline)

    archived_rows: dict[str, dict[str, Any]] = {}
    with G037_PER_REGIME.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            archived_rows[row["regime_week"]] = row

    parity_rows: list[dict[str, Any]] = []
    set_can = {row_key(r) for r in young_can}
    set_inl = {row_key(r) for r in young_inl}
    jac = jaccard(set_can, set_inl)

    per_regime_diff_notes: list[str] = []

    for reg in regimes:
        ar = archived_rows.get(reg, {})
        n_arch = int(ar["holdout_n_30m"]) if ar.get("holdout_n_30m") not in (None, "") else 0
        n_can = len(per_can.get(reg, []))
        n_inl = len(per_inl.get(reg, []))
        st_can = g037.stats_returns([r["net_return_30m"] for r in per_can.get(reg, []) if r.get("net_return_30m") is not None])
        st_inl = g037.stats_returns([r["net_return_30m"] for r in per_inl.get(reg, []) if r.get("net_return_30m") is not None])
        m_arch = float(ar["holdout_mean_30m"]) if ar.get("holdout_mean_30m") not in (None, "") else None
        parity_rows.append(
            {
                "regime_week": reg,
                "archived_n": n_arch,
                "canonical_n": n_can,
                "inline_n": n_inl,
                "archived_mean_30m": m_arch,
                "canonical_mean_30m": st_can.get("mean"),
                "inline_mean_30m": st_inl.get("mean"),
                "canonical_pf_30m": st_can.get("profit_factor"),
                "inline_pf_30m": st_inl.get("profit_factor"),
                "canonical_label_1_share": st_can.get("label_1_share"),
                "inline_label_1_share": st_inl.get("label_1_share"),
                "n_match_archived_vs_canonical": n_arch == n_can,
            }
        )
        if n_arch != n_can:
            per_regime_diff_notes.append(f"{reg}: archived_n={n_arch} canonical_n={n_can}")

    agg_arch = arch.get("baseline_aggregate_holdout_30m") or {}
    st_can_all = g037.stats_returns([r["net_return_30m"] for r in young_can if r.get("net_return_30m") is not None])
    st_inl_all = g037.stats_returns([r["net_return_30m"] for r in young_inl if r.get("net_return_30m") is not None])

    total_arch_n = int(agg_arch.get("n", 0))
    parity_total_n = total_arch_n == st_can_all.get("n") == len(young_can)

    first_seen_mismatch = sum(1 for p in pools_all if pgf_can.get(p) != pgf_inl.get(p))

    diff_attribution = {
        "onset_semantics": (
            "canonical uses sliding ONSET_BUCKET_SEC windows + count_swaps_between on uniswap_v3_swaps; "
            "inline uses trailing-count xs[len-thresh] semantics on uniswap_v3_swaps_enriched timestamps."
        ),
        "holdout_split": (
            "canonical split_chrono uses cut=max(1,int(n*(1-HOLDOUT_FRAC))); "
            "inline uses max(0,min(n,int(n*(1-frac))))."
        ),
        "swap_source_and_window": (
            "canonical: eatlq.uniswap_v3_swaps, DateTime64 half-open [dt_min, dt_max). "
            "inline: eatlq.uniswap_v3_swaps_enriched, closed [ts_min, ts_max], GET+Basic auth."
        ),
        "stats_returns_definition": (
            "Parity metrics use sprint037g.stats_returns for both canonical and inline row sets "
            "to isolate cohort differences from statistic definition."
        ),
        "pool_first_seen_source_tables": {
            "canonical_table": "eatlq.uniswap_v3_swaps",
            "inline_table": "eatlq.uniswap_v3_swaps_enriched",
            "pools_with_differing_first_seen_count": first_seen_mismatch,
            "pools_total": len(pools_all),
        },
        "young_holdout_jaccard_keys_pool_entry": jac,
    }

    parity_canonical_vs_archived = all(r["n_match_archived_vs_canonical"] for r in parity_rows) and parity_total_n
    inline_diverged = (len(young_can) != len(young_inl)) or (jac is not None and jac < 1.0)

    # Scorecard (canonical replay only)
    s38c_summary = json.loads(SPRINT038C_SUMMARY.read_text(encoding="utf-8"))
    pr = s38c_summary.get("proposed_route_rule") or {}
    tau_y = float(pr.get("tau_young_frac", 0.234724))
    tau_a = float(pr.get("tau_activity_heat_p50_q4_all", 95.0))
    margin = 0.028

    rows_attr = load_attr_rows()
    ah_by_reg = pivot_q4_all_activity_p50()
    pass_weeks = [r["regime_week"] for r in rows_attr if r["regime_bucket"] == "PASS_LIKE"]

    allowed_weeks_csv = scan_scorecard_allow_from_csv()
    allowed_weeks = {rw for rw, ok in allowed_weeks_csv.items() if ok}

    canon_uni = young_can
    canon_route = [r for r in young_can if r["regime_week"] in allowed_weeks]

    def pack_route(label: str, slice_rows: list[dict[str, Any]]) -> dict[str, Any]:
        st = g037.stats_returns([r["net_return_30m"] for r in slice_rows if r.get("net_return_30m") is not None])
        pc = pool_conc_g037(slice_rows)
        return {
            "scenario": label,
            "holdout_young_pool_n": len(slice_rows),
            "mean_30m": st.get("mean"),
            "median_30m": st.get("median"),
            "profit_factor_30m": st.get("profit_factor"),
            "label_1_share_30m": st.get("label_1_share"),
            **{f"pools_{k}": v for k, v in pc.items()},
        }

    sim_uni = pack_route("canonical_universal_all_weeks", canon_uni)
    sim_rt = pack_route("canonical_routed_scorecard_v1", canon_route)

    route_csv_rows: list[dict[str, Any]] = [sim_uni, sim_rt]
    for reg in regimes:
        sub = [r for r in canon_uni if r["regime_week"] == reg]
        st = g037.stats_returns([r["net_return_30m"] for r in sub if r.get("net_return_30m") is not None])
        tax = next(x["regime_bucket"] for x in rows_attr if x["regime_week"] == reg)
        route_csv_rows.append(
            {
                "regime_week": reg,
                "taxonomy": tax,
                "scorecard_allow": reg in allowed_weeks,
                "young_holdout_n": len(sub),
                "mean_30m": st.get("mean"),
                "median_30m": st.get("median"),
                "profit_factor_30m": st.get("profit_factor"),
                "label_1_share_30m": st.get("label_1_share"),
            }
        )

    fp_weeks: list[str] = []
    fn_weeks: list[str] = []
    for r in rows_attr:
        w = r["regime_week"]
        gold = r["regime_bucket"] == "PASS_LIKE"
        pred = w in allowed_weeks
        if pred and not gold:
            fp_weeks.append(w)
        if not pred and gold:
            fn_weeks.append(w)

    loocv_miss, loocv_detail = loocv_pass_weeks(pass_weeks, rows_attr, ah_by_reg, margin)
    loocv_fragile = bool(loocv_miss)

    if not parity_canonical_vs_archived:
        status = "BLOCKED_BY_SCHEMA_OR_DATA"
        scorecard_viable = False
    elif fp_weeks or fn_weeks:
        status = "ROUTE_SCORECARD_INVALIDATED"
        scorecard_viable = False
    elif loocv_fragile:
        status = "CANONICAL_PARITY_PASS_SCORECARD_STILL_WEAK"
        scorecard_viable = True
    elif inline_diverged:
        status = "INLINE_REPLAY_DIVERGED_DIAGNOSTIC_ONLY"
        scorecard_viable = True
    else:
        status = "CANONICAL_PARITY_PASS_ROUTE_REVIEW_REQUIRED"
        scorecard_viable = True

    summary = {
        "kind": "sprint038d_canonical_route_parity",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "parity_canonical_vs_archived_pass": parity_canonical_vs_archived,
        "parity_notes_per_regime": per_regime_diff_notes,
        "inline_replay_trustworthy_for_route_claims": not inline_diverged,
        "sprint038c_route_simulation_classification": (
            "diagnostic_only_unverified_inline_replay"
            if inline_diverged
            else "aligned_with_canonical_replay_unexpected"
        ),
        "scorecard_v1_post_canonical": {
            "tau_young_frac": tau_y,
            "tau_activity_heat_p50_q4_all": tau_a,
            "false_positive_weeks": fp_weeks,
            "false_negative_weeks": fn_weeks,
            "pass_like_vs_predict_allow_intact": not (fp_weeks or fn_weeks),
            "loocv_pass_weeks_missed": loocv_miss,
            "loocv_fragile": loocv_fragile,
            "loocv_detail": loocv_detail,
        },
        "aggregates": {
            "archived_g037_json": {
                "n": agg_arch.get("n"),
                "mean": agg_arch.get("mean"),
                "median": agg_arch.get("median"),
                "profit_factor": agg_arch.get("profit_factor"),
                "label_1_share": agg_arch.get("label_1_share"),
            },
            "canonical_restored_replay": {
                "n": st_can_all.get("n"),
                "mean": st_can_all.get("mean"),
                "median": st_can_all.get("median"),
                "profit_factor": st_can_all.get("profit_factor"),
                "label_1_share": st_can_all.get("label_1_share"),
            },
            "inline_038c_style_replay": {
                "n": st_inl_all.get("n"),
                "mean": st_inl_all.get("mean"),
                "median": st_inl_all.get("median"),
                "profit_factor": st_inl_all.get("profit_factor"),
                "label_1_share": st_inl_all.get("label_1_share"),
            },
        },
        "canonical_route_simulation": {"universal": sim_uni, "routed": sim_rt},
        "scorecard_viability_notes": {
            "frozen_rule_no_fp_fn_vs_pass_like_taxonomy": not (fp_weeks or fn_weeks),
            "pass_only_loocv_fragile": loocv_fragile,
            "hypothesis": "scorecard_v1 remains an indicative route gate — human review only; NO_ALPHA_CLAIM.",
        },
        "difference_attribution": diff_attribution,
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

    if parity_canonical_vs_archived and scorecard_viable is False and status == "BLOCKED_BY_SCHEMA_OR_DATA":
        summary["status_notes"] = "Archived audit CSV mismatch vs live canonical replay — dataset/schema drift or environment."

    (OUT / "canonical_replay_parity_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    with (OUT / "canonical_vs_inline_parity.csv").open("w", newline="", encoding="utf-8") as f:
        keys = list(parity_rows[0].keys()) if parity_rows else []
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(parity_rows)

    with (OUT / "route_scorecard_canonical_readout.csv").open("w", newline="", encoding="utf-8") as f:
        keys = sorted({k for row in route_csv_rows for k in row.keys()})
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in route_csv_rows:
            w.writerow(row)

    sup = supplement_fee_weth_q4all()
    if sup:
        sk = list(sup[0].keys())
        with (OUT / "regime_family_features_fee_weth_supplement.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sk)
            w.writeheader()
            w.writerows(sup)

    md_path = RUNS / "run-038D-canonical-route-parity-readout.md"
    md_path.write_text(
        "\n".join(
            [
                "# Run-038D — canonical replay parity + route scorecard hardening",
                "",
                f"Generated: {summary['created_at']}",
                "",
                f"**status:** `{status}`",
                "",
                f"- **parity_canonical_vs_archived_pass:** `{parity_canonical_vs_archived}`",
                f"- **inline diverged from canonical:** `{inline_diverged}` (Jaccard≈{jac})",
                f"- **scorecard FP/FN vs PASS_LIKE:** fp={fp_weeks} fn={fn_weeks}",
                f"- **LOOCV fragile:** `{loocv_fragile}` missed={loocv_miss}",
                "",
                "## Canonical routed vs universal (30m, sprint037g.stats_returns)",
                "",
                f"- Universal n={sim_uni.get('holdout_young_pool_n')} mean={sim_uni.get('mean_30m')} PF={sim_uni.get('profit_factor_30m')}",
                f"- Routed n={sim_rt.get('holdout_young_pool_n')} mean={sim_rt.get('mean_30m')} PF={sim_rt.get('profit_factor_30m')}",
                "",
                "`NO_ALPHA_CLAIM=true` · `supersession_candidate=false`",
                "",
                "---",
                "",
                "*Generated by scripts/sprint038d_canonical_route_parity.py*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _log(f"done status={status} parity_arch={parity_canonical_vs_archived} inline_div={inline_diverged}")


if __name__ == "__main__":
    main()
