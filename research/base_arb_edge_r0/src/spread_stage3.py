"""Stage 3: re-test the gap against a POINT-IN-TIME cost, not a single-block one.

Stage 2 found the gap survives one block with mean +2.28 bps. That number used
R2's cost measured at one block (50123160) as if it were constant across 53,267
different blocks. Backing the fee out of the panel shows it is not:

    pool          venue     fee() field   implied p25 / p50 / p75
    0xb4cb8009    univ3     1.00 bps      1.000 / 1.000 / 1.001    <- static, matches
    0xd0b53d92    univ3     5.00 bps      5.000 / 5.000 / 5.003    <- static, matches
    0xb2cc224c    aero_cl   3.64 bps      2.170 / 2.450 / 3.000    <- DYNAMIC
    0x3fe04a59    aero_cl   3.98 bps      2.330 / 3.320 / 4.316    <- DYNAMIC

Aerodrome Slipstream sets fees through a fee module, so fee() is a snapshot. A
1-2 bps fee swing is the whole edge. So the fee has to be point-in-time too.

Cost at block b, per leg, both terms causal:
    fee_leg  = hourly median implied fee of that pool, from the hour before b
    slip_leg = V / (L_b * sqrt(p_b))          # CLMM closed form, R3
    cost     = fee_A + slip_A + fee_B + slip_B

Also fixes a labelling slip in stage 1/2: with p = token1/token0, the profitable
path "token0 -> token1 in X -> token0 in Y" needs p_X > p_Y and pays
(p_X - p_Y)/p_Y. Stage 1/2 paired that expression with the opposite pool's cost
label. For these pools buy_bps == sell_bps to 2 decimals so the numbers were
unaffected, but here the gap is computed direction-agnostically to remove the
ambiguity entirely.
"""
import bisect, json, os, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
MIN_BLOCK = 46_000_000
ETH_USD, SIZE_USD = 4000.0, 5000.0
V_WETH_RAW = (SIZE_USD / ETH_USD) * 1e18
LAGS = [1, 2, 5, 30]
WETH = "0x4200000000000000000000000000000000000006"


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--max_memory_usage=30000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


class Step:
    """piecewise-constant lookup over sorted keys."""

    def __init__(self, keys, vals):
        self.k, self.v = keys, vals

    def at(self, x):
        i = bisect.bisect_right(self.k, x) - 1
        return self.v[i] if i >= 0 else None


