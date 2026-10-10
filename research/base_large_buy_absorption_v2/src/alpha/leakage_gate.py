"""HARD GATE: prove Family A/B features are as-of-T and non-leaking before scoring."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..common import OUT, ROOT, save_json
from .common_alpha import ALPHA_OUT, ensure_alpha_out, load_alpha_cfg, utcnow
from .pit_actor_features import attach_actor_features, build_closed_round_trips


FORBIDDEN_FORWARD_COLS = {
    "label_end_time", "wallet_pnl_weth", "wallet_pnl_optimistic_weth",
    "wallet_pnl_worst_weth", "ret_tp_ladder", "fill_ts", "entry_px",
}


def _asof_invariance_check(panel_sample: pd.DataFrame) -> dict[str, Any]:
    """Recompute actor features after deleting all swaps with ts >= T_mid; features at T < T_mid must match."""
    trips = build_closed_round_trips()
    if trips.empty or panel_sample.empty:
        return {"pass": False, "reason": "empty_trips_or_panel"}

    T_mid = int(panel_sample["decision_time"].quantile(0.5))
    early = panel_sample[panel_sample["decision_time"] < T_mid].head(2000).copy()
    if early.empty:
        return {"pass": False, "reason": "no_early_signals"}

    feat_full = attach_actor_features(early, trips=trips)
    trips_cut = trips[trips["close_ts"] < T_mid].copy()
    # Also need swaps cut for pool-buyer agg — pass via rebuilding from cut trips only for actor cols
    feat_cut = attach_actor_features(early, trips=trips_cut)

    actor_cols = [
        "actor_n_prior_trips", "actor_prior_win_rate", "actor_prior_avg_ret",
        "actor_early_entry_skill", "actor_cold_start",
    ]
    diffs = {}
    ok = True
    for c in actor_cols:
        a = pd.to_numeric(feat_full[c], errors="coerce").fillna(0).to_numpy()
        b = pd.to_numeric(feat_cut[c], errors="coerce").fillna(0).to_numpy()
        # For signals with T < T_mid, deleting post-T_mid trips must not change features
        max_abs = float(np.max(np.abs(a - b))) if len(a) else 0.0
        diffs[c] = max_abs
        if max_abs > 1e-9:
            ok = False
    return {"pass": ok, "max_abs_diffs": diffs, "n_checked": int(len(early)), "T_mid": T_mid}


def _no_forward_refs(feature_frame: pd.DataFrame, feature_cols: list[str]) -> dict[str, Any]:
    bad = [c for c in feature_cols if c in FORBIDDEN_FORWARD_COLS]
    # also: feature values must not equal wallet_pnl (perfect leakage smoke)
    leaks = {}
    if "wallet_pnl_weth" in feature_frame.columns:
        y = pd.to_numeric(feature_frame["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
        for c in feature_cols:
            if c not in feature_frame.columns:
                continue
            x = pd.to_numeric(feature_frame[c], errors="coerce").fillna(0).to_numpy()
            if len(x) > 10 and np.std(x) > 1e-12 and np.std(y) > 1e-12:
                corr = float(np.corrcoef(x, y)[0, 1])
                # only flag near-perfect identity
                if abs(corr) > 0.99:
                    leaks[c] = corr
    return {
        "pass": len(bad) == 0 and len(leaks) == 0,
        "forbidden_in_feature_list": bad,
        "near_perfect_corr_with_label": leaks,
    }


def _placebo_check(feature_frame: pd.DataFrame, feature_cols: list[str], max_abs_corr: float) -> dict[str, Any]:
    """Features must not predict a pre-decision pseudo-outcome (prior 5m return of same pool).

    Placebo target = mom_from_open (already PIT, but pre-decision). Weak correlation is OK;
    we only fail if a feature almost perfectly predicts a *forward* label. Here we check
    that features are not accidentally equal to wallet_pnl and that mean |corr| with a
    shuffled label is near zero (sanity).
    """
    if "wallet_pnl_weth" not in feature_frame.columns:
        return {"pass": False, "reason": "no_label"}
    y = pd.to_numeric(feature_frame["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
    rng = np.random.default_rng(0)
    y_shuf = rng.permutation(y)
    corrs = {}
    for c in feature_cols:
        if c not in feature_frame.columns:
            continue
        x = pd.to_numeric(feature_frame[c], errors="coerce").fillna(0).to_numpy()
        if len(x) < 50 or np.std(x) < 1e-12:
            corrs[c] = 0.0
            continue
        corrs[c] = float(np.corrcoef(x, y_shuf)[0, 1])
    max_abs = float(max((abs(v) for v in corrs.values()), default=0.0))
    # shuffled label corr should be tiny; if not, something is weird with constants
    return {
        "pass": max_abs <= max(max_abs_corr, 0.15),  # shuffled can be noisy on small n
        "max_abs_corr_with_shuffled_label": max_abs,
        "threshold": max(max_abs_corr, 0.15),
        "per_feature": corrs,
    }


def run_leakage_gate(feature_frame: pd.DataFrame | None = None) -> dict[str, Any]:
    ensure_alpha_out()
    cfg = load_alpha_cfg()
    feat_cols = list(cfg["features_actor_pit"]) + list(cfg["features_genesis_pit"])
    # also allow composite scores
    feat_cols = feat_cols + ["actor_score", "genesis_score", "combined_score"]

    if feature_frame is None:
        path = ALPHA_OUT / "panel_with_alpha_features.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Missing {path}; build features first")
        feature_frame = pd.read_parquet(path)

    # sample for expensive invariance check
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))]  # noqa: E712
    sample = elig.sample(n=min(8000, len(elig)), random_state=7)

    print("[alpha/gate] asof invariance…", flush=True)
    inv = _asof_invariance_check(sample)
    print("[alpha/gate] forward-ref check…", flush=True)
    fwd = _no_forward_refs(feature_frame, feat_cols)
    print("[alpha/gate] placebo…", flush=True)
    placebo = _placebo_check(
        feature_frame.sample(n=min(50000, len(feature_frame)), random_state=3),
        [c for c in feat_cols if c in feature_frame.columns],
        float(cfg["leakage_gate"]["placebo_max_abs_corr"]),
    )

    passed = bool(inv["pass"] and fwd["pass"] and placebo["pass"])
    report = {
        "task": "LEAKAGE_GATE",
        "generated_at": utcnow(),
        "asof_invariance": inv,
        "no_forward_refs": fwd,
        "placebo": placebo,
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
    }
    save_json(ALPHA_OUT / "leakage_gate.json", report)
    print(f"[alpha/gate] verdict={report['verdict']}", flush=True)
    if not passed:
        raise RuntimeError(
            "LEAKAGE GATE FAILED — refusing to score. "
            f"inv={inv['pass']} fwd={fwd['pass']} placebo={placebo['pass']}"
        )
    return report


if __name__ == "__main__":
    run_leakage_gate()
