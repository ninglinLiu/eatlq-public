#!/usr/bin/env python3
"""Adversarial audit of the stage 2 positive result. Six ways it could be wrong.

Stage 2 returned the first positive verdict in 39 rounds, so the job here is to
try to break it rather than to describe it.

  1. Baseline reproduction. If this pipeline is sound, the non-selected tiers
     should reproduce round 36's finding that fees and impermanent loss roughly
     cancel. If the baseline is already strongly positive then the pipeline is
     inflating fees and the selection effect is an artifact.
  2. Independent windows. A day-blocked bootstrap over 9 entry dates is not the
     same as one over 65. T=7 must be counted, not assumed.
  3. Range contamination. Tick spacing stops the ranges from being identical, so
     part of the paired difference could be "narrower range" rather than
     "better tier".
  4. Which tier gets picked, and is the selection rule doing anything a coin flip
     would not.
  5. Fee plausibility, annualised.
  6. The generous cost convention. GOAL_R1 section 3 allowed buying the alt at
     the cheapest measured pool; round 36 charged the LP pool's own cost.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict

ALPHA = 0.05 / 6
N_BOOT = 10_000


def mean(a):
    return sum(a) / len(a)


def median(a):
    s = sorted(a)
    k = len(s)
    return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])


def boot(byday, stat=mean, n=N_BOOT, seed=7):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None, None
    point = stat([v for d in ds for v in byday[d]])
    acc = []
    for _ in range(n):
        s = []
        for _ in range(len(ds)):
            s.extend(byday[ds[rng.randrange(len(ds))]])
        if s:
            acc.append(stat(s))
    acc.sort()
    return point, acc[int(len(acc) * ALPHA / 2)], acc[int(len(acc) * (1 - ALPHA / 2))]


def cell(rows, T, c, sel=None):
    g = [r for r in rows if r["T"] == T and r["c"] == c]
    if sel is not None:
        g = [r for r in g if r["selected"] == sel]
    return g


def main() -> None:
    rows = json.load(open("out/stage2_pnl.json"))
    cells = [(1, 0.5), (1, 1.0), (1, 2.0), (7, 0.5), (7, 1.0), (7, 2.0)]

    print("=" * 108)
    print("CHECK 1  Baseline. Round 36 found fees ~= IL, gross indistinguishable from 0.")
    print("         Do the NON-selected deployable tiers reproduce that?")
    print("=" * 108)
    print(f"{'T':<4}{'c':<6}{'n':>6}{'gross(fee-IL)':>15}{'lo':>9}{'hi':>9}"
          f"{'net vs HODL':>13}{'lo':>9}{'hi':>9}")
    for T, c in cells:
        g = cell(rows, T, c, sel=0)
        if len(g) < 20:
            continue
        bg, bn = defaultdict(list), defaultdict(list)
        for r in g:
            bg[r["entry"]].append(r["gross_bps"])
            bn[r["entry"]].append(r["net_bps"])
        mg, lg, hg = boot(bg)
        mn, ln, hn = boot(bn)
        print(f"{T:<4}{c:<6}{len(g):>6}{mg:>15.1f}{lg:>9.1f}{hg:>9.1f}"
              f"{mn:>13.1f}{ln:>9.1f}{hn:>9.1f}")
    print("\nround 36 reference: gross 1 day +1.8 [-5.7, +8.7], 7 day +8.0 [-36.3, +47.4]")

    print("\n" + "=" * 108)
    print("CHECK 2  How many independent entry dates actually back each interval?")
    print("=" * 108)
    print(f"{'T':<4}{'c':<6}{'n_pos':>7}{'entry_dates':>13}{'n_alts':>9}"
          f"{'pos_per_date':>14}")
    for T, c in cells:
        g = cell(rows, T, c, sel=1)
        if not g:
            continue
        ds = {r["entry"] for r in g}
        print(f"{T:<4}{c:<6}{len(g):>7}{len(ds):>13}{len({r['alt'] for r in g}):>9}"
              f"{len(g) / len(ds):>14.1f}")
    print("\nA day-blocked bootstrap resamples entry dates. With 9 of them the interval")
    print("is a statement about 9 draws, however many positions sit inside them.")

    print("\n" + "=" * 108)
    print("CHECK 3  Tick spacing stops ranges from matching. Is the paired difference")
    print("         really 'better tier', or just 'narrower range'?")
    print("=" * 108)
    print(f"{'T':<4}{'c':<6}{'sel_fee_bps':>12}{'oth_fee_bps':>12}"
          f"{'sel_in_range':>14}{'oth_in_range':>14}{'sel_IL':>9}{'oth_IL':>9}")
    for T, c in cells:
        s, o = cell(rows, T, c, 1), cell(rows, T, c, 0)
        if len(s) < 20 or len(o) < 20:
            continue
        print(f"{T:<4}{c:<6}{median([r['fee'] / 100 for r in s]):>12.2f}"
              f"{median([r['fee'] / 100 for r in o]):>12.2f}"
              f"{median([r['in_range'] for r in s]):>14.2f}"
              f"{median([r['in_range'] for r in o]):>14.2f}"
              f"{median([r['il_bps'] for r in s]):>9.1f}"
              f"{median([r['il_bps'] for r in o]):>9.1f}")
    print("\nIf the selected tier had systematically more IL, the paired gain would be")
    print("risk taken rather than revenue captured.")

    print("\n" + "=" * 108)
    print("CHECK 4  Is the selection rule better than a coin flip on the same groups?")
    print("=" * 108)
    rng = random.Random(4242)
    print(f"{'T':<4}{'c':<6}{'pairs':>6}{'real_d_net':>12}{'random_d_net':>14}"
          f"{'rand_p95':>10}{'beats_random':>14}")
    for T, c in cells:
        grp = defaultdict(list)
        for r in rows:
            if r["T"] == T and r["c"] == c:
                grp[(r["alt"], r["entry"])].append(r)
        real, rand_draws = [], []
        groups = [v for v in grp.values() if len(v) >= 2 and sum(x["selected"] for x in v) == 1]
        for v in groups:
            s = [x for x in v if x["selected"]][0]
            o = [x for x in v if not x["selected"]]
            real.append(s["net_bps"] - mean([x["net_bps"] for x in o]))
        if len(real) < 20:
            continue
        for _ in range(2000):
            acc = []
            for v in groups:
                p = rng.randrange(len(v))
                s = v[p]
                o = [x for i, x in enumerate(v) if i != p]
                acc.append(s["net_bps"] - mean([x["net_bps"] for x in o]))
            rand_draws.append(mean(acc))
        rand_draws.sort()
        p95 = rand_draws[int(0.95 * len(rand_draws))]
        print(f"{T:<4}{c:<6}{len(real):>6}{mean(real):>12.1f}"
              f"{mean(rand_draws):>14.1f}{p95:>10.1f}"
              f"{('yes' if mean(real) > p95 else 'NO'):>14}")
    print("\nA random pick inside the same groups has expectation 0 by construction, so")
    print("this is a check that the machinery is not manufacturing a difference.")

    print("\n" + "=" * 108)
    print("CHECK 5  Are these fee levels plausible when annualised?")
    print("=" * 108)
    print(f"{'T':<4}{'c':<6}{'fee_bps/window':>16}{'annualised %':>14}"
          f"{'IL_bps':>9}{'cost_bps':>10}")
    for T, c in cells:
        g = cell(rows, T, c, 1)
        if len(g) < 20:
            continue
        f = median([r["fee_bps"] for r in g])
        print(f"{T:<4}{c:<6}{f:>16.1f}{f / 1e4 * 365 / T * 100:>14.0f}"
              f"{median([r['il_bps'] for r in g]):>9.1f}"
              f"{median([r['cost_bps'] for r in g]):>10.1f}")

    print("\n" + "=" * 108)
    print("CHECK 6  GOAL_R1 allowed buying the alt at its cheapest measured pool.")
    print("         Round 36 charged the LP pool's own cost. Does the result survive?")
    print("=" * 108)
    cost5k = json.load(open("/root/base_lp_edge_r0/out/cost_5k.json"))
    print(f"{'T':<4}{'c':<6}{'n_kept':>8}{'net_own_cost':>14}{'lo':>9}{'hi':>9}"
          f"{'net_cheapest':>14}")
    for T, c in cells:
        g = cell(rows, T, c, 1)
        kept, byd = [], defaultdict(list)
        for r in g:
            m = cost5k.get(r["pool"])
            if not m:
                continue
            own = float(m["cost_bps"])
            if own > 400:                     # unmeasurable pool, drop rather than model
                continue
            # cost_bps in the row was charged at the alt's cheapest pool; rescale
            adj = r["net_bps"] - r["cost_bps"] * (own / max(r["cost_bps"], 1e-9) - 1.0) \
                if r["cost_bps"] > 0 else r["net_bps"]
            # the row's cost was (alt notional) * cheapest_rate; swap the rate
            base_rate = r["cost_bps"]
            adj = r["net_bps"] - (base_rate * (own / _cheapest_rate(r, cost5k) - 1.0)) \
                if _cheapest_rate(r, cost5k) > 0 else r["net_bps"]
            kept.append(adj)
            byd[r["entry"]].append(adj)
        if len(kept) < 20:
            print(f"{T:<4}{c:<6}{len(kept):>8}{'too few':>14}")
            continue
        mu, lo, hi = boot(byd)
        print(f"{T:<4}{c:<6}{len(kept):>8}{mu:>14.1f}{lo:>9.1f}{hi:>9.1f}"
              f"{mean([r['net_bps'] for r in g]):>14.1f}")


_ALT_CHEAP: dict = {}


def _cheapest_rate(row, cost5k):
    if not _ALT_CHEAP:
        for v in cost5k.values():
            a, c = v["alt"], float(v["cost_bps"])
            if a not in _ALT_CHEAP or c < _ALT_CHEAP[a]:
                _ALT_CHEAP[a] = c
    return _ALT_CHEAP.get(row["alt"], 0.0)


if __name__ == "__main__":
    main()
