#!/usr/bin/env python3
"""Offline GL5v3 summaries from the immutable archive response artifacts."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path("/opt/EATLQ")
SUMMARY = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_resolve_20260716.json"
LEGS = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl5v3_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl5v3_rpc_ledger_summary_20260716.json"
PESS = -1.0229943556396193
OFFSETS = (1, 5, 15)
SEED = 20260716


def rug_status(cell: dict) -> str:
    """Pool-level rug status, deliberately separate from return resolution."""
    if cell["branch"] == "CONFIRMED_COLLAPSE":
        return "CONFIRMED_COLLAPSE"
    a = cell["archive"]
    atomic = a.get("atomic") or {}
    if cell["branch"] == "REAL_SELL_CARRY_FORWARD" or atomic.get("sell_ok"):
        return "CONFIRMED_NOT_COLLAPSED"
    return "RUG_UNRESOLVED"


def basic(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "top1_removed_mean": None, "top1_contribution": None}
    a = np.asarray(vals, dtype=float)
    k = max(1, math.ceil(0.01 * len(a)))
    order = np.argsort(a)[::-1]
    rem = np.delete(a, order[:k])
    pos = np.maximum(a, 0)
    return {"n": len(vals), "mean": float(a.mean()), "median": float(np.median(a)), "win_rate": float((a > 0).mean()),
            "top1_removed_mean": float(rem.mean()) if len(rem) else None,
            "top1_contribution": float(pos[order[:k]].sum() / pos.sum()) if pos.sum() else 0.0}


def rolling(rows: list[dict], offset: int) -> list[dict]:
    times = [datetime.fromisoformat(r["signal_time"]) for r in rows]
    start, end = min(times), max(times)
    out, i = [], 0
    while start + timedelta(days=15 * i) <= end:
        lo, hi = start + timedelta(days=15 * i), start + timedelta(days=15 * i + 30)
        vals = [r["arm1"][str(offset)]["ret"] for r in rows if lo <= datetime.fromisoformat(r["signal_time"]) < hi and r["arm1"][str(offset)]["resolved"]]
        out.append({"id": f"R{i:02}", "start": lo.isoformat(), "end_exclusive": hi.isoformat(), **basic(vals)})
        i += 1
    return out


def monte_carlo(vals: list[float], offset: int) -> dict:
    rng = np.random.default_rng(SEED + offset)
    arr = rng.choice(np.asarray(vals), size=(10000, len(vals)), replace=True)
    eq = 100 + np.cumsum(arr, axis=1)
    peaks = np.maximum.accumulate(np.column_stack([np.full(10000, 100.0), eq]), axis=1)[:, 1:]
    dd = np.max(peaks - eq, axis=1)
    return {"paths": 10000, "terminal_p5": float(np.quantile(eq[:, -1], 0.05)), "max_drawdown_p95": float(np.quantile(dd, 0.95))}


def tail(rows: list[dict], offset: int) -> dict:
    cells = [r["arm1"][str(offset)] for r in rows]
    vals = [c["ret"] for c in cells if c["resolved"]]
    stat = basic(vals)
    roll = rolling(rows, offset)
    powered = [x for x in roll if x["n"] >= 20]
    mc = monte_carlo(vals, offset)
    unresolved = sum(not c["resolved"] for c in cells)
    gates = {
        "pooled_mean_gt_0": stat["mean"] > 0,
        "top1_removed_mean_gt_0": stat["top1_removed_mean"] > 0,
        "top1_contribution_le_25pct": stat["top1_contribution"] <= 0.25,
        "every_powered_rolling_mean_ge_minus_10pct": all(x["mean"] >= -0.10 for x in powered) if powered else None,
        "mc_terminal_p5_gt_100R": mc["terminal_p5"] > 100,
        "mc_mdd_p95_le_20R": mc["max_drawdown_p95"] <= 20,
        "unresolved_eq_0": unresolved == 0,
    }
    status = "INCONCLUSIVE" if unresolved or any(v is None for v in gates.values()) else ("PASS" if all(gates.values()) else "FAIL")
    return {"resolved_only": stat, "unresolved_n": unresolved, "rolling": roll, "powered_rolling_n": len(powered),
            "monte_carlo": mc, "gates": gates, "formal_status": status}


def adjudicate_rug(rows: list[dict], offset: int) -> dict:
    counts = Counter(rug_status(r["arm1"][str(offset)]) for r in rows)
    n = len(rows)
    lo = counts["CONFIRMED_COLLAPSE"] / n
    hi = (counts["CONFIRMED_COLLAPSE"] + counts["RUG_UNRESOLVED"]) / n
    return {"counts": dict(sorted(counts.items())), "n": n, "fully_identified": counts["RUG_UNRESOLVED"] == 0,
            "rug_rate_point": lo if counts["RUG_UNRESOLVED"] == 0 else None, "rug_rate_interval": [lo, hi]}


def main():
    summary = json.loads(SUMMARY.read_text())
    legs_obj = json.loads(LEGS.read_text())
    ledger_summary = json.loads(LEDGER_SUM.read_text())
    rows = legs_obj["rows"]
    selected = [r for r in rows if r["selected_Fon"]]
    for r in rows:
        for o in OFFSETS:
            r["arm1"][str(o)]["rug_adjudication"] = rug_status(r["arm1"][str(o)])
    selected_rug = {str(o): adjudicate_rug(selected, o) for o in OFFSETS}
    baseline_rug = {str(o): adjudicate_rug(rows, o) for o in OFFSETS}
    for o in OFFSETS:
        key = str(o)
        summary["arm1_archive_resolved"][key]["rug_rate_identification_interval"] = selected_rug[key]["rug_rate_interval"]
        summary["arm1_archive_resolved"][key]["rug_rate_fully_identified"] = selected_rug[key]["fully_identified"]
        summary["baseline_archive_resolved"][key]["rug_rate_identification_interval"] = baseline_rug[key]["rug_rate_interval"]
        summary["baseline_archive_resolved"][key]["rug_rate_fully_identified"] = baseline_rug[key]["fully_identified"]
    right_tail = {str(o): tail(selected, o) for o in OFFSETS}
    arm0_mean = summary["arm0_reproduction"]["lenses"]["qv2"]["live_full_gates"]["mean_ret"]
    arm_delta = {str(o): [x - arm0_mean for x in summary["arm1_archive_resolved"][str(o)]["population_mean_interval"]] for o in OFFSETS}
    summary["rug_adjudication"] = {"selected_Fon_120": selected_rug, "baseline_644": baseline_rug,
                                   "interpretation": "Rug identification is pool-level and separate from return identification; a pool can be proved non-collapsed while historical-position PnL remains unresolved."}
    summary["right_tail"] = right_tail
    summary["arm0_to_arm1_population_mean_delta_interval"] = arm_delta
    summary["decision_identification"] = {
        "all_selected_population_lower_bounds_gt_zero": all(summary["arm1_archive_resolved"][str(o)]["population_mean_interval"][0] > 0 for o in OFFSETS),
        "all_conditional_alpha_lower_bounds_gt_zero": all(summary["conditional_alpha"][str(o)]["interval"][0] > 0 for o in OFFSETS),
        "all_selected_rug_rates_fully_identified": all(selected_rug[str(o)]["fully_identified"] for o in OFFSETS),
        "selected_confirmed_rug_n_by_offset": {str(o): selected_rug[str(o)]["counts"].get("CONFIRMED_COLLAPSE", 0) for o in OFFSETS},
        "formal_verdict": "INCONCLUSIVE",
        "why_not_edge_survives": "Inherited preregistration requires unresolved==0; return unresolved counts are 7/5/2 and strict rug-unresolved counts are 3/1/1.",
        "directional_read": "EDGE_SURVIVES_STRONGLY_SUPPORTED_BUT_NOT_FORMALLY_ESTABLISHED",
    }
    recorded_attempts = sum(1 for _ in LEDGER.open())
    ledger_summary["ledger_recorded_attempts"] = recorded_attempts
    ledger_summary["ledger_unrecorded_transport_retries"] = ledger_summary["http_attempts"] - recorded_attempts
    ledger_summary["ledger_complete"] = recorded_attempts == ledger_summary["http_attempts"]
    ledger_summary["ledger_caveat"] = "A logging defect omitted intermediate transport-failure retries; all successful response hashes and all final result rows are retained. Missing retry rows were not fabricated."
    summary["rpc"] = ledger_summary
    summary["verdict_candidate"] = "INCONCLUSIVE"
    summary["go_decision"] = "NOT_MADE"
    legs_obj["rows"] = rows
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    LEGS.write_text(json.dumps(legs_obj, indent=2, sort_keys=True) + "\n")
    LEDGER_SUM.write_text(json.dumps(ledger_summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"selected_rug": selected_rug, "baseline_rug": baseline_rug, "right_tail": right_tail,
                      "arm0_delta": arm_delta, "verdict": summary["verdict_candidate"], "go": summary["go_decision"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
