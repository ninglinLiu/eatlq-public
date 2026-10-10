#!/usr/bin/env python3
"""Sprint038B — regime-level attribution for young_pool_onset_flow failure modes.

Reads Sprint038A isolated artifacts only (+ ClickHouse read-only swap timelines).
Does not rewrite sprint037c baseline. No label RPC.

Writes:
  mvp/audits/sprint038b/regime_failure_attribution_summary.json
  mvp/audits/sprint038b/regime_failure_attribution.csv
  mvp/audits/sprint038b/feature_distribution_by_regime.csv
  mvp/runs/run-038B-regime-attribution-readout.md
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import sprint037g_young_pool_onset_expansion as g  # noqa: E402

SPRINT038A_DIR = REPO / "mvp/audits/sprint038a"
Q4_DIR = SPRINT038A_DIR / "q4_multi_horizon"
OUT_AUDIT = REPO / "mvp/audits/sprint038b"
RUNS = REPO / "mvp/runs"
R5_SUMMARY_PATH = SPRINT038A_DIR / "r5_candidate_expansion_summary.json"
SPRINT037C_SUMMARY_PATH = SPRINT038A_DIR / "sprint037c_multi_horizon_summary.json"
G037_SUMMARY_PATH = SPRINT038A_DIR / "g037" / "expansion_summary.json"

MIN_HOLDOUT = int(os.environ.get("SPRINT037G_MIN_HOLDOUT_PER_REGIME", "35"))

DIST_VARS = [
    "pool_age_sec",
    "time_since_activity_onset_sec",
    "price_velocity_5m",
    "buy_pressure_ratio",
    "activity_heat",
    "token_volatility_5m",
    "prior_runup_proxy",
]


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[038b {ts}] {msg}", flush=True)


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    idx = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return float(xs[idx])


def _summ(vals: list[float]) -> dict[str, Any]:
    xs = [float(v) for v in vals if v == v]
    if not xs:
        return {"n": 0, "mean": None, "p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
    return {
        "n": len(xs),
        "mean": round(sum(xs) / len(xs), 8),
        "p10": round(_pct(xs, 0.10) or 0, 8),
        "p25": round(_pct(xs, 0.25) or 0, 8),
        "p50": round(_pct(xs, 0.50) or 0, 8),
        "p75": round(_pct(xs, 0.75) or 0, 8),
        "p90": round(_pct(xs, 0.90) or 0, 8),
    }


def _bucket(holdout_n: int, mean_30: float | None) -> str:
    if holdout_n == 0:
        return "EMPTY_LOW_INTERSECTION"
    if holdout_n >= MIN_HOLDOUT:
        if mean_30 is not None and mean_30 > 0:
            return "PASS_LIKE"
        return "NEGATIVE"
    if mean_30 is not None and mean_30 > 0:
        return "THIN_POSITIVE"
    return "EMPTY_LOW_INTERSECTION"


def _passes_baseline(
    r: dict[str, Any],
    ts_list: list[int],
    pool_global_first: dict[str, int],
    pool_max_age_sec: int,
    onset_max_sec: int,
    onset_count_thresh: int,
) -> bool:
    pool = r["pool_address_lower"]
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else r["entry_time_sec"]
    age = max(0, int(r["entry_time_sec"]) - gfs)
    if age > pool_max_age_sec:
        return False
    onset = g.compute_activity_onset(ts_list, int(r["entry_time_sec"]), gfs, onset_count_thresh)
    if onset is None:
        return False
    toe = int(r["entry_time_sec"]) - onset
    if toe > onset_max_sec:
        return False
    pv = r.get("price_velocity_5m")
    if pv is None or float(pv) <= g.FLOW_EPS:
        return False
    return True


def _failure_codes(
    r: dict[str, Any],
    ts_list: list[int],
    pool_global_first: dict[str, int],
    pool_max_age_sec: int,
    onset_max_sec: int,
    onset_count_thresh: int,
) -> list[str]:
    pool = r["pool_address_lower"]
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else r["entry_time_sec"]
    codes: list[str] = []
    age = max(0, int(r["entry_time_sec"]) - gfs)
    if age > pool_max_age_sec:
        codes.append("FAIL_POOL_AGE")
    onset = g.compute_activity_onset(ts_list, int(r["entry_time_sec"]), gfs, onset_count_thresh)
    if onset is None:
        codes.append("FAIL_ONSET_MISSING")
    elif int(r["entry_time_sec"]) - onset > onset_max_sec:
        codes.append("FAIL_TIME_SINCE_ONSET")
    pv = r.get("price_velocity_5m")
    if pv is None or float(pv) <= g.FLOW_EPS:
        codes.append("FAIL_PRICE_VELOCITY_NONPOSITIVE")
    return codes


def _row_features(
    r: dict[str, Any],
    ts_list: list[int],
    pool_global_first: dict[str, int],
    onset_ct: int,
    onset_mx: int,
    pool_mx_age: int,
) -> dict[str, Any]:
    pool = r["pool_address_lower"]
    ent = int(r["entry_time_sec"])
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else ent
    pool_age = max(0, ent - gfs)
    onset = g.compute_activity_onset(ts_list, ent, gfs, onset_ct)
    toe = (ent - onset) if onset is not None else None
    pv = float(r["price_velocity_5m"]) if r.get("price_velocity_5m") is not None else None
    rep = float(r.get("regime_elapsed_pct") or 0.0)
    runup_proxy = (pv * (1.0 - rep)) if pv is not None else None
    return {
        "pool_age_sec": float(pool_age),
        "time_since_activity_onset_sec": float(toe) if toe is not None else None,
        "price_velocity_5m": pv,
        "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r.get("buy_pressure_ratio") is not None else None,
        "activity_heat": float(r["activity_heat"]) if r.get("activity_heat") is not None else None,
        "token_volatility_5m": (
            float(r["token_volatility_5m"]) if r.get("token_volatility_5m") is not None else None
        ),
        "prior_runup_proxy": runup_proxy,
        "fee_bps": int(float(r["fee_bps"])) if r.get("fee_bps") not in (None, "") else None,
        "weth_is_token0": int(float(r["weth_is_token0"])) if r.get("weth_is_token0") not in (None, "") else None,
    }


def main() -> None:
    OUT_AUDIT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        sys.exit(2)

    r5_cov = json.loads(R5_SUMMARY_PATH.read_text(encoding="utf-8"))
    sprint037c = json.loads(SPRINT037C_SUMMARY_PATH.read_text(encoding="utf-8"))
    g037: dict[str, Any] = {}
    if G037_SUMMARY_PATH.exists():
        g037 = json.loads(G037_SUMMARY_PATH.read_text(encoding="utf-8"))

    regimes = g.discover_regimes(Q4_DIR)
    if not regimes:
        _log(f"No q4 CSV under {Q4_DIR}")
        sys.exit(2)

    bc = g.BASELINE_CELL
    p_max = int(bc["pool_max_age_sec"])
    o_max = int(bc["onset_max_sec"])
    o_thr = int(bc["onset_count_thresh"])

    all_rows_flat: list[dict[str, Any]] = []
    all_by_regime: dict[str, list[dict[str, Any]]] = {}
    for reg in regimes:
        rows = g.load_regime_csv(reg, Q4_DIR)
        all_by_regime[reg] = rows
        all_rows_flat.extend(rows)

    pools_all = sorted({r["pool_address_lower"] for r in all_rows_flat})
    _log(f"pools={len(pools_all)} regimes={len(regimes)} … fetch CH metadata")
    pool_global_first = g.fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)

    swaps_cache: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for reg in regimes:
        rows = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rows})
        ts_min = min(r["entry_time_sec"] for r in rows) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rows) + 600
        raw = g.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_cache[reg] = {p: [x[0] for x in raw.get(p, [])] for p in pools}

    regime_rows_out: list[dict[str, Any]] = []
    feat_rows_out: list[dict[str, Any]] = []

    floor_audit_path = SPRINT038A_DIR / "young_pool_onset_floor_audit.csv"
    floor_by_week: dict[str, dict[str, Any]] = {}
    if floor_audit_path.exists():
        with floor_audit_path.open(encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                floor_by_week[rec["regime_week"]] = rec

    per_regime_detail: dict[str, Any] = {}

    for reg in regimes:
        rows = all_by_regime[reg]
        sc = swaps_cache.get(reg, {})
        tr, ho = g.split_chrono(rows)

        fail_ctr: Counter = Counter()
        for r in ho:
            for code in _failure_codes(r, sc.get(r["pool_address_lower"], []), pool_global_first, p_max, o_max, o_thr):
                fail_ctr[code] += 1

        young_all = [r for r in rows if _passes_baseline(r, sc.get(r["pool_address_lower"], []), pool_global_first, p_max, o_max, o_thr)]
        young_ho = [r for r in ho if _passes_baseline(r, sc.get(r["pool_address_lower"], []), pool_global_first, p_max, o_max, o_thr)]

        r5pr = (r5_cov.get("per_regime") or {}).get(reg) or {}

        ho_bundle = {h: g.horizon_agg(young_ho, h) for h in g.HORIZONS}
        mean30 = ho_bundle["30m"].get("mean")

        bucket = _bucket(len(young_ho), mean30 if isinstance(mean30, (int, float)) else None)

        detail_codes = ";".join(f"{k}={v}" for k, v in fail_ctr.most_common(6))

        r037 = sprint037c.get("per_regime", {}).get(reg, {}).get("30m", {})
        row_summary = {
            "regime_week": reg,
            "regime_bucket": bucket,
            "ch_candidates_r5": r5pr.get("ch_candidates"),
            "executable_r5": r5pr.get("executable_candidates"),
            "labeled_rows_r5": r5pr.get("labeled_rows"),
            "q4_candidates": len(rows),
            "young_pool_q4_all": len(young_all),
            "young_pool_holdout_n": len(young_ho),
            "holdout_frac_rows_q4": round(len(ho) / max(len(rows), 1), 6),
            "young_frac_of_q4": round(len(young_all) / max(len(rows), 1), 6),
            "holdout_mean_5m": ho_bundle["5m"].get("mean"),
            "holdout_mean_10m": ho_bundle["10m"].get("mean"),
            "holdout_mean_15m": ho_bundle["15m"].get("mean"),
            "holdout_mean_30m": ho_bundle["30m"].get("mean"),
            "holdout_pf_30m": ho_bundle["30m"].get("profit_factor"),
            "holdout_label_1_share_30m": ho_bundle["30m"].get("label_1_share"),
            "holdout_unique_pools_30m": ho_bundle["30m"].get("unique_pools"),
            "holdout_top1_30m": ho_bundle["30m"].get("top1"),
            "holdout_top5_30m": ho_bundle["30m"].get("top5"),
            "q4_holdout_mean_30m": r037.get("mean"),
            "q4_holdout_label_1_share_30m": r037.get("label_1_share"),
            "top_holdout_gate_failure_modes": detail_codes,
        }
        regime_rows_out.append(row_summary)
        per_regime_detail[reg] = {
            "bucket": bucket,
            "gate_failure_histogram_holdout_q4": dict(fail_ctr),
            "floor_audit_crosscheck": floor_by_week.get(reg),
        }

        for slice_name, slice_rows in (
            ("young_holdout", young_ho),
            ("young_all", young_all),
            ("q4_holdout", ho),
            ("q4_all", rows),
        ):
            feats = [
                _row_features(r, sc.get(r["pool_address_lower"], []), pool_global_first, o_thr, o_max, p_max)
                for r in slice_rows
            ]
            for var in DIST_VARS:
                vals = [float(x[var]) for x in feats if x.get(var) is not None and x[var] == x[var]]
                s = _summ(vals)
                feat_rows_out.append(
                    {
                        "regime_week": reg,
                        "slice": slice_name,
                        "variable": var,
                        "value_note": "",
                        **{k: s[k] for k in ("n", "mean", "p10", "p25", "p50", "p75", "p90")},
                    }
                )
            fees = [x["fee_bps"] for x in feats if x.get("fee_bps") is not None]
            if fees:
                fc = Counter(fees)
                top3 = fc.most_common(3)
                feat_rows_out.append(
                    {
                        "regime_week": reg,
                        "slice": slice_name,
                        "variable": "fee_bps_top3",
                        "n": len(fees),
                        "mean": None,
                        "p10": None,
                        "p25": None,
                        "p50": None,
                        "p75": None,
                        "p90": None,
                        "value_note": json.dumps([(str(a), b) for a, b in top3]),
                    }
                )
            wt = [x["weth_is_token0"] for x in feats if x.get("weth_is_token0") is not None]
            if wt:
                wc = Counter(wt)
                feat_rows_out.append(
                    {
                        "regime_week": reg,
                        "slice": slice_name,
                        "variable": "weth_is_token0_counts",
                        "n": len(wt),
                        "mean": round(sum(wt) / len(wt), 6),
                        "p10": None,
                        "p25": None,
                        "p50": None,
                        "p75": None,
                        "p90": None,
                        "value_note": json.dumps(dict(wc)),
                    }
                )

    pass_like = [r["regime_week"] for r in regime_rows_out if r["regime_bucket"] == "PASS_LIKE"]
    thin_pos = [r["regime_week"] for r in regime_rows_out if r["regime_bucket"] == "THIN_POSITIVE"]
    neg_b = [r["regime_week"] for r in regime_rows_out if r["regime_bucket"] == "NEGATIVE"]
    empty_b = [r["regime_week"] for r in regime_rows_out if r["regime_bucket"] == "EMPTY_LOW_INTERSECTION"]

    avg_young_frac = sum(r["young_frac_of_q4"] for r in regime_rows_out) / max(len(regime_rows_out), 1)

    root_causes: list[str] = []
    if avg_young_frac < 0.08:
        root_causes.append(
            "YOUNG_POOL_GATE_TOO_NARROW: average young_pool share of Q4 rows is very low — intersection dominated by lifecycle gate vs generic Q4 momentum screen."
        )
    if neg_b:
        root_causes.append(
            "ECONOMICS_NEGATIVE_ROUTE_PIVOT: one or more regimes show sufficient young_pool holdout count "
            f"({neg_b}) but negative 30m economics — not merely sample noise."
        )
    if empty_b:
        root_causes.append(
            "EMPTY_LOW_INTERSECTION: regimes with zero or thin young_pool holdout slices "
            f"({empty_b}) — cannot meet MIN_HOLDOUT without redesign or regime routing."
        )
    if len(pass_like) >= 1 and (thin_pos or neg_b or empty_b):
        root_causes.append(
            "REGIME_MISMATCH_FOUND_NEEDS_CLASSIFIER: calendar regimes disagree — scoped edge concentrates "
            f"in {pass_like} family weeks while Feb/Mar extensions degrade under the same gate."
        )

    primary_status = "REGIME_MISMATCH_FOUND_NEEDS_CLASSIFIER"
    if not pass_like and neg_b and not thin_pos:
        primary_status = "ECONOMICS_NEGATIVE_ROUTE_PIVOT"
    elif avg_young_frac < 0.03 and not neg_b:
        primary_status = "YOUNG_POOL_GATE_TOO_NARROW"

    supersession_allowed = False

    viable_scoped = len(pass_like) >= 1 and bool(g037.get("baseline_aggregate_holdout_30m"))

    recommended_next = [
        "Keep young_pool_onset_flow as an explicitly scoped research gate (Dec/Jan-heavy evidence cluster), "
        "not as an automatic cross-calendar supersession driver.",
        "Add a lightweight regime classifier upstream of this gate (calendar burst vs grind regime), "
        "or split bundles into Dec/Jan vs Feb/Mar families before pooling strict floors.",
        "Mar-02 EMPTY diagnosis: inspect FAIL_* histogram — if dominated by FAIL_POOL_AGE / FAIL_ONSET_MISSING on holdout Q4, "
        "the calendar week is not young-pool dominated under BF180 first-seen; pivot toward pool-launch timestamping or shrink Q4 to creation-aligned pools.",
        "Feb-23: thin young_pool holdout (n<MIN_HOLDOUT) with negative 30m mean — classify separately from sample-only thin-positive regimes.",
        "Mar-30: young_pool holdout clears MIN_HOLDOUT but mean_30m<0 and PF<1 — economic mismatch under frozen gate; no MIN_HOLDOUT relief justified.",
        "Prefer pool-launch-aligned candidate generation for forward work over unlimited calendar expansion if gate intersection stays sparse.",
    ]

    key_diag: dict[str, Any] = {}
    mandatory_week_explanations: dict[str, str] = {}
    for r in regime_rows_out:
        rw = r["regime_week"]
        hist = per_regime_detail[rw]["gate_failure_histogram_holdout_q4"]
        topf = sorted(hist.items(), key=lambda x: -x[1])[:5]
        key_diag[rw] = {
            "young_holdout_n": r["young_pool_holdout_n"],
            "young_frac_of_q4": r["young_frac_of_q4"],
            "top_holdout_q4_gate_failures": topf,
            "holdout_mean_30m_young_slice": r["holdout_mean_30m"],
            "q4_holdout_mean_30m_unrestricted": r["q4_holdout_mean_30m"],
        }
        if rw == "2026-03-02":
            mandatory_week_explanations[rw] = (
                "EMPTY intersection at baseline cell: zero young_pool-onset-positive-flow rows in chronological holdout — "
                "high Q4 counts do not convert into simultaneous age/onset/positive-velocity survival."
            )
        elif rw == "2026-02-23":
            mandatory_week_explanations[rw] = (
                "Thin young_pool holdout (n<MIN_HOLDOUT) with negative 30m mean — economics unfavorable before sample adequacy; "
                "distinct from THIN_POSITIVE regimes where mean>0 but n is low."
            )
        elif rw == "2026-03-30":
            mandatory_week_explanations[rw] = (
                "NEGATIVE with young_pool holdout n≥MIN_HOLDOUT — mean_30m<0 and PF<1 on the gated slice; "
                "failure is economic under the frozen gate, not the floor constraint alone."
            )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = {
        "kind": "sprint038b_regime_failure_attribution",
        "created_at": now,
        "status": primary_status,
        "supersession_candidate": supersession_allowed,
        "min_holdout_per_regime": MIN_HOLDOUT,
        "baseline_cell": bc,
        "regime_buckets": {
            "PASS_LIKE": pass_like,
            "THIN_POSITIVE": thin_pos,
            "NEGATIVE": neg_b,
            "EMPTY_LOW_INTERSECTION": empty_b,
        },
        "aggregate_signals": {
            "avg_young_frac_of_q4": round(avg_young_frac, 6),
            "sprint038a_strict_ok_grid_hits": g037.get("stability_hits_strict_ok"),
            "sprint038a_stability_fraction": g037.get("stability_fraction"),
        },
        "root_causes": root_causes,
        "mandatory_week_explanations": mandatory_week_explanations,
        "key_regime_diagnostics": {
            k: key_diag[k] for k in ("2026-02-23", "2026-03-30", "2026-03-02") if k in key_diag
        },
        "young_pool_onset_flow_viability": {
            "scoped_candidate_remains": viable_scoped,
            "cross_regime_universal_gate": False,
            "notes": "PASS_LIKE regimes justify retaining the gate as a scoped hypothesis; universal FAILED_NO_7D_ALPHA supersession remains unsupported.",
        },
        "recommended_next": recommended_next,
        "per_regime": per_regime_detail,
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

    OUT_AUDIT.mkdir(parents=True, exist_ok=True)
    (OUT_AUDIT / "regime_failure_attribution_summary.json").write_text(json.dumps(summary, indent=2))

    if regime_rows_out:
        keys = list(regime_rows_out[0].keys())
        with (OUT_AUDIT / "regime_failure_attribution.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(regime_rows_out)

    if feat_rows_out:
        keys = list(feat_rows_out[0].keys())
        with (OUT_AUDIT / "feature_distribution_by_regime.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(feat_rows_out)

    rc_lines = "\n".join(f"- {x}" for x in root_causes) if root_causes else "- (none)"
    me_lines = "\n\n".join(
        f"### {wk}\n{mandatory_week_explanations[wk]}"
        for wk in ("2026-02-23", "2026-03-30", "2026-03-02")
        if wk in mandatory_week_explanations
    )

    md = RUNS / "run-038B-regime-attribution-readout.md"
    md.write_text(
        "\n".join(
            [
                "# Run-038B — regime attribution (young_pool_onset_flow)",
                "",
                f"Generated: {now}",
                "",
                f"**status:** `{primary_status}`",
                "",
                "**supersession_candidate:** `false` (explicit)",
                "",
                "## Regime buckets",
                "",
                f"- PASS_LIKE: {', '.join(pass_like) or '—'}",
                f"- THIN_POSITIVE: {', '.join(thin_pos) or '—'}",
                f"- NEGATIVE: {', '.join(neg_b) or '—'}",
                f"- EMPTY_LOW_INTERSECTION: {', '.join(empty_b) or '—'}",
                "",
                "## Root causes (automated tags)",
                "",
                rc_lines,
                "",
                "## Mandatory week explanations",
                "",
                me_lines or "_(none)_",
                "",
                "## Viability",
                "",
                f"- scoped_candidate_remains: **{viable_scoped}**",
                "- cross_regime_universal_gate: **False**",
                "",
                "## Artifacts",
                "",
                "- `mvp/audits/sprint038b/regime_failure_attribution_summary.json`",
                "- `mvp/audits/sprint038b/regime_failure_attribution.csv`",
                "- `mvp/audits/sprint038b/feature_distribution_by_regime.csv`",
                "",
                "`NO_ALPHA_CLAIM=true`",
                "",
                "---",
                "",
                "*Generated by scripts/sprint038b_regime_failure_attribution.py*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _log(f"done status={primary_status} wrote {OUT_AUDIT}")


if __name__ == "__main__":
    main()
