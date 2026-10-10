"""Stage 3: LP PnL against HODL, with entry/exit costs from R2 measurements.

net = (LP value at exit + fees - entry cost - exit cost) - HODL value at exit
    = fees - impermanent loss - execution cost

reported in bps of the $5,000 notional. HODL is the only meaningful benchmark:
reporting LP return alone mixes in the asset's own drift.
"""
import json, math, os, random, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
WETH = "0x4200000000000000000000000000000000000006"
NOTIONAL_USD, ETH_USD = 5000.0, 4000.0
V_WETH = NOTIONAL_USD / ETH_USD
N_BOOT = 10000
ALPHA = 0.05 / 9          # Bonferroni over the 9 pre-registered cells


def ch(query, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=3600", "--format", fmt, "--query", query],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


def position_amounts(L, p, pa, pb):
    """token0 / token1 held by a range position at price p (all in raw units)."""
    sp = math.sqrt(max(p, 1e-300)); spa, spb = math.sqrt(pa), math.sqrt(pb)
    if p <= pa:
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if p >= pb:
        return 0.0, L * (spb - spa)
    return L * (1.0 / sp - 1.0 / spb), L * (sp - spa)


def to_weth(amt0, amt1, p, weth_is_token1):
    return (amt0 * p + amt1) if weth_is_token1 else (amt0 + amt1 / p)


def main():
    pos = json.load(open(os.path.join(OUT, "positions.json")))
    fees = json.load(open(os.path.join(OUT, "fees.json")))
    pools = sorted({p["pool"] for p in pos})
    pool_list = ",".join("'%s'" % x for x in pools)

    # daily closing price per pool (used only for the exit lookup)
    closes = defaultdict(dict)
    raw = ch("""SELECT pool, toDate(block_time) AS d,
                       argMax(toString(sqrt_price_x96), (block_number, log_index)) AS sp
                FROM census.cl_swaps
                WHERE pool IN (%s) AND sqrt_price_x96 > 0
                GROUP BY pool, d""" % pool_list)
    for ln in raw.strip().split("\n")[1:]:
        pool, d, sp = ln.split("\t")
        s = int(sp) / (2.0 ** 96)
        closes[pool][d] = s * s
    sys.stderr.write("pools with daily closes: %d\n" % len(closes))

    import datetime

    def prev_close(pool, exit_date):
        """last observed close strictly before exit_date; None if pool went silent."""
        y, m, dd = map(int, exit_date.split("-"))
        d = datetime.date(y, m, dd)
        for _ in range(40):
            d -= datetime.timedelta(days=1)
            k = d.isoformat()
            if k in closes[pool]:
                return k, closes[pool][k]
        return None, None

    rows, skipped = [], defaultdict(int)
    for i, p in enumerate(pos):
        f = fees[str(i)]
        pa = 1.0001 ** p["tick_lower"]; pb = 1.0001 ** p["tick_upper"]
        p_in = p["p_entry"]
        d_out, p_out = prev_close(p["pool"], p["exit"])
        if p_out is None or p_out <= 0:
            skipped["no_exit_price"] += 1
            continue
        w1 = bool(p["weth_is_token1"])

        a0_in, a1_in = p["amt0_entry"], p["amt1_entry"]
        v_in = to_weth(a0_in, a1_in, p_in, w1) / 1e18
        a0_out, a1_out = position_amounts(p["L"], p_out, pa, pb)
        v_lp = to_weth(a0_out, a1_out, p_out, w1) / 1e18
        v_hodl = to_weth(a0_in, a1_in, p_out, w1) / 1e18

        cb = p["cost_bps"] / 10000.0
        alt_in_weth = (a0_in * p_in if w1 else a1_in / p_in) / 1e18
        alt_out_weth = (a0_out * p_out if w1 else a1_out / p_out) / 1e18
        cost = (alt_in_weth + alt_out_weth) * cb

        fee_w = f["fees_weth"]
        net = (v_lp + fee_w - cost) - v_hodl
        if not all(math.isfinite(x) for x in (v_lp, v_hodl, fee_w, cost, net)):
            skipped["nonfinite"] += 1
            continue
        if v_in <= 0:
            skipped["nonpositive_entry_value"] += 1
            continue

        rows.append(dict(
            pool=p["pool"], alt=p["alt"], entry=p["entry"], T=p["T"], c=p["c"],
            venue=p["venue"], cost_bps=p["cost_bps"], sigma_7d=p["sigma_7d"], w=p["w"],
            fee_bps=1e4 * fee_w / V_WETH,
            il_bps=1e4 * (v_hodl - v_lp) / V_WETH,
            cost_bps_paid=1e4 * cost / V_WETH,
            net_bps=1e4 * net / V_WETH,
            in_range_frac=(f["n_in_range"] / f["n_window"]) if f["n_window"] else 0.0,
            n_window=f["n_window"]))

    sys.stderr.write("pnl rows: %d\n" % len(rows))
    for k, v in sorted(skipped.items(), key=lambda x: -x[1]):
        sys.stderr.write("  skipped %-28s %d\n" % (k, v))

    with open(os.path.join(OUT, "pnl.json"), "w") as fh:
        json.dump(rows, fh)

    # ---------- per-cell verdicts ----------
    def block_bootstrap(vals_by_date, stat, n=N_BOOT, seed=12345):
        rng = random.Random(seed)
        dates = list(vals_by_date)
        if not dates:
            return None, None, None
        pooled = [v for d in dates for v in vals_by_date[d]]
        point = stat(pooled)
        out = []
        for _ in range(n):
            samp = []
            for _ in range(len(dates)):
                samp.extend(vals_by_date[dates[rng.randrange(len(dates))]])
            if samp:
                out.append(stat(samp))
        out.sort()
        lo = out[int(len(out) * ALPHA / 2)]
        hi = out[int(len(out) * (1 - ALPHA / 2))]
        return point, lo, hi

    def mean(a): return sum(a) / len(a)

    def median(a):
        s = sorted(a); k = len(s)
        return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])

    cells = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cells[(r["T"], r["c"])][r["entry"]].append(r["net_bps"])

    print("%-4s %-5s %7s %10s %10s %12s %12s %10s %s"
          % ("T", "c", "n", "mean_bps", "med_bps", "mean_lo", "mean_hi", "win%", "verdict"))
    summary = []
    for (T, c) in sorted(cells):
        byd = cells[(T, c)]
        allv = [v for d in byd for v in byd[d]]
        mu, lo, hi = block_bootstrap(byd, mean)
        md, mlo, mhi = block_bootstrap(byd, median, n=2000, seed=999)
        win = 100.0 * sum(1 for v in allv if v > 0) / len(allv)
        # trimmed mean: does the average survive without the extreme 1%?
        s = sorted(allv); k = max(1, int(len(s) * 0.005))
        trimmed = mean(s[k:-k]) if len(s) > 2 * k else mu
        if mu > 0 and md > 0 and lo > 0:
            verdict = "LP_EDGE_CONFIRMED"
        elif mu > 0 and (md <= 0 or trimmed <= 0):
            verdict = "LOTTERY_DEPENDENT"
        elif hi < 0:
            verdict = "NO_LP_EDGE"
        elif lo < 0 < hi and (hi - lo) > 2 * abs(mu):
            verdict = "UNDERPOWERED"
        else:
            verdict = "NO_LP_EDGE"
        print("%-4d %-5.1f %7d %10.1f %10.1f %12.1f %12.1f %10.1f %s"
              % (T, c, len(allv), mu, md, lo, hi, win, verdict))
        summary.append(dict(T=T, c=c, n=len(allv), mean_bps=mu, median_bps=md,
                            mean_lo=lo, mean_hi=hi, trimmed_mean_bps=trimmed,
                            win_pct=win, verdict=verdict))

    # ---------- decomposition: where does the money go ----------
    print()
    print("%-4s %-5s %10s %10s %10s %10s %10s"
          % ("T", "c", "fee_bps", "il_bps", "cost_bps", "net_bps", "in_range"))
    for (T, c) in sorted(cells):
        g = [r for r in rows if r["T"] == T and r["c"] == c]
        print("%-4d %-5.1f %10.1f %10.1f %10.1f %10.1f %10.2f"
              % (T, c, median([x["fee_bps"] for x in g]),
                 median([x["il_bps"] for x in g]),
                 median([x["cost_bps_paid"] for x in g]),
                 median([x["net_bps"] for x in g]),
                 median([x["in_range_frac"] for x in g])))

    with open(os.path.join(OUT, "verdicts.json"), "w") as fh:
        json.dump(summary, fh, indent=1)


if __name__ == "__main__":
    main()
