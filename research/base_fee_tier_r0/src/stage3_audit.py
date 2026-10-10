#!/usr/bin/env python3
"""Adversarial audit of C2 stage 3, whose Test A cleared zero on T7_c0.5.

Forty rounds have produced no positive result that survived scrutiny, so the prior
is that this one has a hole, and the job is to find it rather than to describe it.

Six checks. The first is the fee reconciliation GOAL_R2 section 4 committed to; the
other five come from what the stage 3 output itself already looks suspicious about.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"
PEGS = ("ETH", "USD", "BTC", "EUR", "DAI")
ALPHA = 0.05 / 9
N_BOOT = 4000


def ch(q: str, fmt: str = "TabSeparatedWithNames") -> str:
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--max_execution_time=14400",
                        "--max_memory_usage=12000000000", "--format", fmt,
                        "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def is_peg(s):
    return any(m in s.upper() for m in PEGS)


def mean(a):
    return sum(a) / len(a) if a else float("nan")


def median(a):
    s = sorted(a)
    return s[len(s) // 2] if s else float("nan")


def boot(byday, n=N_BOOT, seed=7):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None, None, len(ds)
    allv = [v for d in ds for v in byday[d]]
    acc = []
    for _ in range(n):
        s = []
        for _ in range(len(ds)):
            s.extend(byday[ds[rng.randrange(len(ds))]])
        if s:
            acc.append(mean(s))
    acc.sort()
    return (mean(allv), acc[int(len(acc) * ALPHA / 2)],
            acc[int(len(acc) * (1 - ALPHA / 2))], len(ds))


def main() -> None:
    rows = json.load(open("out/stage3_pnl.json"))
    win = [r for r in rows if r["T"] == 7 and r["c"] == 0.5 and r["selected"]]
    print(f"loaded {len(rows)} positions; the cell that cleared zero "
          f"(T=7, c=0.5, selected) has {len(win)}\n")

    print("=" * 100)
    print("(1) FEE RECONCILIATION -- pool-level fee APR vs my position-level fee yield")
    print("=" * 100)
    print("    GOAL_R2 sec.4: if the pools really pay this much, pool-level")
    print("    sum(volume x fee) / TVL should land in the same range. If it is far")
    print("    lower, my share attribution is wrong and the whole line is void.")
    pools = sorted({r["pool"] for r in win})
    plist = ",".join(f"'{p}'" for p in pools)
    out = ch(f"""
    WITH sw AS (
      SELECT s.pool AS pool, toDate(s.block_time) AS d,
             abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
               / 1e18 AS w,
             toFloat64(s.liquidity) AS L,
             pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2) AS p,
             pl.fee AS fee
      FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
      WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0 AND s.liquidity > 0
    ),
    d AS (
      SELECT pool, d, any(fee) AS fee_v, sum(w) AS vol_weth,
             quantile(0.5)(L) AS L_p50, quantile(0.5)(p) AS p_p50
      FROM sw GROUP BY pool, d
    ),
    e AS (
      SELECT pool, d, fee_v, vol_weth,
             2 * L_p50 * sqrt(p_p50) / 1e18 AS tvl_weth,
             1e4 * (vol_weth * (fee_v / 1e6))
               / nullIf(2 * L_p50 * sqrt(p_p50) / 1e18, 0) AS fee_bps_day
      FROM d
    )
    SELECT pool,
           round(quantile(0.5)(vol_weth), 3) AS vol_weth_day_p50,
           any(fee_v) AS fee,
           round(quantile(0.5)(tvl_weth), 3) AS tvl_weth_p50,
           round(quantile(0.5)(fee_bps_day), 2) AS pool_fee_bps_day
    FROM e GROUP BY pool ORDER BY pool_fee_bps_day DESC
    """)
    lines = out.strip().split("\n")
    pool_apr = {}
    for ln in lines[1:]:
        if not ln:
            continue
        f = ln.split("\t")
        try:
            pool_apr[f[0]] = float(f[4])
        except ValueError:
            continue
    mine = defaultdict(list)
    for r in win:
        mine[r["pool"]].append(r["fee_bps"] / 7.0)   # per day
    print(f"\n    {'pool':<14}{'pool_fee_bps/day':>18}{'my_fee_bps/day':>16}"
          f"{'ratio':>8}{'n':>5}  symbol")
    ratios = []
    for p in sorted(mine, key=lambda x: -median(mine[x]))[:18]:
        pa = pool_apr.get(p)
        mm = median(mine[p])
        sym = next(r["symbol"] for r in win if r["pool"] == p)
        if pa and pa > 0:
            ratios.append(mm / pa)
            print(f"    {p[:12]:<14}{pa:>18.2f}{mm:>16.2f}{mm/pa:>8.2f}"
                  f"{len(mine[p]):>5}  {sym}")
    if ratios:
        print(f"\n    ratio my_yield / pool_yield: median {median(ratios):.2f}, "
              f"min {min(ratios):.2f}, max {max(ratios):.2f}")
        print("    A position concentrated into a narrow range SHOULD out-yield the")
        print("    pool average, so a ratio above 1 is expected; a ratio far above")
        print("    the concentration factor would mean the attribution is broken.")

    print("\n" + "=" * 100)
    print("(2) WHERE THE RESULT LIVES -- pegged pairs versus volatile alts")
    print("=" * 100)
    for lbl, sub in (("pegged (WETH derivs, stables, BTC)",
                      [r for r in win if is_peg(r["symbol"])]),
                     ("volatile alts", [r for r in win if not is_peg(r["symbol"])])):
        if not sub:
            continue
        byd = defaultdict(list)
        for r in sub:
            byd[r["entry"]].append(r["net_strict_bps"])
        mu, lo, hi, nb = boot(byd)
        print(f"    {lbl:<36} n={len(sub):<5} blocks={nb:<4} "
              f"net={mu:>8.1f} [{lo:>8.1f}, {hi:>8.1f}]  "
              f"fee={median([r['fee_bps'] for r in sub]):>7.1f} "
              f"IL={median([r['il_bps'] for r in sub]):>7.1f} "
              f"in_range={mean([r['in_range'] for r in sub]):.3f}")

    print("\n" + "=" * 100)
    print("(3) SYMBOL CONCENTRATION -- 47 day blocks, but how many independent assets?")
    print("=" * 100)
    bysym = defaultdict(list)
    for r in win:
        bysym[r["symbol"]].append(r["net_strict_bps"])
    print(f"    distinct symbols: {len(bysym)}")
    print(f"    {'symbol':<16}{'n':>5}{'mean_net':>10}{'share_of_total':>16}")
    tot = sum(sum(v) for v in bysym.values())
    for s in sorted(bysym, key=lambda x: -abs(sum(bysym[x])))[:12]:
        print(f"    {s[:15]:<16}{len(bysym[s]):>5}{mean(bysym[s]):>10.1f}"
              f"{100.0*sum(bysym[s])/tot if tot else float('nan'):>15.1f}%")
    print("\n    leave-one-symbol-out on the cell mean:")
    for s in sorted(bysym, key=lambda x: -abs(sum(bysym[x])))[:6]:
        rest = [r for r in win if r["symbol"] != s]
        byd = defaultdict(list)
        for r in rest:
            byd[r["entry"]].append(r["net_strict_bps"])
        mu, lo, hi, nb = boot(byd)
        flag = "" if (lo is not None and lo > 0) else "   <-- no longer clears zero"
        print(f"      drop {s[:14]:<15} n={len(rest):<5} net={mu:>8.1f} "
              f"[{lo:>8.1f}, {hi:>8.1f}]{flag}")

    print("\n" + "=" * 100)
    print("(4) TAIL DEPENDENCE -- is the cell mean carried by a few positions?")
    print("=" * 100)
    v = sorted(r["net_strict_bps"] for r in win)
    print(f"    mean {mean(v):.1f}  median {median(v):.1f}")
    for tr in (0.01, 0.05, 0.10):
        k = int(len(v) * tr)
        print(f"    trimmed {int(tr*100):>2}% each side: {mean(v[k:len(v)-k]):>8.1f}")
    print(f"    best 5 positions: {[round(x) for x in v[-5:]]}")
    print(f"    worst 5:          {[round(x) for x in v[:5]]}")

    print("\n" + "=" * 100)
    print("(5) IS THE SELECTED TIER JUST A NARROWER RANGE? "
          "(range width is set by sigma, which differs per pool)")
    print("=" * 100)
    grp = defaultdict(list)
    for r in rows:
        if r["T"] == 7 and r["c"] == 0.5:
            grp[(r["alt"], r["entry"])].append(r)
    ds, do = [], []
    for v2 in grp.values():
        s = [r for r in v2 if r["selected"]]
        o = [r for r in v2 if not r["selected"]]
        if len(s) != 1 or not o:
            continue
        ds.append(s[0])
        do.append(o)
    print(f"    pairs: {len(ds)}")
    print(f"    {'':<22}{'selected':>12}{'others':>12}")
    for lbl, key in (("fee tier (bps)", "fee"), ("in-range fraction", "in_range"),
                     ("fee revenue (bps)", "fee_bps"), ("IL (bps)", "il_bps"),
                     ("own exec cost (bps)", "cost_strict_bps"),
                     ("share of L_active", "share")):
        a = median([r[key] / (100.0 if key == "fee" else 1.0) for r in ds])
        b = median([mean([r[key] / (100.0 if key == "fee" else 1.0) for r in o])
                    for o in do])
        print(f"    {lbl:<22}{a:>12.3f}{b:>12.3f}")

    print("\n" + "=" * 100)
    print("(6) COIN FLIP -- does picking the best trailing yield beat picking at random?")
    print("=" * 100)
    rng = random.Random(11)
    real, flips = [], []
    for v2 in grp.values():
        dep = v2
        if len(dep) < 2:
            continue
        sel = [r for r in dep if r["selected"]]
        if len(sel) != 1:
            continue
        real.append(sel[0]["net_strict_bps"])
        flips.append(mean([rng.choice(dep)["net_strict_bps"] for _ in range(40)]))
    print(f"    selection rule: {mean(real):>8.1f} bps   random pick: "
          f"{mean(flips):>8.1f} bps   difference {mean(real)-mean(flips):>8.1f}")

    print("\n" + "=" * 100)
    print("(7) SIGN OF THE WHOLE GRID under strict cost, non-pegged only")
    print("=" * 100)
    print(f"    {'T':<4}{'c':<6}{'n':>6}{'blocks':>7}{'net':>9}{'lo':>9}{'hi':>9}")
    for T in (1, 7, 30):
        for c in (0.5, 1.0, 2.0):
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["selected"]
                 and not is_peg(r["symbol"])]
            if len(g) < 20:
                continue
            byd = defaultdict(list)
            for r in g:
                byd[r["entry"]].append(r["net_strict_bps"])
            mu, lo, hi, nb = boot(byd)
            print(f"    {T:<4}{c:<6}{len(g):>6}{nb:>7}{mu:>9.1f}{lo:>9.1f}{hi:>9.1f}")


if __name__ == "__main__":
    main()
