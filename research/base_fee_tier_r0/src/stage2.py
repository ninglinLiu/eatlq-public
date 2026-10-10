#!/usr/bin/env python3
"""C2 stage 2: does choosing the fee tier actually pay?

Pre-registration: ../GOAL_R1.md.

Stage 1 showed that within one pair the best tier collects a median 3.8x more fee
revenue per dollar than the worst, while impermanent loss is pointwise identical
across tiers of one pair. Round 36 showed fees and impermanent loss roughly
cancel. Put together, multiplying the revenue side while holding the cost side
fixed should turn the gross positive. This script tests that sentence.

Three rules are enforced ex ante because stage 1's positive verdict partly came
from a gate added after the fact:

  1. capacity: L_mine / L_active <= 10%, with L_active from the 7 days BEFORE entry
  2. selection: the tier is chosen by trailing 7-day fee yield, never by what
     happened during the holding window
  3. notional: point-in-time ETH price, not the 4000 constant

Two tests. Test A asks whether the selected tier beats HODL -- the money
question. Test B is paired within (alt, entry date, T, c): selected tier minus
the mean of the other deployable tiers. Impermanent loss cancels inside that
difference, so B isolates the value of the selection itself.
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
MIN_BLOCK = 46_000_000
MIN_SWAPS, MIN_DAYS = 500, 60
MAX_ENTRY_COST_BPS = 100.0
MAX_SHARE = 0.10
NOTIONAL_USD = 5000.0
C_GRID = [0.5, 1.0, 2.0]
T_GRID = [1, 7]
MIN_HIST_DAYS = 30
PANEL_START, PANEL_END = "2026-05-14", "2026-08-17"
N_BOOT = 10_000
ALPHA = 0.05 / 6          # Bonferroni over the 6 pre-registered cells


def ch(query: str, fmt: str = "TabSeparatedWithNames", stdin: str | None = None,
       mem: int = 20_000_000_000) -> str:
    cmd = ["docker", "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd += ["eatlq-clickhouse-1", "clickhouse-client", "--max_execution_time=7200",
            f"--max_memory_usage={mem}", "--format", fmt, "--query", query]
    p = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:5000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def dplus(d: str, k: int) -> str:
    y, m, dd = map(int, d.split("-"))
    return (dt.date(y, m, dd) + dt.timedelta(days=k)).isoformat()


def universe() -> tuple[dict, dict]:
    """Candidate pools grouped by alt, plus the cheapest measured entry cost per alt."""
    cost = json.load(open("/root/base_lp_edge_r0/out/cost_5k.json"))
    alt_cost: dict[str, float] = {}
    for v in cost.values():
        a, c = v["alt"], float(v["cost_bps"])
        if a not in alt_cost or c < alt_cost[a]:
            alt_cost[a] = c

    rows = ch(f"""
    WITH w AS (
      SELECT pool, venue, fee, tick_spacing,
             if(token0 = '{WETH}', token1, token0) AS alt,
             if(token0 = '{WETH}', 0, 1) AS weth_is_token1
      FROM census.pools WHERE token0 = '{WETH}' OR token1 = '{WETH}'
    )
    SELECT w.alt AS alt, w.pool AS pool, w.venue AS venue, w.fee AS fee,
           w.tick_spacing AS tick_spacing, w.weth_is_token1 AS weth_is_token1,
           s.n AS n_swaps, s.nd AS n_days
    FROM w INNER JOIN (
      SELECT pool, count() AS n, uniqExact(toDate(block_time)) AS nd
      FROM census.cl_swaps WHERE block_number >= {MIN_BLOCK} GROUP BY pool
    ) AS s ON s.pool = w.pool
    WHERE s.n >= {MIN_SWAPS} AND s.nd >= {MIN_DAYS} AND w.fee > 0
    """).strip().split("\n")[1:]

    byalt: dict[str, list[dict]] = defaultdict(list)
    for ln in rows:
        if not ln:
            continue
        alt, pool, ven, fee, ts, w1, ns, nd = ln.split("\t")
        byalt[alt].append(dict(alt=alt, pool=pool, venue=ven, fee=int(fee),
                               tick_spacing=int(ts) or 1,
                               weth_is_token1=int(w1), n_swaps=int(ns), n_days=int(nd)))

    keep = {a: v for a, v in byalt.items()
            if len(v) >= 2 and a in alt_cost and alt_cost[a] <= MAX_ENTRY_COST_BPS}
    sys.stderr.write(f"[universe] alts with >=2 qualifying pools and a measured entry "
                     f"cost <= {MAX_ENTRY_COST_BPS:.0f} bps: {len(keep)}  "
                     f"pools: {sum(len(v) for v in keep.values())}\n")
    return keep, alt_cost


def daily(pools: list[str]) -> dict:
    """Per pool per day: closing price, swap count, median active liquidity, fee yield."""
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
    WHERE s.pool IN ({plist}) AND s.block_number >= {MIN_BLOCK}
          AND s.sqrt_price_x96 > 0
    GROUP BY pool, d
    """)
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, sp, n, L, vL = ln.split("\t")
        out[pool][d] = dict(p=(int(sp) / 2.0 ** 96) ** 2, n=int(n),
                            L=float(L), vol_over_L=float(vL))
    return out


