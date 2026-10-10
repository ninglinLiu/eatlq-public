#!/usr/bin/env python3
"""C3: does market making pay on assets that are structurally quiet against WETH?

Pre-registration: ../GOAL_R3_CBBTC.md, plus ../GOAL_R3_AMENDMENT_SIZE.md which
moves the trade size to $50 and charges gas. Both were written before any C3
number existed.

The hypothesis, in one line: market-making PnL is fees minus impermanent loss,
impermanent loss is driven by the volatility of the alt/WETH *cross rate* rather
than by the alt's own volatility, so an alt that moves together with ETH should
be a better pool to provide liquidity in even if the alt itself is volatile.

Two independent things point here. Round 40's only positive cell was 103.6% one
asset -- cbBTC, a wrapped major, whose median impermanent loss was 49 bps against
966 bps of fees. Round 41 split every market-making position by which way the
price actually went and found the only positive column is the one where the price
barely moved. So the question is whether "barely moved" can be *selected in
advance* by picking the asset, rather than hoped for.

The measurement that makes this a cross-sectional prediction instead of a story
about one coin:

    ratio = sigma(alt priced in WETH) / sigma(alt priced in USD)

For a stablecoin this is large -- its USD price is fixed, so all of its WETH-priced
movement is ETH's own movement. For cbBTC it is small -- BTC/ETH moves far less
than BTC/USD. H1 says net LP return falls as this ratio rises; H2 says it does so
monotonically across bins, not just at the extreme.

cbBTC generated the hypothesis and is therefore reported but excluded from the
verdict, per section 3 of the pre-registration.
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
NOTIONAL_USD = 50.0
COST_TABLE = "census.pit_cost_50"
MAX_ENTRY_COST_BPS = 100.0
MAX_SHARE = 0.10
C_GRID = [0.5, 1.0, 2.0]
T_GRID = [7, 30]
MIN_HIST_DAYS = 30
PANEL_START, PANEL_END = "2025-08-18", "2026-08-17"
MID = "2026-02-16"
N_BOOT = 10_000
ALPHA = 0.05 / 6                    # pre-registered Bonferroni divisor
MIN_BLOCKS = 20                     # sec.7 doubt 3: fewer than this is not reported

LP_CYCLE_GAS = 1_620_000.0
GAS_USD_PER_GAS_MEAN = 1.61e-7
GAS_USD_PER_GAS_TRIM = 7.14e-8
LP_GAS_BPS = LP_CYCLE_GAS * GAS_USD_PER_GAS_MEAN / NOTIONAL_USD * 1e4
LP_GAS_BPS_TRIM = LP_CYCLE_GAS * GAS_USD_PER_GAS_TRIM / NOTIONAL_USD * 1e4

# Wrapped claims on off-chain majors, from GOAL_R3_CBBTC.md sec.3. cbBTC is the
# generating group and is held out of every verdict.
GENERATING = {"cbBTC"}
WRAPPED_MAJORS = {
    "cbBTC", "tBTC", "cbXRP", "uXRP", "cbADA", "uADA", "cbLTC", "uSOL",
    "uDOGE", "uSUI", "LINK", "AAVE", "MORPHO", "ZEN", "BGCI", "CMC20", "LCAP",
}

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=12000000000"]


def ch(q: str, fmt: str = "TabSeparatedWithNames", stdin: str | None = None) -> str:
    cmd = CH + ["--format", fmt, "--query", q] if fmt != "TSV" else \
        CH + ["--query", q]
    p = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def dplus(d: str, k: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=k)).isoformat()


def load_universe() -> list[dict]:
    syms = ",".join(f"'{s}'" for s in WRAPPED_MAJORS)
    raw = ch(f"""
    WITH c AS (
      SELECT pool, quantile(0.5)(cost_bps_med) AS cost, count() AS days
      FROM {COST_TABLE}
      WHERE feasible = 1 AND cost_bps_med > 0 AND cost_bps_med <= {MAX_ENTRY_COST_BPS}
      GROUP BY pool HAVING days >= 90)
    SELECT p.pool AS pool,
           if(p.token0 = '{WETH}', p.token1, p.token0) AS alt,
           p.venue AS venue, p.fee AS fee, p.tick_spacing AS tick_spacing,
           if(p.token1 = '{WETH}', 1, 0) AS weth_is_token1,
           t.symbol AS symbol, t.decimals AS decimals, c.cost AS cost,
           c.days AS days
    FROM census.pools p
    INNER JOIN c ON c.pool = p.pool
    INNER JOIN census.tokens t
      ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
    WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
      AND t.symbol IN ({syms})
    ORDER BY symbol, cost
    """)
    out = []
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        (pool, alt, venue, fee, ts, w1, sym, dec, cost, days) = ln.split("\t")
        out.append(dict(pool=pool, alt=alt, venue=venue, fee=int(fee),
                        tick_spacing=int(ts), weth_is_token1=int(w1),
                        symbol=sym, decimals=int(dec), cost=float(cost),
                        days=int(days)))
    return out


def load_cost(pools) -> dict:
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT pool, toString(d), cost_bps_med, feasible FROM {COST_TABLE}
    WHERE pool IN ({plist}) AND cost_bps_med > 0 AND cost_bps_med < 20000
    """)
    out = {}
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, c, f = ln.split("\t")
        out[(pool, d)] = (float(c), int(f))
    return out


