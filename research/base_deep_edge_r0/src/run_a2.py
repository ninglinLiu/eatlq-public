#!/usr/bin/env python3
"""A2: multi-day mean reversion on the year panel. Pre-registration ../GOAL_R1.md.

The reason this is not just another feature: rounds 35, 38a and 38b all died on the
same arithmetic, and round 38 measured it. The round trip costs about two thirds of
a 4-hour standard deviation, while the best signal found so far captures about a
tenth of one. The toll is FIXED but volatility grows with the square root of the
holding period, so at 7 days the same toll is a tenth of a standard deviation
instead of two thirds -- 6.5x more favourable. That is structural, not another
attempt to find a better predictor.

The price of the longer horizon is independent observations: at h=168 and no
overlap each symbol yields ~52 trades a year, and the day-blocked bootstrap
resamples entry days, so the block count is what limits inference. It is reported
for every cell rather than buried, because round 39 was killed by exactly this.

Costs are point-in-time per (pool, date) from census.pit_cost, using the LP pool's
own cost -- the same strict convention C2 stage 3 uses as primary.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import subprocess
import sys
from collections import defaultdict

# Trade size, and the two cost terms that behave differently under it.
# Slippage scales with size; the fee tier does not; gas does not either. At $5,000 gas
# was 0.04 bps and was ignored. At $50 it must be charged: measured from 286 real
# single-swap receipts on Base, one swap costs a MEAN of $0.0359 (median $0.0038 --
# the mean is used because a strategy that trades repeatedly pays the mean, and it is
# the conservative choice).
SIZE_USD = float(sys.argv[2]) if len(sys.argv) > 2 else 5000.0
COST_TABLE = sys.argv[1] if len(sys.argv) > 1 else "census.pit_cost"
BARS_TABLE = "edge.bars_1h_year" + ("_50" if COST_TABLE.endswith("_50") else "")
UNIV_TABLE = "edge.bars_universe" + ("_50" if COST_TABLE.endswith("_50") else "")
GAS_USD_PER_SWAP = 0.0359
GAS_ROUNDTRIP_BPS = 2.0 * GAS_USD_PER_SWAP / SIZE_USD * 1e4
MAX_COST_BPS = 100.0
L_GRID = [72, 168]
K_GRID = [1.5, 2.5]
H_GRID = [24, 72, 168]
VOL_WIN = 168
N_BOOT = 10_000
ALPHA = 0.05 / 12
MIN_TRADES, MIN_BLOCKS = 50, 20
PANEL_START, PANEL_END = "2025-08-18", "2026-08-17"
MID = "2026-02-16"
PEGS = ("ETH", "USD", "BTC", "EUR", "DAI")


def ch(q: str, fmt: str = "TabSeparatedWithNames") -> str:
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--max_execution_time=14400",
                        "--max_memory_usage=12000000000", "--format", fmt,
                        "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def is_peg(s: str) -> bool:
    return any(m in s.upper() for m in PEGS)


def pick_pools():
    """One pool per symbol: the one executable on the most days, then most swaps.

    Using several pools of the same alt would multiply-count one price process, and
    mixing a price from one pool with a cost from another would mismatch the two.
    """
    raw = ch(f"""
    SELECT u.symbol AS symbol, u.pool AS pool, u.tradeable_days AS td,
           u.cost_p50 AS cost_p50, s.n AS n_bars
    FROM {UNIV_TABLE} u
    INNER JOIN (SELECT pool, count() AS n FROM {BARS_TABLE} GROUP BY pool) s
      ON s.pool = u.pool
    """)
    best = {}
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        sym, pool, td, cp, nb = ln.split("\t")
        if is_peg(sym):
            continue
        key = (int(td), int(nb))
        if sym not in best or key > best[sym][0]:
            best[sym] = (key, pool, float(cp))
    out = {s: (v[1], v[2]) for s, v in best.items()}
    sys.stderr.write(f"[pools] non-pegged symbols: {len(out)}\n")
    return out


def load_bars(pools):
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT pool, toString(bar) AS bar, open, close, n_swaps
    FROM {BARS_TABLE} WHERE pool IN ({plist}) ORDER BY pool, bar
    """)
    out = defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, bar, o, c, n = ln.split("\t")
        out[pool].append((bar, float(o), float(c), int(n)))
    return out


def load_cost():
    raw = ch(f"""
    SELECT pool, toString(d), cost_bps_med, feasible FROM {COST_TABLE}
    WHERE cost_bps_med > 0 AND cost_bps_med <= {MAX_COST_BPS} AND feasible = 1
    """)
    out = {}
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, c, _ = ln.split("\t")
        out[(pool, d)] = float(c)
    return out


# GOAL_R1 sec.3b, written before any A2 return was computed: 28 of 656,852 bars
# move by more than 10x in an hour, up to e^161, because the pool was pushed to a
# CLMM tick extreme. They are separable from real moves -- every glitch exceeds 10x
# and the largest genuine hourly move in the panel is 2.5x -- so a bar beyond this
# is treated as a gap rather than as a price.
MAX_ABS_LR = 2.3
_glitch = {"n": 0}


