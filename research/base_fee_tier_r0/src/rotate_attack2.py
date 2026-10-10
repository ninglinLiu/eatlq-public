#!/usr/bin/env python3
"""Independent leave-one-out and a non-meme cut of the top-quintile lead.

rotate_attack.py showed the result dies after dropping the two largest
contributors *cumulatively*. That confounds "is it one name" with "is it
three names". This does each drop on its own, then drops the three together,
then drops anything that looks like a Base meme (short-lived, high-fee,
not a wrapped major or a stable).
"""
from __future__ import annotations

import json
import random
from collections import defaultdict

T, C, NQ = 7, 0.5, 5
N_BOOT, ALPHA = 4000, 0.05
RNG = random.Random(20260820)

# Names that are claims on something off-chain or a dollar. Everything else
# in the top quintile is treated as a Base-native / meme name for this cut.
MAJORS = {
    "cbBTC", "tBTC", "cbXRP", "uXRP", "cbADA", "uADA", "cbLTC", "uSOL",
    "uDOGE", "uSUI", "LINK", "AAVE", "MORPHO", "ZEN", "USDC", "USDT",
    "USD+", "EURC", "cbETH", "wstETH", "weETH", "ezETH", "WETH",
}


def mean(a):
    v = list(a)
    return sum(v) / len(v) if v else float("nan")


def boot_days(byday):
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


def cell(items):
    byday = defaultdict(list)
    for d, r in items:
        byday[d].append(r["net_strict_bps"])
    lo, hi = boot_days(byday)
    return len(items), mean(r["net_strict_bps"] for _, r in items), lo, hi


rows = json.load(open("out/stage3_pnl_50_vfee.json"))
sel = {(r["symbol"], r["entry"]): r
       for r in rows if r["T"] == T and r["c"] == C and r["selected"] == 1}
dates = sorted({e for _, e in sel})
bydate = defaultdict(list)
for (s, d), r in sel.items():
    i = dates.index(d)
    if i == 0:
        continue
    prev = sel.get((s, dates[i - 1]))
    if prev is None:
        continue
    bydate[d].append((prev["fee_bps"], s, r))

top = []
for d, v in bydate.items():
    if len(v) < NQ * 3:
        continue
    v.sort(key=lambda x: x[0])
    top.extend((d, s, r) for _, s, r in v[-max(1, len(v) // NQ):])

bysym = defaultdict(list)
for d, s, r in top:
    bysym[s].append((d, r))
tot = sum(r["net_strict_bps"] for _, _, r in top)
contrib = sorted(((sum(r["net_strict_bps"] for _, r in v), s, len(v))
                  for s, v in bysym.items()), reverse=True)

print("INDEPENDENT leave-one-out (drop one name, keep the rest)")
print(f"{'dropped':<16}{'n':>6}{'net':>9}{'lo':>9}{'hi':>9}  {'pct_was':>8}")
n, mu, lo, hi = cell([(d, r) for d, _, r in top])
print(f"{'(none)':<16}{n:>6}{mu:>9.1f}{(lo or float('nan')):>9.1f}"
      f"{(hi or float('nan')):>9.1f}")
for c_, s, k in contrib[:8]:
    keep = [(d, r) for d, sy, r in top if sy != s]
    n, mu, lo, hi = cell(keep)
    flag = "  CLEARS" if lo and lo > 0 else "  FAILS"
    print(f"{s:<16}{n:>6}{mu:>9.1f}{(lo or float('nan')):>9.1f}"
          f"{(hi or float('nan')):>9.1f}{100.0 * c_ / tot:>8.1f}{flag}")

print("\nDROP THE TOP 3 TOGETHER")
drop3 = {s for _, s, _ in contrib[:3]}
n, mu, lo, hi = cell([(d, r) for d, s, r in top if s not in drop3])
print(f"  drop {sorted(drop3)}: n={n} net={mu:.1f} "
      f"[{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}] "
      f"{'CLEARS' if lo and lo > 0 else 'FAILS'}")

print("\nMAJORS-ONLY vs MEME-ONLY inside the top quintile")
maj = [(d, r) for d, s, r in top if s in MAJORS]
mem = [(d, r) for d, s, r in top if s not in MAJORS]
for label, items in (("majors/stables", maj), ("everything else", mem)):
    if len(items) < 10:
        print(f"  {label}: n={len(items)} too few")
        continue
    n, mu, lo, hi = cell(items)
    print(f"  {label:<18} n={n:<5} net {mu:>8.1f} "
          f"[{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}]  "
          f"fee {mean(r['fee_bps'] for _, r in items):>7.1f}  "
          f"IL {mean(r['il_bps'] for _, r in items):>7.1f}")

print("\nQUINTILES AFTER DROPPING THE THREE NAMES FROM THE WHOLE PANEL")
# rebuild quintiles with those names removed before ranking
banned = drop3
qnet = defaultdict(list)
for d, v in bydate.items():
    v = [(sig, s, r) for sig, s, r in v if s not in banned]
    if len(v) < NQ * 3:
        continue
    v.sort(key=lambda x: x[0])
    k = len(v)
    for i, (_, s, r) in enumerate(v):
        qnet[min(NQ - 1, i * NQ // k)].append((d, r))
print(f"{'q':<4}{'n':>6}{'net':>9}{'lo':>9}{'hi':>9}{'fee':>9}{'IL':>9}")
for q in range(NQ):
    items = qnet[q]
    n, mu, lo, hi = cell(items)
    print(f"{q + 1:<4}{n:>6}{mu:>9.1f}{(lo or float('nan')):>9.1f}"
          f"{(hi or float('nan')):>9.1f}"
          f"{mean(r['fee_bps'] for _, r in items):>9.1f}"
          f"{mean(r['il_bps'] for _, r in items):>9.1f}")
