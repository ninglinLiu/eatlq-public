#!/usr/bin/env python3
"""Before running the rotation, ask whether there is anything to rotate into.

Tier selection is worth +69.5 bps a week and leaves market making at -31.9, about
32 bps short of zero. The rotation hypothesis is that choosing among 274 symbols
instead of 3 fee tiers closes that gap, because the cross-sectional spread across
symbols is far wider than across tiers of one symbol.

Wider spread is necessary but nowhere near sufficient, and two things have to be
true beyond it:

  - The ranking has to be predictive. Spread that is pure noise cannot be
    harvested, however wide it is.
  - The gain has to survive impermanent loss. Test B's paired design made
    impermanent loss cancel because the tiers of one symbol face the same price
    path, and that cancellation does not exist across symbols. Market making is
    short volatility, so a rule that chases fee revenue will walk into busier
    symbols, which is where impermanent loss lives. Fee and impermanent loss are
    therefore reported separately, never netted before being looked at.

All of this runs on out/stage3_pnl_50_vfee.json, the positions already built with
measured monthly fees, so it costs nothing and can kill or sharpen the
pre-registration before a round is spent on it.

The signal is deliberately the crudest causal one available: a symbol's realised
fee for the window that ended on the entry date. It is known at entry, it needs no
new machinery, and if even this does not rank, a more refined signal built from the
same information is unlikely to.
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict

T, C = 7, 0.5
MID = "2026-02-16"
N_BOOT = 4000
ALPHA = 0.05
RNG = random.Random(20260820)


def mean(a):
    v = list(a)
    return sum(v) / len(v) if v else float("nan")


def sd(a):
    v = list(a)
    if len(v) < 2:
        return float("nan")
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def boot_days(byday):
    """Day-blocked bootstrap of the mean, since one entry date holds many symbols."""
    ds = list(byday)
    if len(ds) < 5:
        return None, None
    sums = [sum(byday[d]) for d in ds]
    cnts = [len(byday[d]) for d in ds]
    nd = len(ds)
    acc = []
    for _ in range(N_BOOT):
        s = c = 0.0
        for _ in range(nd):
            j = RNG.randrange(nd)
            s += sums[j]
            c += cnts[j]
        if c:
            acc.append(s / c)
    acc.sort()
    return acc[int(ALPHA / 2 * len(acc))], acc[int((1 - ALPHA / 2) * len(acc))]


rows = json.load(open("out/stage3_pnl_50_vfee.json"))
sel = [r for r in rows if r["T"] == T and r["c"] == C and r["selected"] == 1]
print(f"selected-tier positions at T={T}, c={C}: {len(sel):,}")

# one record per (symbol, entry): the tier already chosen causally by trailing yield
bysd = {}
for r in sel:
    bysd[(r["symbol"], r["entry"])] = r
dates = sorted({e for _, e in bysd})
syms = sorted({s for s, _ in bysd})
print(f"entry dates: {len(dates)}   distinct symbols: {len(syms)}")

print("\n" + "=" * 104)
print("1. Is there spread to harvest? Cross-sectional dispersion within each "
      "entry date")
print("=" * 104)
print(f"{'':<10}{'breadth':>9}{'net_p10':>10}{'net_p50':>10}{'net_p90':>10}"
      f"{'net_sd':>10}{'best_minus_median':>19}")
brs, spreads = [], []
for d in dates:
    v = sorted(r["net_strict_bps"] for (s, e), r in bysd.items() if e == d)
    if len(v) < 10:
        continue
    n = len(v)
    brs.append(n)
    spreads.append(v[int(0.9 * n)] - v[n // 2])
print(f"{'median':<10}{sorted(brs)[len(brs) // 2]:>9}"
      f"{'':>10}{'':>10}{'':>10}{'':>10}"
      f"{sorted(spreads)[len(spreads) // 2]:>19.1f}")
allv = sorted(r["net_strict_bps"] for r in bysd.values())
n = len(allv)
print(f"\n  pooled across all dates: p10 {allv[int(0.1 * n)]:.1f}   "
      f"p50 {allv[n // 2]:.1f}   p90 {allv[int(0.9 * n)]:.1f}   "
      f"sd {sd(allv):.1f}")
print(f"  median breadth per date: {sorted(brs)[len(brs) // 2]} symbols; "
      f"median gap from the 90th percentile symbol to the median symbol: "
      f"{sorted(spreads)[len(spreads) // 2]:.1f} bps")
print("  For reference the whole gap left to close is about 32 bps, and tier "
      "selection was worth 69.5.")

print("\n" + "=" * 104)
print("2. Does last window's realised fee rank the next window? Deciles by a "
      "causal signal")
print("=" * 104)
print("Signal for (symbol, date) is that symbol's fee_bps in the window ending "
      "at date -- known at entry.\n")


def prev_date(d):
    i = dates.index(d)
    return dates[i - 1] if i > 0 else None


ranked = []
for (s, d), r in bysd.items():
    pd_ = prev_date(d)
    if pd_ is None:
        continue
    prev = bysd.get((s, pd_))
    if prev is None:
        continue
    ranked.append((d, s, prev["fee_bps"], r))

bydate = defaultdict(list)
for d, s, sig, r in ranked:
    bydate[d].append((sig, s, r))

NQ = 5
buckets = defaultdict(list)
for d, v in bydate.items():
    if len(v) < NQ * 3:
        continue
    v.sort(key=lambda x: x[0])
    k = len(v)
    for i, (sig, s, r) in enumerate(v):
        q = min(NQ - 1, i * NQ // k)
        buckets[q].append((d, r))

print(f"{'quintile':<26}{'n':>7}{'net':>9}{'lo':>9}{'hi':>9}"
      f"{'fee':>9}{'IL':>9}{'cost':>8}{'win%':>7}{'H1':>8}{'H2':>8}")
labels = {0: "1 lowest prior fee", NQ - 1: f"{NQ} highest prior fee"}
qnet = {}
for q in range(NQ):
    v = buckets.get(q, [])
    if not v:
        continue
    byday = defaultdict(list)
    for d, r in v:
        byday[d].append(r["net_strict_bps"])
    lo, hi = boot_days(byday)
    h1 = [r for d, r in v if d < MID]
    h2 = [r for d, r in v if d >= MID]
    qnet[q] = mean(r["net_strict_bps"] for _, r in v)
    lbl = labels.get(q, f"{q + 1}")
    print(f"{lbl:<26}{len(v):>7}{qnet[q]:>9.1f}"
          f"{(lo if lo is not None else float('nan')):>9.1f}"
          f"{(hi if hi is not None else float('nan')):>9.1f}"
          f"{mean(r['fee_bps'] for _, r in v):>9.1f}"
          f"{mean(r['il_bps'] for _, r in v):>9.1f}"
          f"{mean(r['cost_strict_bps'] for _, r in v):>8.1f}"
          f"{100.0 * sum(1 for _, r in v if r['net_strict_bps'] > 0) / len(v):>7.1f}"
          f"{(mean(r['net_strict_bps'] for r in h1) if h1 else float('nan')):>8.1f}"
          f"{(mean(r['net_strict_bps'] for r in h2) if h2 else float('nan')):>8.1f}")

if len(qnet) == NQ:
    seq = [qnet[q] for q in range(NQ)]
    print(f"\n  monotone increasing in prior fee = "
          f"{all(seq[i] <= seq[i + 1] for i in range(NQ - 1))}")
    print(f"  top minus bottom = {seq[-1] - seq[0]:.1f} bps")

print("\n" + "=" * 104)
print("3. The question Test B could not ask: does chasing fee revenue buy "
      "impermanent loss?")
print("=" * 104)
print("Within each date, the top quintile by prior fee against that date's own "
      "median symbol.\n")
dn, df, di = [], [], []
byday = defaultdict(list)
for d, v in bydate.items():
    if len(v) < NQ * 3:
        continue
    v.sort(key=lambda x: x[0])
    top = v[-max(1, len(v) // NQ):]
    med_net = sorted(r["net_strict_bps"] for _, _, r in v)[len(v) // 2]
    med_fee = sorted(r["fee_bps"] for _, _, r in v)[len(v) // 2]
    med_il = sorted(r["il_bps"] for _, _, r in v)[len(v) // 2]
    x = mean(r["net_strict_bps"] for _, _, r in top) - med_net
    byday[d].append(x)
    dn.append(x)
    df.append(mean(r["fee_bps"] for _, _, r in top) - med_fee)
    di.append(mean(r["il_bps"] for _, _, r in top) - med_il)
lo, hi = boot_days(byday)
print(f"  dates used          {len(dn)}")
print(f"  delta net           {mean(dn):+.1f} bps  "
      f"[{(lo if lo is not None else float('nan')):.1f}, "
      f"{(hi if hi is not None else float('nan')):.1f}]")
print(f"  delta fee           {mean(df):+.1f} bps")
print(f"  delta IL            {mean(di):+.1f} bps   "
      f"<- Test B had this at about zero by construction")
print(f"  fee kept per unit of IL taken on: "
      f"{(mean(df) / mean(di) if mean(di) else float('nan')):.2f}")
print(f"  dates where the top quintile beat the median: "
      f"{100.0 * sum(1 for x in dn if x > 0) / len(dn):.1f}%")