def grid(series):
    """Reindex onto a complete hourly grid; a gap carries the previous close.

    Round 35's first attempt indexed by ROW, so h=4 was 'four prints later', not
    four hours. On a year panel with sparse pools that would be much worse.
    """
    if not series:
        return []
    t0 = dt.datetime.fromisoformat(series[0][0])
    t1 = dt.datetime.fromisoformat(series[-1][0])
    have = {b: (o, c, n) for b, o, c, n in series}
    out, cur, prev = [], t0, None
    while cur <= t1:
        k = cur.strftime("%Y-%m-%d %H:%M:%S")
        o = c = n = None
        if k in have:
            o, c, n = have[k]
            if prev is not None and prev > 0 and c > 0 \
                    and abs(math.log(c / prev)) > MAX_ABS_LR:
                _glitch["n"] += 1
                o = c = prev
                n = 0
            else:
                prev = c
        else:
            o = c = prev
            n = 0
        if c is not None and c > 0:
            out.append((k, o, c, n))
        cur += dt.timedelta(hours=1)
    return out


def run_symbol(sym, pool, bars, cost, L, k, h):
    g = grid(bars.get(pool, []))
    if len(g) < VOL_WIN + L + h + 10:
        return [], 0.0, 0
    close = [x[2] for x in g]
    open_ = [x[1] for x in g]
    lr = [0.0] + [math.log(close[i] / close[i - 1]) if close[i - 1] > 0 else 0.0
                  for i in range(1, len(g))]

    # unconditional h-hour log return for this symbol, same grid
    unc = [math.log(close[i + h] / close[i]) for i in range(len(g) - h)
           if close[i] > 0 and close[i + h] > 0]
    unc_mean = sum(unc) / len(unc) if unc else 0.0

    trades, i = [], VOL_WIN + L
    while i < len(g) - h - 1:
        w = lr[i - VOL_WIN + 1:i + 1]
        m = sum(w) / len(w)
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1))
        if sd <= 0 or close[i - L] <= 0:
            i += 1
            continue
        z = math.log(close[i] / close[i - L]) / (sd * math.sqrt(L))
        if z >= -k:
            i += 1
            continue
        d_entry = g[i + 1][0][:10]
        c_bps = cost.get((pool, d_entry))
        if c_bps is None:
            i += 1
            continue
        p_in, p_out = open_[i + 1], close[i + 1 + h]
        if p_in <= 0 or p_out <= 0:
            i += 1
            continue
        r = math.log(p_out / p_in)
        trades.append(dict(symbol=sym, entry=g[i + 1][0], day=d_entry, z=z,
                           L=L, k=k, h=h,
                           ret_bps=1e4 * r, edge_bps=1e4 * (r - unc_mean),
                           hurdle_bps=2.0 * c_bps + GAS_ROUNDTRIP_BPS,
                           net_bps=1e4 * (r - unc_mean) - 2.0 * c_bps
                                    - GAS_ROUNDTRIP_BPS))
        i += h + 1
    return trades, 1e4 * unc_mean, len(g)


def boot(byday, n=N_BOOT, seed=20260819):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None, None, len(ds)
    allv = [v for d in ds for v in byday[d]]
    point = sum(allv) / len(allv)
    acc = []
    for _ in range(n):
        s = []
        for _ in range(len(ds)):
            s.extend(byday[ds[rng.randrange(len(ds))]])
        if s:
            acc.append(sum(s) / len(s))
    acc.sort()
    return (point, acc[int(len(acc) * ALPHA / 2)],
            acc[int(len(acc) * (1 - ALPHA / 2))], len(ds))


def mean(a):
    return sum(a) / len(a) if a else float("nan")


