#!/usr/bin/env python3
"""C2 stage 3 on the full year, and C1's 30-day cell as a by-product.

Pre-registration: ../GOAL_R2.md. Five changes from stage2.py, nothing else moved:

  1. Panel 94.6 days -> 366 days. T=7 goes from 9 independent entry dates to ~48,
     which was round 39's fatal weakness.
  2. T grid gains 30. Round 36 called T=30 UNTESTABLE on 2 windows; a year gives ~11.
  3. Bonferroni divisor 6 -> 9.
  4. Cost is now point-in-time per (pool, date) from census.pit_cost instead of one
     static R2 number per pool. On a year panel a single number measured in August
     2026 would be charging September 2025 positions the wrong toll.
  5. The primary cost convention is strict -- each position pays its own pool's
     cost -- because round 39's only positive cell survived only under the
     generous one. The generous variant is still computed, and reported as
     secondary, so the gap between them is visible rather than hidden.

The non-selected tiers are C1: market making without tier choice. So one run
answers both questions, and the paired difference between the arms isolates the
selection because impermanent loss is common to them.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"
MAX_ENTRY_COST_BPS = 100.0
MAX_SHARE = 0.10
# Position size. Every earlier round used 5000, which was wrong for this account: the
# real size is about $50. Only the execution-cost term is size-dependent -- fee_bps and
# il_bps are both ratios to notional and are invariant while the position stays a small
# share of the pool, which at $50 it always is. That invariance is why $50 also
# dissolves the capacity-gate flaw that invalidated round 40's positive result.
# Gas, however, becomes material: a mint+burn cycle plus the two inventory swaps is
# about 1.62M gas, which is 0.03 bps at $5,000 and tens of bps at $50.
NOTIONAL_USD = float(sys.argv[1]) if len(sys.argv) > 1 else 5000.0
SUF = "" if NOTIONAL_USD == 5000.0 else f"_{int(NOTIONAL_USD)}"
COST_TABLE = "census.pit_cost" + SUF
# Round 43: census.pools.fee is one reading taken when a pool was resolved, and the
# fee is not constant -- Aerodrome Slipstream pools can have it changed. One pool
# was recorded at 25 bps and charges 2.6-4.4, and because tier selection ranks by
# fee times volume over liquidity, the selection rule went looking for exactly that
# error and put 42 of 47 cbBTC positions in it. With --verified-fees the fee comes
# from census.pool_fee_monthly, solved from price impact month by month, so it is
# measured rather than looked up, and it can move over the year the way real fees
# do. Default is unchanged so earlier rounds still reproduce.
VERIFIED_FEES = "--verified-fees" in sys.argv
LP_CYCLE_GAS = 1_620_000.0
GAS_USD_PER_GAS_MEAN = 1.61e-7      # 286 real receipts, mean regime (conservative)
GAS_USD_PER_GAS_TRIM = 7.14e-8      # same, 99% trimmed (one 10 gwei spike removed)
LP_GAS_BPS = LP_CYCLE_GAS * GAS_USD_PER_GAS_MEAN / NOTIONAL_USD * 1e4
LP_GAS_BPS_TRIM = LP_CYCLE_GAS * GAS_USD_PER_GAS_TRIM / NOTIONAL_USD * 1e4
C_GRID = [0.5, 1.0, 2.0]
T_GRID = [1, 7, 30]
MIN_HIST_DAYS = 30
PANEL_START, PANEL_END = "2025-08-18", "2026-08-17"
MID = "2026-02-16"                # half-year split for the regime check
N_BOOT = 10_000
ALPHA = 0.05 / 9
MIN_N = 20


def ch(q: str, fmt: str = "TabSeparatedWithNames", stdin: str | None = None,
       mem: int = 30_000_000_000) -> str:
    cmd = ["docker", "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd += ["eatlq-clickhouse-1", "clickhouse-client", "--max_execution_time=14400",
            f"--max_memory_usage={mem}", "--format", fmt, "--query", q]
    p = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:5000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def dplus(d: str, k: int) -> str:
    y, m, dd = map(int, d.split("-"))
    return (dt.date(y, m, dd) + dt.timedelta(days=k)).isoformat()


def load_universe():
    rows = ch("""
    SELECT u.pool, u.venue, u.fee, u.tick_spacing, u.alt, u.weth_is_token1,
           t.symbol
    FROM census.pit_universe u LEFT JOIN census.tokens t ON t.token = u.alt
    """).strip().split("\n")[1:]
    byalt = defaultdict(list)
    for ln in rows:
        if not ln:
            continue
        pool, ven, fee, ts, alt, w1, sym = ln.split("\t")
        byalt[alt].append(dict(pool=pool, venue=ven, fee=int(fee),
                               tick_spacing=int(ts) or 1, alt=alt,
                               weth_is_token1=int(w1), symbol=sym or alt[:10]))
    keep = {a: v for a, v in byalt.items() if len(v) >= 2}
    sys.stderr.write(f"[universe] alts with >=2 pools: {len(keep)}  "
                     f"pools: {sum(len(v) for v in keep.values())}\n")
    return keep


FEE_PIT: dict[tuple[str, str], int] = {}
FEE_FALLBACK = [0, 0]


def load_fee_pit():
    """Charged fee per (pool, month), in the same hundredths-of-a-bip unit as
    census.pools.fee so it is a drop-in substitute."""
    if not VERIFIED_FEES:
        return
    raw = ch("""SELECT pool, substring(toString(month), 1, 7), implied_bps, n_obs
                FROM census.pool_fee_monthly""")
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, mo, bps, n = ln.split("\t")
        FEE_PIT[(pool, mo)] = int(round(float(bps) * 100.0))
    sys.stderr.write(f"[fee] measured pool-months loaded: {len(FEE_PIT)}\n")


def fee_at(cand: dict, d: str) -> int:
    """The fee that pool actually charged in the month containing d.

    Falls back to the snapshot when a pool-month has too few clean observations to
    solve, and the fallback is counted so the report can state how much of the
    result still rests on unmeasured values.
    """
    if not VERIFIED_FEES:
        return cand["fee"]
    v = FEE_PIT.get((cand["pool"], d[:7]))
    if v is None or v <= 0:
        FEE_FALLBACK[1] += 1
        return cand["fee"]
    FEE_FALLBACK[0] += 1
    return v


def load_cost():
    """Point-in-time cost and feasibility per (pool, date)."""
    raw = ch(f"""
    SELECT pool, toString(d), cost_bps_med, feasible
    FROM {COST_TABLE} WHERE cost_bps_med > 0 AND cost_bps_med < 20000
    """)
    out = {}
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, c, f = ln.split("\t")
        out[(pool, d)] = (float(c), int(f))
    sys.stderr.write(f"[cost] {COST_TABLE} rows loaded: {len(out)}\n")
    return out


def load_daily(pools):
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT s.pool AS pool, toString(toDate(s.block_time)) AS d,
           argMax(toString(s.sqrt_price_x96), (s.block_number, s.log_index)) AS sp,
           count() AS n,
           quantile(0.5)(toFloat64(s.liquidity)) AS L_p50,
           sum(if(toFloat64(s.liquidity) > 0,
                  abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                  / toFloat64(s.liquidity), 0)) AS vol_over_L
    FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
    WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0
    GROUP BY pool, d
    """)
    out = defaultdict(dict)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, sp, n, L, vL = ln.split("\t")
        out[pool][d] = dict(p=(int(sp) / 2.0 ** 96) ** 2, n=int(n),
                            L=float(L), vol_over_L=float(vL))
    return out


