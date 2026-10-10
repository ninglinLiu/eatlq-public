#!/usr/bin/env python3
"""Decay profiles, LOWO, leave-one-regime, cluster stress — OOF predictions only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import OUT, load_freeze, utcnow  # noqa: E402
from src.models_routes import eval_block  # noqa: E402

PRED = OUT / "predictions"
METRICS = OUT / "metrics"


def decay_profile(pred: pd.DataFrame, H: int) -> dict:
    if pred.empty:
        return {"status": "EMPTY"}
    t = pred["decision_time"]
    t_max = t.max()
    modes = {}
    # expanding: already OOF; report last third vs first third of OOF timeline
    q1, q2 = t.quantile(0.33), t.quantile(0.66)
    early = eval_block(pred[t <= q1], H)
    late = eval_block(pred[t >= q2], H)
    modes["expanding_early_vs_late"] = {"early_ev": early.get("ev_accepted"), "late_ev": late.get("ev_accepted")}
    # rolling 90d window ending at max
    roll = pred[t >= t_max - 90 * 86400]
    modes["rolling_90d"] = eval_block(roll, H)
    # recency weights on accepted EV
    acc = pred[pred["selected"] & pred["atomic_net_return"].notna()].copy()
    if len(acc):
        age_d = (t_max - acc["decision_time"]) / 86400.0
        w = np.exp(-np.log(2) * age_d / 30.0)
        modes["recency_halflife_30d_ev"] = float(np.average(acc["atomic_net_return"], weights=w))
    else:
        modes["recency_halflife_30d_ev"] = None
    # status
    late_ev = late.get("ev_accepted")
    early_ev = early.get("ev_accepted")
    if late_ev is None:
        status = "DISABLED"
    elif early_ev is not None and late_ev < 0 and early_ev > 0:
        status = "DEGRADED"
    elif late_ev > 0:
        status = "ACTIVE"
    else:
        status = "DISABLED"
    return {"modes": modes, "decay_status": status}


def leave_one_week_out(pred: pd.DataFrame, H: int) -> dict:
    if pred.empty:
        return {}
    pred = pred.copy()
    pred["week"] = pred["decision_time"] // (7 * 86400)
    out = {}
    for w, g in pred.groupby("week"):
        out[str(int(w))] = eval_block(g, H).get("ev_accepted")
    vals = [v for v in out.values() if v is not None]
    return {"by_week": out, "worst_week": min(vals) if vals else None, "best_week": max(vals) if vals else None}


def leave_one_regime(pred: pd.DataFrame, H: int) -> dict:
    """Regime by market breadth terciles."""
    if pred.empty or "base_breadth_1h" not in pred.columns:
        return {}
    pred = pred.copy()
    pred["regime"] = pd.qcut(pred["base_breadth_1h"].rank(method="first"), 3, labels=["low", "mid", "high"])
    out = {}
    for r, g in pred.groupby("regime", observed=False):
        out[str(r)] = eval_block(g, H).get("ev_accepted")
    return out


def cluster_stress(pred: pd.DataFrame, H: int) -> dict:
    """Collapse by token/week — report if EV depends on one cluster."""
    if pred.empty:
        return {}
    acc = pred[pred["selected"]].copy()
    if acc.empty:
        return {"n_accepted": 0}
    by_token = acc.groupby("pool")["atomic_net_return"].sum().sort_values(ascending=False)
    tot = by_token.sum()
    return {
        "n_accepted": int(len(acc)),
        "n_tokens": int(acc["pool"].nunique()),
        "top_token_share": float(by_token.iloc[0] / tot) if tot != 0 and len(by_token) else None,
        "top5_token_share": float(by_token.head(5).sum() / tot) if tot != 0 else None,
    }


def main():
    freeze = load_freeze()
    H = int(freeze["primary_horizon_seconds"])
    scorecard = pd.read_csv(METRICS / "oof_scorecard.csv")
    reports = {}
    for mid in scorecard["model_id"]:
        path = PRED / f"oof_{mid}.parquet"
        if not path.exists():
            continue
        pred = pd.read_parquet(path)
        reports[mid] = {
            "decay": decay_profile(pred, H),
            "lowo": leave_one_week_out(pred, H),
            "regime": leave_one_regime(pred, H),
            "cluster": cluster_stress(pred, H),
        }
    out = {"created_utc": utcnow(), "reports": reports}
    (METRICS / "validation_extra.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"n_models": len(reports), "path": str(METRICS / "validation_extra.json")}, indent=2), flush=True)


if __name__ == "__main__":
    main()