def main() -> None:
    sys.stderr.write(f"[size] ${SIZE_USD:.0f} per trade, cost table {COST_TABLE}, "
                     f"gas round trip {GAS_ROUNDTRIP_BPS:.1f} bps\n")
    sel = pick_pools()
    bars = load_bars([p for p, _ in sel.values()])
    cost = load_cost()
    sys.stderr.write(f"[data] bars for {len(bars)} pools, "
                     f"{len(cost)} tradeable (pool,date) cells\n")

    cut_is = "2026-04-18"      # first 2/3 of the panel
    results, alltrades = {}, []
    print("=" * 132)
    print("A2 multi-day mean reversion: buy when z_L < -k, hold h hours. "
          "edge = conditional minus unconditional. CI at alpha=0.05/12")
    print("=" * 132)
    print(f"{'L':<5}{'k':<6}{'h':<5}{'n':>6}{'blocks':>7}{'syms':>6}"
          f"{'gross':>9}{'lo':>9}{'hi':>9}{'hurdle':>8}{'net':>9}"
          f"{'net_lo':>9}{'net_hi':>9}{'IS':>8}{'OOS':>8}{'H1':>8}{'H2':>8}")
    for L in L_GRID:
        for k in K_GRID:
            for h in H_GRID:
                tr = []
                for sym, (pool, _) in sel.items():
                    t, _u, _n = run_symbol(sym, pool, bars, cost, L, k, h)
                    tr.extend(t)
                if len(tr) < MIN_TRADES:
                    print(f"{L:<5}{k:<6}{h:<5}{len(tr):>6}   -- fewer than "
                          f"{MIN_TRADES} trades, not reported per GOAL_R1 sec.5")
                    continue
                byd_g, byd_n = defaultdict(list), defaultdict(list)
                for x in tr:
                    byd_g[x["day"]].append(x["edge_bps"])
                    byd_n[x["day"]].append(x["net_bps"])
                gm, glo, ghi, nb = boot(byd_g)
                nm, nlo, nhi, _ = boot(byd_n)
                if nb < MIN_BLOCKS:
                    print(f"{L:<5}{k:<6}{h:<5}{len(tr):>6}{nb:>7}   -- fewer than "
                          f"{MIN_BLOCKS} day blocks, not reported")
                    continue
                is_ = [x["net_bps"] for x in tr if x["day"] < cut_is]
                oos = [x["net_bps"] for x in tr if x["day"] >= cut_is]
                h1 = [x["net_bps"] for x in tr if x["day"] < MID]
                h2 = [x["net_bps"] for x in tr if x["day"] >= MID]
                key = f"L{L}_k{k}_h{h}"
                results[key] = dict(n=len(tr), blocks=nb,
                                    syms=len({x['symbol'] for x in tr}),
                                    gross=gm, gross_lo=glo, gross_hi=ghi,
                                    hurdle=mean([x["hurdle_bps"] for x in tr]),
                                    net=nm, net_lo=nlo, net_hi=nhi,
                                    is_=mean(is_), oos=mean(oos),
                                    h1=mean(h1), h2=mean(h2))
                r = results[key]
                print(f"{L:<5}{k:<6}{h:<5}{r['n']:>6}{nb:>7}{r['syms']:>6}"
                      f"{gm:>9.1f}{glo:>9.1f}{ghi:>9.1f}{r['hurdle']:>8.1f}"
                      f"{nm:>9.1f}{nlo:>9.1f}{nhi:>9.1f}"
                      f"{r['is_']:>8.1f}{r['oos']:>8.1f}{r['h1']:>8.1f}"
                      f"{r['h2']:>8.1f}")
                alltrades.extend(tr)

    sys.stderr.write(f"[glitch] bars neutralised at >10x hourly move: "
                     f"{_glitch['n']}\n")
    json.dump(results, open(f"out/a2_results{'_50' if SIZE_USD==50 else ''}.json", "w"), indent=1)
    json.dump(alltrades[:60000], open(f"out/a2_trades{'_50' if SIZE_USD==50 else ''}.json", "w"))

    print("\nhurdle versus the volatility of what is being predicted "
          "(round 38's diagnostic, recomputed per holding period):")
    print(f"{'h(hours)':<10}{'trades':>8}{'hurdle_bps':>12}{'sigma_h_bps':>13}"
          f"{'hurdle/sigma':>14}{'gross_bps':>11}{'gross/sigma':>13}")
    for h in H_GRID:
        rows = [r for k, r in results.items() if k.endswith(f"_h{h}")]
        if not rows:
            continue
        # sigma of the thing actually being predicted at THIS holding period, not
        # pooled across the grid
        sub = [x for x in alltrades if x["h"] == h]
        rs = [x["ret_bps"] for x in sub]
        if len(rs) < 2:
            continue
        m = mean(rs)
        sig = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1))
        hd = mean([r["hurdle"] for r in rows])
        gr = mean([r["gross"] for r in rows])
        print(f"{h:<10}{len(sub):>8}{hd:>12.1f}{sig:>13.1f}"
              f"{hd/sig if sig else float('nan'):>14.3f}{gr:>11.1f}"
              f"{gr/sig if sig else float('nan'):>13.3f}")

    pos = [k for k, r in results.items()
           if r["net_lo"] is not None and r["net_lo"] > 0]
    conf = [k for k in pos if results[k]["is_"] > 0 and results[k]["oos"] > 0
            and results[k]["h1"] * results[k]["h2"] > 0]
    allneg = results and all(r["net_hi"] is not None and r["net_hi"] < 0
                             for r in results.values())
    if conf:
        v = f"MR_EDGE_CONFIRMED -- {conf} clear zero and agree across IS/OOS and both half-years"
    elif pos:
        v = (f"REGIME_DEPENDENT -- {pos} clear zero but disagree across "
             f"IS/OOS or the two half-years")
    elif allneg:
        worst = max(r["net_hi"] for r in results.values())
        v = (f"NO_TRADEABLE_MR_EDGE -- every cell's upper bound is below zero; "
             f"the loosest excludes any net edge above {worst:.1f} bps")
    else:
        widest = max((r["net_hi"] - r["net_lo"] for r in results.values()
                      if r["net_hi"] is not None), default=float("nan"))
        v = (f"UNDERPOWERED -- intervals straddle zero, widest {widest:.0f} bps; "
             f"does not constitute a lead")
    print("\nVERDICT (A2) = " + v)
    json.dump({"verdict": v, "cells": results},
              open(f"out/a2_verdict{'_50' if SIZE_USD==50 else ''}.json", "w"), indent=1)


if __name__ == "__main__":
    main()
