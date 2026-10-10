"""Stage 6: ground truth. Simulate the actual two-leg arb on-chain at historical blocks.

Stages 1-5 measured the edge with a MODEL of cost (point-in-time fee backed out of
the CLMM identity, plus closed-form slippage). The surviving edge is 0.32-1.14 bps
while that model disagrees with R2's router simulation by 0.33-0.51 bps. The signal
sits inside the ruler's graduations, so no amount of extra statistics settles it.
Only a real execution does.

At each sampled entry block, with WETH balance and allowance granted by
state-override:

    leg 1   WETH -> USDC   through pool_high's router   (exact input, $5,000 of WETH)
    leg 2   USDC -> WETH   through pool_low's  router   (exact input = leg 1 output)
    net     = 1e4 * (weth_out - weth_in) / weth_in

Direction: with p = token1/token0 = USDC per WETH, the pool quoting the HIGHER p
pays more USDC per WETH, so that is where WETH is sold; the lower-p pool is where
WETH is bought back. Profit = p_high/p_low - 1, matching the gap used in stage 5.

Leg 2 is submitted in the same aggregate3 as a replay of leg 1, so it hits pool_high
already moved by my own first leg and pays the real, not the marginal, price.

Pre-registered verdict rules (written in reports/R0_CROSS_VENUE_ARB.md section 4
before this ran):
  - realised mean > 0 AND day-blocked bootstrap lower bound > 0  -> ARB_EDGE_CONFIRMED
  - realised mean <= 0                                          -> ARB_CLOSED_BY_BOTS
  - |realised - model| > 1 bps                                  -> model void, R3 must be fixed too
"""
import bisect, json, os, random, sys, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

PANEL = "/root/base_truth_panel_r0"
sys.path.insert(0, os.path.join(PANEL, "src"))
os.chdir(PANEL)                       # depth_631 reads out/router_map.json relatively

import depth_631 as D                                          # noqa: E402
from slot_detect import SlotDetector                            # noqa: E402
from web3 import Web3                                           # noqa: E402

OUT = "/root/base_arb_edge_r0/out"
MIN_BLOCK = 46_000_000
ETH_USD, SIZE_USD = 4000.0, 5000.0
V_WETH_RAW = int((SIZE_USD / ETH_USD) * 1e18)
WETH_L = "0x4200000000000000000000000000000000000006"
USDC = Web3.to_checksum_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
N_SAMPLE = 100        # per pair; 2 pairs x 100 x 2 eth_calls = 400 calls ~ 8,000 credits
PAIRS = [("0x3fe04a59ebd38cf06080a6f60a98d124eb59392a",
          "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59"),
         ("0x3fe04a59ebd38cf06080a6f60a98d124eb59392a",
          "0xd0b53d9277642d899df5c87a3966a349a798f224")]
ALPHA = 0.05 / 2                      # two surviving pairs
_lock = threading.Lock()