def hourly(pools: list[str]) -> dict:
    plist = ",".join(f"'{p}'" for p in pools)
    raw = ch(f"""
    SELECT pool, toString(toStartOfHour(block_time)) AS h,
           argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sp
    FROM census.cl_swaps
    WHERE pool IN ({plist}) AND block_number >= {MIN_BLOCK} AND sqrt_price_x96 > 0
    GROUP BY pool, h ORDER BY pool, h
    """)
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, h, sp = ln.split("\t")
        out[pool].append((h, (int(sp) / 2.0 ** 96) ** 2))
    return out


def eth_daily() -> dict[str, float]:
    raw = ch("""SELECT toString(toDate(bar)) AS d, quantile(0.5)(eth_usd) AS e
                FROM edge.eth_usd_1h GROUP BY d""")
    return {ln.split("\t")[0]: float(ln.split("\t")[1])
            for ln in raw.strip().split("\n")[1:] if ln}


def sigma_and_price(hrs: list[tuple[str, float]], entry: str) -> tuple[float, float]:
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
    v = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    s = math.sqrt(v)
    return (s, last) if s > 0 and math.isfinite(s) else (0.0, 0.0)


def amounts(L: float, p: float, pa: float, pb: float) -> tuple[float, float]:
    sp, spa, spb = math.sqrt(max(p, 1e-300)), math.sqrt(pa), math.sqrt(pb)
    if p <= pa:
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if p >= pb:
        return 0.0, L * (spb - spa)
    return L * (1.0 / sp - 1.0 / spb), L * (sp - spa)


def to_weth(a0: float, a1: float, p: float, w1: bool) -> float:
    return (a0 * p + a1) if w1 else (a0 + a1 / p)


