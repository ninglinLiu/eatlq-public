"""Stage 4: collapse signal blocks into independent episodes, then size the edge.

Stage 3 counted 88,522 signal BLOCKS for the best pair. Those are not 88,522
opportunities. A dislocation that stays open for 40 blocks is one opportunity, not
forty: my own arb trade moves both pools toward each other and closes it. Counting
blocks instead of episodes overstates the opportunity by whatever the mean episode
length happens to be.

An episode = a maximal run of signal blocks with no gap longer than GAP_BLOCKS.
Each episode is traded at most once, entering at (first signal block + 1) since a
non-MEV actor cannot land in the same block it observed.

Also drops the cbBTC pair: validating the point-in-time cost model against R2's
router simulation showed pool 0x42d4a22c is modelled 2.53 bps too cheap, which is
larger than that pair's entire measured edge. The USDC pools validate within
+0.33 to +0.51 bps, and in the conservative direction.
"""
import bisect, json, os, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
MIN_BLOCK = 46_000_000
ETH_USD, SIZE_USD = 4000.0, 5000.0
V_WETH_RAW = (SIZE_USD / ETH_USD) * 1e18
GAP_BLOCKS = 1          # consecutive signal blocks belong to one episode
WETH = "0x4200000000000000000000000000000000000006"
# pools whose PIT cost model is validated against R2 within a conservative margin
COST_MODEL_REJECT = {"0x42d4a22cad0f5a49681a5715ce994af73a43b76b": -2.53}


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--max_memory_usage=30000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


class Step:
    def __init__(self, k, v):
        self.k, self.v = k, v

    def at(self, x):
        i = bisect.bisect_right(self.k, x) - 1
        return self.v[i] if i >= 0 else None


