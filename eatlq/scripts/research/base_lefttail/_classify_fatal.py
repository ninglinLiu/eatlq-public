#!/usr/bin/env python3
"""Classify the gate+ML holdout FATAL trades by failure type using the real-gas
replay (entry_sell / exit_sell) so we know whether the 3.8% are 'buy-then-honeypot'
or genuine price dumps that were still sellable."""
import json, os, sys
import numpy as np
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
df["fric"] = df.pool.map(lambda x: cache.get(x, {}).get("fric"))
df["net"] = df.pool.map(lambda x: cache.get(x, {}).get("net"))
df["why"] = df.pool.map(lambda x: cache.get(x, {}).get("why"))

trd = df[tr & (df.why == "ok") & (df.fric >= -0.05)]
ted = df[te & (df.why == "ok") & (df.fric >= -0.05)]
p_cut = trd.p.quantile(0.6)
kept = ted[ted.p <= p_cut].copy()
print(f"gate+ML holdout n={len(kept)}  p_cut={p_cut:.3f}")

fatal = kept[kept.net <= -0.5]
print(f"FATAL (net<=-50%) n={len(fatal)}  = {len(fatal)/len(kept)*100:.1f}%")
print()
print(f"{'pool':14}{'net':>8}  {'entry_sell':>11} {'exit_sell':>10}  classification")
cls_count = {}
for _, row in fatal.iterrows():
    r = real.get(row.pool, {})
    es, xs = r.get("entry_sell", "?"), r.get("exit_sell", "?")
    if es == "ok" and xs == "revert":
        c = "buy-then-honeypot (toggled/rug after buy)"
    elif es != "ok":
        c = "UNSELLABLE AT ENTRY (gate should block!)"
    elif xs == "ok":
        c = "price dump but still SELLABLE (real loss)"
    else:
        c = f"other ({es}/{xs})"
    cls_count[c] = cls_count.get(c, 0) + 1
    print(f"{row.pool[:12]:14}{row.net:>+8.3f}  {es:>11} {xs:>10}  {c}")
print("\n--- fatal type breakdown ---")
for c, n in sorted(cls_count.items(), key=lambda x: -x[1]):
    print(f"  {n:3d}  {c}")

# also: how many of ALL holdout (not just fatal) are buy-then-honeypot caught as -100
allbthp = sum(1 for _, row in kept.iterrows()
              if real.get(row.pool, {}).get("entry_sell") == "ok"
              and real.get(row.pool, {}).get("exit_sell") == "revert")
print(f"\nholdout trades that are buy-then-honeypot: {allbthp}/{len(kept)} ({allbthp/len(kept)*100:.1f}%)")
print(f"holdout winners (net>0): {(kept.net>0).sum()}  mean={kept.net.mean():+.3f}")
