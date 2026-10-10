#!/usr/bin/env python3
"""Hardening #1 — walk-forward across regimes. For each calendar test-month, train
the left-tail model + derive the p_cut threshold using ONLY pools born strictly
before that month, then evaluate the depth-gate+model strategy on that month's
pools at REAL cost (TRAIL=0.35 net already cached). Confirms the edge is not a
single-regime / single-holdout artifact. Zero new RPC."""
from __future__ import annotations
import sys, json
import numpy as np, pandas as pd
sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import load, build
from sklearn.ensemble import HistGradientBoostingClassifier

FRIC_THR = -0.05
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def fit(Xtr, ytr):
    c = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                       l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    c.fit(Xtr, ytr); return c


def stat(r):
    r = np.array([x for x in r if x is not None])
    if len(r) == 0:
        return "n=0"
    top1 = (np.sort(r)[-1] / r[r > 0].sum()) if (r > 0).any() else 0
    return (f"n={len(r):3d} mean={r.mean():+.3f} med={np.median(r):+.3f} WR={(r>0).mean():.2f} "
            f"fatal={(r<=-0.5).mean():.3f} best={r.max():+.2f} top1share={top1:.2f}")


def main():
    df, _ = load()
    X, y = build(df)
    base = json.load(open(PCACHE))
    df = df.copy()
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    df["net"] = df.pool.map(lambda x: base.get(x, {}).get("net"))
    df["mon"] = df.t0.dt.strftime("%Y-%m")
    print("#### WALK-FORWARD (expanding window, real-cost TRAIL=0.35, friction>=-0.05) ####")
    print("each row: model+p_cut trained ONLY on pools before the test month\n")
    agg = []
    for tm in TEST_MONTHS:
        trm = df.mon < tm
        if trm.sum() < 150:
            print(f"  {tm}: TRAIN too small ({trm.sum()})"); continue
        clf = fit(X[trm.values], y[trm.values])
        df.loc[:, "p"] = clf.predict_proba(X)[:, 1]
        gtr = df[trm & df.fric.notna() & (df.fric >= FRIC_THR)]
        p_cut = gtr.p.quantile(0.6)
        tem = (df.mon == tm) & df.fric.notna()
        gate = df[tem & (df.fric >= FRIC_THR)]
        keep = gate[gate.p <= p_cut]
        print(f"  TEST {tm} | train_n={trm.sum():4d} p_cut={p_cut:.3f}")
        print(f"     gate-only : {stat(gate.net.values)}")
        print(f"     gate+model: {stat(keep.net.values)}")
        r = np.array([x for x in keep.net.values if x is not None])
        if len(r):
            agg.append(r)
    if agg:
        allr = np.concatenate(agg)
        print(f"\n  POOLED gate+model across all test months: {stat(allr)}")
        print(f"  per-month gate+model mean: " +
              "  ".join(f"{tm}={np.mean(a):+.2f}(n{len(a)})" for tm, a in zip(TEST_MONTHS[-len(agg):], agg)))


if __name__ == "__main__":
    main()
