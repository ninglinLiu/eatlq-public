#!/usr/bin/env python3
"""Compare LADDER sizing (largest of 100/300/1000 passing friction) vs
CONTINUOUS sizing (same, but additionally capped at 5% of pool WETH depth =
the live executor's max_pool_share). Report money: monthly return, end equity,
max drawdown, on the holdout set. Then layer the active-exposure margin.
"""
from __future__ import annotations
import json, os, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
SIZES = [1000, 300, 100]
MAX_POOL_SHARE = 0.05
ETH_USD = 3000.0
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
    pp = per_pool.get(pool, {})
    s = pp.get("share_1000usd")
    return (1000.0 / s) if s else None


def pick(pool, cap_share=None):
    """Return (size_usd, real_net) for the largest size passing friction (+depth cap)."""
    rusd = reserve_usd(pool)
    for s in SIZES:
        if cap_share is not None and rusd is not None and s > cap_share * rusd:
            continue
        rec = dyn.get(f"{pool}:{s}")
        if not rec:
            continue
        fr, rn = rec.get("fric"), rec.get("real_net")
        if fr is None or rn is None or fr < FRIC:
            continue
        return float(s), float(rn) - GAS_RT / s
    return None, None


def simulate(mode, cap_share, start=1000.0, frac=0.10, cap_usd=1000.0, margin=0.0, seed=0):
    rng = np.random.default_rng(seed)
    base_rug = 4 / max(1, expo.get("n_pools_live", 1))
    trades = []
    for _, r in hold.iterrows():
        s, rn = pick(r.pool, cap_share)
        if s is None:
            continue
        share = (s / 1000.0) * (per_pool.get(r.pool, {}).get("share_1000usd") or 0)
        trades.append((s, rn, share))
    # deterministic historical path
    eq = start; peak = start; mdd = 0.0
    for s, rn, share in trades:
        stake = min(s, cap_usd, eq * frac)
        eq += stake * rn
        peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    monthly = (eq / start) ** (30.0 / days) - 1 if eq > 0 else -1
    sizes = [t[0] for t in trades]
    rets = np.array([t[1] for t in trades])
    mix = {int(s): sizes.count(s) for s in SIZES}
    out = {"mode": mode, "n": len(trades), "mix": mix, "mean": rets.mean(),
           "median": float(np.median(rets)), "fatal": float((rets <= -0.5).mean()),
           "end": eq, "monthly": monthly, "mdd": mdd}
    # exposure-margin Monte Carlo (median end)
    if margin > 0:
        nonfatal = rets[rets > -0.5]
        if len(nonfatal) == 0:
            nonfatal = np.array([0.0])
        ends = []
        for _ in range(2000):
            e = start; pk = start
            for s, rn, share in trades:
                p_rug = base_rug + (margin if share > 0.05 else 0)
                v = -1.0 if rng.random() < p_rug else float(rng.choice(nonfatal))
                st = min(s, cap_usd, e * frac)
                e += st * v; pk = max(pk, e)
                if e <= 0:
                    e = 0; break
            ends.append(e)
        out["mc_median_end"] = float(np.median(ends))
        out["mc_p10_end"] = float(np.quantile(ends, 0.10))
        out["mc_monthly_median"] = (np.median(ends) / start) ** (30.0 / days) - 1
    return out


print(f"holdout span = {days} days, {len(hold)} model-passed pools\n")
print(f"{'mode':28s} {'n':>4} {'mix(1000/300/100)':>18} {'mean':>7} {'fatal':>6} {'end$':>9} {'monthly':>9} {'MDD':>7}")
for mode, cap in [("LADDER 100/300/1000", None), ("CONTINUOUS +5% depth cap", MAX_POOL_SHARE)]:
    o = simulate(mode, cap)
    mix = f"{o['mix'].get(1000,0)}/{o['mix'].get(300,0)}/{o['mix'].get(100,0)}"
    print(f"{o['mode']:28s} {o['n']:>4} {mix:>18} {o['mean']:>+7.2f} {o['fatal']:>6.1%} "
          f"{o['end']:>9,.0f} {o['monthly']:>+9.1%} {o['mdd']:>7.1%}")

print("\n--- CONTINUOUS + active-exposure margin (median of 2000 Monte Carlo paths) ---")
print(f"{'margin':>8} {'mc_median_end$':>14} {'mc_p10_end$':>12} {'mc_monthly(median)':>18}")
for m in (0.02, 0.05, 0.10):
    o = simulate("cont+margin", MAX_POOL_SHARE, margin=m, seed=7)
    print(f"{m:>8.0%} {o['mc_median_end']:>14,.0f} {o['mc_p10_end']:>12,.0f} {o['mc_monthly_median']:>+18.1%}")

# also report $300-capped book for the cautious case
print("\n--- if we CAP position at $300 (more conservative) ---")
o = simulate("CONT cap $300", MAX_POOL_SHARE, cap_usd=300)
mix = f"{o['mix'].get(1000,0)}/{o['mix'].get(300,0)}/{o['mix'].get(100,0)}"
print(f"  n={o['n']} mix={mix} mean={o['mean']:+.2f} end=${o['end']:,.0f} monthly={o['monthly']:+.1%} mdd={o['mdd']:.1%}")
