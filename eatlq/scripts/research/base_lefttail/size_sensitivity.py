#!/usr/bin/env python3
"""Capacity / size-sensitivity of the keystone holdout edge. Re-quote the TEST
holdout pools at several notionals: at each size recompute the live friction gate
(bigger size => worse slippage => fewer pools pass) and the trailing-exit net.
This tells us at what capital the convex edge dies."""
from __future__ import annotations
import sys, os, json, math
import numpy as np
sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import (load, build, exit_block, pool_fee, quote, rpc, WETH_BASE,
                           LN1P, HOLD_BLOCKS, ETH_USD)
from sklearn.ensemble import HistGradientBoostingClassifier

SIZES = [100, 300, 1000, 3000]
FRIC_THR = -0.05
SCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_size.json"


def main():
    df, path = load()
    X, y = build(df)
    cut = df.t0.quantile(0.6); tr = (df.t0 <= cut).values; te = ~tr
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                         l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    clf.fit(X[tr], y[tr]); df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]
    # p_cut from TRAIN gated@$100 (reuse prior protocol): approximate with friction cache
    base = json.load(open("/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"))
    df["fric100"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    gtr = df[tr & (df.fric100.notna()) & (df.fric100 >= FRIC_THR)]
    p_cut = gtr.p.quantile(0.6)
    head = int(rpc.call("eth_blockNumber", [], timeout=15), 16)
    # candidate live set = TEST pools that pass model; gate re-checked per size
    cand = df[te & df.fric100.notna() & (df.p <= p_cut)].copy()
    te_days = (cand.t0.max() - cand.t0.min()).days or 1
    print(f"#### SIZE SENSITIVITY | model p_cut(TRAIN)={p_cut:.3f}, friction gate {FRIC_THR} re-checked per size ####")
    print(f"candidate TEST pools (pass model): {len(cand)} over {te_days}d\n")
    cache = json.load(open(SCACHE)) if os.path.exists(SCACHE) else {}
    for sz in SIZES:
        amt = int(sz / ETH_USD * 1e18)
        nets, ngate = [], 0
        for _, r in cand.iterrows():
            key = f"{r.pool}:{sz}"
            if key in cache:
                rec = cache[key]
            else:
                fee = pool_fee(r.pool)
                rec = {"fric": None, "net": None}
                if fee in (100, 500, 3000, 10000):
                    out = quote(WETH_BASE, r.alt, amt, fee, int(r.entry_block))
                    if out and out > 0:
                        back = quote(r.alt, WETH_BASE, out, fee, int(r.entry_block))
                        rec["fric"] = -1.0 if back is None else back / amt - 1.0
                        if rec["fric"] is not None and rec["fric"] >= FRIC_THR:
                            elp = r.last_tick_pre * LN1P * r.orient
                            exb, _ = exit_block(path.get(r.pool, []), elp, r.orient, int(r.entry_block), head)
                            sell = quote(r.alt, WETH_BASE, out, fee, exb)
                            rec["net"] = -1.0 if sell is None else sell / amt - 1.0
                cache[key] = rec
            if rec["fric"] is not None and rec["fric"] >= FRIC_THR and rec["net"] is not None:
                nets.append(rec["net"]); ngate += 1
        json.dump(cache, open(SCACHE, "w"))
        a = np.array(nets)
        nmo = len(a) / te_days * 30
        if len(a):
            print(f"  ${sz:>5}: pass_gate&model n={len(a):3d} (~{nmo:.0f}/mo) "
                  f"mean={a.mean():+.3f} med={np.median(a):+.3f} WR={(a>0).mean():.2f} "
                  f"fatal={(a<=-0.5).mean():.3f} best={a.max():+.2f}  "
                  f"=> @5%size ~{nmo*0.05*a.mean()*100:+.0f}%/mo")
        else:
            print(f"  ${sz:>5}: n=0 (edge gone / nothing passes gate)")


if __name__ == "__main__":
    main()