def load_daily(pools) -> dict:
    """Per (pool, day): closing price, swap count, fee-yield input, and the 10th
    percentile of *instantaneous* liquidity.

    Round 40's capacity gate used the median of trailing daily median liquidity
    while fee attribution ran on per-swap instantaneous liquidity, and the gate
    leaked in the direction of the result: 8.2% of positions in the winning cell
    had already crossed 10% realized share, and that subset's median net was 8x
    the compliant subset's. The amendment fixes the gate to the q10 of
    instantaneous liquidity.
    """
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT s.pool AS pool, toString(toDate(s.block_time)) AS d,
           argMax(toString(s.sqrt_price_x96), (s.block_number, s.log_index)) AS sp,
           count() AS n,
           quantile(0.1)(toFloat64(s.liquidity)) AS L_q10,
           quantile(0.5)(toFloat64(s.liquidity)) AS L_p50,
           sum(if(toFloat64(s.liquidity) > 0,
                  abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                  / toFloat64(s.liquidity), 0)) AS vol_over_L
    FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
    WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0
    GROUP BY pool, d
    """)
    out: dict = defaultdict(dict)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, sp, n, lq, lm, vL = ln.split("\t")
        out[pool][d] = dict(p=(int(sp) / 2.0 ** 96) ** 2, n=int(n),
                            L_q10=float(lq), L_p50=float(lm),
                            vol_over_L=float(vL))
    return out


def load_hourly(pools) -> dict:
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT pool, toString(toStartOfHour(block_time)) AS h,
           argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sp
    FROM census.cl_swaps
    WHERE pool IN ({plist}) AND sqrt_price_x96 > 0
    GROUP BY pool, h ORDER BY pool, h
    """)
    out: dict = defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, h, sp = ln.split("\t")
        out[pool].append((h, (int(sp) / 2.0 ** 96) ** 2))
    return out


def load_eth() -> dict:
    raw = ch("""SELECT toString(toDate(bar)), quantile(0.5)(eth_usd)
                FROM edge.eth_usd_1h GROUP BY 1""")
    return {a: float(b) for a, b in
            (ln.split("\t") for ln in raw.strip().split("\n")[1:] if ln)}


def to_own(p_raw: float, w1: bool, dec: int) -> float:
    """Raw sqrt-price square to alt priced in WETH, decimals corrected."""
    scale = 10.0 ** (18 - dec)
    return p_raw / scale if w1 else 1.0 / (p_raw * scale)


