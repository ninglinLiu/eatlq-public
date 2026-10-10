#!/usr/bin/env python3
"""READ-ONLY entry-latency sensitivity overlay on the honest sellability EV.

The strategy already enters LATE by design (pool age 10-15 min). Detection /
execution lag pushes entry even later. This measures how per-trade return decays
if entry slips d forward buckets (10 min each) past the intended entry, using the
realized forward tick path for both the delayed entry price and the causal
trailing exit, with the fork-replay rug haircut applied.
"""
from __future__ import annotations
import glob, json, math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
GAS_FRAC = 0.30 / 100.0

labels = {}
for f in glob.glob(os.path.join(HERE, "_uni_shards/_uni_s*.json")):
    for r in json.load(open(f))["pools"]:
        labels[r["pool"]] = r
priced = json.load(open(K.PCACHE))
ub = json.load(open(os.path.join(HERE, "_universe_blocks.json")))
traded = set(r["pool"] for r in ub["rows"] if r.get("in_traded_131_217"))


def rug_before(pool, exb):
    lab = labels.get(pool)
    if not lab:
        return False
    if lab.get("label") in ("dead", "instant_hp"):
        return True
    if lab.get("label") == "delayed_rug":
        fb = lab.get("flip_block")
        return exb is not None and fb is not None and fb <= exb
    return False


def delayed_return(df_row, path_steps, delay_buckets):
    """Return net% entering `delay_buckets` (10-min) into the forward path."""
    o = df_row.orient
    if not path_steps:
        return None
    steps = sorted(path_steps)
    # find entry step at delay
    idx = None
    for i, (bkt, lt, lb) in enumerate(steps):
        if bkt >= delay_buckets:
            idx = i
            break
    if idx is None:
        return None  # pool died before this delay -> we could not enter
    entry_bkt, entry_tick, entry_block = steps[idx]
    entry_lp = entry_tick * K.LN1P * o
    exb, _why = K.exit_block(steps[idx:], entry_lp, o, entry_block, steps[-1][2])
    # exit price = tick at exb
    exit_lp = entry_lp
    for bkt, lt, lb in steps[idx:]:
        if lb <= exb:
            exit_lp = lt * K.LN1P * o
    if rug_before(df_row.pool, exb):
        return -1.0
    return math.exp(exit_lp - entry_lp) - 1.0 - GAS_FRAC


def main():
    df, path = K.load()
    df = df[df.pool.isin(traded)].copy()
    print(f"# entry-latency curve on {len(df)} traded pools (10-min buckets, honest rug haircut)")
    print(f"{'entry delay':<16}{'n':>5}{'mean':>9}{'median':>9}{'WR':>7}{'fatal':>8}")
    for d in (0, 1, 2, 3, 6):
        rets = []
        for r in df.itertuples(index=False):
            v = delayed_return(r, path.get(r.pool, []), d)
            if v is not None:
                rets.append(v)
        a = np.array(rets)
        lbl = f"+{d*10}min late"
        print(f"{lbl:<16}{len(a):>5}{a.mean():>+9.3f}{np.median(a):>+9.3f}{(a>0).mean():>7.2f}{(a<=-0.5).mean():>8.3f}")

    # exit-latency risk: how fast do rugs flip? (blocks after flip_interval start)
    print("\n# rug flip-speed (delayed_rug flip_offset in blocks; Base ~2s/block)")
    from collections import Counter
    fo = Counter(labels[p].get("flip_offset") for p in traded
                 if labels.get(p, {}).get("label") == "delayed_rug")
    for off in sorted(k for k in fo if k is not None):
        print(f"  flip_offset={off:>5} blocks (~{off*2/60:.0f} min): {fo[off]} pools")


if __name__ == "__main__":
    main()