def build(keep: dict, alt_cost: dict, day: dict, hrs: dict, eth: dict) -> list[dict]:
    """Positions in every deployable tier, with the causally selected one flagged."""
    dates = sorted({d for pool in day for d in day[pool]})
    dates = [d for d in dates if d >= dplus(PANEL_START, MIN_HIST_DAYS)]
    if not dates:
        return []
    base = dates[0]
    pos, skip = [], defaultdict(int)

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
            for alt, cands in keep.items():
                # reference price and vol from the pool with most trailing swaps,
                # so every tier of this pair gets the same range in price terms
                ref = max(cands, key=lambda c: sum(day[c["pool"]].get(x, {}).get("n", 0)
                                                   for x in win)
                          if c["pool"] in day else 0)
                if ref["pool"] not in hrs:
                    skip["no_ref_hours"] += 1
                    continue
                sig, p_ref = sigma_and_price(hrs[ref["pool"]], d)
                if sig <= 0 or p_ref <= 0:
                    skip["no_sigma"] += 1
                    continue
                e = eth.get(d)
                if not e:
                    skip["no_eth_price"] += 1
                    continue
                v_weth = (NOTIONAL_USD / e) * 1e18

                tier_rows = []
                for c_ in cands:
                    pl = c_["pool"]
                    if pl not in day:
                        continue
                    tw = [day[pl][x] for x in win if x in day[pl]]
                    if len(tw) < 4:
                        skip["thin_trailing_window"] += 1
                        continue
                    L_act = sorted(x["L"] for x in tw)[len(tw) // 2]
                    if L_act <= 0:
                        skip["zero_trailing_liquidity"] += 1
                        continue
                    ty = c_["fee"] / 1e6 * sum(x["vol_over_L"] for x in tw)
                    # this pool's own entry price, for its own tick snapping
                    _, p_own = sigma_and_price(hrs.get(pl, []), d)
                    if p_own <= 0:
                        skip["no_own_price"] += 1
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
                            skip["range_collapsed"] += 1
                            continue
                        pa, pb = 1.0001 ** tl, 1.0001 ** tu
                        a0u, a1u = amounts(1.0, p_own, pa, pb)
                        if a0u < 0 or a1u < 0:
                            skip["outside_own_range"] += 1
                            continue
                        vu = to_weth(a0u, a1u, p_own, bool(c_["weth_is_token1"]))
                        if vu <= 0:
                            skip["nonpositive_unit_value"] += 1
                            continue
                        L = v_weth / vu
                        share = L / L_act
                        tier_rows.append(dict(
                            alt=alt, pool=pl, venue=c_["venue"], fee=c_["fee"],
                            tick_spacing=ts, weth_is_token1=c_["weth_is_token1"],
                            entry=d, exit=dplus(d, T), T=T, c=cc,
                            tick_lower=tl, tick_upper=tu, L=L, share=share,
                            trailing_yield=ty, p_entry=p_own, sigma_7d=sig,
                            w=w, range_bps=1e4 * (math.log(pb) - math.log(pa)),
                            amt0_entry=a0u * L, amt1_entry=a1u * L,
                            entry_cost_bps=alt_cost[alt], eth_usd=e))
                for cc in C_GRID:
                    grp = [r for r in tier_rows if r["c"] == cc]
                    dep = [r for r in grp if r["share"] <= MAX_SHARE]
                    if len(dep) < 2:
                        skip["fewer_than_2_deployable_tiers"] += len(grp)
                        continue
                    sel = max(dep, key=lambda r: r["trailing_yield"])
                    for r in dep:
                        r["selected"] = 1 if r is sel else 0
                        r["n_deployable"] = len(dep)
                        pos.append(r)

    sys.stderr.write(f"[build] positions: {len(pos)}  "
                     f"selected: {sum(p['selected'] for p in pos)}\n")
    for k2, v in sorted(skip.items(), key=lambda x: -x[1])[:8]:
        sys.stderr.write(f"  skipped {k2:<32} {v}\n")
    return pos


def fees_for(pos: list[dict]) -> dict[int, dict]:
    ch("DROP TABLE IF EXISTS census.c2_pos", fmt="TSV")
    ch("""CREATE TABLE census.c2_pos (
            pos_id UInt32, pool String, entry Date, T UInt16,
            tick_lower Int32, tick_upper Int32, L Float64, fee UInt32
          ) ENGINE = MergeTree ORDER BY (T, pool, entry)""", fmt="TSV")
    tsv = "\n".join("\t".join(map(str, [i, p["pool"], p["entry"], p["T"],
                                        p["tick_lower"], p["tick_upper"],
                                        int(p["L"]), p["fee"]]))
                    for i, p in enumerate(pos))
    ch("INSERT INTO census.c2_pos FORMAT TabSeparated", fmt="TSV", stdin=tsv + "\n")

    base = min(p["entry"] for p in pos)
    res: dict[int, dict] = {}
    for T in T_GRID:
        sys.stderr.write(f"[fees] T={T} ...\n")
        out = ch(f"""
        WITH sw AS (
          SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
                 abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1))) AS weth_amt,
                 toDate('{base}') + {T} * intDiv(toDate(s.block_time) - toDate('{base}'), {T})
                   AS entry_d
          FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
          WHERE s.pool IN (SELECT DISTINCT pool FROM census.c2_pos WHERE T = {T})
                AND s.block_number >= {MIN_BLOCK} AND s.sqrt_price_x96 > 0
                AND s.block_time >= toDate('{base}')
        )
        SELECT p.pos_id AS pos_id,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0)) AS fees_raw,
               countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in,
               count() AS n_win
        FROM sw INNER JOIN census.c2_pos p
          ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T}
        GROUP BY p.pos_id
        """)
        for ln in out.strip().split("\n")[1:]:
            if not ln:
                continue
            pid, f, ni, nw = ln.split("\t")
            res[int(pid)] = dict(fees_weth=float(f) / 1e18, n_in=int(ni), n_win=int(nw))
    for i in range(len(pos)):
        res.setdefault(i, dict(fees_weth=0.0, n_in=0, n_win=0))
    return res


def boot(byday: dict[str, list[float]], stat, n: int = N_BOOT, seed: int = 20260818):
    rng = random.Random(seed)
    ds = list(byday)
    if len(ds) < 5:
        return None, None, None
    pooled = [v for d in ds for v in byday[d]]
    point = stat(pooled)
    acc = []
    for _ in range(n):
        s = []
        for _ in range(len(ds)):
            s.extend(byday[ds[rng.randrange(len(ds))]])
        if s:
            acc.append(stat(s))
    acc.sort()
    return point, acc[int(len(acc) * ALPHA / 2)], acc[int(len(acc) * (1 - ALPHA / 2))]


def mean(a):
    return sum(a) / len(a)


def median(a):
    s = sorted(a)
    k = len(s)
    return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])


