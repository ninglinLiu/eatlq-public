#!/usr/bin/env python3
"""Capital ceiling: the SAME depth-capped per-trade sizing, but starting from
different capital. Shows that return-on-capital COLLAPSES as capital grows,
because pool depth caps each trade (mostly <= $1000) so big capital sits idle.

This resolves the apparent contradiction: $1000 -> $32k (32x) is only possible
because $1000 is tiny and the depth cap rarely binds. At larger capital the cap
binds every trade and most money is idle."""
from __future__ import annotations
import json, os, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
SIZES = [1000, 300, 100]
MAX_POOL_SHARE = 0.05
GAS_RT = 0.15
HOLD_HOURS = 4.0   # capital locked per trade

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
    s = per_pool.get(pool, {}).get("share_1000usd")
    return (1000.0 / s) if s else None


# Precompute each trade's depth-capped chosen size and net return
trades = []
for _, r in hold.iterrows():
    rusd = reserve_usd(r.pool)
    for s in SIZES:
        if rusd is not None and s > MAX_POOL_SHARE * rusd:
            continue
        rec = dyn.get(f"{r.pool}:{s}")
        if not rec:
            continue
        fr, net = rec.get("fric"), rec.get("real_net")
        if fr is None or net is None or fr < FRIC:
            continue
        trades.append((float(s), float(net) - GAS_RT / s, r.t0))
        break

print(f"holdout {days} days, {len(trades)} depth-capped trades")
print(f"per-trade size cap = min($1000, 5% of pool depth); each trade's return "
      f"already includes that size's QuoterV2 price impact.\n")

# Each trade's max deployable $ = its depth-capped size (independent of our equity).
sizes = np.array([t[0] for t in trades])
rets = np.array([t[1] for t in trades])
print(f"max deployable per trade: mean ${sizes.mean():.0f}  median ${np.median(sizes):.0f}  "
      f"max ${sizes.max():.0f}")
print(f"sum of all per-trade capacity over {days}d = ${sizes.sum():,.0f}\n")

print("=== 不同起始本金下的真实回报 (每笔 = min(深度上限size, 10%权益), 4h锁仓) ===")
print(f"{'起始本金':>10} {'终值':>12} {'总回报':>9} {'月化复利':>10} {'平均每笔$':>10} {'资金利用率':>10}")
for C in [1000, 3000, 10000, 30000, 100000]:
    eq = float(C); peak = eq; mdd = 0
    stakes = []
    util = []
    for s, ret, t0 in trades:
        stake = min(s, eq * 0.10)   # capped by BOTH depth-size and 10% of equity
        stakes.append(stake)
        util.append(stake / eq)
        eq += stake * ret
        peak = max(peak, eq); mdd = min(mdd, eq/peak - 1)
    mult = eq / C
    monthly = mult ** (30/days) - 1
    print(f"${C:>9,} ${eq:>11,.0f} {mult:>8.1f}x {monthly:>+9.0%} "
          f"${np.mean(stakes):>9,.0f} {np.mean(util):>9.1%}")

print("\n=== 同样本金, 但每笔只用固定金额 (不复利, 看纯产能) ===")
for fixed in [100, 300, 1000]:
    # each trade deploys min(fixed, its depth cap); profit not reinvested
    pnl = 0.0
    n = 0
    for s, ret, t0 in trades:
        stake = min(fixed, s)
        pnl += stake * ret
        n += 1
    print(f"  每笔固定 ${fixed:>5}: {days}天总赚 ${pnl:>+8,.0f}  ≈ ${pnl/days*30:>+7,.0f}/月  ({n}笔)")

print("\n关键结论:")
print("- 每笔能投多少由【池子深度】决定(<=5%深度且<=$1000),跟我们有多少钱无关。")
print("- 本金小($1000)时深度上限几乎不咬, 利润能滚进下一笔 -> 高倍数(但绝对金额小)。")
print("- 本金大时, 每笔仍被深度卡在~$1000, 多余的钱闲置 -> 月化%暴跌, 但绝对$更多。")
print("- 所以32倍不是'每月稳定32倍', 是'$1000这种极小本金+复利'的特例。")
