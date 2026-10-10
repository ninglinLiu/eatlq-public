#!/usr/bin/env python3
"""Reconcile destruction(fatal) rate: gate-only vs gate+ML, cached vs real-gas,
plus fail-open vs fail-closed A/B. Pure-offline (uses cached prices, no RPC)."""
import json, math, sys, os
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sklearn.ensemble import HistGradientBoostingClassifier
import keystone_live as K

df, path = K.load()
X, y = K.build(df)
cut = df.t0.quantile(0.6); tr = (df.t0 <= cut).values; te = ~tr
clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                     l2_regularization=1.0, min_samples_leaf=20, random_state=0)
clf.fit(X[tr], y[tr])
df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]

cache = json.load(open(K.PCACHE))
real = json.load(open(os.path.join(os.path.dirname(K.PCACHE), "_realgas_replay.json")))
df["fee"] = df.pool.map(lambda x: cache.get(x, {}).get("fee"))
df["fric"] = df.pool.map(lambda x: cache.get(x, {}).get("fric"))
df["net"] = df.pool.map(lambda x: cache.get(x, {}).get("net"))
df["why"] = df.pool.map(lambda x: cache.get(x, {}).get("why"))
def realnet(p):
    r = real.get(p)
    if not r or r.get("status") != "ok":
        return None
    return r.get("real_net")  # None if unverifiable
def verifiable(p):
    r = real.get(p)
    return bool(r and r.get("status") == "ok" and r.get("real_net") is not None)
df["real_net"] = df.pool.map(realnet)
df["verifiable"] = df.pool.map(verifiable)

te_days = (df[te].t0.max() - df[te].t0.min()).days or 1
full_days = (df.t0.max() - df.t0.min()).days or 1
print(f"data window: {df.t0.min().date()} -> {df.t0.max().date()}  ({full_days}d)  TEST={te_days}d")

def rep(tag, sub, col="net"):
    a = sub[col].dropna().values.astype(float)
    if len(a) == 0:
        print(f"  {tag:34s} n=0"); return
    nmo = len(sub) / te_days * 30
    print(f"  {tag:34s} n={len(a):4d} mean={a.mean():+.3f} med={np.median(a):+.3f} "
          f"WR={(a>0).mean():.2f} FATAL(<=-50%)={(a<=-0.5).mean():.3f} "
          f"~{nmo:.0f} trades/mo")

ok = df[df.why == "ok"]
print("\n================ DESTRUCTION-RATE RECONCILIATION ================")
for thr in (-0.05,):
    trd = df[tr & (df.why == "ok") & (df.fric >= thr)]
    ted = df[te & (df.why == "ok") & (df.fric >= thr)]
    p_cut = trd.p.quantile(0.6)
    print(f"\n--- friction>= {thr}, p_cut(TRAIN q60)={p_cut:.3f} ---")
    print(" [GATE-ONLY]  (no ML filter)")
    rep("TEST gate-only  CACHED", ted, "net")
    rep("TEST gate-only  REAL-GAS", ted, "real_net")
    kept = ted[ted.p <= p_cut]
    print(" [GATE + ML]  (the actual strategy)")
    rep("TEST gate+ML  CACHED  (HOLDOUT)", kept, "net")
    rep("TEST gate+ML  REAL-GAS(HOLDOUT)", kept, "real_net")

    # fail-open vs fail-closed on the ML-gated holdout
    print(" [FAIL-OPEN vs FAIL-CLOSED]  on gate+ML holdout")
    fo = kept                              # fail-open: keep unverifiable (use cached)
    fc = kept[kept.verifiable]             # fail-closed: drop unverifiable
    n_unver = len(kept) - len(fc)
    rep(f"fail-OPEN  (keep {n_unver} unverif.)", fo, "net")
    rep("fail-CLOSED (drop unverifiable)", fc, "net")
    if n_unver:
        unv = kept[~kept.verifiable]
        print(f"   unverifiable trades' cached net: {list(np.round(unv.net.dropna().values,2))}")
