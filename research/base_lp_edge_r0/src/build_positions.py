"""Stage 1: build the position table, using only information available at entry.

For each (pool, entry_date) in the point-in-time universe:
  - entry price   = price at the last swap strictly before entry_date 00:00
  - sigma_7d      = stdev of hourly log returns over the 7 days strictly before entry
  - range         = [p*exp(-w), p*exp(+w)],  w = c * sigma_7d * sqrt(24*T)
  - L_mine        = liquidity of a $5,000-notional position in that range

Everything here is causal: no price, no volume, no liquidity from on/after entry_date
is used to choose the range or to decide whether to enter.

Position math (Uniswap V3 / Slipstream, identical):
  V = L * (2*sqrt(p) - p/sqrt(pb) - sqrt(pa))     -> L = V / (...)
with p, pa, pb in the pool's native raw price units (token1_raw per token0_raw),
so L comes out in the same raw units as the `liquidity` field of the swap events.
"""
import json, math, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
WETH = "0x4200000000000000000000000000000000000006"

# frozen by GOAL.md
NOTIONAL_USD = 5000.0
ETH_USD = 4000.0            # same constant R2 used, so cost and notional align
C_GRID = [0.5, 1.0, 2.0]
T_GRID = [1, 7, 30]
MIN_HIST_DAYS = 30
MIN_SWAPS_7D = 500
PANEL_START = "2026-05-14"
PANEL_END = "2026-08-17"