def load_hourly(pools):
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT pool, toString(toStartOfHour(block_time)) AS h,
           argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sp
    FROM census.cl_swaps
    WHERE pool IN ({plist}) AND sqrt_price_x96 > 0
    GROUP BY pool, h ORDER BY pool, h
    """)
    out = defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, h, sp = ln.split("\t")
        out[pool].append((h, (int(sp) / 2.0 ** 96) ** 2))
    return out


def eth_daily():
    raw = ch("""SELECT toString(toDate(bar)), quantile(0.5)(eth_usd)
                FROM edge.eth_usd_1h GROUP BY 1""")
    return {ln.split("\t")[0]: float(ln.split("\t")[1])
            for ln in raw.strip().split("\n")[1:] if ln}


def sigma_and_price(hrs, entry):
    cut, lo = entry + " 00:00:00", dplus(entry, -7) + " 00:00:00"
    px, last = [], None
    for h, p in hrs:
        if h >= cut:
            break
        last = p
        if h >= lo:
            px.append(p)
    if last is None or len(px) < 24:
        return 0.0, 0.0
    r = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))
         if px[i] > 0 and px[i - 1] > 0]
    if len(r) < 12:
        return 0.0, 0.0
    m = sum(r) / len(r)
    s = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    return (s, last) if s > 0 and math.isfinite(s) else (0.0, 0.0)


def amounts(L, p, pa, pb):
    sp, spa, spb = math.sqrt(max(p, 1e-300)), math.sqrt(pa), math.sqrt(pb)
    if p <= pa:
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if p >= pb:
        return 0.0, L * (spb - spa)
    return L * (1.0 / sp - 1.0 / spb), L * (sp - spa)


def to_weth(a0, a1, p, w1):
    return (a0 * p + a1) if w1 else (a0 + a1 / p)


def build(keep, cost, day, hrs, eth):
    pos, skip = [], defaultdict(int)
    base = dplus(PANEL_START, MIN_HIST_DAYS)
    for T in T_GRID:
        k, entries = 0, []
        while True:
            d = dplus(base, k * T)
            if dplus(d, T) > PANEL_END:
                break
            entries.append(d)
            k += 1
        for d in entries:
            win = [dplus(d, -i) for i in range(1, 8)]
            e = eth.get(d)
            if not e:
                skip["no_eth"] += 1
                continue
            v_weth = (NOTIONAL_USD / e) * 1e18
            for alt, cands in keep.items():
                priced = [c for c in cands if (c["pool"], d) in cost]
                if len(priced) < 2:
                    skip["fewer_than_2_priced_pools"] += 1
                    continue
                feas = [c for c in priced if cost[(c["pool"], d)][1] == 1]
                if not feas:
                    skip["no_feasible_pool"] += 1
                    continue
                cheapest = min(cost[(c["pool"], d)][0] for c in feas)
                if cheapest > MAX_ENTRY_COST_BPS:
                    skip["alt_too_expensive"] += 1
                    continue
                ref = max(priced, key=lambda c: sum(
                    day[c["pool"]].get(x, {}).get("n", 0) for x in win))
                sig, p_ref = sigma_and_price(hrs.get(ref["pool"], []), d)
                if sig <= 0 or p_ref <= 0:
                    skip["no_sigma"] += 1
                    continue

                tier_rows = []
                for c_ in priced:
                    pl = c_["pool"]
                    own_cost, own_feas = cost[(pl, d)]
                    if own_feas != 1:
                        skip["pool_not_feasible"] += 1
                        continue
                    tw = [day[pl][x] for x in win if x in day[pl]]
                    if len(tw) < 4:
                        skip["thin_trailing"] += 1
                        continue
                    L_act = sorted(x["L"] for x in tw)[len(tw) // 2]
                    if L_act <= 0:
                        continue
                    fee_pit = fee_at(c_, d)
                    ty = fee_pit / 1e6 * sum(x["vol_over_L"] for x in tw)
                    _, p_own = sigma_and_price(hrs.get(pl, []), d)
                    if p_own <= 0:
                        continue
                    for cc in C_GRID:
                        w = cc * sig * math.sqrt(24.0 * T)
                        if not (0 < w <= 5):
                            skip["degenerate_width"] += 1
                            continue
                        ts = c_["tick_spacing"]
                        tl = math.floor(math.log(p_own * math.exp(-w))
                                        / math.log(1.0001) / ts) * ts
                        tu = math.ceil(math.log(p_own * math.exp(w))
                                       / math.log(1.0001) / ts) * ts
                        if tu <= tl:
                            continue
                        pa, pb = 1.0001 ** tl, 1.0001 ** tu
                        a0u, a1u = amounts(1.0, p_own, pa, pb)
                        if a0u < 0 or a1u < 0:
                            continue
                        vu = to_weth(a0u, a1u, p_own, bool(c_["weth_is_token1"]))
                        if vu <= 0:
                            continue
                        L = v_weth / vu
                        tier_rows.append(dict(
                            alt=alt, symbol=c_["symbol"], pool=pl,
                            venue=c_["venue"], fee=fee_pit,
                            fee_snapshot=c_["fee"], tick_spacing=ts,
                            weth_is_token1=c_["weth_is_token1"], entry=d,
                            exit=dplus(d, T), T=T, c=cc, tick_lower=tl,
                            tick_upper=tu, L=L, share=L / L_act,
                            trailing_yield=ty, p_entry=p_own, sigma_7d=sig,
                            amt0_entry=a0u * L, amt1_entry=a1u * L,
                            cost_strict_bps=own_cost, cost_cheap_bps=cheapest,
                            eth_usd=e))
                for cc in C_GRID:
                    dep = [r for r in tier_rows
                           if r["c"] == cc and r["share"] <= MAX_SHARE]
                    if len(dep) < 2:
                        skip["fewer_than_2_deployable"] += 1
                        continue
                    sel = max(dep, key=lambda r: r["trailing_yield"])
                    for r in dep:
                        r["selected"] = 1 if r is sel else 0
                        r["n_deployable"] = len(dep)
                        pos.append(r)
    sys.stderr.write(f"[build] positions={len(pos)} selected="
                     f"{sum(p['selected'] for p in pos)}\n")
    for k2, v in sorted(skip.items(), key=lambda x: -x[1])[:8]:
        sys.stderr.write(f"  skipped {k2:<30} {v}\n")
    return pos


def fees_for(pos):
    ch("DROP TABLE IF EXISTS census.c3_pos", fmt="TSV")
    ch("""CREATE TABLE census.c3_pos (
            pos_id UInt32, pool String, entry Date, T UInt16,
            tick_lower Int32, tick_upper Int32, L Float64, fee UInt32
          ) ENGINE = MergeTree ORDER BY (T, pool, entry)""", fmt="TSV")
    tsv = "\n".join("\t".join(map(str, [i, p["pool"], p["entry"], p["T"],
                                        p["tick_lower"], p["tick_upper"],
                                        int(p["L"]), p["fee"]]))
                    for i, p in enumerate(pos))
    ch("INSERT INTO census.c3_pos FORMAT TabSeparated", fmt="TSV", stdin=tsv + "\n")
    res = {}
    for T in T_GRID:
        base = min(p["entry"] for p in pos if p["T"] == T)
        sys.stderr.write(f"[fees] T={T} base={base} ...\n")
        out = ch(f"""
        WITH sw AS (
          SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
                 abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                   AS weth_amt,
                 toDate('{base}') + {T} * intDiv(toDate(s.block_time)
                   - toDate('{base}'), {T}) AS entry_d
          FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
          WHERE s.pool IN (SELECT DISTINCT pool FROM census.c3_pos WHERE T = {T})
                AND s.sqrt_price_x96 > 0 AND s.block_time >= toDate('{base}')
        )
        SELECT p.pos_id AS pos_id,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0))
                 AS fees_raw,
               countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in,
               count() AS n_win
        FROM sw INNER JOIN census.c3_pos p
          ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T}
        GROUP BY p.pos_id
        """)
        for ln in out.strip().split("\n")[1:]:
            if not ln:
                continue
            pid, f, ni, nw = ln.split("\t")
            res[int(pid)] = dict(fees_weth=float(f) / 1e18, n_in=int(ni),
                                 n_win=int(nw))
    for i in range(len(pos)):
        res.setdefault(i, dict(fees_weth=0.0, n_in=0, n_win=0))
    return res


def mean(a):
    return sum(a) / len(a)


def median(a):
    s = sorted(a)
    k = len(s)
    return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])


def boot(byday, stat=mean, n=N_BOOT, seed=20260819):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None, None, len(ds)
    point = stat([v for d in ds for v in byday[d]])
    acc = []
    for _ in range(n):
        s = []
        for _ in range(len(ds)):
            s.extend(byday[ds[rng.randrange(len(ds))]])
        if s:
            acc.append(stat(s))
    acc.sort()
    return (point, acc[int(len(acc) * ALPHA / 2)],
            acc[int(len(acc) * (1 - ALPHA / 2))], len(ds))


# A Uniswap V3 pool lives on ticks [-887272, 887272], i.e. prices
# [1.0001**-887272, 1.0001**887272] = [2.94e-39, 3.40e38]. A pool sitting AT a tick
# extreme has been drained, and its printed price is not a market quote.
#
# One such pool is in this panel: bsdETH/WETH 0xdea629c5 traded at 0.958 on
# 2025-12-06, printed exactly 2.9389568101428185e-39 = 1.0001**-887272 on
# 2025-12-07, and never traded again. Marking a position against that quote made
# the HODL benchmark (which holds some bsdETH) worth 1.6e42 WETH and produced a
# -1.6e42 bps "return" that, being a mean, moved every T=1 cell by 38 orders of
# magnitude.
#
# The position itself is not the problem: at MIN_TICK the LP holds pure token0.
# What is missing is a price at which to mark the benchmark, so the observation is
# undefined and is dropped rather than booked as a loss. 3 of 36,153 rows.
PRICE_FLOOR, PRICE_CEIL = 1e-30, 1e30


def pnl_rows(pos, f, day):
    rows, dropped = [], 0
    for i, p in enumerate(pos):
        pa, pb = 1.0001 ** p["tick_lower"], 1.0001 ** p["tick_upper"]
        p_out = None
        for kk in range(1, 40):
            x = dplus(p["exit"], -kk)
            if x in day.get(p["pool"], {}):
                p_out = day[p["pool"]][x]["p"]
                break
        if not p_out or p_out <= 0:
            continue
        if not (PRICE_FLOOR < p_out < PRICE_CEIL
                and PRICE_FLOOR < p["p_entry"] < PRICE_CEIL):
            dropped += 1
            continue
        w1 = bool(p["weth_is_token1"])
        a0i, a1i = p["amt0_entry"], p["amt1_entry"]
        a0o, a1o = amounts(p["L"], p_out, pa, pb)
        v_lp = to_weth(a0o, a1o, p_out, w1) / 1e18
        v_hodl = to_weth(a0i, a1i, p_out, w1) / 1e18
        alt_in = (a0i * p["p_entry"] if w1 else a1i / p["p_entry"]) / 1e18
        alt_out = (a0o * p_out if w1 else a1o / p_out) / 1e18
        fw = f[i]["fees_weth"]
        v_ref = NOTIONAL_USD / p["eth_usd"]
        il = v_hodl - v_lp
        rec = dict(alt=p["alt"], symbol=p["symbol"], pool=p["pool"],
                   entry=p["entry"], T=p["T"], c=p["c"], fee=p["fee"],
                   selected=p["selected"], share=p["share"],
                   fee_bps=1e4 * fw / v_ref, il_bps=1e4 * il / v_ref,
                   gross_bps=1e4 * (fw - il) / v_ref,
                   in_range=(f[i]["n_in"] / f[i]["n_win"]) if f[i]["n_win"] else 0.0,
                   p_entry=p["p_entry"], p_out=p_out)
        for tag, rate in (("strict", p["cost_strict_bps"]),
                          ("cheap", p["cost_cheap_bps"])):
            swap_cost = (alt_in + alt_out) * rate / 1e4
            gas = LP_GAS_BPS / 1e4 * v_ref
            rec[f"cost_{tag}_bps"] = 1e4 * (swap_cost + gas) / v_ref
            rec[f"net_{tag}_bps"] = 1e4 * (fw - il - swap_cost - gas) / v_ref
            rec[f"net_{tag}_nogas_bps"] = 1e4 * (fw - il - swap_cost) / v_ref
            rec[f"net_{tag}_trimgas_bps"] = (
                1e4 * (fw - il - swap_cost) / v_ref - LP_GAS_BPS_TRIM)
        if all(math.isfinite(rec[k]) for k in
               ("fee_bps", "il_bps", "net_strict_bps", "net_cheap_bps")):
            rows.append(rec)
    sys.stderr.write(f"[pnl] dropped_at_tick_extreme={dropped}\n")
    return rows


def seg(rows, lo, hi):
    return [r for r in rows if lo <= r["entry"] < hi]


def main() -> None:
    print(f"fee source: {'MEASURED per pool-month (census.pool_fee_monthly)' if VERIFIED_FEES else 'census.pools.fee snapshot'}")
    keep = load_universe()
    load_fee_pit()
    cost = load_cost()
    pools = sorted({c["pool"] for v in keep.values() for c in v})
    sys.stderr.write(f"[data] daily+hourly for {len(pools)} pools\n")
    day, hrs, eth = load_daily(pools), load_hourly(pools), eth_daily()
    pos = build(keep, cost, day, hrs, eth)
    if not pos:
        raise SystemExit("no positions")
    f = fees_for(pos)
    rows = pnl_rows(pos, f, day)
    sys.stderr.write(f"[pnl] rows={len(rows)}\n")
    if VERIFIED_FEES:
        got, fell = FEE_FALLBACK
        print(f"fee coverage: {got:,} pool-days measured, {fell:,} fell back to "
              f"the snapshot ({100.0 * fell / max(1, got + fell):.1f}%)")
    tag = "_vfee" if VERIFIED_FEES else ""
    json.dump(rows, open(f"out/stage3_pnl{SUF}{tag}.json", "w"))

    for arm, lbl in ((1, "TEST A -- selected tier (C2)"),
                     (0, "C1 BASELINE -- tiers not selected (plain market making)")):
        print("\n" + "=" * 120)
        print(f"{lbl}, net vs HODL in bps of ${NOTIONAL_USD:,.0f}. "
              f"PRIMARY = strict cost "
              f"(own pool). CI at alpha=0.05/9")
        print("=" * 120)
        print(f"{'T':<4}{'c':<6}{'n':>6}{'blocks':>7}{'net_strict':>11}{'lo':>9}"
              f"{'hi':>9}{'med':>8}{'net_cheap':>11}{'fee':>8}{'IL':>8}{'cost':>8}"
              f"{'win%':>7}{'H1':>8}{'H2':>8}")
        for T in T_GRID:
            for c in C_GRID:
                g = [r for r in rows if r["T"] == T and r["c"] == c
                     and r["selected"] == arm]
                if len(g) < MIN_N:
                    continue
                byd = defaultdict(list)
                for r in g:
                    byd[r["entry"]].append(r["net_strict_bps"])
                mu, lo, hi, nb = boot(byd)
                h1 = seg(g, PANEL_START, MID)
                h2 = seg(g, MID, PANEL_END)
                print(f"{T:<4}{c:<6}{len(g):>6}{nb:>7}"
                      f"{(mu if mu is not None else float('nan')):>11.1f}"
                      f"{(lo if lo is not None else float('nan')):>9.1f}"
                      f"{(hi if hi is not None else float('nan')):>9.1f}"
                      f"{median([r['net_strict_bps'] for r in g]):>8.1f}"
                      f"{mean([r['net_cheap_bps'] for r in g]):>11.1f}"
                      f"{median([r['fee_bps'] for r in g]):>8.1f}"
                      f"{median([r['il_bps'] for r in g]):>8.1f}"
                      f"{median([r['cost_strict_bps'] for r in g]):>8.1f}"
                      f"{100.0*sum(1 for r in g if r['net_strict_bps']>0)/len(g):>7.1f}"
                      f"{(mean([r['net_strict_bps'] for r in h1]) if h1 else float('nan')):>8.1f}"
                      f"{(mean([r['net_strict_bps'] for r in h2]) if h2 else float('nan')):>8.1f}")

    print("\n" + "=" * 120)
    print("TEST B -- paired: selected minus mean of the other deployable tiers "
          "(impermanent loss cancels)")
    print("=" * 120)
    print(f"{'T':<4}{'c':<6}{'pairs':>6}{'blocks':>7}{'d_net':>9}{'lo':>9}{'hi':>9}"
          f"{'d_fee':>9}{'d_IL':>9}{'d_cost':>9}{'win%':>7}{'H1':>8}{'H2':>8}")
    outB = {}
    for T in T_GRID:
        for c in C_GRID:
            grp = defaultdict(list)
            for r in rows:
                if r["T"] == T and r["c"] == c:
                    grp[(r["alt"], r["entry"])].append(r)
            byd, dn, df, di, dc = defaultdict(list), [], [], [], []
            h1, h2 = [], []
            for (alt, d), v in grp.items():
                s = [r for r in v if r["selected"]]
                o = [r for r in v if not r["selected"]]
                if len(s) != 1 or not o:
                    continue
                x = s[0]["net_strict_bps"] - mean([r["net_strict_bps"] for r in o])
                byd[d].append(x)
                dn.append(x)
                df.append(s[0]["fee_bps"] - mean([r["fee_bps"] for r in o]))
                di.append(s[0]["il_bps"] - mean([r["il_bps"] for r in o]))
                dc.append(s[0]["cost_strict_bps"]
                          - mean([r["cost_strict_bps"] for r in o]))
                (h1 if d < MID else h2).append(x)
            if len(dn) < MIN_N:
                continue
            mu, lo, hi, nb = boot(byd)
            outB[f"T{T}_c{c}"] = dict(pairs=len(dn), blocks=nb, mean=mu, lo=lo,
                                      hi=hi, d_fee=mean(df), d_il=mean(di),
                                      d_cost=mean(dc),
                                      h1=mean(h1) if h1 else None,
                                      h2=mean(h2) if h2 else None)
            print(f"{T:<4}{c:<6}{len(dn):>6}{nb:>7}"
                  f"{(mu if mu is not None else float('nan')):>9.1f}"
                  f"{(lo if lo is not None else float('nan')):>9.1f}"
                  f"{(hi if hi is not None else float('nan')):>9.1f}"
                  f"{mean(df):>9.1f}{mean(di):>9.1f}{mean(dc):>9.1f}"
                  f"{100.0*sum(1 for x in dn if x>0)/len(dn):>7.1f}"
                  f"{(mean(h1) if h1 else float('nan')):>8.1f}"
                  f"{(mean(h2) if h2 else float('nan')):>8.1f}")

    outA = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["selected"]]
            if len(g) < MIN_N:
                continue
            byd = defaultdict(list)
            for r in g:
                byd[r["entry"]].append(r["net_strict_bps"])
            mu, lo, hi, nb = boot(byd)
            h1 = seg(g, PANEL_START, MID)
            h2 = seg(g, MID, PANEL_END)
            outA[f"T{T}_c{c}"] = dict(n=len(g), blocks=nb, mean=mu, lo=lo, hi=hi,
                                      h1=mean([r["net_strict_bps"] for r in h1]) if h1 else None,
                                      h2=mean([r["net_strict_bps"] for r in h2]) if h2 else None)

    def flips(d):
        return [k for k, v in d.items()
                if v.get("h1") is not None and v.get("h2") is not None
                and v["h1"] * v["h2"] < 0]

    bpos = [k for k, v in outB.items() if v["lo"] is not None and v["lo"] > 0]
    apos = [k for k, v in outA.items() if v["lo"] is not None and v["lo"] > 0]
    bflip, aflip = flips(outB), flips(outA)
    if bpos and apos and not (set(bpos) & set(bflip)) and not (set(apos) & set(aflip)):
        verdict = f"TIER_SELECTION_PAYS -- B on {bpos}, A on {apos}, both segments agree"
    elif bpos and (set(bpos) & set(bflip)):
        verdict = f"REGIME_DEPENDENT -- B clears zero on {bpos} but half-year signs disagree on {sorted(set(bpos) & set(bflip))}"
    elif bpos:
        verdict = (f"TIER_SELECTION_WORKS_BUT_LP_STILL_LOSES -- B clears zero on "
                   f"{bpos}; no cell of A does under the strict cost convention")
    else:
        worst = max((v["hi"] for v in outB.values() if v["hi"] is not None),
                    default=float("nan"))
        verdict = (f"NO_TIER_SELECTION_EFFECT -- no cell of B clears zero "
                   f"(loosest upper bound {worst:.1f})")
    print("\nVERDICT (C2) = " + verdict)

    c1 = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in rows if r["T"] == T and r["c"] == c
                 and not r["selected"]]
            if len(g) < MIN_N:
                continue
            byd = defaultdict(list)
            for r in g:
                byd[r["entry"]].append(r["net_strict_bps"])
            mu, lo, hi, nb = boot(byd)
            c1[f"T{T}_c{c}"] = dict(n=len(g), blocks=nb, mean=mu, lo=lo, hi=hi)
    c30 = {k: v for k, v in c1.items() if k.startswith("T30")}
    if c30 and all(v["hi"] is not None and v["hi"] < 0 for v in c30.values()):
        v1 = (f"NO_LP_EDGE_30D -- every 30-day cell's upper bound < 0 "
              f"(loosest {max(v['hi'] for v in c30.values()):.1f})")
    elif c30 and any(v["lo"] is not None and v["lo"] > 0 for v in c30.values()):
        v1 = ("LP_EDGE_CONFIRMED_30D -- "
              + ", ".join(k for k, v in c30.items() if v["lo"] and v["lo"] > 0))
    else:
        v1 = "UNDERPOWERED_30D -- 30-day intervals straddle zero"
    print("VERDICT (C1, 30 day) = " + v1)

    # Diagnostic, not a universe change: rounds 35 and 38 excluded WETH
    # derivatives and stablecoins from DIRECTIONAL tests because their alt/WETH
    # price is a peg. For market making a peg is a legitimate and arguably the most
    # realistic venue, so they stay in, but if the result lives only in one group
    # that has to be visible.
    print("\n" + "=" * 120)
    print("DIAGNOSTIC -- pegged pairs (WETH derivatives, stables, BTC wrappers) "
          "vs the rest. Selected arm, strict cost.")
    print("=" * 120)
    print(f"{'T':<4}{'c':<6}{'peg_n':>7}{'peg_net':>9}{'peg_fee':>9}{'peg_IL':>9}"
          f"{'alt_n':>7}{'alt_net':>9}{'alt_fee':>9}{'alt_IL':>9}")
    pegs = ("ETH", "USD", "BTC", "EUR", "DAI")

    def is_peg(s):
        return any(m in s.upper() for m in pegs)

    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["selected"]]
            a = [r for r in g if is_peg(r["symbol"])]
            b = [r for r in g if not is_peg(r["symbol"])]
            if len(a) < MIN_N and len(b) < MIN_N:
                continue
            def blk(v, k):
                return mean([r[k] for r in v]) if v else float("nan")
            print(f"{T:<4}{c:<6}{len(a):>7}{blk(a,'net_strict_bps'):>9.1f}"
                  f"{blk(a,'fee_bps'):>9.1f}{blk(a,'il_bps'):>9.1f}"
                  f"{len(b):>7}{blk(b,'net_strict_bps'):>9.1f}"
                  f"{blk(b,'fee_bps'):>9.1f}{blk(b,'il_bps'):>9.1f}")

    # Round 39's only positive cell was positive under the generous convention and
    # not under the strict one, which is why GOAL_R2 made strict primary. Reporting
    # the generous variant with its own interval, rather than just its mean, is what
    # makes the size of that dependence readable instead of a footnote.
    print("\n" + "=" * 120)
    print("SECONDARY -- same tests under the GENEROUS cost convention "
          "(execute in the pair's cheapest feasible pool)")
    print("=" * 120)
    print(f"{'test':<8}{'T':<4}{'c':<6}{'n':>7}{'blocks':>7}{'mean':>9}{'lo':>9}"
          f"{'hi':>9}")
    gen = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["selected"]]
            if len(g) >= MIN_N:
                byd = defaultdict(list)
                for r in g:
                    byd[r["entry"]].append(r["net_cheap_bps"])
                mu, lo, hi, nb = boot(byd)
                gen[f"A_T{T}_c{c}"] = dict(mean=mu, lo=lo, hi=hi, n=len(g))
                print(f"{'A':<8}{T:<4}{c:<6}{len(g):>7}{nb:>7}{mu:>9.1f}"
                      f"{lo:>9.1f}{hi:>9.1f}")
            grp = defaultdict(list)
            for r in rows:
                if r["T"] == T and r["c"] == c:
                    grp[(r["alt"], r["entry"])].append(r)
            byd = defaultdict(list)
            nn = 0
            for (alt, d), v in grp.items():
                s = [r for r in v if r["selected"]]
                o = [r for r in v if not r["selected"]]
                if len(s) != 1 or not o:
                    continue
                byd[d].append(s[0]["net_cheap_bps"]
                              - mean([r["net_cheap_bps"] for r in o]))
                nn += 1
            if nn >= MIN_N:
                mu, lo, hi, nb = boot(byd)
                gen[f"B_T{T}_c{c}"] = dict(mean=mu, lo=lo, hi=hi, n=nn)
                print(f"{'B':<8}{T:<4}{c:<6}{nn:>7}{nb:>7}{mu:>9.1f}"
                      f"{lo:>9.1f}{hi:>9.1f}")

    json.dump({"C2": verdict, "C1_30d": v1, "testA": outA, "testB": outB,
               "C1": c1, "generous": gen},
              open(f"out/stage3_verdict{SUF}{tag}.json", "w"), indent=1)


if __name__ == "__main__":
    main()
