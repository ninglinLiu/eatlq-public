#!/usr/bin/env python3
"""TRUE continuous dynamic sizing, compounded from $1000, single-trade cap $1000.

size_per_trade = min( $1000 abs cap,
                      available cash,
                      5% * pool_depth_usd,
                      size where friction == -5% )   <- binary-search equivalent

If the chosen size falls at/above a size where the real-gas SELL fails
(real_net is None), that trade is treated as a TOTAL LOSS (-100%), i.e. a
size-dependent honeypot. Returns at the chosen size are linearly interpolated
from the $100/$300/$1000 QuoterV2+real-gas probes (price impact is ~linear)."""
from __future__ import annotations
import json, os, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
MAX_POOL_SHARE = 0.05
ABS_CAP = 1000.0
MIN_SIZE = 100.0
GAS_RT = 0.15  # round-trip gas $

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


def pool_curve(pool):
    """Return (reserve_usd, fric_a, fric_b, net_points, fatal_size)."""
    pts = []
    for s in (100, 300, 1000):
        rec = dyn.get(f"{pool}:{s}")
        if rec is None:
            continue
        pts.append((s, rec.get("fric"), rec.get("real_net")))
    if not pts:
        return None
    # reserve from share
    pp = per_pool.get(pool, {})
    s100 = pp.get("share_100usd")
    reserve = (100.0 / s100) if s100 else None
    # linear fric model from extreme points that have fric
    fpts = [(s, f) for s, f, _ in pts if f is not None]
    if len(fpts) >= 2:
        (s0, f0), (s1, f1) = fpts[0], fpts[-1]
        b = (f1 - f0) / (s1 - s0) if s1 != s0 else 0.0
        a = f0 - b * s0
    else:
        a, b = (fpts[0][1] if fpts else 0.0), 0.0
    net_pts = [(s, n) for s, _, n in pts if n is not None]
    fatal_size = min([s for s, _, n in pts if n is None], default=None)
    return reserve, a, b, net_pts, fatal_size


def fric_capped_size(a, b):
    if b >= 0:
        return ABS_CAP
    s = (FRIC - a) / b
    return max(MIN_SIZE, min(ABS_CAP, s))


def net_at(net_pts, s):
    if not net_pts:
        return None
    xs = [p[0] for p in net_pts]; ys = [p[1] for p in net_pts]
    return float(np.interp(s, xs, ys))


# Precompute per-trade continuous size + return (independent of capital)
trades = []
for _, r in hold.iterrows():
    cur = pool_curve(r.pool)
    if cur is None:
        continue
    reserve, a, b, net_pts, fatal_size = cur
    caps = [ABS_CAP, fric_capped_size(a, b)]
    if reserve:
        caps.append(MAX_POOL_SHARE * reserve)
    cont_size = min(caps)
    if cont_size < MIN_SIZE:
        continue
    # size-dependent honeypot: chosen size >= fatal size => total loss
    if fatal_size is not None and cont_size >= fatal_size:
        ret = -1.0
    else:
        n = net_at(net_pts, cont_size)
        if n is None:
            continue
        ret = n - GAS_RT / cont_size
    trades.append((cont_size, ret, r.t0, reserve))

sizes = np.array([t[0] for t in trades])
rets = np.array([t[1] for t in trades])
print(f"holdout {days} 天, {len(trades)} 笔 (真·连续动态仓位)\n")

print("=== 真实连续动态仓位分布 (每笔 = min($1000, 现金, 5%池深, 摩擦≤5%)) ===")
print(f"  均值仓位 ${sizes.mean():.0f}   中位 ${np.median(sizes):.0f}   "
      f"最小 ${sizes.min():.0f}   最大 ${sizes.max():.0f}")
for lo, hi in [(100,200),(200,400),(400,700),(700,1000),(1000,1001)]:
    m = (sizes>=lo)&(sizes<hi)
    if m.sum():
        print(f"  ${lo:>4}-{hi-1 if hi<1001 else 1000:>4}: {m.sum():>3} 笔 ({m.mean()*100:>4.0f}%)  "
              f"均收益 {rets[m].mean():+.2f}")
fatal = (rets <= -0.999).sum()
print(f"  其中大额卡蜜罐(全损)的笔数 = {fatal}")

print("\n=== 复利: 起$1000, 单笔上限$1000, 用真实连续仓位 ===")
for C in [1000, 3000, 10000]:
    eq = float(C); peak = eq; mdd = 0
    used = []
    for s, ret, t0, res in trades:
        stake = min(s, eq)          # cash-constrained early; s already <= $1000
        used.append(stake)
        eq += stake * ret
        peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
    mult = eq / C
    monthly = mult ** (30/days) - 1
    print(f"  起${C:>6,} -> ${eq:>10,.0f}  ({mult:>4.1f}x)  月化复利 {monthly:>+5.0%}  "
          f"均每笔 ${np.mean(used):>5,.0f}  最大回撤 {mdd:.0%}")

print("\n=== 对照: 不复利, 每笔就投这个连续仓位(利润不回滚) ===")
pnl = sum(s*ret for s,ret,_,_ in trades)
print(f"  {days}天总盈亏 ${pnl:+,.0f}  ≈ ${pnl/days*30:+,.0f}/月   (均仓 ${sizes.mean():.0f})")

print("\n=== 扣减后(现实): 3% MEV/笔 + 大额额外rug(每$1000仓位 +6%毁灭) 400次蒙卡 ===")
rng = np.random.default_rng(0)
for C in [1000, 3000, 10000, 30000]:
    ends = []; mdds = []
    for _ in range(400):
        eq = float(C); peak = eq; mdd = 0
        for s, ret, t0, res in trades:
            stake = min(s, eq)
            r = ret
            if r > -0.999:
                r = r - 0.03                      # sandwich MEV
                if rng.random() < 0.06 * (s / 1000.0):  # size-scaled butterfly rug
                    r = -1.0
            eq += stake * r; peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
        ends.append(eq); mdds.append(mdd)
    ends = np.array(ends)
    med = np.median(ends); mult = med / C; monthly = mult ** (30/days) - 1
    print(f"  起${C:>6,} -> 中位${med:>9,.0f} ({mult:>4.1f}x) 月化{monthly:>+5.0%}  "
          f"10分位${np.percentile(ends,10):>8,.0f}  最差回撤{min(mdds):.0%}")

print("\n=== 头几笔真实例子: 池深$, 连续选定size$, 该笔收益 ===")
for s, ret, t0, res in trades[:10]:
    rs = f"${res:,.0f}" if res else "n/a"
    cap = "深度卡" if res and abs(s-0.05*res)<1 else ("$1000上限" if s>=999 else "摩擦/上限")
    print(f"  池深 {rs:>10}  选 ${s:>6,.0f}  ({cap})  收益 {ret:+.2f}")
