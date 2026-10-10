#!/usr/bin/env python3
"""READ-ONLY offline gate-threshold sweep + safer-variant comparison on the 644
labeled universe (clean / delayed_rug ground truth + flip blocks) and the
real-gas walk-forward return series. No env/strategy/sizing changes.

Per-pool inputs (all already computed; this script only filters & aggregates):
  ns_pre (MIN_PRE feature), p (p_fatal model), fric/why (friction gate),
  label+mechanism+flip_offset (fork-replay ground truth), entry WETH depth
  (archive), senders_pre/buyers_pre (social proof proxy), real-gas net (PnL).
"""
from __future__ import annotations
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import risk_rebacktest as R  # noqa

GAS = R.GAS_USD_RT
HAIRCUT = 0.02
SIZE = 100.0
START, CAP, FRAC = 10000.0, 1000.0, 0.10


def load_all() -> pd.DataFrame:
    df, _ = K.load()
    clf, X, tr, te = R.fit_model(df)
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    _, y = K.build(df)
    df["y_fatal"] = y
    df["is_test"] = te
    priced = json.load(open(K.PCACHE))
    realgas = json.load(open(os.path.join(HERE, "_realgas_replay.json")))
    ewd = json.load(open(os.path.join(HERE, "_entry_weth_644.json")))
    labels = {}
    for f in glob.glob(os.path.join(HERE, "_uni_shards/_uni_s*.json")):
        for r in json.load(open(f)).get("pools", []):
            labels[r["pool"]] = r
    df = df[df.pool.isin(labels)].copy()  # restrict to 644 labeled

    def ret_of(pool):
        pr = priced.get(pool) or {}
        if pr.get("why") != "ok":
            return None, None
        fric = pr.get("fric")
        if fric is None or fric < R.FRIC_THR:
            return None, fric
        raw = (realgas.get(pool) or {}).get("real_net", pr.get("net"))
        if raw is None:
            return None, fric
        return float(raw) - GAS / SIZE - HAIRCUT, fric

    df["label"] = df.pool.map(lambda p: labels[p]["label"])
    df["mechanism"] = df.pool.map(lambda p: labels[p].get("mechanism"))
    df["flip_offset"] = df.pool.map(lambda p: labels[p].get("flip_offset"))
    df["entry_weth"] = df.pool.map(lambda p: (ewd.get(p) or 0) / 1e18)
    rr = df.pool.map(ret_of)
    df["ret"] = [x[0] for x in rr]
    df["fric"] = [x[1] for x in rr]
    df["tradeable"] = df.ret.notna()
    return df


def simulate_monthly(sub: pd.DataFrame):
    sub = sub.sort_values("t0")
    eq, peak, mdd = START, START, 0.0
    for r in sub.itertuples(index=False):
        stake = min(SIZE, CAP, eq * FRAC)
        eq += stake * r.ret
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1.0)
    if len(sub) < 2:
        return 0.0, 0.0
    span = max((sub.t0.max() - sub.t0.min()).days, 1)
    monthly = (eq / START) ** (30.0 / span) - 1.0 if eq > 0 else -1.0
    return monthly, mdd


def metrics(df: pd.DataFrame, traded_mask) -> dict:
    base_cw = (df.label == "clean") & df.tradeable & (df.ret > 0)
    n_cw = int(base_cw.sum())
    dr = df.label == "delayed_rug"
    fast = dr & (df.flip_offset == 30)
    tr = traded_mask & df.tradeable
    t = df[tr]
    blocked_cw = base_cw & ~traded_mask
    monthly, mdd = simulate_monthly(t)
    return {
        "pass_n": int(tr.sum()),
        "dr_traded": int((tr & dr).sum()),
        "dr_recall": (int((dr & ~traded_mask).sum())) / max(int(dr.sum()), 1),
        "fast_total": int(fast.sum()),
        "fast_traded": int((tr & fast).sum()),
        "fast_recall": (int((fast & ~traded_mask).sum())) / max(int(fast.sum()), 1),
        "miskill_winners": int(blocked_cw.sum()),
        "miskill_pnl_per100": float((df[blocked_cw].ret * SIZE).sum()),
        "n_cw": n_cw,
        "monthly": monthly,
        "mdd": mdd,
        "fatal_rate": float((t.ret <= -0.5).mean()) if len(t) else 0.0,
        "mean_ret": float(t.ret.mean()) if len(t) else 0.0,
    }


def fmt(m):
    return (f"pass={m['pass_n']:3d} dr_traded={m['dr_traded']:3d} dr_recall={m['dr_recall']:5.1%} "
            f"fast_traded={m['fast_traded']}/{m['fast_total']} "
            f"miskill={m['miskill_winners']:3d}(-${-m['miskill_pnl_per100']:6.0f}/100) "
            f"monthly={m['monthly']:+7.1%} mdd={m['mdd']:6.1%} fatal={m['fatal_rate']:.1%}")


