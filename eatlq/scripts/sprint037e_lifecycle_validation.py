#!/usr/bin/env python3
"""Sprint037E — Regime lifecycle validation on Sprint037C Q4 executable CSVs.

Reads labeled rows only (no RPC). Predeclared gates — no holdout tuning.

Outputs:
  mvp/audits/sprint037e/lifecycle_validation_summary.json
  mvp/audits/sprint037e/lifecycle_bucket_readout.csv
  mvp/audits/sprint037e/early_phase_holdout_readout.csv
  mvp/runs/run-037E-lifecycle-holdout-readout.md
"""

from __future__ import annotations

import csv
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

REPO = pathlib.Path(__file__).resolve().parents[1]
Q4_DIR = REPO / "mvp/audits/sprint037c"
OUT_DIR = REPO / "mvp/audits/sprint037e"
RUNS_DIR = REPO / "mvp/runs"
SPRINT037D_SUMMARY = REPO / "mvp/diagnostics/sprint037d_summary.json"

REGIMES = ["2026-02-02", "2026-01-26", "2026-01-05", "2025-12-01"]
HORIZONS = ["5m", "10m", "15m", "30m"]
HOLDOUT_FRAC = 0.30
REGIME_SEC = 7 * 86400

# Predeclared constants (not tuned on holdout)
ACTIVITY_ONSET_THRESH = 55  # first crossing of activity_heat in chronological regime rows
PRIOR_RUNUP_WINDOW_SEC = 3600
# Prior-row proxy only uses exits observable before entry (avoid lookahead).
LABEL_KNOWN_LAG_SEC = 1800  # frozen 30m label horizon
PRIOR_RUNUP_LOW_HIGH = 0.06  # absolute net_return_30m bins for prior candidate
FLOW_POS_EPS = 1e-9
TOKEN_VOL_HIGH_FIXED = 1.55  # early-phase + high TV gate


