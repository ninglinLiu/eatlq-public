#!/usr/bin/env python3
"""Does a $100 floor still trade the bad shallow cohort? Compare staking a FIXED
$100 but with different DEPTH-ELIGIBILITY thresholds (pool must support $100 vs
$200 vs $300 at 5% depth). Return at $100 size comes from the real-gas probe."""
from __future__ import annotations
import json, os, sys
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
GAS_RT = 0.15
MAX_POOL_SHARE = 0.05

dyn = json.load(open(os.path.join(os.path.dirname(__file__), "_dynamic_size_realgas.json")))
expo = json.load(open(os.path.join(os.path.dirname(__file__), "_active_exposure.json")))
per_pool = expo.get("per_pool", {})

df, _ = K.load(); X, y = K.build(df)
cut = df.t0.quantile(.6); tr = (df.t0 <= cut).values; te = ~tr
clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=.05,
    l2_regularization=1.0, min_samples_leaf=20, random_state=0).fit(X[tr], y[tr])
df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]
pl = json.load(open(K.PCACHE))
df["fric"] = df.pool.map(lambda p: pl.get(p, {}).get("fric"))
df["why"] = df.pool.map(lambda p: pl.get(p, {}).get("why"))
pc = df[tr & (df.why == "ok") & df.fric.notna() & (df.fric >= FRIC)].p.quantile(.6)
hold = df[te & (df.why == "ok") & df.fric.notna() & (df.fric >= FRIC) & (df.p <= pc)].copy().sort_values("t0")
days = max((hold.t0.max() - hold.t0.min()).days, 1)


def reserve_usd(pool):
    s = per_pool.get(pool, {}).get("share_100usd")
    return (100.0 / s) if s else None


def net100(pool):
    rec = dyn.get(f"{pool}:100")
    if not rec:
        return None
    n = rec.get("real_net")
    if n is None:
        return -1.0  # $100 sell sim failed = honeypot at $100
    return float(n) - GAS_RT / 100.0


rows = []
for _, r in hold.iterrows():
    res = reserve_usd(r.pool)
    n = net100(r.pool)
    if res is None or n is None:
        continue
    rows.append((res, n))

res_arr = np.array([x[0] for x in rows])
ret_arr = np.array([x[1] for x in rows])

print(f"holdout {days}d, {len(rows)} pools with $100 data\n")
print("固定每笔 $100,但只交易'5%深度能撑得起 X'的池子:")
print(f"{'门槛X':>8} {'reserve>=':>10} {'笔数':>5} {'均收益':>8} {'胜率':>6} {'全损笔':>6} {'每月$(不复利)':>12}")
for qualify in [100, 200, 300, 500]:
    need_reserve = qualify / MAX_POOL_SHARE   # 5% depth must cover qualify
    m = res_arr >= need_reserve
    if m.sum() == 0:
        print(f"  ${qualify:>5} {need_reserve:>10.0f}  0 笔"); continue
    sub = ret_arr[m]
    monthly = sub.sum() * 100 / days * 30  # $100/trade, non-compounded
    print(f"  ${qualify:>5} ${need_reserve:>9,.0f} {m.sum():>5} "
          f"{sub.mean():>+7.2f} {(sub>0).mean():>5.0%} {(sub<=-0.999).sum():>5} "
          f"${monthly:>+11,.0f}")