def main():
    df = load_all()
    P_CUT0 = 0.27
    print(f"# loaded {len(df)} labeled pools | tradeable={int(df.tradeable.sum())} "
          f"| delayed_rug={int((df.label=='delayed_rug').sum())} clean={int((df.label=='clean').sum())}")

    print("\n## PART 1: MIN_PRE sweep (p_cut=0.27)")
    for mp in [5, 6, 7, 8, 9, 10, 12, 15]:
        mask = (df.ns_pre >= mp) & (df.p <= P_CUT0)
        print(f"MIN_PRE>={mp:2d}: {fmt(metrics(df, mask))}")

    print("\n## PART 1b: fast-rug (flip<=+30) ns_pre distribution (does raising MIN_PRE help?)")
    fast = df[(df.label == "delayed_rug") & (df.flip_offset == 30)]
    print("  fast-rug ns_pre values:", sorted(fast.ns_pre.tolist()))
    dr = df[df.label == "delayed_rug"]
    print(f"  delayed_rug ns_pre: median={dr.ns_pre.median():.0f} p25={dr.ns_pre.quantile(.25):.0f} p75={dr.ns_pre.quantile(.75):.0f}")
    cln = df[df.label == "clean"]
    print(f"  clean       ns_pre: median={cln.ns_pre.median():.0f} p25={cln.ns_pre.quantile(.25):.0f} p75={cln.ns_pre.quantile(.75):.0f}")

    print("\n## PART 2: p_fatal sweep (MIN_PRE=5)")
    for pc in [0.27, 0.20, 0.15, 0.10, 0.07, 0.05]:
        mask = (df.ns_pre >= 5) & (df.p <= pc)
        m = metrics(df, mask)
        # split recall: lp_pull (撤池型) vs price-death (价格猝死型 = y_fatal)
        lp = (df.label == "delayed_rug") & (df.mechanism == "lp_pull")
        pd_ = (df.y_fatal == 1)
        lp_recall = int((lp & ~mask).sum()) / max(int(lp.sum()), 1)
        pd_recall = int((pd_ & ~mask).sum()) / max(int(pd_.sum()), 1)
        print(f"p_cut<={pc:.2f}: {fmt(m)} | lp_pull_recall={lp_recall:5.1%} pricedeath_recall={pd_recall:5.1%}")

    print("\n## PART 3: MIN_PRE x p_cut grid (cell = dr_recall / monthly)")
    mps = [5, 7, 10, 15]
    pcs = [0.27, 0.15, 0.10, 0.05]
    hdr = "MIN_PRE\\p_cut |" + "".join(f" {pc:>14.2f}" for pc in pcs)
    print(hdr)
    grid = {}
    for mp in mps:
        cells = []
        for pc in pcs:
            mask = (df.ns_pre >= mp) & (df.p <= pc)
            m = metrics(df, mask)
            grid[(mp, pc)] = m
            cells.append(f" {m['dr_recall']:5.1%}/{m['monthly']:+6.1%}")
        print(f"  >={mp:2d}        |" + "".join(cells))

    print("\n## PART 4: safer-variant comparison")
    v0 = (df.ns_pre >= 5) & (df.p <= P_CUT0)
    print(f"V0  current (MP5,p0.27)        : {fmt(metrics(df, v0))}")
    # V-A LP-lock ORACLE upper bound: exclude all lp_pull rugs (perfect lock detection)
    va = v0 & ~((df.label == "delayed_rug") & (df.mechanism == "lp_pull"))
    print(f"V-A LP-lock oracle (no lp_pull) : {fmt(metrics(df, va))}")
    # V-B social proof: require N independent sellers in pre-window (senders_pre proxy)
    for n in [1, 2, 3]:
        vb = v0 & (df.senders_pre >= n)
        print(f"V-B social proof senders>={n}     : {fmt(metrics(df, vb))}")
    # V-C depth floor
    for d in [3, 5, 10]:
        vc = v0 & (df.entry_weth >= d)
        print(f"V-C depth>= {d:2d} ETH              : {fmt(metrics(df, vc))}")

    # save machine-readable
    out = {"part4": {
        "V0": metrics(df, v0),
        "V-A_lp_oracle": metrics(df, va),
        **{f"V-B_n{n}": metrics(df, v0 & (df.senders_pre >= n)) for n in [1, 2, 3]},
        **{f"V-C_d{d}": metrics(df, v0 & (df.entry_weth >= d)) for d in [3, 5, 10]},
    }}
    json.dump(out, open(os.path.join(HERE, "_gate_sweep.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
