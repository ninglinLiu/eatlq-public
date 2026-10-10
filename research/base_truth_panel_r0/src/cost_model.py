"""Validate a closed-form CLMM cost model against R2's 537 on-chain measurements.

Why: R2 measured execution cost by atomic simulation at ONE block (50123160). Any
analysis that selects pools by that number leaks a little future information, and
pools that died before that block have no cost at all. A year-long panel makes
both problems worse.

The closed form. For a swap of size V (denominated in token1 raw units) against
active liquidity L at price p:

    sqrt(p') = sqrt(p) + V/L                    (token1 in)
    execution price = sqrt(p) * sqrt(p')
    slippage       = (sqrt(p') - sqrt(p)) / sqrt(p) = V / (L * sqrt(p))

The token0-input direction gives V*sqrt(p)/L on a token0-denominated size, which
is the same expression once the size is converted to token1. So one formula covers
both sides. Adding the fee tier:

    cost_bps ~= 1e4 * fee_rate + 1e4 * V / (L * sqrt(p))

This is exact within a single tick range and OPTIMISTIC across tick boundaries,
because liquidity beyond the current range is usually thinner. So it is a lower
bound on true cost, and it is computable from panel data alone at zero RPC cost
for every pool at every block.

This script asks only one question: how well does it reproduce R2's measured
numbers? Nothing downstream may use the model until this passes.
"""
import json, math, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
WETH = "0x4200000000000000000000000000000000000006"
R2_BLOCK = 50123160
ETH_USD = 4000.0


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=3600", "--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


def main():
    meas = json.load(open(os.path.join(ROOT, "out", "slippage2_631.json")))
    rows = [r for r in meas["rows"] if r["klass"] == "OK" and r.get("worst_bps") is not None]
    by = {}
    for r in rows:
        by.setdefault(r["pool"], {})[r["usd"]] = r
    pool_list = ",".join("'%s'" % p for p in by)
    sys.stderr.write("R2-measured pools: %d   sizes: %s\n" % (len(by), meas["sizes_usd"]))

    # state of each pool at (or just before) the R2 measurement block
    state = {}
    raw = ch("""
        SELECT s.pool AS pool,
               argMax(toString(s.sqrt_price_x96), (s.block_number, s.log_index)) AS sp,
               argMax(toString(s.liquidity),      (s.block_number, s.log_index)) AS L,
               max(s.block_number) AS blk,
               any(pl.fee) AS fee,
               any(pl.token1) AS token1
        FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
        WHERE s.pool IN (%s) AND s.block_number <= %d AND s.sqrt_price_x96 > 0
        GROUP BY s.pool""" % (pool_list, R2_BLOCK))
    for ln in raw.strip().split("\n")[1:]:
        pool, sp, L, blk, fee, t1 = ln.split("\t")
        sq = int(sp) / (2.0 ** 96)
        state[pool] = dict(sqrt_p=sq, p=sq * sq, L=float(L), blk=int(blk),
                           fee=int(fee), weth_is_token1=(t1.lower() == WETH))
    sys.stderr.write("pools with state at/before block %d: %d\n" % (R2_BLOCK, len(state)))

    def predict(pool, usd):
        st = state.get(pool)
        if st is None or st["L"] <= 0 or st["sqrt_p"] <= 0:
            return None
        v_weth_raw = (usd / ETH_USD) * 1e18
        # convert the trade size into token1 raw units
        v_t1 = v_weth_raw if st["weth_is_token1"] else v_weth_raw * st["p"]
        slip = v_t1 / (st["L"] * st["sqrt_p"])
        return 1e4 * (st["fee"] / 1e6) + 1e4 * slip

    print("%-8s %6s %12s %12s %12s %10s %10s"
          % ("usd", "n", "med_pred", "med_meas", "med_ratio", "spear", "frac_2x"))
    detail = {}
    for usd in meas["sizes_usd"]:
        pairs = []
        for pool in by:
            if usd not in by[pool]:
                continue
            m = by[pool][usd]["worst_bps"]
            pr = predict(pool, usd)
            if pr is None or m is None or m <= 0:
                continue
            pairs.append((pr, m, pool))
        if len(pairs) < 20:
            continue
        pred = sorted(x[0] for x in pairs); mea = sorted(x[1] for x in pairs)
        ratios = sorted(x[0] / x[1] for x in pairs)
        # Spearman on ranks
        n = len(pairs)
        rp = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: pairs[i][0]))}
        rm = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: pairs[i][1]))}
        d2 = sum((rp[i] - rm[i]) ** 2 for i in range(n))
        spear = 1 - 6.0 * d2 / (n * (n * n - 1))
        frac2 = sum(1 for r in ratios if 0.5 <= r <= 2.0) / n
        print("%-8d %6d %12.1f %12.1f %12.3f %10.3f %10.3f"
              % (usd, n, pred[n // 2], mea[n // 2], ratios[n // 2], spear, frac2))
        detail[usd] = dict(n=n, med_pred=pred[n // 2], med_meas=mea[n // 2],
                           med_ratio=ratios[n // 2], spearman=spear, frac_within_2x=frac2,
                           ratio_p10=ratios[int(n * .1)], ratio_p90=ratios[int(n * .9)])

    print()
    print("ratio (predicted / measured) distribution at $5,000:")
    if 5000 in detail:
        d = detail[5000]
        print("  p10=%.3f  p50=%.3f  p90=%.3f   within 2x: %.1f%%   spearman=%.3f"
              % (d["ratio_p10"], d["med_ratio"], d["ratio_p90"],
                 100 * d["frac_within_2x"], d["spearman"]))

    # the decision that matters downstream: can the model reproduce R2's tiering?
    print()
    print("TIER AGREEMENT at $5,000 (does the model put pools in the same cost bucket?)")
    bins = [(0, 30), (30, 100), (100, 300), (300, 1e9)]

    def tier(v):
        for i, (a, b) in enumerate(bins):
            if a <= v < b:
                return i
        return len(bins) - 1
    hit = tot = 0
    conf = [[0] * len(bins) for _ in bins]
    for pool in by:
        if 5000 not in by[pool]:
            continue
        m = by[pool][5000]["worst_bps"]; pr = predict(pool, 5000)
        if pr is None or m is None or m <= 0:
            continue
        tm, tp = tier(m), tier(pr)
        conf[tm][tp] += 1
        tot += 1
        hit += (tm == tp)
    names = ["<30", "30-100", "100-300", "300+"]
    print("      %s" % "  ".join("%8s" % ("pred " + x) for x in names))
    for i, r in enumerate(conf):
        print("%-7s %s" % ("meas " + names[i], "  ".join("%8d" % x for x in r)))
    print("exact tier agreement: %d/%d = %.1f%%" % (hit, tot, 100.0 * hit / max(tot, 1)))
    cheap_m = sum(conf[0])
    cheap_agree = conf[0][0]
    print("of pools R2 calls CHEAP (<30bps), model also calls cheap: %d/%d = %.1f%%"
          % (cheap_agree, cheap_m, 100.0 * cheap_agree / max(cheap_m, 1)))

    json.dump(detail, open(os.path.join(OUT, "cost_model_fit.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