def ch(q, fmt="TabSeparatedWithNames"):
    import subprocess
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
    pools = sorted({p for pr in PAIRS for p in pr})
    pl = ",".join("'%s'" % p for p in pools)

    meta = {}
    for ln in ch("SELECT pool, venue, factory, fee, tick_spacing, token1 "
                 "FROM census.pools WHERE pool IN (%s)" % pl).strip().split("\n")[1:]:
        pool, venue, fac, fee, tsp, t1 = ln.split("\t")
        meta[pool] = dict(pool=pool, venue=venue, factory=fac, fee=int(fee),
                          tick_spacing=int(tsp), w1=(t1.lower() == WETH_L))
        if not D.ROUTERS.get(fac):
            sys.stderr.write("NO ROUTER for factory %s (pool %s)\n" % (fac, pool[:12]))

    # ---- rebuild the model's signal blocks exactly as stage 5 did ----
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
               quantile(0.5)(1.0-(L*(sq-sqp))/a1)*1e4 AS f, count() AS n
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
        blk[pool][0].append(int(b)); blk[pool][1].append(sq)
        blk[pool][2].append(float(L)); blk[pool][3].append(int(ts))
    sq_lut = {p: Step(blk[p][0], blk[p][1]) for p in pools}
    L_lut = {p: Step(blk[p][0], blk[p][2]) for p in pools}
    ts_lut = {p: Step(blk[p][0], blk[p][3]) for p in pools}

    def leg_cost(pool, b):
        f = fee_lut[pool].at(b) if pool in fee_lut else None
        sq, L = sq_lut[pool].at(b), L_lut[pool].at(b)
        if f is None or sq is None or L is None or L <= 0 or sq <= 0:
            return None
        p = sq * sq
        v = V_WETH_RAW if meta[pool]["w1"] else V_WETH_RAW * p
        return f + 1e4 * v / (L * sq)

    def model_net(X, Y, b):
        sx, sy = sq_lut[X].at(b), sq_lut[Y].at(b)
        if sx is None or sy is None:
            return None
        px, py = sx * sx, sy * sy
        cx, cy = leg_cost(X, b), leg_cost(Y, b)
        if cx is None or cy is None or px <= 0 or py <= 0:
            return None
        return 1e4 * abs(px - py) / min(px, py) - cx - cy

    samples = []
    for X, Y in PAIRS:
        ev = sorted(set(blk[X][0]) | set(blk[Y][0]))
        sig = [b for b in ev if (model_net(X, Y, b) or -1) > 0]
        idx = {b: i for i, b in enumerate(ev)}
        eps, cur = [], [sig[0]]
        for prev, b in zip(sig, sig[1:]):
            if idx[b] - idx[prev] <= 1:
                cur.append(b)
            else:
                eps.append(cur)
                cur = [b]
        eps.append(cur)
        entries = [(e[0] + 1, model_net(X, Y, e[0] + 1)) for e in eps if e]
        entries = [(b, m) for b, m in entries if m is not None]
        # stratify by day so the sample cannot pile into a few sessions
        byday = defaultdict(list)
        for b, m in entries:
            t = ts_lut[X].at(b)
            byday[(t // 86400) if t else 0].append((b, m))
        rng = random.Random(20260818)
        days = sorted(byday)
        per = max(1, N_SAMPLE // max(len(days), 1))
        picked = []
        for d in days:
            picked.extend(rng.sample(byday[d], min(per, len(byday[d]))))
        rng.shuffle(picked)
        picked = picked[:N_SAMPLE]
        sys.stderr.write("pair %s/%s : %d episodes, %d days, sampled %d\n"
                         % (X[:8], Y[:8], len(entries), len(days), len(picked)))
        for b, m in picked:
            samples.append((X, Y, b, m))

    sys.stderr.write("total samples: %d  -> %d eth_calls (~%d credits)\n"
                     % (len(samples), 2 * len(samples), 2 * len(samples) * 20))

    # ---- slot detection once; ERC-20 storage layout does not move ----
    det = SlotDetector(D.RPC_URL)
    head = int(D.rpc("eth_blockNumber", [])["result"], 16) - 20
    routers = {p: D.ROUTERS.get(meta[p]["factory"]) for p in pools}
    ws = det.detect(D.WETH, D.MULTICALL3, list(routers.values())[0], head)
    us = det.detect(USDC, D.MULTICALL3, list(routers.values())[0], head)
    if not (ws and us):
        raise SystemExit("slot detection failed: WETH=%s USDC=%s" % (bool(ws), bool(us)))
    big = 1 << 128
    ov = {D.WETH: {"stateDiff": {ws["bal"]: D._w(big), ws["allow"]: D._w(big)}},
          USDC: {"stateDiff": {us["bal"]: D._w(big), us["allow"]: D._w(big)}}}
    sys.stderr.write("slots resolved; simulating ...\n")

    def dialect_key(pool):
        m = meta[pool]
        d = "univ3" if m["venue"] == "univ3" else "slipstream"
        return d, (m["fee"] if d == "univ3" else m["tick_spacing"])

    results = []

    def run(job):
        X, Y, b, mnet = job
        sx, sy = sq_lut[X].at(b), sq_lut[Y].at(b)
        if sx is None or sy is None:
            return None
        px, py = sx * sx, sy * sy
        # p = USDC per WETH: sell WETH where p is higher, buy it back where p is lower
        hi, lo = (X, Y) if px > py else (Y, X)
        rh, rl = routers[hi], routers[lo]
        if not rh or not rl:
            return None
        dh, kh = dialect_key(hi)
        dl, kl = dialect_key(lo)
        try:
            leg1 = (rh, D.swap_call(dh, D.WETH, USDC, kh, V_WETH_RAW))
            r1 = D.aggregate3([leg1], ov, b)
            got = D.amt_out(r1[0]) if r1 else None
            if not got:
                return dict(X=X, Y=Y, b=b, model=mnet, klass="LEG1_REVERT")
            leg2 = (rl, D.swap_call(dl, USDC, D.WETH, kl, got))
            r2 = D.aggregate3([leg1, leg2], ov, b)
            back = D.amt_out(r2[1]) if r2 and len(r2) > 1 else None
            if not back:
                return dict(X=X, Y=Y, b=b, model=mnet, klass="LEG2_REVERT")
            net = 1e4 * (back - V_WETH_RAW) / V_WETH_RAW
            t = ts_lut[X].at(b)
            return dict(X=X, Y=Y, b=b, model=mnet, realised=net, klass="OK",
                        hi=hi, lo=lo, usdc=got, day=(t // 86400) if t else 0)
        except Exception as e:                                    # noqa: BLE001
            return dict(X=X, Y=Y, b=b, model=mnet, klass="ERR_" + type(e).__name__)

    with ThreadPoolExecutor(max_workers=3) as ex:      # low, the backfill needs the RPC
        for i, r in enumerate(ex.map(run, samples)):
            if r:
                with _lock:
                    results.append(r)
            if (i + 1) % 50 == 0:
                sys.stderr.write("  %d/%d\n" % (i + 1, len(samples)))

    json.dump(results, open(os.path.join(OUT, "atomic.json"), "w"), indent=1)
    ok = [r for r in results if r["klass"] == "OK"]
    print("samples attempted : %d" % len(samples))
    print("simulated OK      : %d" % len(ok))
    for k, n in sorted(defaultdict(int, {kk: sum(1 for r in results if r["klass"] == kk)
                                         for kk in {r["klass"] for r in results}}).items(),
                       key=lambda x: -x[1]):
        print("  %-18s %d" % (k, n))
    if not ok:
        print("VERDICT = UNTESTABLE_ALL_REVERTED")
        return

    def mean(a): return sum(a) / len(a)

    def median(a):
        s = sorted(a); k = len(s)
        return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])

    def dayboot(byday, stat, n=8000, seed=5):
        rng = random.Random(seed)
        ds = list(byday)
        pooled = [v for d in ds for v in byday[d]]
        pt = stat(pooled)
        if len(ds) < 5:
            return pt, None, None
        o = []
        for _ in range(n):
            s = []
            for _ in range(len(ds)):
                s.extend(byday[ds[rng.randrange(len(ds))]])
            o.append(stat(s))
        o.sort()
        return pt, o[int(len(o) * ALPHA / 2)], o[int(len(o) * (1 - ALPHA / 2))]

    print()
    print("=" * 116)
    print("REALISED (on-chain simulation) vs MODEL")
    print("=" * 116)
    print("%-10s %-10s %6s %5s %10s %10s %12s %12s %9s %s"
          % ("poolX", "poolY", "n", "days", "model_mu", "real_mu", "real_lo",
             "real_hi", "real_win%", "verdict"))
    summary = []
    for X, Y in PAIRS:
        g = [r for r in ok if r["X"] == X and r["Y"] == Y]
        if len(g) < 20:
            print("%-10s %-10s %6d  (too few)" % (X[:10], Y[:10], len(g)))
            continue
        byday = defaultdict(list)
        for r in g:
            byday[r["day"]].append(r["realised"])
        mu, lo, hi = dayboot(byday, mean)
        mm = mean([r["model"] for r in g])
        win = 100.0 * sum(1 for r in g if r["realised"] > 0) / len(g)
        gapmod = mu - mm
        if abs(gapmod) > 1.0:
            note = "MODEL_VOID(%+.2f)" % gapmod
        else:
            note = "model_ok(%+.2f)" % gapmod
        if mu > 0 and lo is not None and lo > 0:
            verdict = "ARB_EDGE_CONFIRMED"
        elif mu <= 0:
            verdict = "ARB_CLOSED_BY_BOTS"
        else:
            verdict = "UNDERPOWERED"
        print("%-10s %-10s %6d %5d %10.2f %10.2f %12s %12s %9.1f %s  %s"
              % (X[:10], Y[:10], len(g), len(byday), mm, mu,
                 ("%.2f" % lo) if lo is not None else "n/a",
                 ("%.2f" % hi) if hi is not None else "n/a", win, verdict, note))
        summary.append(dict(X=X, Y=Y, n=len(g), days=len(byday), model_mean=mm,
                            realised_mean=mu, realised_lo=lo, realised_hi=hi,
                            realised_median=median([r["realised"] for r in g]),
                            win_pct=win, model_error=gapmod, verdict=verdict))

    print()
    print("=" * 116)
    print("REALISED DISTRIBUTION (a real arb contract reverts below threshold, so the")
    print("left tail costs only gas, not the full round trip)")
    print("=" * 116)
    for X, Y in PAIRS:
        g = sorted(r["realised"] for r in ok if r["X"] == X and r["Y"] == Y)
        if len(g) < 20:
            continue
        q = [g[int(len(g) * x)] for x in (0.05, 0.25, 0.5, 0.75, 0.95)]
        pos = [v for v in g if v > 0]
        print("%s/%s n=%d  p05=%.2f p25=%.2f p50=%.2f p75=%.2f p95=%.2f | "
              "mean_if_positive=%.2f (%.0f%% of samples)"
              % (X[:8], Y[:8], len(g), *q, mean(pos) if pos else 0.0,
                 100.0 * len(pos) / len(g)))

    json.dump(summary, open(os.path.join(OUT, "atomic_verdict.json"), "w"), indent=1)
    print()
    print("rpc calls used: %d" % D._stats["calls"])


if __name__ == "__main__":
    main()
