#!/usr/bin/env python3
"""C3: does market-making net PnL rise with how closely the alt tracks WETH?

Pre-registration: ../GOAL_R3_CBBTC.md, frozen before any C3 number existed.

Round 40's only cell that cleared zero drew 103.6% of its net from one asset,
cbBTC, whose 7-day positions showed 966 bps of fees against 49 bps of
impermanent loss. That asset is the hypothesis generator and is therefore
excluded from this round's verdict: BTC and ETH move together, so the BTC/ETH
cross is slow while the cbBTC/WETH pools still trade heavily. Stated as a
cross-sectional prediction the claim is testable away from cbBTC, which is what
this file does.

Three things differ from stage3.py, all of them mandated by the pre-registration:

  1. Universe is the wrapped-major family, split into a generation group (cbBTC,
     reported but never part of a verdict) and a confirmation group (the rest,
     which decides).
  2. The capacity gate moves onto instantaneous liquidity. Round 40 gated on the
     rolling 7-day MEDIAN of `liquidity` while attributing fees off each swap's
     instantaneous `liquidity`; 8.2% of positions in the winning cell had already
     crossed the gate's own 10% threshold on realised share, and that 8.2% had 8x
     the median net of the compliant part. The gate leaked toward the result.
     Here it is the minimum over the trailing window of each day's q10, which is
     causal and strictly more conservative than a pooled q10.
  3. A concentration gate: any cell where a single symbol supplies >30% of total
     net is judged on its leave-one-out interval, not its headline. The absence of
     this rule is what let round 40 print a false positive.

Primary convention is the real $50 notional. At $50 an LP cycle's 1.62M gas is
about 52 bps, which is why round 40's +78.9 became -12.9 when the size was
corrected; a $50-scale claim has to clear that toll on top of everything else.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import random
import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"

# The generation group. Present in every table and figure, absent from every verdict.
GEN = {"cbBTC"}
# The confirmation group: wrapped claims on off-chain majors, per GOAL_R3 section 3.
CONFIRM = {"tBTC", "cbXRP", "uXRP", "cbADA", "uADA", "cbLTC", "uSOL", "uDOGE",
           "uSUI", "LINK", "AAVE", "MORPHO", "ZEN", "BGCI", "CMC20", "LCAP"}
FAMILY = GEN | CONFIRM

NOTIONAL_USD = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0
SUF = "" if NOTIONAL_USD == 5000.0 else f"_{int(NOTIONAL_USD)}"
COST_TABLE = "census.pit_cost" + SUF
POS_TABLE = "census.c4_pos"

LP_CYCLE_GAS = 1_620_000.0
GAS_USD_PER_GAS_MEAN = 1.61e-7
GAS_USD_PER_GAS_TRIM = 7.14e-8
LP_GAS_BPS = LP_CYCLE_GAS * GAS_USD_PER_GAS_MEAN / NOTIONAL_USD * 1e4
LP_GAS_BPS_TRIM = LP_CYCLE_GAS * GAS_USD_PER_GAS_TRIM / NOTIONAL_USD * 1e4

MAX_ENTRY_COST_BPS = 100.0
MAX_SHARE = 0.10
C_GRID = [0.5, 1.0, 2.0]
T_GRID = [7, 30]                  # T=1 was confirmed loss-making in round 40
MIN_HIST_DAYS = 30
PANEL_START, PANEL_END = "2025-08-18", "2026-08-17"
MID = "2026-02-16"
N_BOOT = 10_000
ALPHA = 0.05 / 6                  # six cells
MIN_N = 20
MIN_BLOCKS = 20                   # GOAL_R3 section 7.3
CONC_LIMIT = 0.30                 # GOAL_R3 section 4.2
SHARE_AUDIT_LIMIT = 0.05          # GOAL_R3 section 4.1
PRICE_FLOOR, PRICE_CEIL = 1e-30, 1e30


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


def rows_of(raw: str):
    out = []
    for ln in raw.strip().split("\n")[1:]:
        if ln:
            out.append(ln.split("\t"))
    return out


def dplus(d: str, k: int) -> str:
    y, m, dd = map(int, d.split("-"))
    return (dt.date(y, m, dd) + dt.timedelta(days=k)).isoformat()


def load_universe():
    syms = ",".join(f"'{s}'" for s in sorted(FAMILY))
    raw = ch(f"""
    SELECT u.pool AS pool, u.venue AS venue, u.fee AS fee,
           u.tick_spacing AS ts, u.alt AS alt, u.weth_is_token1 AS w1,
           t.symbol AS symbol
    FROM census.pit_universe u LEFT JOIN census.tokens t ON t.token = u.alt
    WHERE t.symbol IN ({syms})
    """)
    byalt = defaultdict(list)
    for pool, ven, fee, ts, alt, w1, sym in rows_of(raw):
        byalt[alt].append(dict(pool=pool, venue=ven, fee=int(fee),
                               tick_spacing=int(ts) or 1, alt=alt,
                               weth_is_token1=int(w1), symbol=sym,
                               grp="gen" if sym in GEN else "confirm"))
    keep = {a: v for a, v in byalt.items() if len(v) >= 2}
    ng = sum(1 for v in keep.values() if v[0]["grp"] == "gen")
    sys.stderr.write(
        f"[universe] family alts seen={len(byalt)}  with>=2 pools={len(keep)} "
        f"(gen={ng}, confirm={len(keep)-ng})  pools={sum(len(v) for v in keep.values())}\n")
    bysym = defaultdict(int)
    for v in keep.values():
        bysym[v[0]["symbol"]] += len(v)
    sys.stderr.write("  " + "  ".join(f"{k}:{v}" for k, v in sorted(bysym.items())) + "\n")
    return keep


def load_cost():
    raw = ch(f"""SELECT pool, toString(d), cost_bps_med, feasible
                 FROM {COST_TABLE} WHERE cost_bps_med > 0 AND cost_bps_med < 20000""")
    out = {}
    for pool, d, c, f in rows_of(raw):
        out[(pool, d)] = (float(c), int(f))
    sys.stderr.write(f"[cost] {COST_TABLE} rows={len(out)}\n")
    return out


def load_daily(pools):
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT s.pool AS pool, toString(toDate(s.block_time)) AS d,
           argMax(toString(s.sqrt_price_x96), (s.block_number, s.log_index)) AS sp,
           count() AS n,
           quantile(0.1)(toFloat64(s.liquidity)) AS L_q10,
           sum(if(toFloat64(s.liquidity) > 0,
                  abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                  / toFloat64(s.liquidity), 0)) AS vol_over_L
    FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
    WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0
    GROUP BY pool, d
    """)
    out = defaultdict(dict)
    for pool, d, sp, n, lq, vl in rows_of(raw):
        out[pool][d] = dict(p=(int(sp) / 2.0 ** 96) ** 2, n=int(n),
                            L_q10=float(lq), vol_over_L=float(vl))
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
    for pool, h, sp in rows_of(raw):
        out[pool].append((h, (int(sp) / 2.0 ** 96) ** 2))
    return out


