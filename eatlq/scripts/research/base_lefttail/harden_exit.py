#!/usr/bin/env python3
"""Hardening #2 — exit-rule robustness. Pre-quote the full forward SELL curve
(real QuoterV2 sell at every forward-bucket block) for gated pools once, then
evaluate many exit rules offline on the holdout. Confirms the edge is not an
artifact of the single TRAIL=0.35 choice."""
from __future__ import annotations
import sys, json, math, os
import numpy as np
sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import (load, build, pool_fee, quote, rpc, WETH_BASE, LN1P,
                           HOLD_BLOCKS, NOTIONAL_USD, ETH_USD)
from sklearn.ensemble import HistGradientBoostingClassifier

FRIC_THR = -0.05
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
SELLCURVE = "/opt/EATLQ/scripts/research/base_lefttail/_sellcurve.json"
HARD_STOP = math.log(0.5)


def sim_exit(steps, entry_lp, orient, entry_block, head, trail=None, hold_only=False, stop_only=False):
    """Return exit block per rule (causal)."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head)
    peak = entry_lp
    for bkt, ltick, lblock in steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock
        if (not hold_only) and (not stop_only) and trail is not None and (lp - peak) <= math.log(1 - trail):
            return lblock
    return steps[-1][2]


def main():
    df, path = load()
    X, y = build(df)
    cut = df.t0.quantile(0.6); tr = (df.t0 <= cut).values; te = ~tr
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                         l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    clf.fit(X[tr], y[tr]); df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]
    base = json.load(open(PCACHE))
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    gated = df[df.fric.notna() & (df.fric >= FRIC_THR)].copy()
    p_cut = df[tr & df.fric.notna() & (df.fric >= FRIC_THR)].p.quantile(0.6)
    head = int(rpc.call("eth_blockNumber", [], timeout=15), 16)
    amt = int(NOTIONAL_USD / ETH_USD * 1e18)
    curve = json.load(open(SELLCURVE)) if os.path.exists(SELLCURVE) else {}
    # pre-quote sell at every needed block for gated pools
    need = []
    for _, r in gated.iterrows():
        blocks = set(s[2] for s in path.get(r.pool, []))
        blocks.add(min(int(r.entry_block) + HOLD_BLOCKS, head))
        blocks.add(int(r.entry_block))
        for b in blocks:
            if f"{r.pool}:{b}" not in curve:
                need.append((r.pool, r.alt, int(r.entry_block), b))
    print(f"gated pools={len(gated)}  sell-quotes to fetch={len(need)}")
    buy_cache = {}
    for i, (pool, alt, eb, b) in enumerate(need):
        fee = pool_fee(pool)
        if fee not in (100, 500, 3000, 10000):
            curve[f"{pool}:{b}"] = None; continue
        if pool not in buy_cache:
            out = quote(WETH_BASE, alt, amt, fee, eb)
            buy_cache[pool] = out
        out = buy_cache[pool]
        if not out:
            curve[f"{pool}:{b}"] = None; continue
        sell = quote(alt, WETH_BASE, out, fee, b)
        curve[f"{pool}:{b}"] = (sell / amt - 1.0) if sell else -1.0
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(need)} rpc={rpc.calls}", flush=True)
            json.dump(curve, open(SELLCURVE, "w"))
    json.dump(curve, open(SELLCURVE, "w"))

    def net_for(r, rule):
        elp = r.last_tick_pre * LN1P * r.orient
        eb = int(r.entry_block)
        xb = sim_exit(path.get(r.pool, []), elp, r.orient, eb, head, **rule)
        return curve.get(f"{r.pool}:{xb}")

    rules = {"trail0.25": {"trail": 0.25}, "trail0.35": {"trail": 0.35},
             "trail0.50": {"trail": 0.50}, "hold_4h": {"hold_only": True},
             "stop_only": {"stop_only": True}}
    holdout = df[te & df.fric.notna() & (df.fric >= FRIC_THR)]
    keep = holdout[holdout.p <= p_cut]
    print(f"\n#### EXIT-RULE ROBUSTNESS | HOLDOUT gate+model n={len(keep)} (p_cut={p_cut:.3f}) ####")
    for name, rule in rules.items():
        r = np.array([v for v in (net_for(row, rule) for _, row in keep.iterrows()) if v is not None])
        if len(r) == 0:
            print(f"  {name:11s} n=0"); continue
        top1 = np.sort(r)[-1] / r[r > 0].sum() if (r > 0).any() else 0
        print(f"  {name:11s} n={len(r):3d} mean={r.mean():+.3f} med={np.median(r):+.3f} "
              f"WR={(r>0).mean():.2f} fatal={(r<=-0.5).mean():.3f} best={r.max():+.2f} top1share={top1:.2f}")


if __name__ == "__main__":
    main()
