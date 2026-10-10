"""Stage 1: does a cross-pool price gap larger than both legs' cost ever exist?

Minute grid, forward-filled. Forward-filling a CLMM pool's price is exact, not an
approximation: a pool's price changes only when someone swaps it, so the last
post-swap sqrt_price IS the current marginal price.

Restricted to block_number >= 46000000. The year-long backfill is writing to
cl_swaps right now and the middle of the range is incomplete; running this over
the partial range would manufacture fake price jumps.

Reports the gross spread and the net after the pre-registered hurdle
(buy_bps(A) + sell_bps(B), both from R2's two-sided $5,000 measurement).

NOTE: everything here is measured at the SAME instant the gap is observable. Per
GOAL.md section 4 that does not constitute a result -- it is only the signal
definition. Executability is stage 2.
"""
import json, os, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
MIN_BLOCK = 46_000_000
HURDLE_MAX = 30.0        # main universe per GOAL.md section 2


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--max_memory_usage=30000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


def main():
    pairs = json.load(open(os.path.join(OUT, "pairs.json")))
    main_pairs = [x for x in pairs if x["hurdle_bps"] < HURDLE_MAX]
    pools = sorted({x["buy_pool"] for x in main_pairs} | {x["sell_pool"] for x in main_pairs})
    sys.stderr.write("main universe: %d ordered pairs over %d pools\n"
                     % (len(main_pairs), len(pools)))
    pool_list = ",".join("'%s'" % p for p in pools)

    # verify the two pools of a pair really share token ordering, else the price
    # comparison is meaningless
    meta = {}
    for ln in ch("SELECT pool, token0, token1, venue, fee FROM census.pools "
                 "WHERE pool IN (%s)" % pool_list).strip().split("\n")[1:]:
        pool, t0, t1, venue, fee = ln.split("\t")
        meta[pool] = (t0.lower(), t1.lower(), venue, int(fee))
    bad = [x for x in main_pairs
           if meta[x["buy_pool"]][:2] != meta[x["sell_pool"]][:2]]
    sys.stderr.write("pairs with mismatched token ordering (dropped): %d\n" % len(bad))
    main_pairs = [x for x in main_pairs if x not in bad]

    # minute-level last price per pool
    raw = ch("""
        SELECT pool, toUnixTimestamp(m) AS ts, sp FROM (
            SELECT pool, toStartOfMinute(block_time) AS m,
                   argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sp
            FROM census.cl_swaps
            WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0
            GROUP BY pool, m)
        ORDER BY pool, ts""" % (pool_list, MIN_BLOCK))
    series = defaultdict(dict)
    n = 0
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, ts, sp = ln.split("\t")
        s = int(sp) / (2.0 ** 96)
        series[pool][int(ts)] = s * s
        n += 1
    sys.stderr.write("minute observations: %d over %d pools\n" % (n, len(series)))

    all_ts = sorted({t for d in series.values() for t in d})
    sys.stderr.write("distinct minutes on the grid: %d (%.1f days)\n"
                     % (len(all_ts), (all_ts[-1] - all_ts[0]) / 86400.0))

    # forward-filled price per pool on the common grid
    ff = {}
    for pool in pools:
        d = series[pool]
        cur, out = None, {}
        for t in all_ts:
            if t in d:
                cur = d[t]
            if cur is not None:
                out[t] = cur
        ff[pool] = out
        
    print("%-10s %-11s %-11s %8s %8s %9s %9s %9s %9s %8s"
          % ("symbol", "buy_pool", "sell_pool", "hurdle", "minutes",
             "gross_p50", "gross_p99", "net_p99", "net_max", "pct_net>0"))
    results = []
    sym = json.load(open("/root/base_truth_panel_r0/out/alt_symbols.json"))

    def S(a):
        s = sym.get(a) or sym.get(a.lower()) or "?"
        return (s.get("symbol", "?") if isinstance(s, dict) else str(s))[:10]

    for x in sorted(main_pairs, key=lambda y: y["hurdle_bps"]):
        A, B, h = x["buy_pool"], x["sell_pool"], x["hurdle_bps"]
        fa, fb = ff[A], ff[B]
        gross, net_pos, ts_pos = [], [], []
        for t in all_ts:
            pa, pb = fa.get(t), fb.get(t)
            if pa is None or pb is None or pa <= 0:
                continue
            g = 1e4 * (pb - pa) / pa
            gross.append(g)
            if g - h > 0:
                net_pos.append(g - h)
                ts_pos.append(t)
        if len(gross) < 1000:
            continue
        gs = sorted(gross)
        m = len(gs)
        pct = 100.0 * len(net_pos) / m
        results.append(dict(alt=x["alt"], sym=S(x["alt"]), buy=A, sell=B, hurdle=h,
                            minutes=m, gross_p50=gs[m // 2], gross_p99=gs[int(m * .99)],
                            net_p99=gs[int(m * .99)] - h,
                            net_max=gs[-1] - h, pct_net_pos=pct,
                            n_net_pos=len(net_pos),
                            n_days_with_signal=len({t // 86400 for t in ts_pos}),
                            mean_net_when_pos=(sum(net_pos) / len(net_pos)) if net_pos else 0.0))
        print("%-10s %-11s %-11s %8.1f %8d %9.1f %9.1f %9.1f %9.1f %8.3f"
              % (S(x["alt"]), A[:10], B[:10], h, m, gs[m // 2], gs[int(m * .99)],
                 gs[int(m * .99)] - h, gs[-1] - h, pct))

    json.dump(results, open(os.path.join(OUT, "stage1.json"), "w"), indent=1)
    print()
    tot_sig = sum(r["n_net_pos"] for r in results)
    print("pairs analysed              : %d" % len(results))
    print("pairs with any net>0 minute : %d" % sum(1 for r in results if r["n_net_pos"] > 0))
    print("pairs with pct_net>0 >= 0.1%%: %d" % sum(1 for r in results if r["pct_net_pos"] >= 0.1))
    print("total net>0 minute events   : %d" % tot_sig)
    if not tot_sig:
        print("VERDICT_STAGE1 = NO_ARB_SPREAD (nothing to take to stage 2)")


if __name__ == "__main__":
    main()