def eth_daily():
    raw = ch("""SELECT toString(toDate(bar)), quantile(0.5)(eth_usd)
                FROM edge.eth_usd_1h GROUP BY 1""")
    return {r[0]: float(r[1]) for r in rows_of(raw)}


def eth_hourly():
    raw = ch("""SELECT toString(bar), eth_usd FROM edge.eth_usd_1h
                WHERE eth_usd > 0 ORDER BY bar""")
    hs, vs = [], []
    for h, v in rows_of(raw):
        hs.append(h)
        vs.append(float(v))
    sys.stderr.write(f"[eth] hourly bars={len(hs)}\n")
    return hs, vs


def eth_at(ethh, h):
    """Last ETH/USD print at or before hour h. Forward fill, never look ahead."""
    hs, vs = ethh
    i = bisect.bisect_right(hs, h) - 1
    return vs[i] if i >= 0 else None


def sd(xs):
    if len(xs) < 12:
        return 0.0
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(v) if v > 0 else 0.0


def trailing_hours(hrs, entry, days=7):
    cut, lo = entry + " 00:00:00", dplus(entry, -days) + " 00:00:00"
    px, last = [], None
    for h, p in hrs:
        if h >= cut:
            break
        last = p
        if h >= lo and PRICE_FLOOR < p < PRICE_CEIL:
            px.append((h, p))
    return px, last


def sigma_and_price(hrs, entry):
    px, last = trailing_hours(hrs, entry)
    if last is None or len(px) < 24:
        return 0.0, 0.0
    r = [math.log(px[i][1] / px[i - 1][1]) for i in range(1, len(px))]
    s = sd(r)
    return (s, last) if s > 0 and math.isfinite(s) else (0.0, 0.0)


