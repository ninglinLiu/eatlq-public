"""Fit the +200% spike classifier per GOAL.md and score the out-of-sample half once.

Hyperparameters are chosen by expanding-window cross-validation inside the
in-sample period only. The out-of-sample period is scored exactly once, at the
end, with the frozen choice.
"""
import json
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

OUT = "/root/base_extreme_select_r0/out"
IS_END = np.datetime64("2026-02-16T00:00:00")
TARGETS = {"+200%": 20000.0, "+50%": 5000.0, "+10%": 1000.0}
N_PICK_OOS = 180                      # about 30 trades a month over six months
REQUIRED = {"+200%": 43.4, "+50%": 50.8, "+10%": 86.4}   # updated for stop-fill failures

FEATS = ["r1", "r4", "r24", "r168", "rv4", "rv24", "rv168", "vr24", "vr168",
         "vr24_168", "sr24", "sr24_168", "lv24", "lns", "pos_hi", "pos_lo",
         "rng", "vwap_gap", "nfr24", "nfr4", "ofi", "ofi24", "age_h", "n_hist",
         "fee", "cost_p50", "tradeable_days", "venue_aero", "venue_univ3"]


def prep():
    df = pd.read_parquet(f"{OUT}/features.parquet")
    df["venue_aero"] = (df["venue"] == "aero_cl").astype(np.float32)
    df["venue_univ3"] = (df["venue"] == "univ3").astype(np.float32)
    # ClickHouse Parquet exports DateTime as uint32 unix seconds
    df["bar"] = pd.to_datetime(df["bar"].astype("int64"), unit="s")
    df = df.sort_values("bar").reset_index(drop=True)
    for c in FEATS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(np.float32)
    df[FEATS] = df[FEATS].replace([np.inf, -np.inf], np.nan)
    return df


def prec_at_k(y, score, k):
    if k <= 0 or len(y) == 0:
        return float("nan")
    idx = np.argsort(-score)[:k]
    return float(y[idx].mean())


def boot_prec(y_top, n=4000, alpha=0.05, seed=11):
    rng = np.random.default_rng(seed)
    m = len(y_top)
    if m == 0:
        return float("nan"), float("nan")
    draws = y_top[rng.integers(0, m, (n, m))].mean(axis=1)
    return tuple(float(x) for x in np.quantile(draws, [alpha / 2, 1 - alpha / 2]))