def ch(query, fmt="TabSeparatedWithNames"):
    p = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_execution_time=3600", "--format", fmt, "--query", query],
        capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def main():
    cost = json.load(open(os.path.join(OUT, "cost_5k.json")))
    pool_list = ",".join("'%s'" % p for p in cost)

    # ---- pool metadata (orientation, fee, tick spacing) ----
    meta_rows = ch("""
        SELECT pool, venue, token0, token1, fee, tick_spacing
        FROM census.pools WHERE pool IN (%s)""" % pool_list).strip().split("\n")[1:]
    meta = {}
    for ln in meta_rows:
        pool, venue, t0, t1, fee, ts = ln.split("\t")
        if t0.lower() != WETH and t1.lower() != WETH:
            continue
        meta[pool] = dict(venue=venue, token0=t0.lower(), token1=t1.lower(),
                          fee=int(fee), tick_spacing=int(ts),
                          weth_is_token1=(t1.lower() == WETH))
    sys.stderr.write("pools with metadata + WETH leg: %d\n" % len(meta))

    # ---- hourly bars: last price in each hour, per pool ----
    # argMax on block_number gives the closing price of the hour deterministically.
    bars_raw = ch("""
        SELECT pool,
               toStartOfHour(block_time) AS h,
               argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sqrtp,
               count() AS n
        FROM census.cl_swaps
        WHERE pool IN (%s) AND sqrt_price_x96 > 0
        GROUP BY pool, h ORDER BY pool, h""" % pool_list)

    from collections import defaultdict
    bars = defaultdict(list)          # pool -> [(hour_str, price_raw, n_swaps)]
    for ln in bars_raw.strip().split("\n")[1:]:
        pool, h, sqrtp, n = ln.split("\t")
        if pool not in meta:
            continue
        s = int(sqrtp) / (2.0 ** 96)
        bars[pool].append((h, s * s, int(n)))
    sys.stderr.write("pools with bars: %d   total bars: %d\n"
                     % (len(bars), sum(len(v) for v in bars.values())))

    # ---- point-in-time universe: (pool, entry_date) qualifying on past data only ----
    univ_raw = ch("""
        WITH first_seen AS (
            SELECT pool, min(block_time) AS t_first FROM census.cl_swaps
            WHERE pool IN (%s) GROUP BY pool),
        grid AS (SELECT toDate('%s') + arrayJoin(range(%d, 96)) AS d)
        SELECT g.d AS d, s.pool AS pool, count() AS n7
        FROM grid g
        INNER JOIN census.cl_swaps s
            ON s.block_time >= g.d - 7 AND s.block_time < g.d
        INNER JOIN first_seen f ON f.pool = s.pool
        WHERE s.pool IN (%s) AND f.t_first <= g.d - %d
        GROUP BY g.d, s.pool
        HAVING n7 >= %d
        ORDER BY d, pool""" % (pool_list, PANEL_START, MIN_HIST_DAYS,
                               pool_list, MIN_HIST_DAYS, MIN_SWAPS_7D))
    universe = defaultdict(set)       # entry_date -> {pool}
    for ln in univ_raw.strip().split("\n")[1:]:
        d, pool, n7 = ln.split("\t")
        if pool in bars:
            universe[d].add(pool)
    sys.stderr.write("qualifying pool-days: %d over %d entry dates\n"
                     % (sum(len(v) for v in universe.values()), len(universe)))

    # ---- per-pool hour index for causal lookups ----
    hour_idx = {}
    for pool, rows in bars.items():
        hour_idx[pool] = {h: i for i, (h, _, _) in enumerate(rows)}

    def sigma_and_price(pool, entry_date):
        """stdev of hourly log returns in [entry-7d, entry), and price at last bar before entry."""
        rows = bars[pool]
        cut = entry_date + " 00:00:00"
        lo = entry_date_minus(entry_date, 7) + " 00:00:00"
        px, idx_last = [], -1
        for i, (h, p, _) in enumerate(rows):
            if h >= cut:
                break
            idx_last = i
            if h >= lo:
                px.append(p)
        if idx_last < 0 or len(px) < 24:
            return None, None
        rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))
                if px[i] > 0 and px[i - 1] > 0]
        if len(rets) < 12:
            return None, None
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        sig = math.sqrt(var)
        if not (sig > 0) or not math.isfinite(sig):
            return None, None
        return sig, rows[idx_last][1]

    def entry_date_minus(d, k):
        import datetime
        y, m, dd = map(int, d.split("-"))
        return (datetime.date(y, m, dd) - datetime.timedelta(days=k)).isoformat()

    def date_plus(d, k):
        import datetime
        y, m, dd = map(int, d.split("-"))
        return (datetime.date(y, m, dd) + datetime.timedelta(days=k)).isoformat()

    V_weth = (NOTIONAL_USD / ETH_USD) * 1e18      # raw WETH units

    positions, skipped = [], defaultdict(int)
    dates_sorted = sorted(universe)
    for T in T_GRID:
        # non-overlapping entries: every T days from the first qualifying date
        entry_dates = dates_sorted[::T]
        for d in entry_dates:
            exit_d = date_plus(d, T)
            if exit_d > PANEL_END:
                skipped["window_past_panel_end"] += 1
                continue
            for pool in sorted(universe[d]):
                m = meta[pool]
                sig, p = sigma_and_price(pool, d)
                if sig is None:
                    skipped["no_sigma_or_price"] += 1
                    continue
                for c in C_GRID:
                    w = c * sig * math.sqrt(24.0 * T)
                    if w <= 0 or w > 5:
                        skipped["degenerate_width"] += 1
                        continue
                    pa, pb = p * math.exp(-w), p * math.exp(w)
                    # snap to tick spacing (a real position can only sit on multiples)
                    ts = m["tick_spacing"] or 1
                    tl = math.floor(math.log(pa) / math.log(1.0001) / ts) * ts
                    tu = math.ceil(math.log(pb) / math.log(1.0001) / ts) * ts
                    if tu <= tl:
                        skipped["range_collapsed_to_tick"] += 1
                        continue
                    pa_t, pb_t = 1.0001 ** tl, 1.0001 ** tu
                    # value in WETH terms of a unit-L position, then scale
                    sp, spa, spb = math.sqrt(p), math.sqrt(pa_t), math.sqrt(pb_t)
                    amt0_u = (1.0 / sp - 1.0 / spb)        # token0 per unit L
                    amt1_u = (sp - spa)                    # token1 per unit L
                    if amt0_u < 0 or amt1_u < 0:
                        skipped["price_outside_own_range"] += 1
                        continue
                    val_u = (amt0_u * p + amt1_u) if m["weth_is_token1"] \
                        else (amt0_u + amt1_u / p)
                    if val_u <= 0:
                        skipped["nonpositive_unit_value"] += 1
                        continue
                    L = V_weth / val_u
                    positions.append(dict(
                        pool=pool, entry=d, exit=exit_d, T=T, c=c,
                        tick_lower=tl, tick_upper=tu, L=int(L),
                        p_entry=p, sigma_7d=sig, w=w,
                        amt0_entry=amt0_u * L, amt1_entry=amt1_u * L,
                        fee=m["fee"], venue=m["venue"],
                        weth_is_token1=1 if m["weth_is_token1"] else 0,
                        cost_bps=cost[pool]["cost_bps"], alt=cost[pool]["alt"]))

    sys.stderr.write("positions built: %d\n" % len(positions))
    for k, v in sorted(skipped.items(), key=lambda x: -x[1]):
        sys.stderr.write("  skipped %-28s %d\n" % (k, v))
    from collections import Counter
    sys.stderr.write("by (T,c): %s\n" % dict(Counter((p["T"], p["c"]) for p in positions)))

    with open(os.path.join(OUT, "positions.json"), "w") as f:
        json.dump(positions, f)
    sys.stderr.write("wrote out/positions.json\n")


if __name__ == "__main__":
    main()
