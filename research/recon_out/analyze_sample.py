#!/usr/bin/env python3
"""Offline analysis of the sampled liquidation logs. Zero RPC."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict

D = json.load(open("/root/recon_out/sample_liq.json"))
SPAN = D["span"]
B0, B1 = D["panel"]
PANEL_BLOCKS = B1 - B0 + 1

TOP = {
    "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41": "MORPHO_Liquidate",
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286": "AAVE_LiquidationCall",
    "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52": "CV2_LiquidateBorrow",
    "0x9850ab1af75177e4a9201c65a2cf7976d5d28e40ef63494b44366f86b2f9412e": "COMET_AbsorbCollateral",
    "0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f": "COMET_AbsorbDebt",
}

wins = [w for w in D["windows"] if "error" not in w]
counts = [w["n"] for w in wins]
print(f"windows={len(wins)}  span={SPAN}  panel_blocks={PANEL_BLOCKS:,}")
print(f"counts: min={min(counts)} p25={statistics.quantiles(counts,n=4)[0]:.0f} "
      f"median={statistics.median(counts)} p75={statistics.quantiles(counts,n=4)[2]:.0f} "
      f"max={max(counts)}  mean={statistics.mean(counts):.1f}")

scale = PANEL_BLOCKS / (len(wins) * SPAN)
print(f"\nscale factor (panel/sampled) = {scale:.2f}")


def extrap(vals, n_win):
    s = PANEL_BLOCKS / (n_win * SPAN)
    return sum(vals) * s


by_proto = defaultdict(list)
for w in wins:
    for k, v in TOP.items():
        by_proto[v].append(w["by_proto"].get(v, 0))

print("\n=== per-protocol extrapolation ===")
print(f"{'protocol':24s} {'sampled':>8} {'all-window':>12} {'drop-max':>12} {'median-based':>13}")
rows = []
for p, vals in by_proto.items():
    tot = sum(vals)
    mx = max(vals)
    drop = extrap([v for v in vals if True], len(vals)) if tot == 0 else (tot - mx) * PANEL_BLOCKS / ((len(vals) - 1) * SPAN)
    med = statistics.median(vals) * PANEL_BLOCKS / SPAN
    rows.append((p, tot, tot * scale, drop, med))
    print(f"{p:24s} {tot:>8} {tot*scale:>12,.0f} {drop:>12,.0f} {med:>13,.0f}")
tot_all = sum(counts)
mx_all = max(counts)
print(f"{'TOTAL':24s} {tot_all:>8} {tot_all*scale:>12,.0f} "
      f"{(tot_all-mx_all)*PANEL_BLOCKS/((len(wins)-1)*SPAN):>12,.0f} "
      f"{statistics.median(counts)*PANEL_BLOCKS/SPAN:>13,.0f}")

# ---- clustering: distinct blocks / txs / episodes within each window
print("\n=== clustering (how many INDEPENDENT episodes?) ===")
for p in ["MORPHO_Liquidate", "AAVE_LiquidationCall", "CV2_LiquidateBorrow"]:
    blocks, txs, n = set(), set(), 0
    for w in wins:
        for l in w["logs"]:
            if TOP[l["topics"][0]] != p:
                continue
            n += 1
            blocks.add(int(l["blockNumber"], 16))
            txs.add(l["transactionHash"])
    # episode = maximal run of blocks with gaps < 300 blocks (10 min)
    bs = sorted(blocks)
    eps = 1 if bs else 0
    for a, b in zip(bs, bs[1:]):
        if b - a > 300:
            eps += 1
    print(f"{p:24s} logs={n:<5} distinct_blocks={len(blocks):<5} distinct_txs={len(txs):<5} "
          f"episodes(gap>300blk)={eps:<4} logs_per_episode={n/max(eps,1):.1f}")

# ---- Morpho market ids
mids = Counter()
for w in wins:
    for l in w["logs"]:
        if TOP[l["topics"][0]] == "MORPHO_Liquidate":
            mids[l["topics"][1]] += 1
print(f"\n=== Morpho distinct market ids in sample: {len(mids)} ===")
for m, c in mids.most_common(20):
    print(f"  {m}  {c}")

# ---- Aave collateral / debt assets
aave_col, aave_debt = Counter(), Counter()
for w in wins:
    for l in w["logs"]:
        if TOP[l["topics"][0]] == "AAVE_LiquidationCall":
            aave_col["0x" + l["topics"][1][-40:]] += 1
            aave_debt["0x" + l["topics"][2][-40:]] += 1
print(f"\n=== Aave-style collateralAsset (topic1) : {len(aave_col)} distinct ===")
for a, c in aave_col.most_common(30):
    print(f"  {a}  {c}")
print(f"=== Aave-style debtAsset (topic2) : {len(aave_debt)} distinct ===")
for a, c in aave_debt.most_common(30):
    print(f"  {a}  {c}")

# ---- emitting contract addresses per protocol
print("\n=== emitting contracts ===")
for p in TOP.values():
    ad = Counter()
    for w in wins:
        for l in w["logs"]:
            if TOP[l["topics"][0]] == p:
                ad[l["address"].lower()] += 1
    if ad:
        print(f"-- {p} ({len(ad)} distinct)")
        for a, c in ad.most_common(15):
            print(f"   {a}  {c}")

# ---- the outlier window
big = max(wins, key=lambda w: w["n"])
print(f"\n=== outlier window {big['from']}-{big['to']}  n={big['n']} ===")
bb = Counter(int(l["blockNumber"], 16) for l in big["logs"])
print(f"  spans blocks {min(bb)}..{max(bb)}  ({max(bb)-min(bb)} blocks, "
      f"{(max(bb)-min(bb))*2/60:.0f} min)  distinct_blocks={len(bb)}")
mm = Counter(l["topics"][1] for l in big["logs"]
             if TOP[l["topics"][0]] == "MORPHO_Liquidate")
print(f"  morpho market ids in cascade: {len(mm)}")
for m, c in mm.most_common(10):
    print(f"    {m} {c}")

json.dump({"mids": dict(mids), "aave_col": dict(aave_col), "aave_debt": dict(aave_debt)},
          open("/root/recon_out/assets_raw.json", "w"), indent=1)