def main():
    df = prep()
    y200 = (df["net_up_bps"].to_numpy() >= TARGETS["+200%"]).astype(np.int8)
    is_mask = (df["bar"].to_numpy() < IS_END)
    Xis, yis = df.loc[is_mask, FEATS].to_numpy(), y200[is_mask]
    Xoos = df.loc[~is_mask, FEATS].to_numpy()
    print(f"IS {is_mask.sum():,} rows / {yis.sum():,} positives ({100 * yis.mean():.4f}%)")
    print(f"OOS {(~is_mask).sum():,} rows / {y200[~is_mask].sum():,} positives "
          f"({100 * y200[~is_mask].mean():.4f}%)", flush=True)

    # --- expanding-window CV inside IS only ---
    bars_is = df.loc[is_mask, "bar"].to_numpy()
    edges = np.quantile(bars_is.astype("int64"), [0.4, 0.6, 0.8, 1.0]).astype("int64")
    folds = []
    for i in range(3):
        tr = bars_is.astype("int64") < edges[i]
        va = (bars_is.astype("int64") >= edges[i]) & (bars_is.astype("int64") < edges[i + 1])
        if tr.sum() > 1000 and va.sum() > 1000:
            folds.append((tr, va))
    grid = [dict(max_depth=md, n_estimators=ne, min_child_weight=mcw, learning_rate=0.05)
            for md in (4, 6) for ne in (300, 600) for mcw in (10, 50)]
    spw = float((len(yis) - yis.sum()) / max(yis.sum(), 1))
    cv = []
    for hp in grid:
        sc = []
        for tr, va in folds:
            m = XGBClassifier(tree_method="hist", n_jobs=-1, eval_metric="logloss",
                              scale_pos_weight=spw, subsample=0.8, colsample_bytree=0.8,
                              reg_lambda=1.0, random_state=0, **hp)
            m.fit(Xis[tr], yis[tr])
            k = max(int(round(N_PICK_OOS * va.sum() / max((~is_mask).sum(), 1))), 20)
            sc.append(prec_at_k(yis[va], m.predict_proba(Xis[va])[:, 1], k))
        cv.append((float(np.nanmean(sc)), hp, [round(x, 4) for x in sc]))
        print(f"  CV {hp} -> prec@k {cv[-1][0]:.4f}  folds {cv[-1][2]}", flush=True)
    cv.sort(key=lambda x: -x[0])
    best = cv[0][1]
    print(f"\nfrozen hyperparameters: {best}  (IS CV prec@k {cv[0][0]:.4f})", flush=True)

    gbm = XGBClassifier(tree_method="hist", n_jobs=-1, eval_metric="logloss",
                        scale_pos_weight=spw, subsample=0.8, colsample_bytree=0.8,
                        reg_lambda=1.0, random_state=0, **best)
    gbm.fit(Xis, yis)

    med = np.nanmedian(Xis, axis=0)
    sc_ = StandardScaler().fit(np.where(np.isnan(Xis), med, Xis))
    lr = LogisticRegression(C=0.1, max_iter=2000, class_weight="balanced")
    lr.fit(sc_.transform(np.where(np.isnan(Xis), med, Xis)), yis)

    scores = {"gbm": gbm.predict_proba(Xoos)[:, 1],
              "logit": lr.predict_proba(sc_.transform(np.where(np.isnan(Xoos), med, Xoos)))[:, 1]}

    oos = df.loc[~is_mask].reset_index(drop=True)
    res = []
    for mname, sco in scores.items():
        top = np.argsort(-sco)[:N_PICK_OOS]
        for tname, thr in TARGETS.items():
            yt = (oos["net_up_bps"].to_numpy() >= thr).astype(np.int8)
            base = float(yt.mean())
            hit = yt[top]
            p = float(hit.mean())
            lo, hi = boot_prec(hit)
            half = oos["bar"].to_numpy()[len(oos) // 2]
            s1 = hit[oos["bar"].to_numpy()[top] < half]
            s2 = hit[oos["bar"].to_numpy()[top] >= half]
            pc = Counter(oos["pool"].to_numpy()[top][hit == 1])
            conc = (max(pc.values()) / max(hit.sum(), 1)) if pc else 0.0
            res.append(dict(model=mname, target=tname, base_rate=base, prec=p,
                            prec_lo=lo, prec_hi=hi, lift=(p / base if base > 0 else float("nan")),
                            lift_needed=REQUIRED[tname] / 100 / base if base > 0 else float("nan"),
                            required=REQUIRED[tname] / 100,
                            n_pick=len(top), n_hit=int(hit.sum()),
                            half1=float(s1.mean()) if len(s1) else float("nan"),
                            half2=float(s2.mean()) if len(s2) else float("nan"),
                            top_pool_share=float(conc)))
            print(f"{mname:6} {tname:>6}: 精度 {100 * p:6.2f}% [{100 * lo:5.2f},{100 * hi:5.2f}] "
                  f"需 {REQUIRED[tname]:5.1f}%  基础 {100 * base:.4f}%  "
                  f"提升 {p / base if base > 0 else float('nan'):6.1f}x "
                  f"(需 {REQUIRED[tname] / 100 / base:6.0f}x)  命中 {int(hit.sum())}/{len(top)}",
                  flush=True)
    imp = sorted(zip(FEATS, gbm.feature_importances_), key=lambda x: -x[1])[:12]
    print("\n特征重要度前 12:", [(a, round(float(b), 4)) for a, b in imp])
    with open(f"{OUT}/oos.json", "w") as f:
        json.dump(dict(results=res, best=best, cv=[(a, b) for a, b, _ in cv],
                       importance=[(a, float(b)) for a, b in imp]), f, indent=1)


if __name__ == "__main__":
    main()
