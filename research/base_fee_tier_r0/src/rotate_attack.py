#!/usr/bin/env python3
"""Attack the top-quintile result before believing any of it.

Ranking symbols by last window's realised fee puts the top quintile at +122.7 bps
[+26.7, +211.2] with both half-years positive, which is the first causally selected
positive number in this project. It is also exactly the shape of the result that
died earlier this round: a positive market-making cell driven by fee revenue.

That one died three ways, so this one gets checked the same three ways plus the one
that killed round 40:

  1. Concentration. Round 40's positive cell was 103.6% one symbol and the
     pre-registration that let it through had no leave-one-out rule. Any cell where
     one symbol carries the result is not a cross-sectional finding.
  2. Fee provenance. 17.8% of pool-days fell back to census.pools.fee because the
     month had too few clean observations to solve. If the top quintile is
     disproportionately made of fallbacks, this is the same bug wearing a
     different hat.
  3. Capacity. A yield that only exists in pools too small to hold the position is
     not a yield. The entry-time share gate is 10% by construction, but round 40
     showed a gate can be satisfied on paper and violated in realisation.
  4. Plausibility. 779 bps a week is 40x a year. Numbers like that were the tell
     last time and should be treated as suspect until the arithmetic is shown.

Nothing here is pre-registered. This is an exploratory lead, and the point of these
checks is to decide whether it deserves a pre-registration, not to claim it.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from collections import defaultdict

T, C = 7, 0.5
NQ = 5
MID = "2026-02-16"
N_BOOT = 4000
ALPHA = 0.05
RNG = random.Random(20260820)
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800"]


def ch(q: str, fmt: str = "TSV") -> str:
    p = subprocess.run(CH + (["--format", fmt] if fmt != "TSV" else []) +
                       ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:2000])
        raise SystemExit("clickhouse failed")
    return p.stdout


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
print(f"top quintile: {len(top)} positions over {len({d for d, _, _ in top})} "
      f"dates, {len({s for _, s, _ in top})} distinct symbols")

print("\n" + "=" * 100)
print("1. CONCENTRATION -- who carries it, and does it survive dropping them")
print("=" * 100)
bysym = defaultdict(list)
for d, s, r in top:
    bysym[s].append((d, r))
tot = sum(r["net_strict_bps"] for _, _, r in top)
contrib = sorted(((sum(r["net_strict_bps"] for _, r in v), s, len(v))
                  for s, v in bysym.items()), reverse=True)
print(f"{'symbol':<12}{'positions':>10}{'sum_net':>11}{'pct_of_total':>14}")
for c_, s, n in contrib[:8]:
    print(f"{s:<12}{n:>10}{c_:>11.0f}{100.0 * c_ / tot:>14.1f}")
print(f"  ... {len(contrib)} symbols in total")

print(f"\n{'dropped':<24}{'n':>7}{'net':>9}{'lo':>9}{'hi':>9}   verdict")


def cell(items):
    byday = defaultdict(list)
    for d, r in items:
        byday[d].append(r["net_strict_bps"])
    lo, hi = boot_days(byday)
    return len(items), mean(r["net_strict_bps"] for _, r in items), lo, hi


base = [(d, r) for d, _, r in top]
n, mu, lo, hi = cell(base)
print(f"{'nothing (as reported)':<24}{n:>7}{mu:>9.1f}"
      f"{(lo or float('nan')):>9.1f}{(hi or float('nan')):>9.1f}")
drop = []
for c_, s, _ in contrib[:3]:
    drop.append(s)
    keep = [(d, r) for d, sy, r in top if sy not in drop]
    n, mu, lo, hi = cell(keep)
    ok = "still clears zero" if (lo is not None and lo > 0) else "NO LONGER CLEARS"
    print(f"{('+ ' + s):<24}{n:>7}{mu:>9.1f}{(lo or float('nan')):>9.1f}"
          f"{(hi or float('nan')):>9.1f}   {ok}")

print("\n" + "=" * 100)
print("2. FEE PROVENANCE -- is the top quintile made of measured fees or "
      "fallbacks to the snapshot")
print("=" * 100)
have = set()
for ln in ch("""SELECT pool, substring(toString(month), 1, 7)
                FROM census.pool_fee_monthly""").strip().split("\n"):
    if ln:
        a, b = ln.split("\t")
        have.add((a, b))
for label, items in (("top quintile", [(d, s, r) for d, s, r in top]),
                     ("all selected positions",
                      [(r["entry"], s, r) for (s, e), r in sel.items()])):
    m = sum(1 for d, _, r in items if (r["pool"], r["entry"][:7]) in have)
    print(f"  {label:<24} measured {m:>6} / {len(items):<6} "
          f"= {100.0 * m / len(items):.1f}%")
mset = [(d, r) for d, _, r in top if (r["pool"], r["entry"][:7]) in have]
fset = [(d, r) for d, _, r in top if (r["pool"], r["entry"][:7]) not in have]
for label, items in (("measured-fee subset", mset), ("fallback subset", fset)):
    if len(items) < 20:
        print(f"  {label:<24} too few to test ({len(items)})")
        continue
    n, mu, lo, hi = cell(items)
    print(f"  {label:<24} n={n:<6} net {mu:>8.1f} "
          f"[{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}]")

print("\n" + "=" * 100)
print("3. CAPACITY -- entry-time share, and how the result splits by it")
print("=" * 100)
sh = sorted(r["share"] for _, _, r in top)
k = len(sh)
print(f"  entry share L_mine/L_act:  p50 {sh[k // 2]:.5f}  p90 "
      f"{sh[int(0.9 * k)]:.5f}  max {sh[-1]:.5f}")
print(f"  positions above 1% of the pool: "
      f"{100.0 * sum(1 for x in sh if x > 0.01) / k:.1f}%")
tight = [(d, r) for d, _, r in top if r["share"] <= 0.001]
loose = [(d, r) for d, _, r in top if r["share"] > 0.001]
for label, items in (("share <= 0.1% (tiny)", tight),
                     ("share > 0.1%", loose)):
    if len(items) < 20:
        print(f"  {label:<24} too few to test ({len(items)})")
        continue
    n, mu, lo, hi = cell(items)
    print(f"  {label:<24} n={n:<6} net {mu:>8.1f} "
          f"[{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}]")

print("\n" + "=" * 100)
print("4. PLAUSIBILITY -- what the 779 bps a week actually implies")
print("=" * 100)
fee = mean(r["fee_bps"] for _, _, r in top)
il = mean(r["il_bps"] for _, _, r in top)
inr = mean(r["in_range"] for _, _, r in top)
share_p50 = sh[k // 2]
print(f"  mean fee {fee:.1f} bps/week on $50 = ${50 * fee / 1e4:.2f} per week")
print(f"  median liquidity share {share_p50:.6f}, so the pool's own fee take in "
      f"range is about ${50 * fee / 1e4 / share_p50:,.0f} per week")
print(f"  mean impermanent loss {il:.1f} bps, mean fraction of swaps in range "
      f"{inr:.2f}")
print(f"  fee/IL ratio {fee / il if il else float('nan'):.2f}   "
      f"(the retracted cbBTC cell was 10.6 before correction, 1.7 after)")
print("\n  A pool paying this much is paying its concentrated liquidity "
      f"{100.0 * 52 * fee / 1e4:,.0f}% a year.")
print("  That is only not absurd if the position is a vanishing share of a pool "
      "with heavy flow,")
print("  which the share above is the test of. It is the same test the retracted "
      "result failed.")

print("\n" + "=" * 100)
print("5. REGIME -- the top quintile by half-year and by quarter")
print("=" * 100)
for label, lo_, hi_ in (("2025-09..2025-11", "2025-09-01", "2025-12-01"),
                        ("2025-12..2026-02", "2025-12-01", "2026-03-01"),
                        ("2026-03..2026-05", "2026-03-01", "2026-06-01"),
                        ("2026-06..2026-08", "2026-06-01", "2026-09-01")):
    items = [(d, r) for d, _, r in top if lo_ <= d < hi_]
    if len(items) < 10:
        print(f"  {label:<20} too few ({len(items)})")
        continue
    n, mu, lo, hi = cell(items)
    print(f"  {label:<20} n={n:<5} net {mu:>8.1f} "
          f"[{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}]  "
          f"fee {mean(r['fee_bps'] for _, r in items):>7.1f}  "
          f"IL {mean(r['il_bps'] for _, r in items):>7.1f}")