def main():
    s3 = json.load(open(os.path.join(OUT, "stage3.json")))
    keep = [r for r in s3 if r["X"] not in COST_MODEL_REJECT
            and r["Y"] not in COST_MODEL_REJECT]
    dropped = [r for r in s3 if r not in keep]
    sys.stderr.write("pairs dropped for unvalidated cost model: %s\n"
                     % [r["sym"] for r in dropped])
    pools = sorted({r["X"] for r in keep} | {r["Y"] for r in keep})
    pl = ",".join("'%s'" % p for p in pools)

    meta = {}
    for ln in ch("SELECT pool, venue, token1 FROM census.pools WHERE pool IN (%s)"
                 % pl).strip().split("\n")[1:]:
        pool, venue, t1 = ln.split("\t")
        meta[pool] = dict(venue=venue, w1=(t1.lower() == WETH))

    fee_series = defaultdict(lambda: ([], []))
    for ln in ch("""
        WITH s AS (
            SELECT pool, block_number, block_time,
                   toFloat64(sqrt_price_x96)/pow(2,96) AS sq, toFloat64(liquidity) AS L,
                   toFloat64(amount0) AS a0, toFloat64(amount1) AS a1,
                   lagInFrame(toFloat64(sqrt_price_x96)/pow(2,96))
                       OVER (PARTITION BY pool ORDER BY block_number, log_index) AS sqp,
                   lagInFrame(toFloat64(liquidity))
                       OVER (PARTITION BY pool ORDER BY block_number, log_index) AS Lp
            FROM census.cl_swaps
            WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0)
        SELECT pool, min(block_number) AS b0,
               quantile(0.5)(1.0-(L*(sq-sqp))/a1)*1e4 AS f_bps, count() AS n
        FROM s WHERE sqp>0 AND L=Lp AND L>0 AND a1>0 AND a0<0 AND (sq-sqp)>0
        GROUP BY pool, toStartOfHour(block_time) ORDER BY pool, b0
        """ % (pl, MIN_BLOCK)).strip().split("\n")[1:]:
        if not ln:
            continue
        pool, b0, f, n = ln.split("\t")
        if int(n) < 5:
            continue
        fee_series[pool][0].append(int(b0))
        fee_series[pool][1].append(float(f))
    fee_lut = {p: Step(*fee_series[p]) for p in fee_series}

    blk = defaultdict(lambda: ([], [], [], []))
    for ln in ch("""
        SELECT pool, block_number AS b,
               argMax(toString(sqrt_price_x96), log_index) AS sp,
               argMax(toString(liquidity), log_index) AS L,
               toUnixTimestamp(max(block_time)) AS ts
        FROM census.cl_swaps
        WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0
        GROUP BY pool, b ORDER BY pool, b""" % (pl, MIN_BLOCK)).strip().split("\n")[1:]:
        if not ln:
            continue
        pool, b, sp, L, ts = ln.split("\t")
        sq = int(sp) / (2.0 ** 96)
        blk[pool][0].append(int(b))
        blk[pool][1].append(sq)
        blk[pool][2].append(float(L))
        blk[pool][3].append(int(ts))
    sq_lut = {p: Step(blk[p][0], blk[p][1]) for p in pools}
    L_lut = {p: Step(blk[p][0], blk[p][2]) for p in pools}
    ts_lut = {p: Step(blk[p][0], blk[p][3]) for p in pools}

    def leg(pool, b):
        f = fee_lut[pool].at(b) if pool in fee_lut else None
        sq, L = sq_lut[pool].at(b), L_lut[pool].at(b)
        if f is None or sq is None or L is None or L <= 0 or sq <= 0:
            return None
        p = sq * sq
        v = V_WETH_RAW if meta[pool]["w1"] else V_WETH_RAW * p
        return f + 1e4 * v / (L * sq)

    print("%-8s %-11s %-11s %9s %9s %8s %9s %9s %9s %8s %9s"
          % ("symbol", "poolX", "poolY", "sig_blks", "episodes", "med_len",
             "ep_net@+1", "ep_med", "pct_pos", "days", "usd_total"))
    out = []
    for r in keep:
        X, Y = r["X"], r["Y"]
        ev = sorted(set(blk[X][0]) | set(blk[Y][0]))

        def net_at(b):
            sx, sy = sq_lut[X].at(b), sq_lut[Y].at(b)
            if sx is None or sy is None:
                return None
            px, py = sx * sx, sy * sy
            cx, cy = leg(X, b), leg(Y, b)
            if cx is None or cy is None or px <= 0 or py <= 0:
                return None
            return 1e4 * abs(px - py) / min(px, py) - cx - cy

        sig = [b for b in ev if (net_at(b) or -1) > 0]
        if len(sig) < 100:
            continue
        # collapse into episodes over the EVENT-BLOCK sequence, not raw block numbers:
        # two signal blocks separated only by blocks where neither pool traded are
        # still the same uninterrupted dislocation.
        idx = {b: i for i, b in enumerate(ev)}
        eps, cur = [], [sig[0]]
        for prev, b in zip(sig, sig[1:]):
            if idx[b] - idx[prev] <= GAP_BLOCKS:
                cur.append(b)
            else:
                eps.append(cur); cur = [b]
        eps.append(cur)

        lens = sorted(len(e) for e in eps)
        entries = []
        for e in eps:
            v = net_at(e[0] + 1)
            if v is not None:
                entries.append((e[0], v))
        if len(entries) < 20:
            continue
        vals = [v for _, v in entries]
        vs = sorted(vals)
        days = len({ts_lut[X].at(b) // 86400 for b, _ in entries
                    if ts_lut[X].at(b) is not None})
        mu = sum(vals) / len(vals)
        usd = sum(vals) * SIZE_USD / 1e4
        print("%-8s %-11s %-11s %9d %9d %8d %9.2f %9.2f %8.1f%% %8d %9.0f"
              % (r["sym"], X[:10], Y[:10], len(sig), len(eps), lens[len(lens) // 2],
                 mu, vs[len(vs) // 2], 100.0 * sum(1 for v in vals if v > 0) / len(vals),
                 days, usd))
        out.append(dict(sym=r["sym"], X=X, Y=Y, n_sig_blocks=len(sig),
                        n_episodes=len(eps), median_episode_len=lens[len(lens) // 2],
                        mean_episode_len=sum(lens) / len(lens),
                        n_tradeable=len(entries), ep_net_mean=mu,
                        ep_net_median=vs[len(vs) // 2],
                        pct_pos=100.0 * sum(1 for v in vals if v > 0) / len(vals),
                        n_days=days, usd_total_94d=usd,
                        usd_per_day=usd / 94.6, entries=entries))

    json.dump([{k: v for k, v in r.items() if k != "entries"} for r in out],
              open(os.path.join(OUT, "episodes.json"), "w"), indent=1)

    print()
    print("=" * 118)
    print("PER-DAY ECONOMICS at $5,000 per arb, one trade per episode")
    print("=" * 118)
    for r in out:
        print("%-8s %s/%s : %d episodes over %d days = %.1f/day, "
              "mean %.2f bps => $%.2f/day gross"
              % (r["sym"], r["X"][:8], r["Y"][:8], r["n_tradeable"], r["n_days"],
                 r["n_tradeable"] / 94.6, r["ep_net_mean"], r["usd_per_day"]))
    print()
    print("episode length in event-blocks tells how long a dislocation stays open;")
    print("a median of 1 means the gap is typically gone by the next traded block.")


if __name__ == "__main__":
    main()