def sigma_and_price(hrs, d, w1, dec, days=7):
    """Trailing sample sd of hourly log returns of the cross rate, and last price."""
    end = f"{d} 00:00:00"
    start = f"{dplus(d, -days)} 00:00:00"
    v = [to_own(x[1], w1, dec) for x in hrs if start <= x[0] < end]
    last = None
    for h, p in hrs:
        if h < end:
            last = to_own(p, w1, dec)
        else:
            break
    if len(v) < 24 or last is None or last <= 0:
        return None, last
    lr = [math.log(v[i] / v[i - 1]) for i in range(1, len(v))
          if v[i] > 0 and v[i - 1] > 0 and abs(math.log(v[i] / v[i - 1])) < 2.3]
    if len(lr) < 20:
        return None, last
    m = sum(lr) / len(lr)
    var = sum((x - m) ** 2 for x in lr) / (len(lr) - 1)
    return (math.sqrt(var) if var > 0 else None), last


def vol_ratio(hrs, w1, dec, eth) -> float | None:
    """sigma(alt in WETH) / sigma(alt in USD), over the whole panel.

    The dose variable of H1. Small means the alt tracks ETH, so its cross rate is
    quiet relative to its own price -- the cbBTC case. Large means the alt is
    pegged to something ETH is not, so all of its WETH-priced motion is ETH's --
    the stablecoin case, which section 7 of the pre-registration predicts should
    land in the bad bin and must not be excused after the fact.
    """
    cross, own = [], []
    prev_c = prev_o = None
    for h, p_raw in hrs:
        e = eth.get(h[:10])
        if not e:
            continue
        c = to_own(p_raw, w1, dec)
        if c <= 0:
            continue
        o = c * e
        if prev_c and prev_o:
            try:
                a, b = math.log(c / prev_c), math.log(o / prev_o)
            except ValueError:
                prev_c, prev_o = c, o
                continue
            if abs(a) < 2.3 and abs(b) < 2.3:
                cross.append(a)
                own.append(b)
        prev_c, prev_o = c, o
    if len(cross) < 500:
        return None
    def sd(v):
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    so = sd(own)
    return sd(cross) / so if so > 0 else None


def amounts(L, p, pa, pb):
    sp, spa, spb = math.sqrt(p), math.sqrt(pa), math.sqrt(pb)
    if sp <= spa:
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if sp >= spb:
        return 0.0, L * (spb - spa)
    return L * (1.0 / sp - 1.0 / spb), L * (sp - spa)


def to_weth(a0, a1, p, w1):
    return (a1 + a0 * p) if w1 else (a0 + a1 / p)


