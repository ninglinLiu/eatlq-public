#!/usr/bin/env python3
"""Is the survivor set just 'the highest fee tiers', i.e. an ex-ante specifiable rule?

s10 killed the survivors as a symbol-level selection: the count of positive cells is
below the sign-flip null and no single cell clears multiplicity. But 'POKT pays' and
'pools with fee >= X pay' are completely different claims. The first needs the symbol
name, which nothing available on 2025-08-17 would have supplied. The second is a rule
you can write down in advance and apply to the whole cross-section.

So bucket the entire panel by the pool's fee tier and test the rule directly, with the
same day-block bootstrap. A high-fee bucket that pays across many symbols in both halves
is a real lead; one that pays only through the four survivor symbols is not.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_condmm_r0/src")
import s02_test as m

OUT = Path("/root/base_condmm_r0/out")
SURVIVOR_SYMS = {"POKT", "doginme", "TOSHI", "PRIME"}


def main():
    uni = [json.loads(l) for l in open(OUT / "universe50.jsonl") if l.strip()]
    fee_of = {p["pool"]: int(p["fee"]) for p in uni}
    m.VENUE = {p["pool"]: p["venue"] for p in uni}

    pos, pan, meta = m.load()
    sweep = pd.DataFrame(json.loads((OUT / "rerun_corrected.json").read_text())["sweep"])
    corr = {(r["pool"], r["T"]): r["corr"] for _, r in sweep.iterrows()}
    pos["corr"] = [corr.get((p, t), 1.0) for p, t in zip(pos["pool"], pos["T"])]
    pos["net_strict_bps_true"] = pos["net_strict_bps"] + pos["fee_bps"] * (pos["corr"] - 1)
    m.OUTCOME = "net_strict_bps_true"
    pos["fee_tier"] = pos["pool"].map(fee_of)

    edges = [0, 100, 500, 1000, 2500, 5000, 10001]
    labels = ["<1bp", "1-5bp", "5-10bp", "10-25bp", "25-50bp", ">=50bp"]
    pos["bucket"] = pd.cut(pos["fee_tier"], bins=edges, labels=labels, right=False)

    for T in (1, 7, 30):
        sub = pos[pos["T"] == T]
        if not len(sub):
            continue
        print("\n" + "=" * 116)
        print(f"FEE-TIER CROSS-SECTION, T={T}  (ex-ante rule: bucket membership is known "
              f"on the entry date)")
        print("=" * 116)
        print(f"{'bucket':9s} {'n':>7s} {'syms':>5s} {'days':>5s} {'mean':>8s} "
              f"{'lo':>8s} {'hi':>8s} {'fee':>8s} {'il':>8s} {'cost':>7s} "
              f"{'h1':>8s} {'h2':>8s} | {'ex-surv mean':>12s} {'lo':>8s} {'syms':>5s}")
        for b in labels:
            g = sub[sub["bucket"] == b]
            if len(g) < 50:
                continue
            m.RNG = np.random.default_rng(20260819)
            s, n = m.day_blocks(g)
            r = m.boot_ci(s, n)
            h1 = g[g["entry"] < m.SPLIT][m.OUTCOME].mean()
            h2 = g[g["entry"] >= m.SPLIT][m.OUTCOME].mean()

            ex = g[~g["symbol"].isin(SURVIVOR_SYMS)]
            if len(ex) >= 30:
                m.RNG = np.random.default_rng(20260819)
                s2, n2 = m.day_blocks(ex)
                r2 = m.boot_ci(s2, n2)
                exs = f"{r2['mean']:12.1f} {r2['lo']:8.1f} {ex['symbol'].nunique():5d}"
            else:
                exs = f"{'-':>12s} {'-':>8s} {'-':>5s}"

            print(f"{b:9s} {len(g):7d} {g['symbol'].nunique():5d} {r['blocks']:5d} "
                  f"{r['mean']:8.1f} {r['lo']:8.1f} {r['hi']:8.1f} "
                  f"{g['fee_bps'].mean():8.1f} {g['il_bps'].mean():8.1f} "
                  f"{g['cost_strict_bps'].mean():7.1f} {h1:8.1f} {h2:8.1f} | {exs}")

    print("\n" + "=" * 116)
    print("WITHIN the >=25bp tiers at T=7: how much of the bucket net is the 4 survivors?")
    print("=" * 116)
    g = pos[(pos["T"] == 7) & (pos["fee_tier"] >= 2500)]
    tot = g[m.OUTCOME].sum()
    by = g.groupby("symbol")[m.OUTCOME].agg(["sum", "mean", "count"])
    by["share_of_bucket_net"] = by["sum"] / tot if tot else np.nan
    by = by.sort_values("sum", ascending=False)
    print(f"bucket total net = {tot:.0f} bps over {len(g)} positions, "
          f"{g['symbol'].nunique()} symbols")
    print(by.head(20).to_string(float_format=lambda x: f"{x:10.2f}"))
    print(f"\n4 survivor symbols account for "
          f"{100 * by.loc[by.index.isin(SURVIVOR_SYMS), 'share_of_bucket_net'].sum():.1f}%"
          f" of the bucket net, from "
          f"{100 * by.loc[by.index.isin(SURVIVOR_SYMS), 'count'].sum() / len(g):.1f}%"
          f" of its positions")

    print("\n" + "=" * 116)
    print("SYMBOL-LEVEL DISTRIBUTION at T=7, fee tier >=25bp "
          "(is the bucket broadly positive or a few names?)")
    print("=" * 116)
    sm = g.groupby("symbol")[m.OUTCOME].mean().sort_values(ascending=False)
    print(f"symbols positive: {(sm > 0).sum()}/{len(sm)}   "
          f"median symbol mean = {sm.median():.1f}   mean of symbol means = {sm.mean():.1f}")
    print("best 8: " + ", ".join(f"{k}={v:.0f}" for k, v in sm.head(8).items()))
    print("worst 8: " + ", ".join(f"{k}={v:.0f}" for k, v in sm.tail(8).items()))

    json.dump({"note": "fee-tier ex-ante cross-section",
               "symbol_means_T7_high_tier": sm.to_dict()},
              open(OUT / "fee_tier_exante.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
