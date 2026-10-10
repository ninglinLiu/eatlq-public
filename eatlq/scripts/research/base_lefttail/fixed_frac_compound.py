#!/usr/bin/env python3
"""User's conservative plan: $1000 total, 10 concurrent slots, each trade = 1/10
of CURRENT equity (starts at $100), profit reinvested. NO pool-depth sizing -
every pool gets the same fractional stake. Return for each trade uses the
QuoterV2+real-gas probe AT THE DEPLOYED SIZE (so price impact scales as the
stake grows), and a size-dependent honeypot (real_net None at that size) = -100%.
"""
from __future__ import annotations
import json, os, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
GAS_RT = 0.15

dyn = json.load(open(os.path.join(os.path.dirname(__file__), "_dynamic_size_realgas.json")))

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


def probes(pool):
    pts = []
    for s in (100, 300, 1000):
        rec = dyn.get(f"{pool}:{s}")
        if rec is None:
            continue
        pts.append((s, rec.get("real_net")))
    net_pts = [(s, n) for s, n in pts if n is not None]
    fatal_size = min([s for s, n in pts if n is None], default=None)
    return net_pts, fatal_size


def ret_at(pool, stake):
    net_pts, fatal_size = probes(pool)
    if not net_pts:
        return None
    if fatal_size is not None and stake >= fatal_size:
        return -1.0
    xs = [p[0] for p in net_pts]; ys = [p[1] for p in net_pts]
    n = float(np.interp(stake, xs, ys))   # clamps outside [100,1000]
    return n - GAS_RT / stake


trades = [r.pool for _, r in hold.iterrows() if probes(r.pool)[0]]
print(f"holdout {days} 天, {len(trades)} 笔可成交\n")

# --- A) pure fixed $100, no compound (baseline risk) ---
pnl = 0.0; wins = 0; fatals = 0
for p in trades:
    r = ret_at(p, 100.0)
    if r is None:
        continue
    pnl += 100.0 * r
    wins += r > 0; fatals += r <= -0.999
print("=== A) 纯固定 $100/笔, 不复利 ===")
print(f"  {days}天总赚 ${pnl:+,.0f}  ≈ ${pnl/days*30:+,.0f}/月  胜率 {wins/len(trades):.0%}  "
      f"$100档卡蜜罐 {fatals} 笔")

# --- B) fixed-fraction 1/10 of equity, compound, $1000 start ---
print("\n=== B) 你的方案: $1000本金, 每笔=权益的1/10(起步$100), 10并发, 复利 ===")
for frac, label in [(0.10, "1/10 (10并发)")]:
    eq = 1000.0; peak = eq; mdd = 0; first_month_eq = None
    sizes_used = []
    t0_min = hold.t0.min()
    for (_, r) in hold.iterrows():
        if not probes(r.pool)[0]:
            continue
        stake = eq * frac
        rr = ret_at(r.pool, stake)
        if rr is None:
            continue
        sizes_used.append(stake)
        eq += stake * rr
        peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
        if first_month_eq is None and (r.t0 - t0_min).days >= 30:
            first_month_eq = eq
    mult = eq / 1000.0
    monthly = mult ** (30/days) - 1
    print(f"  {label}: $1000 -> ${eq:,.0f} ({mult:.1f}x)  月化复利 {monthly:+.0%}")
    if first_month_eq:
        print(f"    第1个月末 ≈ ${first_month_eq:,.0f}  (首月 {(first_month_eq/1000-1):+.0%})")
    print(f"    每笔仓位: 起${sizes_used[0]:.0f} -> 末${sizes_used[-1]:.0f}  均${np.mean(sizes_used):.0f}")
    print(f"    最大回撤 {mdd:.0%}")

# --- B2) compound but CAP per-trade size (stay in safe small zone) ---
print("\n=== B2) 复利但单笔封顶 (10%权益, 但不超过上限) — 权衡表 ===")
print(f"  {'单笔上限':>8} {'终值':>10} {'倍数':>6} {'月化':>7} {'首月末':>9} {'最大回撤':>8} {'均仓':>6}")
for cap in [100, 150, 200, 300, 500, 99999]:
    eq = 1000.0; peak = eq; mdd = 0; fm = None; su = []
    t0_min = hold.t0.min()
    for (_, r) in hold.iterrows():
        if not probes(r.pool)[0]:
            continue
        stake = min(eq * 0.10, cap)
        rr = ret_at(r.pool, stake)
        if rr is None:
            continue
        su.append(stake)
        eq += stake * rr
        peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
        if fm is None and (r.t0 - t0_min).days >= 30:
            fm = eq
    mult = eq/1000.0; monthly = mult ** (30/days) - 1
    lbl = "无上限" if cap > 9999 else f"${cap}"
    print(f"  {lbl:>8} ${eq:>9,.0f} {mult:>5.1f}x {monthly:>+6.0%} ${fm or eq:>8,.0f} "
          f"{mdd:>7.0%} ${np.mean(su):>5.0f}")

# --- C) same but with 3% MEV haircut + size-scaled rug, Monte Carlo ---
print("\n=== C) 扣 3% MEV/笔 + 大额额外rug, 400次蒙卡(中位) ===")
rng = np.random.default_rng(0)
plist = [r.pool for _, r in hold.iterrows() if probes(r.pool)[0]]
for frac in [0.10]:
    ends = []; mdds = []; m1 = []
    for _ in range(400):
        eq = 1000.0; peak = eq; mdd = 0; fm = None
        t0_min = hold.t0.min()
        for (_, r) in hold.iterrows():
            if not probes(r.pool)[0]:
                continue
            stake = eq * frac
            rr = ret_at(r.pool, stake)
            if rr is None:
                continue
            if rr > -0.999:
                rr = rr - 0.03
                if rng.random() < 0.06 * (stake / 1000.0):
                    rr = -1.0
            eq += stake * rr
            peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
            if fm is None and (r.t0 - t0_min).days >= 30:
                fm = eq
        ends.append(eq); mdds.append(mdd); m1.append(fm or eq)
    ends = np.array(ends)
    med = np.median(ends); mult = med/1000.0; monthly = mult ** (30/days) - 1
    print(f"  中位 $1000 -> ${med:,.0f} ({mult:.1f}x)  月化 {monthly:+.0%}  "
          f"10分位 ${np.percentile(ends,10):,.0f}  90分位 ${np.percentile(ends,90):,.0f}")
    print(f"  首月末中位 ${np.median(m1):,.0f} ({np.median(m1)/1000-1:+.0%})  最差回撤 {min(mdds):.0%}")
