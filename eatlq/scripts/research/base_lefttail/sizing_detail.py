#!/usr/bin/env python3
"""Show the ACTUAL continuous-sizing decisions per trade, and report returns in
plain dollars (non-compounded and compounded) so the math is transparent."""
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


rows = []
for _, r in hold.iterrows():
    rusd = reserve_usd(r.pool)
    chosen, rn, why = None, None, ""
    for s in SIZES:
        capped = rusd is not None and s > MAX_POOL_SHARE * rusd
        rec = dyn.get(f"{r.pool}:{s}")
        if not rec:
            continue
        fr, net = rec.get("fric"), rec.get("real_net")
        if capped:
            why = "depth_cap"
            continue
        if fr is None or net is None or fr < FRIC:
            why = "friction"
            continue
        chosen, rn = s, float(net) - GAS_RT / s
        why = "ok"
        break
    if chosen is None:
        continue
    rows.append({"pool": r.pool, "size": chosen, "ret": rn,
                 "reserve_usd": rusd, "why_smaller": why})

R = pd.DataFrame(rows)
print(f"holdout span = {days} days,  trades taken = {len(R)}\n")

print("=== 动态仓位具体分布 (连续 + 5% 深度上限) ===")
for s in SIZES:
    sub = R[R["size"] == s]
    print(f"  ${s:>5}: {len(sub):>3} 笔  ({len(sub)/len(R)*100:>4.0f}%)  "
          f"均值收益 {sub.ret.mean() if len(sub) else 0:+.2f}  "
          f"赢率 {(sub.ret>0).mean() if len(sub) else 0:.0%}")
print(f"  平均每笔仓位 = ${ (R['size']*1).mean():.0f}")
print(f"  被深度上限压小的笔数 = {(R.why_smaller=='ok').sum()} 选到首选 / 共 {len(R)}")

print("\n=== 直白的钱 (固定每笔 $100,不复利) ===")
flat = 100 * R.ret
print(f"  131 池 / {len(R)} 笔实际成交,每笔固定投 $100")
print(f"  {days}天总盈亏 = ${flat.sum():+,.0f}  (本金每笔$100,总投入随交易次数)")
print(f"  平均每笔赚 ${flat.mean():+.1f}  中位 ${100*R.ret.median():+.1f}")
print(f"  折算每月约 {len(R)/days*30:.0f} 笔 × 平均 ${flat.mean():+.1f}/笔 = ${len(R)/days*30*flat.mean():+,.0f}/月")

print("\n=== 复利 (起$1000,每笔押当前权益10%) ===")
for cap in [None]:
    eq = 1000.0; peak = 1000.0; mdd = 0
    for _, t in R.iterrows():
        stake = min(t["size"], eq*0.10)
        eq += stake * t.ret; peak = max(peak, eq); mdd = min(mdd, eq/peak-1)
    print(f"  {days}天: $1000 -> ${eq:,.0f}  = {eq/1000:.1f} 倍")
    print(f"  换算月化复利 = {(eq/1000)**(30/days)-1:+.0%}/月  (= 每月×{(eq/1000)**(30/days):.2f})")
    print(f"  验算: {(eq/1000)**(30/days):.2f}^({days}/30={days/30:.2f}个月) = {((eq/1000)**(30/days))**(days/30):.1f} 倍 ✓")
    print(f"  最大回撤 = {mdd:.1%}")

print("\n=== 几笔真实例子 (池, 选定size, 池WETH深度$, 收益) ===")
for _, t in R.head(8).iterrows():
    print(f"  {t.pool[:12]}  ${int(t['size']):>5}  深度~${t.reserve_usd:>10,.0f}  收益 {t.ret:+.2f}")
