"""HARD GATE for Alpha Hunt v2 features."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..common import OUT, save_json
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out, load_alpha_v2_cfg, utcnow
from .pit_actor_xpool import attach_actor_xpool_features, pull_xpool_swaps
from ..alpha.pit_actor_features import build_closed_round_trips, _expanding_actor_stats, _asof_actor

FORBIDDEN = {
    "label_end_time", "wallet_pnl_weth", "wallet_pnl_optimistic_weth",
    "wallet_pnl_worst_weth", "ret_tp_ladder", "fill_ts", "entry_px",
}


def _asof_invariance() -> dict[str, Any]:
    """Recompute A2 actor features after cutting trips at T_mid; early signals must match."""
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))]  # noqa: E712
    sample = elig.sample(n=min(5000, len(elig)), random_state=11)
    T_mid = int(sample["decision_time"].quantile(0.5))
    early = sample[sample["decision_time"] < T_mid].head(1500).copy()
    if early.empty:
        return {"pass": False, "reason": "no_early"}

    trips_path = ALPHA_V2_OUT / "actor_xpool_closed_trips.parquet"
    if not trips_path.exists():
        return {"pass": False, "reason": "no_trips_cache"}
    trips = pd.read_parquet(trips_path)
    stats_full = _expanding_actor_stats(trips)
    stats_cut = _expanding_actor_stats(trips[trips["close_ts"] < T_mid])

    early = early.copy()
    early["_row"] = np.arange(len(early))
    early["economic_actor"] = early["economic_actor"].astype(str).str.lower()
    early["decision_time"] = early["decision_time"].astype(np.int64)
    a_full = _asof_actor(early, stats_full)
    a_cut = _asof_actor(early, stats_cut)
    cols = ["n_trips", "win_rate", "avg_ret"]
    diffs = {}
    ok = True
    for c in cols:
        a = pd.to_numeric(a_full[c], errors="coerce").fillna(0).to_numpy()
        b = pd.to_numeric(a_cut[c], errors="coerce").fillna(0).to_numpy()
        mx = float(np.max(np.abs(a - b))) if len(a) else 0.0
        diffs[c] = mx
        if mx > 1e-9:
            ok = False
    return {"pass": ok, "max_abs_diffs": diffs, "n_checked": int(len(early)), "T_mid": T_mid}


def run_leakage_gate_v2(feature_frame: pd.DataFrame) -> dict[str, Any]:
    ensure_alpha_v2_out()
    cfg = load_alpha_v2_cfg()
    feat_cols = list(cfg["features_actor_xpool_pit"]) + list(cfg["features_clone_family_pit"])
    feat_cols += ["actor_xpool_score", "family_score", "combined_score"]

    print("[alpha_v2/gate] asof invariance…", flush=True)
    inv = _asof_invariance()

    bad = [c for c in feat_cols if c in FORBIDDEN]
    leaks = {}
    if "wallet_pnl_weth" in feature_frame.columns:
        y = pd.to_numeric(feature_frame["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
        for c in feat_cols:
            if c not in feature_frame.columns:
                continue
            x = pd.to_numeric(feature_frame[c], errors="coerce").fillna(0).to_numpy()
            if len(x) > 10 and np.std(x) > 1e-12 and np.std(y) > 1e-12:
                corr = float(np.corrcoef(x, y)[0, 1])
                if abs(corr) > 0.99:
                    leaks[c] = corr
    fwd = {"pass": len(bad) == 0 and len(leaks) == 0, "forbidden": bad, "near_perfect": leaks}

    print("[alpha_v2/gate] placebo…", flush=True)
    sub = feature_frame.sample(n=min(50000, len(feature_frame)), random_state=5)
    y = pd.to_numeric(sub["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
    y_shuf = np.random.default_rng(0).permutation(y)
    corrs = {}
    for c in feat_cols:
        if c not in sub.columns:
            continue
        x = pd.to_numeric(sub[c], errors="coerce").fillna(0).to_numpy()
        if len(x) < 50 or np.std(x) < 1e-12:
            corrs[c] = 0.0
            continue
        corrs[c] = float(np.corrcoef(x, y_shuf)[0, 1])
    max_abs = float(max((abs(v) for v in corrs.values()), default=0.0))
    placebo = {"pass": max_abs <= 0.15, "max_abs_corr_shuffled": max_abs, "per_feature": corrs}

    passed = bool(inv["pass"] and fwd["pass"] and placebo["pass"])
    report = {
        "task": "LEAKAGE_GATE_V2",
        "generated_at": utcnow(),
        "asof_invariance": inv,
        "no_forward_refs": fwd,
        "placebo": placebo,
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
    }
    save_json(ALPHA_V2_OUT / "leakage_gate.json", report)
    print(f"[alpha_v2/gate] verdict={report['verdict']}", flush=True)
    if not passed:
        raise RuntimeError(
            f"LEAKAGE GATE V2 FAILED inv={inv['pass']} fwd={fwd['pass']} placebo={placebo['pass']}"
        )
    return report
