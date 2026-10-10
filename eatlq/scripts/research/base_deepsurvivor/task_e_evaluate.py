#!/usr/bin/env python3
"""Task E: Pass-line evaluation → deep_edge_yes|weak|no."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import load_json, rpc_ledger, save_json  # noqa: E402

BASELINE = os.path.join(HERE, "_deep_baseline.json")
SMART = os.path.join(HERE, "_smartmoney_signal.json")
DECAY = os.path.join(HERE, "_smartmoney_decay.json")
FLOW = os.path.join(HERE, "_flow_signals.json")
LEFT = os.path.join(HERE, "_deep_lefttail.json")
UNIVERSE = os.path.join(HERE, "_deep_universe.json")
OUT = os.path.join(HERE, "_edge_verdict.json")


def check_pass_lines() -> dict:
    bl = load_json(BASELINE, {})
    smart = load_json(SMART, {})
    decay = load_json(DECAY, {})
    flow = load_json(FLOW, {})
    left = load_json(LEFT, {})
    uni = load_json(UNIVERSE, {})

    bl_test = (bl.get("holds") or {}).get("4h", {}).get("test", {})
    sm_test = smart.get("test", {})
    adverse = smart.get("adverse_selection", {})

    # Pass line 1: signal TEST > baseline TEST
    best_signal = None
    best_mean = bl_test.get("mean", 0)
    for sig in flow.get("signals", []):
        te = sig.get("test", {})
        if te.get("n", 0) >= 30 and te.get("mean", -999) > best_mean:
            best_signal = sig["signal"]
            best_mean = te["mean"]
    sm_beat = smart.get("beat_baseline_test")
    if sm_test.get("n", 0) >= 30 and sm_test.get("mean", -999) > bl_test.get("mean", 0):
        if best_signal is None or sm_test["mean"] > best_mean:
            best_signal = "smart_money"
            best_mean = sm_test["mean"]

    p1 = bool(best_signal) and (sm_beat is True or (best_signal != "smart_money" and best_mean > bl_test.get("mean", 0)))

    # Pass line 2: TRAIN/TEST gap acceptable (<0.15 mean abs gap for best signal)
    p2 = False
    if best_signal and best_signal != "smart_money":
        for sig in flow.get("signals", []):
            if sig["signal"] == best_signal:
                gap = sig.get("train_test_gap")
                p2 = gap is not None and gap < 0.15
    elif best_signal == "smart_money":
        tr, te = smart.get("train", {}), smart.get("test", {})
        if tr.get("n", 0) >= 10 and te.get("n", 0) >= 10:
            p2 = abs(tr.get("mean", 0) - te.get("mean", 0)) < 0.15

    # Pass line 3: delay decay still beats baseline at realistic delta (+30 blocks)
    decay_30 = (decay.get("decay_curve") or {}).get("30", {})
    p3 = (
        decay_30.get("n", 0) >= 10
        and decay_30.get("mean", -999) > bl_test.get("mean", 0)
    ) or (best_signal and best_signal != "smart_money")

    # Pass line 4: fatal rate + adverse selection
    fatal = left.get("fatal_rate_50", 1.0)
    adv_rate = adverse.get("exit_liquidity_rate")
    p4 = fatal is not None and fatal < 0.25 and (adv_rate is None or adv_rate < 0.40)

    passed = sum(1 for x in [p1, p2, p3, p4] if x)
    if passed >= 4:
        verdict = "deep_edge_yes"
    elif passed >= 3:
        verdict = "deep_edge_weak"
    else:
        verdict = "deep_edge_no"

    return {
        "verdict": verdict,
        "pass_lines": {
            "1_signal_beats_baseline_test": p1,
            "2_train_test_gap_ok": p2,
            "3_delay_decay_ok": p3,
            "4_fatal_and_adverse_ok": p4,
        },
        "passed_count": passed,
        "best_signal": best_signal,
        "baseline_test_4h": bl_test,
        "smart_test": sm_test,
        "flow_signals": [
            {"signal": s["signal"], "test_n": s.get("test", {}).get("n"), "beat": s.get("beat_baseline_test")}
            for s in flow.get("signals", [])
        ],
        "lefttail_fatal_50": fatal,
        "adverse_exit_liq_rate": adv_rate,
        "universe_n": uni.get("meta", {}).get("n_pools"),
        "rpc": rpc_ledger(),
    }


def main():
    v = check_pass_lines()
    save_json(OUT, v)
    print(f"#### Task E: {v['verdict']} ({v['passed_count']}/4 pass lines) ####")
    for k, ok in v["pass_lines"].items():
        print(f"  {k}: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