def build(univ, cost, day, hrs, eth):
    by_alt = defaultdict(list)
    for c in univ:
        by_alt[c["alt"]].append(c)
    pos, skip = [], defaultdict(int)
    base = dplus(PANEL_START, MIN_HIST_DAYS)
    for T in T_GRID:
        entries, k = [], 0
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
            for alt, cands in by_alt.items():
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
                    # amendment: q10 of instantaneous liquidity, taken as the
                    # minimum of the trailing daily q10s -- for a 7-day window
                    # that is about the 14th percentile of the daily figures and
                    # is deliberately more conservative than a pooled q10
                    L_act = min(x["L_q10"] for x in tw)
                    if L_act <= 0:
                        skip["no_liquidity"] += 1
                        continue
                    ty = c_["fee"] / 1e6 * sum(x["vol_over_L"] for x in tw)
                    w1 = bool(c_["weth_is_token1"])
                    sig, p_own = sigma_and_price(hrs.get(pl, []), d, w1,
                                                 c_["decimals"])
                    if sig is None or not p_own or p_own <= 0:
                        skip["no_sigma"] += 1
                        continue
                    for cc in C_GRID:
                        w = cc * sig * math.sqrt(24.0 * T)
                        if not (0 < w <= 5):
                            skip["degenerate_width"] += 1
                            continue
                        ts = c_["tick_spacing"]
                        # ticks are in raw-price space, so undo the decimals
                        # correction before quantising and redo it after
                        scale = 10.0 ** (18 - c_["decimals"])
                        p_raw = p_own * scale if w1 else 1.0 / (p_own * scale)
                        tl = math.floor(math.log(p_raw * math.exp(-w))
                                        / math.log(1.0001) / ts) * ts
                        tu = math.ceil(math.log(p_raw * math.exp(w))
                                       / math.log(1.0001) / ts) * ts
                        if tu <= tl:
                            continue
                        pa, pb = 1.0001 ** tl, 1.0001 ** tu
                        a0u, a1u = amounts(1.0, p_raw, pa, pb)
                        if a0u < 0 or a1u < 0:
                            continue
                        vu = to_weth(a0u, a1u, p_raw, w1)
                        if vu <= 0:
                            continue
                        L = v_weth / vu
                        tier_rows.append(dict(
                            alt=alt, symbol=c_["symbol"], pool=pl,
                            venue=c_["venue"], fee=c_["fee"], tick_spacing=ts,
                            weth_is_token1=int(w1), decimals=c_["decimals"],
                            entry=d, exit=dplus(d, T), T=T, c=cc,
                            tick_lower=tl, tick_upper=tu, L=L,
                            share=L / L_act, L_act=L_act,
                            trailing_yield=ty, p_entry=p_raw, sigma_7d=sig,
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
    sys.stderr.write(f"[build] positions={len(pos)} "
                     f"selected={sum(p['selected'] for p in pos)}\n")
    for k2, v in sorted(skip.items(), key=lambda x: -x[1])[:8]:
        sys.stderr.write(f"  skipped {k2:<30} {v}\n")
    return pos


def earn(pos):
    """Exact per-swap fee attribution, plus the realized fee share for the audit."""
    ch("DROP TABLE IF EXISTS census.c3x_pos", fmt="TSV")
    ch("""CREATE TABLE census.c3x_pos (
      pos_id UInt32, pool String, entry Date, T UInt16,
      tick_lower Int64, tick_upper Int64, L Float64, fee Int64
    ) ENGINE = MergeTree ORDER BY (T, pool, entry)""", fmt="TSV")
    tsv = "\n".join(
        f"{i}\t{p['pool']}\t{p['entry']}\t{p['T']}\t{p['tick_lower']}\t"
        f"{p['tick_upper']}\t{p['L']!r}\t{p['fee']}"
        for i, p in enumerate(pos))
    ch("INSERT INTO census.c3x_pos FORMAT TabSeparated", fmt="TSV",
       stdin=tsv + "\n")
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
          WHERE s.pool IN (SELECT DISTINCT pool FROM census.c3x_pos WHERE T = {T})
                AND s.sqrt_price_x96 > 0 AND s.block_time >= toDate('{base}')
        )
        SELECT p.pos_id AS pos_id,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0))
                 AS fees_raw,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * p.L / (sw.Lact + p.L), 0))
                 AS share_num,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt, 0)) AS share_den,
               countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in,
               count() AS n_win
        FROM sw INNER JOIN census.c3x_pos p
          ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T}
        GROUP BY p.pos_id
        """)
        for ln in out.strip().split("\n")[1:]:
            if not ln:
                continue
            pid, f, sn, sd_, ni, nw = ln.split("\t")
            den = float(sd_)
            res[int(pid)] = dict(fees_weth=float(f) / 1e18, n_in=int(ni),
                                 n_win=int(nw),
                                 realized_share=(float(sn) / den) if den > 0 else 0.0)
    for i in range(len(pos)):
        res.setdefault(i, dict(fees_weth=0.0, n_in=0, n_win=0,
                               realized_share=0.0))
    return res


PRICE_FLOOR, PRICE_CEIL = 1e-30, 1e30


def pnl(pos, f, day):
    rows, dropped = [], 0
    for i, p in enumerate(pos):
        dd = day.get(p["pool"], {}).get(p["exit"])
        if not dd:
            # exit day had no swap; walk forward up to 5 days
            for k in range(1, 6):
                dd = day.get(p["pool"], {}).get(dplus(p["exit"], k))
                if dd:
                    break
        if not dd:
            continue
        p_out = dd["p"]
        if not (PRICE_FLOOR < p_out < PRICE_CEIL) or \
           not (PRICE_FLOOR < p["p_entry"] < PRICE_CEIL):
            dropped += 1
            continue
        pa, pb = 1.0001 ** p["tick_lower"], 1.0001 ** p["tick_upper"]
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
        rec = dict(symbol=p["symbol"], pool=p["pool"], entry=p["entry"],
                   T=p["T"], c=p["c"], fee=p["fee"], selected=p["selected"],
                   share=p["share"], realized_share=f[i]["realized_share"],
                   fee_bps=1e4 * fw / v_ref, il_bps=1e4 * il / v_ref,
                   in_range=(f[i]["n_in"] / f[i]["n_win"]) if f[i]["n_win"] else 0.0)
        sc = (alt_in + alt_out) * p["cost_strict_bps"] / 1e4
        rec["cost_bps"] = 1e4 * sc / v_ref + LP_GAS_BPS
        rec["net_bps"] = 1e4 * (fw - il - sc) / v_ref - LP_GAS_BPS
        rec["net_trimgas_bps"] = 1e4 * (fw - il - sc) / v_ref - LP_GAS_BPS_TRIM
        if all(math.isfinite(rec[k]) for k in ("fee_bps", "il_bps", "net_bps")):
            rows.append(rec)
    sys.stderr.write(f"[pnl] rows={len(rows)} dropped_at_tick_extreme={dropped}\n")
    return rows


def mean(a):
    v = list(a)
    return sum(v) / len(v) if v else float("nan")


def median(a):
    s = sorted(a)
    k = len(s)
    return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])


def boot(byday, n=N_BOOT, seed=20260819):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None
    sums = [sum(byday[d]) for d in ds]
    cnts = [len(byday[d]) for d in ds]
    nd = len(ds)
    ms = []
    for _ in range(n):
        s = c = 0.0
        for _ in range(nd):
            j = rng.randrange(nd)
            s += sums[j]
            c += cnts[j]
        ms.append(s / c if c else 0.0)
    ms.sort()
    return ms[int(ALPHA / 2 * n)], ms[int((1 - ALPHA / 2) * n)]


def cell(rows, sel_only=True):
    v = [r for r in rows if (r["selected"] == 1 or not sel_only)]
    if not v:
        return None
    byday = defaultdict(list)
    for r in v:
        byday[r["entry"]].append(r["net_bps"])
    lo, hi = boot(byday)
    return dict(n=len(v), blocks=len(byday), mean=mean(r["net_bps"] for r in v),
                med=median([r["net_bps"] for r in v]), lo=lo, hi=hi,
                fee=mean(r["fee_bps"] for r in v),
                il=mean(r["il_bps"] for r in v),
                cost=mean(r["cost_bps"] for r in v),
                trim=mean(r["net_trimgas_bps"] for r in v),
                win=100.0 * sum(1 for r in v if r["net_bps"] > 0) / len(v))


def main() -> None:
    print(f"C3 cross-rate market making at ${NOTIONAL_USD:.0f} per position")
    print(f"LP gas charged: {LP_GAS_BPS:.1f} bps per cycle "
          f"(trimmed regime {LP_GAS_BPS_TRIM:.1f})")
    print(f"Bonferroni alpha = {ALPHA:.5f}   pre-registration: "
          f"GOAL_R3_CBBTC.md + GOAL_R3_AMENDMENT_SIZE.md\n")

    univ = load_universe()
    if not univ:
        raise SystemExit("no wrapped-major pools passed the universe filter")
    bysym = defaultdict(list)
    for c in univ:
        bysym[c["symbol"]].append(c)
    print(f"{'symbol':<10}{'pools':>6}{'cheapest_cost_bps':>19}{'tradeable_days':>16}"
          f"   group")
    for s in sorted(bysym):
        cs = bysym[s]
        grp = "GENERATING (held out)" if s in GENERATING else "confirmation"
        print(f"{s:<10}{len(cs):>6}{min(c['cost'] for c in cs):>19.1f}"
              f"{max(c['days'] for c in cs):>16}   {grp}")
    pools = [c["pool"] for c in univ]
    sys.stderr.write(f"[universe] {len(bysym)} symbols, {len(pools)} pools\n")

    cost = load_cost(pools)
    day = load_daily(pools)
    hrs = load_hourly(pools)
    eth = load_eth()
    sys.stderr.write(f"[data] cost rows {len(cost)}, pools with dailies {len(day)}\n")

    # dose variable, one per symbol, from the deepest pool of that symbol
    print("\nH1 dose variable: sigma(alt in WETH) / sigma(alt in USD). "
          "small = tracks ETH.")
    print(f"{'symbol':<10}{'vol_ratio':>11}   group")
    ratio = {}
    for s in sorted(bysym):
        best = min(bysym[s], key=lambda c: c["cost"])
        r = vol_ratio(hrs.get(best["pool"], []), bool(best["weth_is_token1"]),
                      best["decimals"], eth)
        if r is not None:
            ratio[s] = r
            grp = "GENERATING" if s in GENERATING else "confirmation"
            print(f"{s:<10}{r:>11.3f}   {grp}")

    pos = build(univ, cost, day, hrs, eth)
    if not pos:
        raise SystemExit("no positions built")
    f = earn(pos)
    rows = pnl(pos, f, day)

    conf = [r for r in rows if r["symbol"] not in GENERATING]
    gen = [r for r in rows if r["symbol"] in GENERATING]

    print("\n" + "=" * 118)
    print("MAIN TEST -- confirmation group only (cbBTC excluded per "
          "pre-registration sec.3), selected tier, strict cost, net vs HODL")
    print("=" * 118)
    print(f"{'T':>4}{'c':>6}{'n':>7}{'blocks':>8}{'net':>9}{'lo':>9}{'hi':>9}"
          f"{'med':>9}{'fee':>9}{'IL':>9}{'cost':>8}{'win%':>7}{'trimgas':>9}")
    results = {}
    for T in T_GRID:
        for c in C_GRID:
            v = [r for r in conf if r["T"] == T and r["c"] == c]
            r = cell(v)
            if not r:
                continue
            results[f"T{T}_c{c}"] = r
            flag = "" if r["blocks"] >= MIN_BLOCKS else "  <- blocks<20, not reported"
            print(f"{T:>4}{c:>6}{r['n']:>7}{r['blocks']:>8}{r['mean']:>9.1f}"
                  f"{(r['lo'] or 0):>9.1f}{(r['hi'] or 0):>9.1f}{r['med']:>9.1f}"
                  f"{r['fee']:>9.1f}{r['il']:>9.1f}{r['cost']:>8.1f}"
                  f"{r['win']:>7.1f}{r['trim']:>9.1f}{flag}")

    print("\n" + "=" * 118)
    print("GENERATING GROUP -- cbBTC, reported for comparison, NOT evidence")
    print("=" * 118)
    print(f"{'T':>4}{'c':>6}{'n':>7}{'blocks':>8}{'net':>9}{'med':>9}"
          f"{'fee':>9}{'IL':>9}")
    for T in T_GRID:
        for c in C_GRID:
            v = [r for r in gen if r["T"] == T and r["c"] == c]
            r = cell(v)
            if not r:
                continue
            print(f"{T:>4}{c:>6}{r['n']:>7}{r['blocks']:>8}{r['mean']:>9.1f}"
                  f"{r['med']:>9.1f}{r['fee']:>9.1f}{r['il']:>9.1f}")

    print("\n" + "=" * 118)
    print("H2 DOSE RESPONSE -- confirmation group binned by vol_ratio. "
          "H1 predicts net FALLS as ratio rises.")
    print("=" * 118)
    have = sorted((s, ratio[s]) for s in ratio if s not in GENERATING)
    if len(have) >= 3:
        third = max(1, len(have) // 3)
        bins = [("low ratio (tracks ETH)", have[:third]),
                ("mid ratio", have[third:2 * third]),
                ("high ratio (independent of ETH)", have[2 * third:])]
        print(f"{'bin':<34}{'symbols':<44}{'T':>4}{'n':>7}{'net':>9}{'lo':>9}"
              f"{'hi':>9}{'fee':>9}{'IL':>9}")
        dose = {}
        for T in T_GRID:
            for label, members in bins:
                syms = {s for s, _ in members}
                v = [r for r in conf if r["T"] == T and r["symbol"] in syms
                     and r["c"] == 1.0]
                r = cell(v)
                if not r:
                    continue
                dose[f"{label}|T{T}"] = r["mean"]
                print(f"{label:<34}{','.join(sorted(syms))[:42]:<44}{T:>4}"
                      f"{r['n']:>7}{r['mean']:>9.1f}{(r['lo'] or 0):>9.1f}"
                      f"{(r['hi'] or 0):>9.1f}{r['fee']:>9.1f}{r['il']:>9.1f}")
        for T in T_GRID:
            seq = [dose.get(f"{lab}|T{T}") for lab, _ in bins]
            if all(x is not None for x in seq):
                mono = all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1))
                print(f"  T={T}: {[round(x, 1) for x in seq]}  "
                      f"monotone decreasing = {mono}")

    print("\n" + "=" * 118)
    print("AUDIT 1 -- capacity honesty (amendment sec. on the q10 gate)")
    print("=" * 118)
    print(f"{'T':>4}{'c':>6}{'n':>7}{'realized_share_p50':>20}"
          f"{'p90':>8}{'pct_over_10%':>14}   verdict")
    for T in T_GRID:
        for c in C_GRID:
            v = [r for r in conf if r["T"] == T and r["c"] == c]
            if not v:
                continue
            rs = sorted(r["realized_share"] for r in v)
            over = 100.0 * sum(1 for x in rs if x > MAX_SHARE) / len(rs)
            print(f"{T:>4}{c:>6}{len(v):>7}{rs[len(rs) // 2]:>20.4f}"
                  f"{rs[int(0.9 * len(rs))]:>8.4f}{over:>14.1f}"
                  f"   {'VOID' if over > 5.0 else 'ok'}")

    print("\n" + "=" * 118)
    print("AUDIT 2 -- concentration guard (a single symbol may not carry >30%)")
    print("=" * 118)
    for T in T_GRID:
        for c in C_GRID:
            v = [r for r in conf if r["T"] == T and r["c"] == c
                 and r["selected"] == 1]
            if len(v) < 20:
                continue
            bysy = defaultdict(float)
            for r in v:
                bysy[r["symbol"]] += r["net_bps"]
            tot = sum(bysy.values())
            if abs(tot) < 1e-9:
                continue
            top, topv = max(bysy.items(), key=lambda kv: abs(kv[1]))
            pct = 100.0 * topv / tot
            loo = [r for r in v if r["symbol"] != top]
            r2 = None
            if loo:
                byday = defaultdict(list)
                for r in loo:
                    byday[r["entry"]].append(r["net_bps"])
                lo, hi = boot(byday)
                r2 = (mean(r["net_bps"] for r in loo), lo, hi, len(loo))
            msg = (f"leave-one-out net {r2[0]:.1f} [{(r2[1] or 0):.1f}, "
                   f"{(r2[2] or 0):.1f}] on {r2[3]} positions" if r2 else "n/a")
            print(f"  T={T} c={c}: {len(bysy)} symbols, largest is {top} "
                  f"carrying {pct:.1f}% -> {msg}")

    print("\n" + "=" * 118)
    print("REGIME -- two half-years (sec.7 doubt 4: BTC/ETH being slow may be "
          "this year's regime)")
    print("=" * 118)
    print(f"{'T':>4}{'c':>6}{'H1_net':>9}{'H1_n':>7}{'H2_net':>9}{'H2_n':>7}"
          f"   same sign")
    for T in T_GRID:
        for c in C_GRID:
            a = [r for r in conf if r["T"] == T and r["c"] == c
                 and r["selected"] == 1 and r["entry"] < MID]
            b = [r for r in conf if r["T"] == T and r["c"] == c
                 and r["selected"] == 1 and r["entry"] >= MID]
            if len(a) < 5 or len(b) < 5:
                continue
            ma, mb = mean(r["net_bps"] for r in a), mean(r["net_bps"] for r in b)
            print(f"{T:>4}{c:>6}{ma:>9.1f}{len(a):>7}{mb:>9.1f}{len(b):>7}"
                  f"   {(ma > 0) == (mb > 0)}")

    json.dump(dict(results=results, ratio=ratio,
                   n_conf=len(conf), n_gen=len(gen)),
              open("out/c3_results.json", "w"), indent=1)
    json.dump(rows, open("out/c3_pnl.json", "w"))


if __name__ == "__main__":
    main()