def _utc_week_start_ts(iso_week: str) -> float:
    dt = datetime.strptime(iso_week, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _log(msg: str) -> None:
    print(f"[e {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_row(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            if r.get("entry_time_sec"):
                r["entry_time_sec"] = int(float(r["entry_time_sec"]))
            for k in ("activity_heat", "entry_block", "fee_bps"):
                if r.get(k) not in (None, ""):
                    try:
                        r[k] = int(float(r[k]))
                    except (ValueError, TypeError):
                        pass
            for k in (
                "price_velocity_5m",
                "buy_pressure_ratio",
                "token_volatility_5m",
            ):
                if r.get(k) not in (None, ""):
                    try:
                        r[k] = float(r[k])
                    except (ValueError, TypeError):
                        r[k] = None
            for h in HORIZONS:
                nr = r.get(f"net_return_{h}")
                r[f"net_return_{h}"] = (
                    float(nr) if nr not in (None, "", "None") else None
                )
            rows.append(r)
    return rows


def enrich_lifecycle(all_by_regime: dict[str, list[dict]]) -> None:
    """Mutates rows: regime_elapsed_pct, activity_onset_ts, onset_elapsed_sec,
    prior_pool_ret_30m_1h, prior_runup_bucket, fixed_phase_bucket."""
    for reg, rows in all_by_regime.items():
        t0 = _utc_week_start_ts(reg)
        rows.sort(key=lambda x: x["entry_time_sec"])

        onset_ts: float | None = None
        for r in rows:
            r["regime_week_start_ts"] = t0
            elapsed = r["entry_time_sec"] - t0
            r["regime_elapsed_pct"] = max(0.0, min(1.0, elapsed / REGIME_SEC))

            ah = int(r.get("activity_heat") or 0)
            if onset_ts is None and ah >= ACTIVITY_ONSET_THRESH:
                onset_ts = float(r["entry_time_sec"])
            r["activity_onset_ts"] = onset_ts
            if onset_ts is not None:
                r["onset_elapsed_sec"] = float(r["entry_time_sec"] - onset_ts)
            else:
                r["onset_elapsed_sec"] = float(elapsed)

            # Fixed-window bucket label
            p = r["regime_elapsed_pct"]
            if p <= 0.20:
                r["fixed_phase_bucket"] = "0-20%"
            elif p <= 0.40:
                r["fixed_phase_bucket"] = "20-40%"
            elif p <= 0.60:
                r["fixed_phase_bucket"] = "40-60%"
            elif p <= 0.80:
                r["fixed_phase_bucket"] = "60-80%"
            else:
                r["fixed_phase_bucket"] = "80-100%"

        # Same-pool prior candidate within 1h (backward-looking only)
        pool_last: dict[str, tuple[float, float | None]] = {}
        for r in rows:
            pool = str(r.get("pool_address") or "").lower()
            ts = float(r["entry_time_sec"])
            prev_ret = None
            if pool in pool_last:
                prev_ts, prev_nr = pool_last[pool]
                if (
                    prev_ts < ts
                    and ts - prev_ts <= PRIOR_RUNUP_WINDOW_SEC
                    and prev_ts + LABEL_KNOWN_LAG_SEC <= ts
                ):
                    prev_ret = prev_nr
            nr30 = r.get("net_return_30m")
            pool_last[pool] = (ts, nr30)

            r["prior_pool_ret_30m_1h"] = prev_ret
            pr = prev_ret
            if pr is None:
                r["prior_runup_bucket"] = "none_cold"
            elif pr < 0:
                r["prior_runup_bucket"] = "low_neg"
            elif pr < PRIOR_RUNUP_LOW_HIGH:
                r["prior_runup_bucket"] = "mid_flat"
            else:
                r["prior_runup_bucket"] = "high_pos"

        streak = 0
        for r in rows:
            ah = int(r.get("activity_heat") or 0)
            if ah >= ACTIVITY_ONSET_THRESH:
                streak += 1
            else:
                streak = 0
            r["consecutive_high_activity"] = streak


def split_chrono(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda x: x["entry_time_sec"])
    n = len(rows)
    split_at = max(1, int(n * (1 - HOLDOUT_FRAC)))
    return rows[:split_at], rows[split_at:]


def stats_returns(rets: list[float]) -> dict[str, Any]:
    rs = [x for x in rets if x == x]
    if not rs:
        return {
            "n": 0,
            "label_1_share": None,
            "mean": None,
            "median": None,
            "trimmed_mean": None,
            "profit_factor": None,
            "p10": None,
            "p90": None,
        }
    rs.sort()
    n = len(rs)
    k = max(1, n // 10)
    tr = rs[k:-k] if n > 2 * k else rs
    pos = [x for x in rs if x > 0]
    neg = [x for x in rs if x < 0]
    gp, gl = sum(pos), abs(sum(neg))
    pf = (gp / gl) if gl > 1e-12 else None
    mean_v = sum(rs) / n
    return {
        "n": n,
        "label_1_share": round(len(pos) / n, 6),
        "mean": round(mean_v, 6),
        "median": round(rs[n // 2], 6),
        "trimmed_mean": round(sum(tr) / max(len(tr), 1), 6),
        "profit_factor": round(pf, 6) if pf is not None else None,
        "p10": round(rs[max(0, n // 10)], 6),
        "p90": round(rs[min(n - 1, 9 * n // 10)], 6),
    }


def pool_conc(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"unique_pools": 0, "top1": None, "top5": None}
    ctr = Counter(str(r.get("pool_address") or "").lower() for r in rows)
    n = len(rows)
    mv = ctr.most_common()
    top1 = mv[0][1] / n if mv else 0
    top5 = sum(c for _, c in mv[:5]) / n if mv else 0
    return {"unique_pools": len(ctr), "top1": round(top1, 6), "top5": round(top5, 6)}


def horizon_stats(rows: list[dict], h: str) -> dict[str, Any]:
    key = f"net_return_{h}"
    rets = [r[key] for r in rows if r.get(key) is not None]
    return stats_returns(rets)


def evaluate_gate_on_split(
    rows: list[dict], predicate: Callable[[dict], bool]
) -> list[dict]:
    return [r for r in rows if predicate(r)]


GATES: dict[str, Callable[[dict], bool]] = {
    "baseline_q4_all": lambda r: True,
    "fixed_phase_le_20pct": lambda r: r["regime_elapsed_pct"] <= 0.20,
    "fixed_phase_le_40pct": lambda r: r["regime_elapsed_pct"] <= 0.40,
    "fixed_phase_le_60pct": lambda r: r["regime_elapsed_pct"] <= 0.60,
    "activity_onset_le_6h": lambda r: r["onset_elapsed_sec"] <= 6 * 3600,
    "activity_onset_le_24h": lambda r: r["onset_elapsed_sec"] <= 24 * 3600,
    "prior_runup_low": lambda r: r["prior_runup_bucket"] in ("none_cold", "low_neg"),
    "prior_runup_mid": lambda r: r["prior_runup_bucket"] == "mid_flat",
    "prior_runup_high": lambda r: r["prior_runup_bucket"] == "high_pos",
    "early40_flow_positive": lambda r: r["regime_elapsed_pct"] <= 0.40
    and float(r.get("price_velocity_5m") or 0) > FLOW_POS_EPS,
    "early40_token_vol_high": lambda r: r["regime_elapsed_pct"] <= 0.40
    and (r.get("token_volatility_5m") or 0) >= TOKEN_VOL_HIGH_FIXED,
    "early40_activity_heat_q4": lambda r: r["regime_elapsed_pct"] <= 0.40
    and int(r.get("activity_heat") or 0) >= int(r.get("_regime_ah_q4_cut") or 1 << 30),
    "early20_flow_positive": lambda r: r["regime_elapsed_pct"] <= 0.20
    and float(r.get("price_velocity_5m") or 0) > FLOW_POS_EPS,
    "early40_not_exhausted_streak": lambda r: r["regime_elapsed_pct"] <= 0.40
    and int(r.get("consecutive_high_activity") or 0) <= 12,
}

GATE_ORDER = list(GATES.keys())
def attach_regime_ah_q4_cut(all_by_regime: dict[str, list[dict]]) -> None:
    """Per-regime Q4 cutoff on activity_heat (same rule family as Sprint037C; uses full Q4 file)."""
    for reg, rows in all_by_regime.items():
        vals = sorted(int(r.get("activity_heat") or 0) for r in rows)
        q3 = vals[max(0, 3 * len(vals) // 4 - 1)] if vals else 0
        for r in rows:
            r["_regime_ah_q4_cut"] = q3


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    all_by_regime: dict[str, list[dict]] = {}
    for reg in REGIMES:
        p = Q4_DIR / f"q4_candidates_{reg}.csv"
        all_by_regime[reg] = load_row(p) if p.exists() else []

    attach_regime_ah_q4_cut(all_by_regime)
    enrich_lifecycle(all_by_regime)

    # Verify baseline vs Sprint037D
    baseline_ok = False
    ref_mean = ref_n = None
    if SPRINT037D_SUMMARY.exists():
        sd = json.loads(SPRINT037D_SUMMARY.read_text())
        ref = sd.get("aggregate_holdout", {}).get("30m", {})
        ref_mean = ref.get("mean")
        ref_n = ref.get("n")

    hold_rows: list[dict] = []
    for reg in REGIMES:
        _, ho = split_chrono(all_by_regime[reg])
        hold_rows.extend(ho)
    h30 = horizon_stats(hold_rows, "30m")
    if ref_mean is not None and h30["mean"] is not None:
        baseline_ok = abs(h30["mean"] - ref_mean) < 1e-4 and h30["n"] == ref_n

    _log(f"baseline_q4 holdout 30m mean={h30['mean']} n={h30['n']} repro037d={baseline_ok}")

    # Lifecycle bucket readout (fixed_phase_bucket, full Q4, insample+holdout pooled per regime)
    bucket_rows_out: list[dict[str, Any]] = []
    for reg in REGIMES:
        rows = all_by_regime[reg]
        for bucket in ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]:
            sub = [r for r in rows if r.get("fixed_phase_bucket") == bucket]
            row_base = {"regime_week": reg, "lifecycle_bucket": bucket, "n": len(sub)}
            for h in HORIZONS:
                st = horizon_stats(sub, h)
                pc = pool_conc(sub)
                row_base.update(
                    {
                        f"{h}_label_1_share": st["label_1_share"],
                        f"{h}_mean": st["mean"],
                        f"{h}_median": st["median"],
                        f"{h}_trimmed_mean": st["trimmed_mean"],
                        f"{h}_pf": st["profit_factor"],
                        f"{h}_p10": st["p10"],
                        f"{h}_p90": st["p90"],
                    }
                )
            row_base["unique_pools"] = pool_conc(sub)["unique_pools"]
            row_base["pool_top1"] = pool_conc(sub)["top1"]
            row_base["pool_top5"] = pool_conc(sub)["top5"]
            bucket_rows_out.append(row_base)

    early_hold_rows: list[dict[str, Any]] = []

    gate_results: dict[str, Any] = {}

    for gate_name in GATE_ORDER:
        pred = GATES[gate_name]
        per_reg_ho: dict[str, dict[str, Any]] = {}
        agg_hold: dict[str, dict[str, Any]] = {}
        ins_hold_comparison: dict[str, dict[str, Any]] = {}

        ho_all: list[dict] = []
        is_all: list[dict] = []
        for reg in REGIMES:
            ins, ho = split_chrono(all_by_regime[reg])
            gi = evaluate_gate_on_split(ins, pred)
            gh = evaluate_gate_on_split(ho, pred)
            is_all.extend(gi)
            ho_all.extend(gh)
            per_reg_ho[reg] = {}
            for h in HORIZONS:
                per_reg_ho[reg][h] = horizon_stats(gh, h)
                per_reg_ho[reg][h]["unique_pools"] = pool_conc(gh)["unique_pools"]

        for h in HORIZONS:
            agg_hold[h] = horizon_stats(ho_all, h)
            agg_hold[h]["unique_pools"] = pool_conc(ho_all)["unique_pools"]
            agg_hold[h]["pool_top1"] = pool_conc(ho_all)["top1"]
            agg_hold[h]["pool_top5"] = pool_conc(ho_all)["top5"]
            ins_hold_comparison[h] = {
                "insample": horizon_stats(is_all, h),
                "holdout": agg_hold[h],
            }

        h30s = agg_hold.get("30m", {})
        regimes_pos = sum(
            1
            for reg in REGIMES
            if (per_reg_ho.get(reg, {}).get("30m", {}).get("mean") or -1) > 0
        )
        regimes_pf_ok = sum(
            1
            for reg in REGIMES
            if (per_reg_ho.get(reg, {}).get("30m", {}).get("profit_factor") or 0) > 1.0
        )

        supersession_style = (
            (h30s.get("mean") or -1) > 0
            and (h30s.get("profit_factor") or 0) > 1.0
            and regimes_pos >= 3
        )

        gate_results[gate_name] = {
            "holdout_n_total": len(ho_all),
            "insample_n_total": len(is_all),
            "aggregate_holdout": agg_hold,
            "per_regime_holdout": per_reg_ho,
            "regimes_holdout_mean_positive_30m": regimes_pos,
            "regimes_holdout_pf_gt_1_30m": regimes_pf_ok,
            "supersession_candidate_30m": supersession_style,
        }

        er = {
            "gate": gate_name,
            "holdout_n": len(ho_all),
            "insample_n": len(is_all),
        }
        for h in HORIZONS:
            a = agg_hold[h]
            er[f"{h}_label_1_share"] = a["label_1_share"]
            er[f"{h}_mean"] = a["mean"]
            er[f"{h}_median"] = a["median"]
            er[f"{h}_trimmed_mean"] = a["trimmed_mean"]
            er[f"{h}_profit_factor"] = a["profit_factor"]
            er[f"{h}_p10"] = a["p10"]
            er[f"{h}_p90"] = a["p90"]
            er[f"{h}_unique_pools"] = a["unique_pools"]
            er[f"{h}_pool_top1"] = a["pool_top1"]
            er[f"{h}_pool_top5"] = a["pool_top5"]
        er["regimes_pos_mean_30m"] = regimes_pos
        er["supersession_candidate"] = supersession_style
        early_hold_rows.append(er)

    # Pick best gate by holdout 30m mean then PF among gates with holdout_n>=100
    ranked = sorted(
        (
            (
                gate_results[g]["aggregate_holdout"]["30m"]["mean"] or -999,
                gate_results[g]["aggregate_holdout"]["30m"]["profit_factor"] or 0,
                gate_results[g]["holdout_n_total"],
                g,
            )
            for g in GATE_ORDER
            if gate_results[g]["holdout_n_total"] >= 100
        ),
        reverse=True,
    )
    best_gate = ranked[0][3] if ranked else "none"

    ranked500 = sorted(
        (
            (
                gate_results[g]["aggregate_holdout"]["30m"]["mean"] or -999,
                gate_results[g]["aggregate_holdout"]["30m"]["profit_factor"] or 0,
                gate_results[g]["holdout_n_total"],
                g,
            )
            for g in GATE_ORDER
            if gate_results[g]["holdout_n_total"] >= 500
        ),
        reverse=True,
    )
    best_gate_min_n500 = ranked500[0][3] if ranked500 else "none"

    any_super = any(gate_results[g]["supersession_candidate_30m"] for g in GATE_ORDER)

    bl_mean = gate_results["baseline_q4_all"]["aggregate_holdout"]["30m"]["mean"] or 0
    best_mean = (
        gate_results[best_gate]["aggregate_holdout"]["30m"]["mean"] or 0
        if best_gate in gate_results
        else bl_mean
    )
    best_reg_pos = gate_results.get(best_gate, {}).get(
        "regimes_holdout_mean_positive_30m", 0
    )

    early_gates = (
        "fixed_phase_le_20pct",
        "fixed_phase_le_40pct",
        "early40_flow_positive",
        "activity_onset_le_6h",
    )
    any_early_pos_mean = any(
        (gate_results[g]["aggregate_holdout"]["30m"]["mean"] or 0) > 0 for g in early_gates
    )

    if not baseline_ok:
        status = "BLOCKED_BY_SCHEMA_OR_DATA"
    elif any_super:
        status = "LIFECYCLE_HOLDOUT_POSITIVE_REVIEW_REQUIRED"
    elif best_reg_pos <= 1 and best_mean > bl_mean:
        status = "EARLY_PHASE_ONLY_SCOPED_EDGE"
    elif best_mean > bl_mean and not any_super:
        status = "LIFECYCLE_IMPROVES_BUT_NOT_ENOUGH"
    elif not any_early_pos_mean:
        status = "ROUTE_PIVOT_OR_FEATURE_GAP"
    else:
        status = "HOLDOUT_STILL_NEGATIVE"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = {
        "kind": "sprint037e_lifecycle_validation",
        "created_at": now,
        "status": status,
        "baseline_sprint037d_reproduced": baseline_ok,
        "baseline_holdout_30m_ref": {"mean": ref_mean, "n": ref_n},
        "baseline_holdout_30m_computed": {"mean": h30["mean"], "n": h30["n"]},
        "best_gate_by_holdout_30m_mean": best_gate,
        "best_gate_min_holdout_n_500": best_gate_min_n500,
        "any_supersession_candidate_30m": any_super,
        "predeclared_constants": {
            "ACTIVITY_ONSET_THRESH": ACTIVITY_ONSET_THRESH,
            "PRIOR_RUNUP_WINDOW_SEC": PRIOR_RUNUP_WINDOW_SEC,
            "LABEL_KNOWN_LAG_SEC": LABEL_KNOWN_LAG_SEC,
            "PRIOR_RUNUP_LOW_HIGH": PRIOR_RUNUP_LOW_HIGH,
            "TOKEN_VOL_HIGH_FIXED": TOKEN_VOL_HIGH_FIXED,
            "HOLDOUT_FRAC": HOLDOUT_FRAC,
        },
        "gates": gate_results,
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

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "lifecycle_validation_summary.json").write_text(
        json.dumps(summary, indent=2)
    )

    with (OUT_DIR / "lifecycle_bucket_readout.csv").open("w", newline="") as f:
        if bucket_rows_out:
            w = csv.DictWriter(f, fieldnames=list(bucket_rows_out[0].keys()))
            w.writeheader()
            w.writerows(bucket_rows_out)

    with (OUT_DIR / "early_phase_holdout_readout.csv").open("w", newline="") as f:
        if early_hold_rows:
            w = csv.DictWriter(f, fieldnames=list(early_hold_rows[0].keys()))
            w.writeheader()
            w.writerows(early_hold_rows)

    md_path = RUNS_DIR / "run-037E-lifecycle-holdout-readout.md"
    md_lines = [
        "# Run-037E — Regime lifecycle holdout readout",
        "",
        f"Generated: {now}",
        "",
        "## Status",
        "",
        status,
        "",
        "## Sprint037D baseline reproduction",
        "",
        f"- Match Sprint037D aggregate holdout 30m: **{baseline_ok}**",
        f"- Reference mean/n: `{ref_mean}` / `{ref_n}`",
        f"- Computed mean/n: `{h30['mean']}` / `{h30['n']}`",
        "",
        "## Best gate (holdout 30m mean, holdout_n≥100)",
        "",
        f"`{best_gate}`",
        "",
        "## Best gate with holdout_n≥500 (avoid thin subsets)",
        "",
        f"`{best_gate_min_n500}`",
        "",
        "## Supersession candidate (holdout mean>0, PF>1 @30m, ≥3 regimes mean>0)",
        "",
        str(any_super),
        "",
        "| Gate | holdout_n | 30m mean | 30m PF | regimes mean>0 | supersession_candidate |",
        "|------|-----------|----------|--------|----------------|------------------------|",
    ]
    for g in GATE_ORDER:
        gr = gate_results[g]
        a30 = gr["aggregate_holdout"]["30m"]
        md_lines.append(
            f"| {g} | {gr['holdout_n_total']} | {a30.get('mean')} | {a30.get('profit_factor')} | "
            f"{gr['regimes_holdout_mean_positive_30m']} | {gr['supersession_candidate_30m']} |"
        )
    md_lines += [
        "",
        "---",
        "",
        "*Generated by scripts/sprint037e_lifecycle_validation.py. Predeclared gates only.*",
        "`NO_ALPHA_CLAIM=true`",
    ]
    md_path.write_text("\n".join(md_lines))

    _log(f"wrote {OUT_DIR} and {md_path}")


if __name__ == "__main__":
    main()