def vol_ratio(hrs, entry, w1, ethh):
    """sigma(alt/WETH) / sigma(alt/USD) over the causal trailing week.

    The pool prints token1/token0. If WETH is token1 the printed price is
    WETH/alt, so ln(alt/WETH) = -ln(p); otherwise it is +ln(p). Getting that sign
    wrong would turn the cross series into the sum instead of the difference and
    invert the whole hypothesis, so the orientation is taken from the universe
    table rather than inferred.

    A low ratio means the alt tracks WETH: its own moves are mostly ETH's moves,
    and what is left over -- the cross the LP is actually exposed to -- is small.
    """
    px, _ = trailing_hours(hrs, entry)
    if len(px) < 24:
        return None
    xs = []
    for h, p in px:
        e = eth_at(ethh, h)
        if e is None or e <= 0:
            continue
        lp = math.log(p)
        x = (-lp if w1 else lp)          # ln(alt/WETH)
        xs.append((x, x + math.log(e)))  # (cross, own in USD)
    if len(xs) < 24:
        return None
    dc = [xs[i][0] - xs[i - 1][0] for i in range(1, len(xs))]
    do = [xs[i][1] - xs[i - 1][1] for i in range(1, len(xs))]
    sc, so = sd(dc), sd(do)
    if sc <= 0 or so <= 0 or not (math.isfinite(sc) and math.isfinite(so)):
        return None
    return dict(sigma_cross=sc, sigma_own=so, ratio=sc / so)


def amounts(L, p, pa, pb):
    sp, spa, spb = math.sqrt(max(p, 1e-300)), math.sqrt(pa), math.sqrt(pb)
    if p <= pa:
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if p >= pb:
        return 0.0, L * (spb - spa)
    return L * (1.0 / sp - 1.0 / spb), L * (sp - spa)


def to_weth(a0, a1, p, w1):
    return (a0 * p + a1) if w1 else (a0 + a1 / p)


def build(keep, cost, day, hrs, eth, ethh):
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
                vr = vol_ratio(hrs.get(ref["pool"], []), d,
                               bool(ref["weth_is_token1"]), ethh)
                if vr is None:
                    skip["no_vol_ratio"] += 1
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
                    L_act = min(x["L_q10"] for x in tw)
                    if L_act <= 0:
                        skip["zero_q10_liquidity"] += 1
                        continue
                    ty = c_["fee"] / 1e6 * sum(x["vol_over_L"] for x in tw)
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
                            alt=alt, symbol=c_["symbol"], grp=c_["grp"], pool=pl,
                            venue=c_["venue"], fee=c_["fee"], tick_spacing=ts,
                            weth_is_token1=c_["weth_is_token1"], entry=d,
                            exit=dplus(d, T), T=T, c=cc, tick_lower=tl,
                            tick_upper=tu, L=L, share_q10=L / L_act,
                            trailing_yield=ty, p_entry=p_own, sigma_7d=sig,
                            sigma_cross=vr["sigma_cross"], sigma_own=vr["sigma_own"],
                            ratio=vr["ratio"], amt0_entry=a0u * L, amt1_entry=a1u * L,
                            cost_strict_bps=own_cost, cost_cheap_bps=cheapest,
                            eth_usd=e))
                for cc in C_GRID:
                    dep = [r for r in tier_rows
                           if r["c"] == cc and r["share_q10"] <= MAX_SHARE]
                    if len(dep) < 2:
                        skip["fewer_than_2_deployable_q10"] += 1
                        continue
                    sel = max(dep, key=lambda r: r["trailing_yield"])
                    for r in dep:
                        r["selected"] = 1 if r is sel else 0
                        r["n_deployable"] = len(dep)
                        pos.append(r)
    sys.stderr.write(f"[build] positions={len(pos)} selected="
                     f"{sum(p['selected'] for p in pos)}\n")
    for k2, v in sorted(skip.items(), key=lambda x: -x[1])[:10]:
        sys.stderr.write(f"  skipped {k2:<32} {v}\n")
    return pos


