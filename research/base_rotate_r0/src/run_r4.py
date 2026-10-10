#!/usr/bin/env python3
"""Registered test: does trailing fee-per-unit-L rank symbols?

Pre-registration: ../GOAL_R4_TRAILING_YIELD.md, written before this file
produced any number. The payoff series is reused from stage3 at $50 with
measured fees; the only new work is the ranking signal, which is the
pool-level trailing yield and is not the position's own realised fee.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta

WETH = "0x4200000000000000000000000000000000000006"
PNL = "/root/base_fee_tier_r0/out/stage3_pnl_50_vfee.json"
T, C, NQ = 7, 0.5, 5
MID = "2026-02-16"
N_BOOT, ALPHA = 10_000, 0.05
RNG = random.Random(20260820)
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800", "--max_memory_usage=8000000000"]


def ch(q: str) -> str:
    p = subprocess.run(CH + ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:2500])
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


def cell(items):
    byday = defaultdict(list)
    for d, r in items:
        byday[d].append(r["net_strict_bps"])
    lo, hi = boot_days(byday)
    return dict(n=len(items), blocks=len(byday),
                net=mean(r["net_strict_bps"] for _, r in items),
                fee=mean(r["fee_bps"] for _, r in items),
                il=mean(r["il_bps"] for _, r in items),
                cost=mean(r["cost_strict_bps"] for _, r in items),
                lo=lo, hi=hi,
                win=100.0 * sum(1 for _, r in items if r["net_strict_bps"] > 0)
                    / len(items) if items else 0.0)


def dplus(d: str, k: int) -> str:
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=k)).isoformat()


print("R4  pre-registration: GOAL_R4_TRAILING_YIELD.md")
print("signal: 7-day trailing vol/L × measured monthly fee; no snapshot fallback\n")

rows = json.load(open(PNL))
sel = [r for r in rows if r["T"] == T and r["c"] == C and r["selected"] == 1]
pools = sorted({r["pool"] for r in sel})
print(f"payoff positions: {len(sel):,}  pools: {len(pools)}")

fee = {}
for ln in ch("SELECT pool, substring(toString(month),1,7), implied_bps "
             "FROM census.pool_fee_monthly").strip().split("\n"):
    if not ln:
        continue
    p, mo, bps = ln.split("\t")
    fee[(p, mo)] = float(bps)

plist = ",".join(f"'{p}'" for p in pools)
daily = defaultdict(dict)
raw = ch(f"""
SELECT s.pool, toString(toDate(s.block_time)),
       sum(if(toFloat64(s.liquidity) > 0,
              abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
              / toFloat64(s.liquidity), 0))
FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0
GROUP BY s.pool, toDate(s.block_time)
""")
for ln in raw.strip().split("\n"):
    if not ln:
        continue
    p, d, v = ln.split("\t")
    daily[p][d] = float(v)
print(f"measured pool-months: {len(fee):,}  daily yield rows: "
      f"{sum(len(v) for v in daily.values()):,}")

elig, skip = [], defaultdict(int)
for r in sel:
    bps = fee.get((r["pool"], r["entry"][:7]))
    if bps is None:
        skip["no_measured_fee"] += 1
        continue
    vol = 0.0
    n = 0
    for i in range(1, 8):
        x = daily[r["pool"]].get(dplus(r["entry"], -i))
        if x is not None:
            vol += x
            n += 1
    if n < 4:
        skip["thin_trailing"] += 1
        continue
    elig.append((bps / 1e4 * vol, r))   # implied_bps is already in bps
print(f"eligible: {len(elig):,}   skipped: {dict(skip)}")

bydate = defaultdict(list)
for yld, r in elig:
    bydate[r["entry"]].append((yld, r))

buckets = defaultdict(list)
used_dates = 0
for d, v in bydate.items():
    if len(v) < NQ * 3:
        continue
    used_dates += 1
    v.sort(key=lambda x: x[0])
    k = len(v)
    for i, (yld, r) in enumerate(v):
        buckets[min(NQ - 1, i * NQ // k)].append((d, r, yld))

print(f"dates with a full quintile cut: {used_dates}\n")
print("=" * 108)
print("MAIN -- quintiles by trailing pool yield (H1 / H2)")
print("=" * 108)
print(f"{'q':<22}{'n':>6}{'blk':>5}{'net':>9}{'lo':>9}{'hi':>9}"
      f"{'fee':>9}{'IL':>9}{'cost':>8}{'win%':>7}{'H1':>8}{'H2':>8}")
qstat = {}
for q in range(NQ):
    items = [(d, r) for d, r, _ in buckets[q]]
    if not items:
        continue
    st = cell(items)
    qstat[q] = st
    h1 = [(d, r) for d, r in items if d < MID]
    h2 = [(d, r) for d, r in items if d >= MID]
    lbl = {0: "1 lowest yield", NQ - 1: f"{NQ} highest yield"}.get(q, str(q + 1))
    print(f"{lbl:<22}{st['n']:>6}{st['blocks']:>5}{st['net']:>9.1f}"
          f"{(st['lo'] or float('nan')):>9.1f}{(st['hi'] or float('nan')):>9.1f}"
          f"{st['fee']:>9.1f}{st['il']:>9.1f}{st['cost']:>8.1f}{st['win']:>7.1f}"
          f"{(mean(r['net_strict_bps'] for _, r in h1) if h1 else float('nan')):>8.1f}"
          f"{(mean(r['net_strict_bps'] for _, r in h2) if h2 else float('nan')):>8.1f}")

top = buckets[NQ - 1]
bot = buckets[0]
print(f"\nH2: top minus bottom net = {qstat[NQ-1]['net'] - qstat[0]['net']:.1f}")
print(f"    lowest quintile mean is "
      f"{'positive (fails H2)' if qstat[0]['net'] > 0 else 'not positive (H2 ok on this clause)'}")

print("\n" + "=" * 108)
print("H3 -- top quintile vs same-date median symbol (fee vs IL)")
print("=" * 108)
dn, df, di = [], [], []
byday = defaultdict(list)
for d, v in bydate.items():
    if len(v) < NQ * 3:
        continue
    v.sort(key=lambda x: x[0])
    topv = v[-max(1, len(v) // NQ):]
    med_net = sorted(r["net_strict_bps"] for _, r in v)[len(v) // 2]
    med_fee = sorted(r["fee_bps"] for _, r in v)[len(v) // 2]
    med_il = sorted(r["il_bps"] for _, r in v)[len(v) // 2]
    x = mean(r["net_strict_bps"] for _, r in topv) - med_net
    byday[d].append(x)
    dn.append(x)
    df.append(mean(r["fee_bps"] for _, r in topv) - med_fee)
    di.append(mean(r["il_bps"] for _, r in topv) - med_il)
lo, hi = boot_days(byday)
print(f"  Δnet {mean(dn):+.1f}  [{(lo or float('nan')):.1f}, {(hi or float('nan')):.1f}]")
print(f"  Δfee {mean(df):+.1f}")
print(f"  ΔIL  {mean(di):+.1f}   H3 requires Δfee > ΔIL: {mean(df) > mean(di)}")

print("\n" + "=" * 108)
print("CONCENTRATION")
print("=" * 108)
bysym = defaultdict(list)
for d, r, _ in top:
    bysym[r["symbol"]].append((d, r))
tot = sum(r["net_strict_bps"] for d, r, _ in top)
contrib = sorted(((sum(r["net_strict_bps"] for _, r in v), s, len(v))
                  for s, v in bysym.items()), reverse=True)
print(f"{'symbol':<14}{'n':>5}{'sum_net':>10}{'pct':>8}")
for c_, s, n in contrib[:8]:
    print(f"{s:<14}{n:>5}{c_:>10.0f}{100.0 * c_ / tot if tot else 0:>8.1f}")
top1_pct = 100.0 * contrib[0][0] / tot if contrib and tot else 0.0
print(f"largest share {contrib[0][1]} = {top1_pct:.1f}%  "
      f"{'>30% → leave-one-out is the verdict' if top1_pct > 30 else 'under 30%'}")
keep = [(d, r) for d, r, _ in top if r["symbol"] != contrib[0][1]]
st = cell(keep)
print(f"leave-one-out {contrib[0][1]}: n={st['n']} net={st['net']:.1f} "
      f"[{(st['lo'] or float('nan')):.1f}, {(st['hi'] or float('nan')):.1f}]")

print("\n" + "=" * 108)
print("CAPACITY (entry share)")
print("=" * 108)
sh = sorted(r["share"] for _, r, _ in top)
k = len(sh)
over = 100.0 * sum(1 for x in sh if x > 0.10) / k
print(f"  p50 {sh[k//2]:.5f}  p90 {sh[int(0.9*k)]:.5f}  max {sh[-1]:.5f}  "
      f"pct>10% {over:.1f}%  {'VOID' if over > 5 else 'ok'}")

h1 = [(d, r) for d, r, _ in top if d < MID]
h2 = [(d, r) for d, r, _ in top if d >= MID]
print("\nHALF-YEARS (highest quintile)")
print(f"  H1 n={len(h1)} net={mean(r['net_strict_bps'] for _, r in h1):.1f}")
print(f"  H2 n={len(h2)} net={mean(r['net_strict_bps'] for _, r in h2):.1f}")
same = (mean(r["net_strict_bps"] for _, r in h1) > 0) == \
       (mean(r["net_strict_bps"] for _, r in h2) > 0)
print(f"  same sign: {same}")

st = qstat[NQ - 1]
st_loo = cell(keep)
top_clears = st["lo"] is not None and st["lo"] > 0
loo_clears = st_loo["lo"] is not None and st_loo["lo"] > 0
h2_ok = qstat[NQ - 1]["net"] > qstat[0]["net"] and qstat[0]["net"] <= 0
h3_ok = mean(df) > mean(di)
top_neg = st["hi"] is not None and st["hi"] < 0

if top_neg:
    verdict = "NO_CROSS_SYMBOL_YIELD"
elif not top_clears:
    verdict = "UNDERPOWERED"
elif (not loo_clears) or top1_pct > 30 and not loo_clears:
    verdict = "YIELD_IS_MEME_CONCENTRATION"
elif top_clears and h2_ok and not h3_ok:
    verdict = "YIELD_RANKS_BUT_IL_DOMINATES"
elif top_clears and loo_clears and same and h2_ok and h3_ok:
    verdict = "CROSS_SYMBOL_YIELD_CONFIRMED"
else:
    verdict = "UNDERPOWERED"

print(f"\nVERDICT = {verdict}")
print(f"  top clears: {top_clears}   loo clears: {loo_clears}   "
      f"same half-year sign: {same}")
print(f"  H2 (top>bottom and bottom≤0): {h2_ok}   H3 (Δfee>ΔIL): {h3_ok}")