def main() -> None:
    keep, alt_cost = universe()
    pools = sorted({c["pool"] for v in keep.values() for c in v})
    sys.stderr.write(f"[data] pulling daily + hourly for {len(pools)} pools\n")
    day, hrs, eth = daily(pools), hourly(pools), eth_daily()
    sys.stderr.write(f"[data] eth days={len(eth)}  pools with bars={len(hrs)}\n")

    pos = build(keep, alt_cost, day, hrs, eth)
    if not pos:
        raise SystemExit("no positions built")

    # how well does impermanent loss actually cancel between tiers?
    rb = defaultdict(list)
    for p in pos:
        rb[(p["alt"], p["entry"], p["T"], p["c"])].append(p["range_bps"])
    spread = [max(v) / min(v) for v in rb.values() if min(v) > 0]
    sys.stderr.write(f"[check] tier range width ratio within a group: "
                     f"p50={median(spread):.3f} p90={sorted(spread)[int(.9*len(spread))]:.3f} "
                     f"(1.000 = identical ranges, so IL cancels exactly)\n")

    f = fees_for(pos)
    rows = []
    for i, p in enumerate(pos):
        pa, pb = 1.0001 ** p["tick_lower"], 1.0001 ** p["tick_upper"]
        dd, p_out = None, None
        for kk in range(1, 40):
            x = dplus(p["exit"], -kk)
            if x in day.get(p["pool"], {}):
                dd, p_out = x, day[p["pool"]][x]["p"]
                break
        if not p_out or p_out <= 0:
            continue
        w1 = bool(p["weth_is_token1"])
        a0i, a1i = p["amt0_entry"], p["amt1_entry"]
        a0o, a1o = amounts(p["L"], p_out, pa, pb)
        v_lp = to_weth(a0o, a1o, p_out, w1) / 1e18
        v_hodl = to_weth(a0i, a1i, p_out, w1) / 1e18
        alt_in = (a0i * p["p_entry"] if w1 else a1i / p["p_entry"]) / 1e18
        alt_out = (a0o * p_out if w1 else a1o / p_out) / 1e18
        cost = (alt_in + alt_out) * p["entry_cost_bps"] / 1e4
        fw = f[i]["fees_weth"]
        v_ref = NOTIONAL_USD / p["eth_usd"]
        net = (v_lp + fw - cost) - v_hodl
        if not all(math.isfinite(x) for x in (v_lp, v_hodl, fw, cost, net)):
            continue
        rows.append(dict(
            alt=p["alt"], pool=p["pool"], entry=p["entry"], T=p["T"], c=p["c"],
            fee=p["fee"], selected=p["selected"], n_deployable=p["n_deployable"],
            share=p["share"], trailing_yield=p["trailing_yield"],
            fee_bps=1e4 * fw / v_ref, il_bps=1e4 * (v_hodl - v_lp) / v_ref,
            cost_bps=1e4 * cost / v_ref, net_bps=1e4 * net / v_ref,
            gross_bps=1e4 * (fw - (v_hodl - v_lp)) / v_ref,
            in_range=(f[i]["n_in"] / f[i]["n_win"]) if f[i]["n_win"] else 0.0))
    sys.stderr.write(f"[pnl] rows={len(rows)}\n")
    json.dump(rows, open("out/stage2_pnl.json", "w"))

    print("=" * 112)
    print("TEST A -- selected tier vs HODL, in bps of a real $5,000 (money question)")
    print("=" * 112)
    print(f"{'T':<4}{'c':<6}{'n':>6}{'mean':>9}{'lo':>9}{'hi':>9}{'median':>9}"
          f"{'win%':>7}{'fee':>8}{'IL':>8}{'cost':>8}{'in_rng':>8}")
    outA = {}
    for T in T_GRID:
        for c in C_GRID:
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["selected"]]
            if len(g) < 20:
                continue
            byd = defaultdict(list)
            for r in g:
                byd[r["entry"]].append(r["net_bps"])
            mu, lo, hi = boot(byd, mean)
            win = 100.0 * sum(1 for r in g if r["net_bps"] > 0) / len(g)
            outA[f"T{T}_c{c}"] = dict(n=len(g), mean=mu, lo=lo, hi=hi,
                                      median=median([r["net_bps"] for r in g]), win=win)
            print(f"{T:<4}{c:<6}{len(g):>6}{mu:>9.1f}"
                  f"{(lo if lo is not None else float('nan')):>9.1f}"
                  f"{(hi if hi is not None else float('nan')):>9.1f}"
                  f"{median([r['net_bps'] for r in g]):>9.1f}{win:>7.1f}"
                  f"{median([r['fee_bps'] for r in g]):>8.1f}"
                  f"{median([r['il_bps'] for r in g]):>8.1f}"
                  f"{median([r['cost_bps'] for r in g]):>8.1f}"
                  f"{median([r['in_range'] for r in g]):>8.2f}")

    print("\n" + "=" * 112)
    print("TEST B -- paired: selected tier minus mean of the other deployable tiers")
    print("(impermanent loss cancels inside this difference, so it isolates selection)")
    print("=" * 112)
    print(f"{'T':<4}{'c':<6}{'pairs':>6}{'d_net':>9}{'lo':>9}{'hi':>9}"
          f"{'d_fee':>9}{'d_IL':>9}{'win%':>7}")
    outB = {}
    for T in T_GRID:
        for c in C_GRID:
            grp = defaultdict(list)
            for r in rows:
                if r["T"] == T and r["c"] == c:
                    grp[(r["alt"], r["entry"])].append(r)
            byd, dn, df, di = defaultdict(list), [], [], []
            for (alt, d), v in grp.items():
                s = [r for r in v if r["selected"]]
                o = [r for r in v if not r["selected"]]
                if len(s) != 1 or not o:
                    continue
                x = s[0]["net_bps"] - mean([r["net_bps"] for r in o])
                byd[d].append(x)
                dn.append(x)
                df.append(s[0]["fee_bps"] - mean([r["fee_bps"] for r in o]))
                di.append(s[0]["il_bps"] - mean([r["il_bps"] for r in o]))
            if len(dn) < 20:
                continue
            mu, lo, hi = boot(byd, mean)
            win = 100.0 * sum(1 for x in dn if x > 0) / len(dn)
            outB[f"T{T}_c{c}"] = dict(pairs=len(dn), mean=mu, lo=lo, hi=hi, win=win,
                                      d_fee=mean(df), d_il=mean(di))
            print(f"{T:<4}{c:<6}{len(dn):>6}{mu:>9.1f}"
                  f"{(lo if lo is not None else float('nan')):>9.1f}"
                  f"{(hi if hi is not None else float('nan')):>9.1f}"
                  f"{mean(df):>9.1f}{mean(di):>9.1f}{win:>7.1f}"
                  )

    b_pos = [k for k, v in outB.items() if v["lo"] is not None and v["lo"] > 0]
    a_pos = [k for k, v in outA.items() if v["lo"] is not None and v["lo"] > 0]
    wide = [k for k, v in outB.items()
            if v["lo"] is not None and v["lo"] < 0 < v["hi"]
            and (v["hi"] - v["lo"]) > 2 * abs(v["mean"])]
    if b_pos and a_pos:
        verdict = (f"TIER_SELECTION_PAYS -- test B clears zero on {b_pos} and test A on "
                   f"{a_pos}. Forward confirmation required; no trading authorised.")
    elif b_pos:
        verdict = (f"TIER_SELECTION_WORKS_BUT_LP_STILL_LOSES -- test B clears zero on "
                   f"{b_pos} but no cell of test A does. The mechanism is real and not "
                   "enough to make market making worth doing.")
    elif wide and len(wide) == len(outB):
        verdict = f"UNDERPOWERED -- every cell of test B straddles zero widely: {wide}"
    else:
        worst = max((v["hi"] for v in outB.values() if v["hi"] is not None),
                    default=float("nan"))
        verdict = (f"NO_TIER_SELECTION_EFFECT -- no cell of test B clears zero "
                   f"(loosest upper bound {worst:.1f} bps). The 3.8x spread from stage 1 "
                   "does not survive causal selection.")
    print("\nVERDICT = " + verdict)
    json.dump({"verdict": verdict, "testA": outA, "testB": outB},
              open("out/stage2_verdict.json", "w"), indent=1)


if __name__ == "__main__":
    main()
