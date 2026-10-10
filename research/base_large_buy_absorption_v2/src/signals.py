"""Corrected signal timing — lookahead-free by construction (v2).

Primary preregistered: absorption_window=5s, measured_latency=0s.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import load_freeze, signal_id


ARM_TIMING = {
    "POOL_ONLY": "immediate",
    "LARGE_BUY_ONLY": "immediate",
    "ABSORPTION_ONLY": "wait_absorption",
    "LARGE_BUY_ABSORPTION": "wait_absorption",
}


def attach_signal_times(
    buys: pd.DataFrame,
    arm: str,
    *,
    absorption_window_seconds: int | None = None,
    measured_latency_seconds: int | None = None,
    ingestion_lag_seconds: int | None = None,
) -> pd.DataFrame:
    freeze = load_freeze()
    st = freeze["signal_timing"]
    absorb_w = int(absorption_window_seconds if absorption_window_seconds is not None else st["absorption_window_seconds"])
    lag = int(ingestion_lag_seconds if ingestion_lag_seconds is not None else st["ingestion_lag_seconds"])
    latency = int(measured_latency_seconds if measured_latency_seconds is not None else st["measured_latency_seconds"])
    mode = ARM_TIMING[arm]
    d = buys.copy()
    d["whale_buy_t"] = d["whale_buy_t"].astype(np.int64)
    if mode == "wait_absorption":
        d["signal_t"] = d["whale_buy_t"] + absorb_w + lag
    else:
        d["signal_t"] = d["whale_buy_t"] + lag
    d["entry_t"] = d["signal_t"] + latency
    d["decision_time"] = d["signal_t"]
    d["timing_mode"] = mode
    d["arm_for_timing"] = arm
    d["absorb_w_used"] = absorb_w
    d["latency_used"] = latency
    # swap absorbed column if multi-window present
    col = f"absorbed_{absorb_w}s"
    if col in d.columns:
        d["absorbed"] = d[col]
        d["absorption_score"] = d["absorbed"]
        if "large_buy_score" in d.columns:
            d["lba_score"] = d["large_buy_score"] * (0.5 + 0.5 * d["absorbed"])
    d["signal_id"] = [
        signal_id(str(p), int(sig), str(a) if isinstance(a, str) else "")
        for p, sig, a in zip(d["pool"], d["signal_t"], d.get("economic_actor", pd.Series([""] * len(d))))
    ]
    return d


def assert_lookahead_free(
    panel: pd.DataFrame,
    *,
    absorption_window_seconds: int | None = None,
    measured_latency_seconds: int | None = None,
) -> dict:
    checks = []
    ok = True
    freeze = load_freeze()
    absorb_w = int(
        absorption_window_seconds
        if absorption_window_seconds is not None
        else freeze["signal_timing"]["absorption_window_seconds"]
    )
    latency = int(
        measured_latency_seconds
        if measured_latency_seconds is not None
        else freeze["signal_timing"]["measured_latency_seconds"]
    )
    if "decision_time" not in panel.columns or "signal_t" not in panel.columns:
        return {"pass": False, "checks": [{"name": "columns", "pass": False}]}
    eq = bool((panel["decision_time"].astype(int) == panel["signal_t"].astype(int)).all())
    checks.append({"name": "decision_time_equals_signal_t", "pass": eq})
    ok &= eq
    ent_ok = bool((panel["entry_t"].astype(int) >= panel["signal_t"].astype(int) + latency).all())
    checks.append({"name": "entry_after_signal_plus_latency", "pass": ent_ok})
    ok &= ent_ok
    wait = panel[panel["timing_mode"] == "wait_absorption"]
    if len(wait):
        wait_ok = bool((wait["signal_t"].astype(int) >= wait["whale_buy_t"].astype(int) + absorb_w).all())
    else:
        wait_ok = True
    checks.append({"name": "absorption_wait_complete", "pass": wait_ok, "absorb_w": absorb_w})
    ok &= wait_ok
    if len(wait):
        known = bool((wait["entry_t"].astype(int) >= wait["whale_buy_t"].astype(int) + absorb_w + latency).all())
    else:
        known = True
    checks.append({"name": "no_entry_before_absorbed_known", "pass": known})
    ok &= known
    if len(wait):
        not_v0 = bool((wait["decision_time"].astype(int) != wait["whale_buy_t"].astype(int)).all())
    else:
        not_v0 = True
    checks.append({"name": "not_v0_buy_time_decision", "pass": not_v0})
    ok &= not_v0
    return {
        "pass": bool(ok),
        "verdict": "LOOKAHEAD_FREE_PASS" if ok else "LOOKAHEAD_FAIL",
        "checks": checks,
        "n_rows": int(len(panel)),
        "n_wait_absorption": int(len(wait)),
        "absorption_window_seconds": absorb_w,
        "measured_latency_seconds": latency,
    }


def build_arm_panel(
    buys: pd.DataFrame,
    arm: str,
    *,
    absorption_window_seconds: int | None = None,
    measured_latency_seconds: int | None = None,
) -> pd.DataFrame:
    return attach_signal_times(
        buys,
        arm,
        absorption_window_seconds=absorption_window_seconds,
        measured_latency_seconds=measured_latency_seconds,
    )