def fees_for(pos):
    """Fee attribution plus the pool's own total, so realised share is auditable.

    Round 40 could only bound my fees from above by the pool total; here the
    ratio itself is carried through to the report because GOAL_R3 section 4.1
    voids any cell where more than 5% of positions realise over 10% of pool fees.
    """
    ch(f"DROP TABLE IF EXISTS {POS_TABLE}", fmt="TSV")
    ch(f"""CREATE TABLE {POS_TABLE} (
             pos_id UInt32, pool String, entry Date, T UInt16,
             tick_lower Int32, tick_upper Int32, L Float64, fee UInt32
           ) ENGINE = MergeTree ORDER BY (T, pool, entry)""", fmt="TSV")
    tsv = "\n".join("\t".join(map(str, [i, p["pool"], p["entry"], p["T"],
                                        p["tick_lower"], p["tick_upper"],
                                        int(p["L"]), p["fee"]]))
                    for i, p in enumerate(pos))
    ch(f"INSERT INTO {POS_TABLE} FORMAT TabSeparated", fmt="TSV", stdin=tsv + "\n")
    res = {}
    for T in T_GRID:
        sub = [p for p in pos if p["T"] == T]
        if not sub:
            continue
        base = min(p["entry"] for p in sub)
        sys.stderr.write(f"[fees] T={T} base={base} ...\n")
        out = ch(f"""
        WITH sw AS (
          SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
                 abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                   AS weth_amt,
                 toDate('{base}') + {T} * intDiv(toDate(s.block_time)
                   - toDate('{base}'), {T}) AS entry_d
          FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
          WHERE s.pool IN (SELECT DISTINCT pool FROM {POS_TABLE} WHERE T = {T})
                AND s.sqrt_price_x96 > 0 AND s.block_time >= toDate('{base}')
        )
        SELECT p.pos_id AS pos_id,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0))
                 AS fees_raw,
               sum(sw.weth_amt * (p.fee / 1e6)) AS pool_fees_raw,
               countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in,
               count() AS n_win
        FROM sw INNER JOIN {POS_TABLE} p
          ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T}
        GROUP BY p.pos_id
        """)
        for pid, f, pf, ni, nw in rows_of(out):
            f, pf = float(f), float(pf)
            res[int(pid)] = dict(fees_weth=f / 1e18, n_in=int(ni), n_win=int(nw),
                                 realized_share=(f / pf) if pf > 0 else 0.0)
    for i in range(len(pos)):
        res.setdefault(i, dict(fees_weth=0.0, n_in=0, n_win=0, realized_share=0.0))
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
        rec = dict(alt=p["alt"], symbol=p["symbol"], grp=p["grp"], pool=p["pool"],
                   entry=p["entry"], T=p["T"], c=p["c"], fee=p["fee"],
                   selected=p["selected"], share_q10=p["share_q10"],
                   realized_share=f[i]["realized_share"],
                   ratio=p["ratio"], sigma_cross=p["sigma_cross"],
                   sigma_own=p["sigma_own"], trailing_yield=p["trailing_yield"],
                   fee_bps=1e4 * fw / v_ref, il_bps=1e4 * il / v_ref,
                   gross_bps=1e4 * (fw - il) / v_ref,
                   in_range=(f[i]["n_in"] / f[i]["n_win"]) if f[i]["n_win"] else 0.0)
        for tag, rate in (("strict", p["cost_strict_bps"]),
                          ("cheap", p["cost_cheap_bps"])):
            swap_cost = (alt_in + alt_out) * rate / 1e4
            gas = LP_GAS_BPS / 1e4 * v_ref
            rec[f"cost_{tag}_bps"] = 1e4 * (swap_cost + gas) / v_ref
            rec[f"net_{tag}_bps"] = 1e4 * (fw - il - swap_cost - gas) / v_ref
            rec[f"net_{tag}_trimgas_bps"] = (
                1e4 * (fw - il - swap_cost) / v_ref - LP_GAS_BPS_TRIM)
        if all(math.isfinite(rec[k]) for k in
               ("fee_bps", "il_bps", "net_strict_bps", "net_cheap_bps")):
            rows.append(rec)
    sys.stderr.write(f"[pnl] rows={len(rows)} dropped_at_tick_extreme={dropped}\n")
    return rows


def cell(g, key="net_strict_bps"):
    byd = defaultdict(list)
    for r in g:
        byd[r["entry"]].append(r[key])
    return boot(byd)


