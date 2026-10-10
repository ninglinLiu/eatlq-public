#!/usr/bin/env python3
"""Which half of a market-making position loses the money?

Two results from this round point at the same place from opposite directions:

  - A3/A5: buying into strength on Base micro-caps loses, confirmed out of
    sample, with the whole Bonferroni interval below zero. So the other side of
    that trade -- selling into strength -- makes money.
  - C1: symmetric concentrated liquidity loses at every holding period, with the
    whole interval below zero, and the loss is impermanent loss, not cost.

A symmetric liquidity range does both halves at once: it sells the asset as the
price rises and buys it as the price falls. If selling into strength is the good
half, then all of C1's loss has to be coming from the buying half, and a range
placed strictly ABOVE spot -- which only ever sells -- would not share it.

This decomposes the existing C1 positions by which way the price actually went,
to see whether that is true before committing a round to testing it. This is a
hypothesis-generating diagnostic on data already used, not a test; whatever it
shows has to be pre-registered and re-run before it can be claimed.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

rows = json.load(open("out/stage3_pnl_50.json"))
base = [r for r in rows if r["selected"] == 0 or True]   # C1 = all deployable arms

print("=" * 108)
print("C1 positions split by the direction the price actually moved over the hold")
print("=" * 108)
print(f"{'T':>4}{'c':>6}{'direction':>12}{'n':>8}{'fee_bps':>10}{'IL_bps':>10}"
      f"{'cost_bps':>10}{'net_bps':>10}{'win%':>8}")

agg = defaultdict(list)
for r in rows:
    if not all(math.isfinite(r.get(k, float("nan")))
               for k in ("fee_bps", "il_bps", "net_strict_bps")):
        continue
    pe, po = r.get("p_entry"), r.get("p_out")
    if not pe or not po or pe <= 0 or po <= 0:
        continue
    # p is alt priced in WETH terms consistently within a pool, so the sign of
    # log(p_out/p_entry) is the direction of the alt against WETH
    lr = math.log(po / pe)
    d = "up" if lr > 0.02 else ("down" if lr < -0.02 else "flat")
    agg[(r["T"], r["c"], d)].append(r)

for T in (1, 7, 30):
    for c in (0.5, 1.0, 2.0):
        for d in ("up", "flat", "down"):
            v = agg.get((T, c, d), [])
            if len(v) < 20:
                continue
            n = len(v)
            f = sum(x["fee_bps"] for x in v) / n
            il = sum(x["il_bps"] for x in v) / n
            co = sum(x["cost_strict_bps"] for x in v) / n
            ne = sum(x["net_strict_bps"] for x in v) / n
            w = 100.0 * sum(1 for x in v if x["net_strict_bps"] > 0) / n
            print(f"{T:>4}{c:>6}{d:>12}{n:>8}{f:>10.1f}{il:>10.1f}"
                  f"{co:>10.1f}{ne:>10.1f}{w:>8.1f}")
    print()

print("=" * 108)
print("the same thing in one line per holding period, pooled over range widths")
print("=" * 108)
print(f"{'T':>4}{'direction':>12}{'n':>8}{'fee_bps':>10}{'IL_bps':>10}"
      f"{'net_bps':>10}   read")
for T in (1, 7, 30):
    tot = {}
    for d in ("up", "flat", "down"):
        v = [x for c in (0.5, 1.0, 2.0) for x in agg.get((T, c, d), [])]
        if len(v) < 20:
            continue
        n = len(v)
        tot[d] = (n, sum(x["fee_bps"] for x in v) / n,
                  sum(x["il_bps"] for x in v) / n,
                  sum(x["net_strict_bps"] for x in v) / n)
    for d, (n, f, il, ne) in tot.items():
        print(f"{T:>4}{d:>12}{n:>8}{f:>10.1f}{il:>10.1f}{ne:>10.1f}")
    if "up" in tot and "down" in tot:
        du, dd = tot["up"][3], tot["down"][3]
        print(f"     {'up minus down':>12}{'':>8}{'':>10}{'':>10}{du - dd:>10.1f}"
              f"   {'selling into strength is the better half' if du > dd else 'buying into weakness is the better half'}")
    print()

json.dump({f"{k[0]}_{k[1]}_{k[2]}": len(v) for k, v in agg.items()},
          open("out/lp_asymmetry.json", "w"), indent=1)