def main():
    s2 = json.load(open(os.path.join(OUT, "stage2.json")))
    pools = sorted({r["buy"] for r in s2} | {r["sell"] for r in s2})
    pl = ",".join("'%s'" % p for p in pools)
    sys.stderr.write("pools: %d\n" % len(pools))

    meta = {}
    for ln in ch("SELECT pool, venue, fee, token1 FROM census.pools WHERE pool IN (%s)"
                 % pl).strip().split("\n")[1:]:
        pool, venue, fee, t1 = ln.split("\t")
        meta[pool] = dict(venue=venue, fee_field=int(fee), weth_is_t1=(t1.lower() == WETH))

    # ---- point-in-time fee: hourly median of the fee implied by the CLMM identity ----
    sys.stderr.write("backing out hourly implied fee ...\n")
    fee_raw = ch("""
        WITH s AS (
            SELECT pool, block_number, log_index, block_time,
                   toFloat64(sqrt_price_x96)/pow(2,96) AS sq,
                   toFloat64(liquidity) AS L,
                   toFloat64(amount0) AS a0, toFloat64(amount1) AS a1,
                   lagInFrame(toFloat64(sqrt_price_x96)/pow(2,96))
                       OVER (PARTITION BY pool ORDER BY block_number, log_index) AS sq_prev,
                   lagInFrame(toFloat64(liquidity))
                       OVER (PARTITION BY pool ORDER BY block_number, log_index) AS L_prev
            FROM census.cl_swaps
            WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0),
        f AS (
            SELECT pool, block_number, block_time,
                   1.0 - (L*(sq-sq_prev))/a1 AS imp
            FROM s
            WHERE sq_prev > 0 AND L = L_prev AND L > 0 AND a1 > 0 AND a0 < 0
              AND (sq-sq_prev) > 0)
        SELECT pool, min(block_number) AS b0, quantile(0.5)(imp) AS fee_med, count() AS n
        FROM f WHERE imp > -0.001 AND imp < 0.01
        GROUP BY pool, toStartOfHour(block_time)
        ORDER BY pool, b0""" % (pl, MIN_BLOCK))
    fee_series = defaultdict(lambda: ([], []))
    for ln in fee_raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, b0, fm, n = ln.split("\t")
        if int(n) < 5:
            continue
        fee_series[pool][0].append(int(b0))
        fee_series[pool][1].append(float(fm) * 1e4)      # bps
    fee_lut = {p: Step(*fee_series[p]) for p in fee_series}
    for p in pools:
        k, v = fee_series[p]
        if not v:
            sys.stderr.write("  %s NO IMPLIED FEE -> excluded\n" % p[:12]); continue
        vs = sorted(v)
        sys.stderr.write("  %-12s %-8s field=%5.2f  implied hourly p10/p50/p90 = "
                         "%5.2f / %5.2f / %5.2f  (%d hours)\n"
                         % (p[:12], meta[p]["venue"], meta[p]["fee_field"] / 100.0,
                            vs[int(len(vs) * .1)], vs[len(vs) // 2], vs[int(len(vs) * .9)],
                            len(vs)))

    # ---- block-level price and liquidity ----
    sys.stderr.write("loading block-level price and liquidity ...\n")
    px_raw = ch("""
        SELECT pool, block_number AS b,
               argMax(toString(sqrt_price_x96), log_index) AS sp,
               argMax(toString(liquidity), log_index) AS L
        FROM census.cl_swaps
        WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0
        GROUP BY pool, b ORDER BY pool, b""" % (pl, MIN_BLOCK))
    blk = defaultdict(lambda: ([], [], []))
    for ln in px_raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, b, sp, L = ln.split("\t")
        sq = int(sp) / (2.0 ** 96)
        blk[pool][0].append(int(b))
        blk[pool][1].append(sq)
        blk[pool][2].append(float(L))
    sq_lut = {p: Step(blk[p][0], blk[p][1]) for p in pools}
    L_lut = {p: Step(blk[p][0], blk[p][2]) for p in pools}

    def leg_cost(pool, b):
        """fee (point in time) + closed-form slippage at block b, in bps."""
        f = fee_lut[pool].at(b) if pool in fee_lut else None
        sq = sq_lut[pool].at(b)
        L = L_lut[pool].at(b)
        if f is None or sq is None or L is None or L <= 0 or sq <= 0:
            return None
        p = sq * sq
        v_t1 = V_WETH_RAW if meta[pool]["weth_is_t1"] else V_WETH_RAW * p
        return f + 1e4 * v_t1 / (L * sq)

    # ---- unordered pairs, direction-agnostic gap ----
    seen, upairs = set(), []
    for r in s2:
        k = tuple(sorted((r["buy"], r["sell"])))
        if k in seen:
            continue
        seen.add(k)
        upairs.append((k[0], k[1], r["sym"]))
    sys.stderr.write("unordered pairs: %d\n" % len(upairs))

    print()
    print("=" * 128)
    print("POINT-IN-TIME COST vs STAGE 2's FIXED COST")
    print("=" * 128)
    print("%-8s %-11s %-11s %10s %10s %10s %11s %11s"
          % ("symbol", "poolX", "poolY", "fixed_hurd", "pit_hurd_p50",
             "ev_blocks", "pit_pct_net>0", "s2_pct_net>0"))
    rows = []
    for X, Y, sym in upairs:
        ev = sorted(set(blk[X][0]) | set(blk[Y][0]))
        nets, sig, hurds = [], [], []
        for b in ev:
            sx, sy = sq_lut[X].at(b), sq_lut[Y].at(b)
            if sx is None or sy is None:
                continue
            px, py = sx * sx, sy * sy
            if px <= 0 or py <= 0:
                continue
            gap = 1e4 * abs(px - py) / min(px, py)
            cx, cy = leg_cost(X, b), leg_cost(Y, b)
            if cx is None or cy is None:
                continue
            h = cx + cy
            hurds.append(h)
            net = gap - h
            nets.append(net)
            if net > 0:
                sig.append(b)
        if len(nets) < 1000:
            continue
        hs = sorted(hurds)
        s2row = next((r for r in s2 if {r["buy"], r["sell"]} == {X, Y}), None)
        print("%-8s %-11s %-11s %10.2f %12.2f %10d %13.3f %11.3f"
              % (sym, X[:10], Y[:10], s2row["hurdle"] if s2row else float("nan"),
                 hs[len(hs) // 2], len(nets), 100.0 * len(sig) / len(nets),
                 s2row["blk_pct_pos"] if s2row and "blk_pct_pos" in s2row else float("nan")))
        rows.append(dict(sym=sym, X=X, Y=Y, ev=len(nets), n_sig=len(sig),
                         pit_hurdle_p50=hs[len(hs) // 2], sig=sig))

    print()
    print("=" * 128)
    print("PERSISTENCE UNDER POINT-IN-TIME COST: net at b+lag given net>0 at b")
    print("=" * 128)
    print("%-8s %-11s %-11s %9s %8s %s"
          % ("symbol", "poolX", "poolY", "n_signal", "net@b",
             "  ".join("%15s" % ("net@b+%d" % L) for L in LAGS)))
    final = []
    for r in rows:
        X, Y, sig = r["X"], r["Y"], r["sig"]
        if len(sig) < 100:
            continue

        def net_at(b):
            sx, sy = sq_lut[X].at(b), sq_lut[Y].at(b)
            if sx is None or sy is None:
                return None
            px, py = sx * sx, sy * sy
            cx, cy = leg_cost(X, b), leg_cost(Y, b)
            if cx is None or cy is None or px <= 0 or py <= 0:
                return None
            return 1e4 * abs(px - py) / min(px, py) - cx - cy

        base = [x for x in (net_at(b) for b in sig) if x is not None]
        rec = dict(sym=r["sym"], X=X, Y=Y, n_signal=len(sig),
                   pit_hurdle_p50=r["pit_hurdle_p50"],
                   net_at_b=sum(base) / len(base))
        cells = []
        for L in LAGS:
            v = [x for x in (net_at(b + L) for b in sig) if x is not None]
            if not v:
                cells.append("n/a"); continue
            mu, pos = sum(v) / len(v), 100.0 * sum(1 for x in v if x > 0) / len(v)
            cells.append("%7.2f/%5.1f%%" % (mu, pos))
            rec["net_b%d" % L], rec["pct_pos_b%d" % L] = mu, pos
        print("%-8s %-11s %-11s %9d %8.2f %s"
              % (r["sym"], X[:10], Y[:10], len(sig), rec["net_at_b"],
                 "  ".join("%15s" % c for c in cells)))
        final.append(rec)

    json.dump(final, open(os.path.join(OUT, "stage3.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