def concentration(g, key="net_strict_bps"):
    """Share of total net supplied by the single largest symbol, and who it is."""
    tot = sum(r[key] for r in g)
    bysym = defaultdict(float)
    for r in g:
        bysym[r["symbol"]] += r[key]
    if not bysym or tot == 0:
        return 0.0, None
    sym = max(bysym, key=lambda s: abs(bysym[s]))
    return bysym[sym] / tot, sym


def terciles(g):
    """Assign each position a ratio bucket among its own entry date's cross-section.

    Cross-sectional rather than pooled, because H1 is a cross-sectional claim and
    pooled cutoffs would let a change in the ratio's overall level over the year
    masquerade as a dose response.
    """
    byd = defaultdict(list)
    for r in g:
        byd[r["entry"]].append(r)
    for d, v in byd.items():
        rs = sorted({x["ratio"] for x in v})
        if len(rs) < 3:
            for x in v:
                x["bucket"] = None
            continue
        lo = rs[len(rs) // 3]
        hi = rs[2 * len(rs) // 3]
        for x in v:
            x["bucket"] = 0 if x["ratio"] <= lo else (1 if x["ratio"] <= hi else 2)


def seg(rows, lo, hi):
    return [r for r in rows if lo <= r["entry"] < hi]


def fmt(x, w=9, p=1):
    return f"{'  n/a':>{w}}" if x is None else f"{x:>{w}.{p}f}"


def main() -> None:
    keep = load_universe()
    cost = load_cost()
    pools = sorted({c["pool"] for v in keep.values() for c in v})
    sys.stderr.write(f"[data] daily+hourly for {len(pools)} pools\n")
    day, hrs = load_daily(pools), load_hourly(pools)
    eth, ethh = eth_daily(), eth_hourly()
    pos = build(keep, cost, day, hrs, eth, ethh)
    if not pos:
        raise SystemExit("no positions")
    f = fees_for(pos)
    rows = pnl_rows(pos, f, day)
    json.dump(rows, open(f"out/stage4_c3_pnl{SUF}.json", "w"))

    sel = [r for r in rows if r["selected"]]
    conf = [r for r in sel if r["grp"] == "confirm"]
    gen = [r for r in sel if r["grp"] == "gen"]
    terciles(conf)

    print("=" * 118)
    print(f"C3 -- cross-rate market making. Net vs HODL in bps of a real "
          f"${NOTIONAL_USD:,.0f}. Strict cost. CI at alpha=0.05/6.")
    print(f"Gas charged: {LP_GAS_BPS:.1f} bps per cycle "
          f"(trimmed regime {LP_GAS_BPS_TRIM:.1f}).")
    print("=" * 118)

    print("\nSYMBOL TABLE -- generation group is shown but decides nothing")
    print(f"{'symbol':<9}{'grp':<9}{'pos':>6}{'ratio':>8}{'s_cross':>9}{'s_own':>8}"
          f"{'net':>9}{'med_net':>9}{'fee':>9}{'IL':>8}{'cost':>8}")
    bysym = defaultdict(list)
    for r in sel:
        bysym[r["symbol"]].append(r)
    for s, v in sorted(bysym.items(), key=lambda x: median([r["ratio"] for r in x[1]])):
        print(f"{s:<9}{v[0]['grp']:<9}{len(v):>6}"
              f"{median([r['ratio'] for r in v]):>8.3f}"
              f"{1e2*median([r['sigma_cross'] for r in v]):>9.2f}"
              f"{1e2*median([r['sigma_own'] for r in v]):>8.2f}"
              f"{mean([r['net_strict_bps'] for r in v]):>9.1f}"
              f"{median([r['net_strict_bps'] for r in v]):>9.1f}"
              f"{median([r['fee_bps'] for r in v]):>9.1f}"
              f"{median([r['il_bps'] for r in v]):>8.1f}"
              f"{median([r['cost_strict_bps'] for r in v]):>8.1f}")

    print("\nMAIN TEST -- CONFIRMATION GROUP ONLY (cbBTC excluded)")
    print(f"{'T':<4}{'c':<6}{'n':>6}{'blocks':>7}{'net':>9}{'lo':>9}{'hi':>9}"
          f"{'med':>8}{'fee':>8}{'IL':>8}{'cost':>8}{'win%':>7}{'H1':>8}{'H2':>8}"
          f"{'conc':>7}{'who':<8}{'LOO_lo':>9}{'LOO_hi':>9}")
    main_cells, audit = {}, {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in conf if r["T"] == T and r["c"] == c]
            if len(g) < MIN_N:
                continue
            mu, lo, hi, nb = cell(g)
            h1, h2 = seg(g, PANEL_START, MID), seg(g, MID, PANEL_END)
            cf, who = concentration(g)
            loo_lo = loo_hi = None
            if abs(cf) > CONC_LIMIT:
                g2 = [r for r in g if r["symbol"] != who]
                if len(g2) >= MIN_N:
                    _, loo_lo, loo_hi, _ = cell(g2)
            bad = sum(1 for r in g if r["realized_share"] > MAX_SHARE) / len(g)
            key = f"T{T}_c{c}"
            main_cells[key] = dict(n=len(g), blocks=nb, mean=mu, lo=lo, hi=hi,
                                   h1=mean([r["net_strict_bps"] for r in h1]) if h1 else None,
                                   h2=mean([r["net_strict_bps"] for r in h2]) if h2 else None,
                                   conc=cf, conc_sym=who, loo_lo=loo_lo, loo_hi=loo_hi,
                                   share_breach=bad,
                                   reportable=(nb is not None and nb >= MIN_BLOCKS))
            audit[key] = bad
            print(f"{T:<4}{c:<6}{len(g):>6}{nb:>7}{fmt(mu)}{fmt(lo)}{fmt(hi)}"
                  f"{median([r['net_strict_bps'] for r in g]):>8.1f}"
                  f"{median([r['fee_bps'] for r in g]):>8.1f}"
                  f"{median([r['il_bps'] for r in g]):>8.1f}"
                  f"{median([r['cost_strict_bps'] for r in g]):>8.1f}"
                  f"{100.0*sum(1 for r in g if r['net_strict_bps']>0)/len(g):>7.1f}"
                  f"{fmt(mean([r['net_strict_bps'] for r in h1]) if h1 else None, 8)}"
                  f"{fmt(mean([r['net_strict_bps'] for r in h2]) if h2 else None, 8)}"
                  f"{100*cf:>6.0f}% {str(who or '')[:7]:<8}{fmt(loo_lo)}{fmt(loo_hi)}")

    print("\nGENERATION GROUP (cbBTC) -- for contrast only, cannot support a verdict")
    print(f"{'T':<4}{'c':<6}{'n':>6}{'blocks':>7}{'net':>9}{'lo':>9}{'hi':>9}"
          f"{'med':>8}{'fee':>8}{'IL':>8}")
    gen_cells = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in gen if r["T"] == T and r["c"] == c]
            if len(g) < 5:
                continue
            mu, lo, hi, nb = cell(g)
            gen_cells[f"T{T}_c{c}"] = dict(n=len(g), blocks=nb, mean=mu, lo=lo, hi=hi,
                                           med=median([r["net_strict_bps"] for r in g]))
            print(f"{T:<4}{c:<6}{len(g):>6}{nb:>7}{fmt(mu)}{fmt(lo)}{fmt(hi)}"
                  f"{median([r['net_strict_bps'] for r in g]):>8.1f}"
                  f"{median([r['fee_bps'] for r in g]):>8.1f}"
                  f"{median([r['il_bps'] for r in g]):>8.1f}")

    print("\nH2 DOSE TEST -- confirmation group by cross/own vol tercile "
          "(0 = tracks WETH closest). H1 predicts net falls from 0 to 2.")
    print(f"{'T':<4}{'c':<6}{'b0_n':>6}{'b0_net':>9}{'b0_lo':>9}{'b0_hi':>9}"
          f"{'b1_n':>6}{'b1_net':>9}{'b2_n':>6}{'b2_net':>9}{'mono':>7}{'b0_ratio':>10}"
          f"{'b2_ratio':>10}")
    dose = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in conf if r["T"] == T and r["c"] == c
                 and r.get("bucket") is not None]
            if len(g) < 3 * MIN_N:
                continue
            bs = [[r for r in g if r["bucket"] == b] for b in (0, 1, 2)]
            if any(len(b) < 5 for b in bs):
                continue
            ms = [mean([r["net_strict_bps"] for r in b]) for b in bs]
            _, lo0, hi0, _ = cell(bs[0])
            mono = ms[0] >= ms[1] >= ms[2]
            dose[f"T{T}_c{c}"] = dict(
                n=[len(b) for b in bs], means=ms, mono=mono, b0_lo=lo0, b0_hi=hi0,
                ratio0=median([r["ratio"] for r in bs[0]]),
                ratio2=median([r["ratio"] for r in bs[2]]))
            print(f"{T:<4}{c:<6}{len(bs[0]):>6}{fmt(ms[0])}{fmt(lo0)}{fmt(hi0)}"
                  f"{len(bs[1]):>6}{fmt(ms[1])}{len(bs[2]):>6}{fmt(ms[2])}"
                  f"{('yes' if mono else 'no'):>7}"
                  f"{median([r['ratio'] for r in bs[0]]):>10.3f}"
                  f"{median([r['ratio'] for r in bs[2]]):>10.3f}")

    print("\nCAPACITY AUDIT -- fraction of positions whose REALISED fee share "
          f"exceeded {MAX_SHARE:.0%} under the q10 gate (cell void above "
          f"{SHARE_AUDIT_LIMIT:.0%})")
    for k, v in sorted(audit.items()):
        print(f"  {k:<10}{100*v:>6.1f}%   {'VOID' if v > SHARE_AUDIT_LIMIT else 'ok'}")

    # Verdict, by the rules written in GOAL_R3 section 6 before any of this existed.
    rep = {k: v for k, v in main_cells.items() if v["reportable"]}
    def clears(v):
        if v["lo"] is None or v["lo"] <= 0:
            return False
        if v["share_breach"] > SHARE_AUDIT_LIMIT:
            return False
        if abs(v["conc"]) > CONC_LIMIT:
            return v["loo_lo"] is not None and v["loo_lo"] > 0
        return True
    pos_cells = [k for k, v in rep.items() if clears(v)]
    flips = [k for k, v in rep.items()
             if v["h1"] is not None and v["h2"] is not None and v["h1"] * v["h2"] < 0]
    mono_ok = [k for k in pos_cells if dose.get(k, {}).get("mono")]
    gen_pos = any(v["mean"] is not None and v["mean"] > 0 for v in gen_cells.values())
    all_neg = rep and all(v["hi"] is not None and v["hi"] < 0 for v in rep.values())

    if pos_cells and mono_ok and not (set(pos_cells) & set(flips)):
        verdict = (f"CROSS_RATE_LP_CONFIRMED -- {sorted(mono_ok)} clear zero past the "
                   f"concentration and capacity gates, both half-years agree, and H2 is "
                   f"monotone. Forward confirmation still required; no trading.")
    elif pos_cells and not mono_ok:
        verdict = (f"CROSS_RATE_LP_NO_DOSE -- {sorted(pos_cells)} clear zero but H2 is "
                   f"not monotone, so the mechanism is unsupported and this is treated "
                   f"as possibly something else, not a lead.")
    elif all_neg and gen_pos:
        worst = max(v["hi"] for v in rep.values())
        verdict = (f"CBBTC_IS_IDIOSYNCRATIC -- no confirmation cell clears zero "
                   f"(loosest upper bound {worst:.1f}) while cbBTC stays positive. "
                   f"Single-asset special case; stop investing in this direction.")
    elif all_neg:
        verdict = (f"NO_CROSS_RATE_LP_EDGE -- every confirmation cell's upper bound "
                   f"is below zero (loosest {max(v['hi'] for v in rep.values()):.1f}).")
    else:
        wide = sorted(((v["hi"] - v["lo"], k) for k, v in rep.items()
                       if v["lo"] is not None), reverse=True)
        verdict = ("UNDERPOWERED -- intervals straddle zero. Widest cell "
                   + (f"{wide[0][1]} spans {wide[0][0]:.0f} bps; " if wide else "")
                   + "the excluded magnitudes are in main_cells.")
    print("\nVERDICT (C3) = " + verdict)
    if not rep:
        print(f"  note: no cell reached {MIN_BLOCKS} independent blocks; "
              f"blocks by cell = "
              + ", ".join(f"{k}:{v['blocks']}" for k, v in sorted(main_cells.items())))

    json.dump(dict(verdict=verdict, notional=NOTIONAL_USD, gas_bps=LP_GAS_BPS,
                   main_cells=main_cells, gen_cells=gen_cells, dose=dose,
                   share_audit=audit,
                   n_confirm_pos=len(conf), n_gen_pos=len(gen),
                   symbols=sorted({r["symbol"] for r in conf})),
              open(f"out/stage4_c3_verdict{SUF}.json", "w"), indent=1)


if __name__ == "__main__":
    main()
