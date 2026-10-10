"""Stage 2: block-level gap, and whether it is still there one block later.

Two things stage 1 could not answer.

1. MINUTE BUCKETING INFLATES THE GAP. Stage 1 compared each pool's last price
   within a minute; those two trades can be 59 seconds apart, so part of the
   apparent gap is pure asynchrony rather than a simultaneous dislocation. Block
   level removes that entirely: at a given block both pools have an exact price.

2. EXECUTABILITY. I have no same-block capability, so a gap visible at block b is
   only mine if it is still open at b+1. Prices are forward-filled, which is exact
   for a CLMM, so evaluating at b+1 answers exactly: had my transaction landed one
   block later, would the trade still have been profitable?

Limitation that historical data cannot settle: a real submission sits in the
mempool where faster actors can see and pre-empt it. Persistence measured here is
therefore an UPPER bound on what is capturable.
"""
import json, os, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
MIN_BLOCK = 46_000_000
LAGS = [1, 2, 5, 30, 150]        # blocks; Base is 2s so 30 blocks ~ 1 min


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--max_memory_usage=30000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


def load_block_prices(pools):
    """block -> price, per pool, only at blocks where that pool traded."""
    pl = ",".join("'%s'" % p for p in pools)
    raw = ch("""
        SELECT pool, block_number AS b,
               argMax(toString(sqrt_price_x96), log_index) AS sp
        FROM census.cl_swaps
        WHERE pool IN (%s) AND block_number >= %d AND sqrt_price_x96 > 0
        GROUP BY pool, b ORDER BY pool, b""" % (pl, MIN_BLOCK))
    out = defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, b, sp = ln.split("\t")
        s = int(sp) / (2.0 ** 96)
        out[pool].append((int(b), s * s))
    return out


class FF:
    """forward-filled price lookup over a sorted (block, price) list."""

    def __init__(self, rows):
        self.b = [r[0] for r in rows]
        self.p = [r[1] for r in rows]

    def at(self, blk):
        import bisect
        i = bisect.bisect_right(self.b, blk) - 1
        return self.p[i] if i >= 0 else None


def main():
    s1 = json.load(open(os.path.join(OUT, "stage1.json")))
    # pre-registered promotion rule: enough events to be testable at all
    promoted = [r for r in s1 if r["n_net_pos"] >= 100]
    promoted.sort(key=lambda r: -r["n_net_pos"])
    sys.stderr.write("pairs promoted to stage 2 (>=100 minute events): %d\n" % len(promoted))
    if not promoted:
        print("no pair had >=100 net>0 minutes; stage 2 not applicable")
        return

    pools = sorted({r["buy"] for r in promoted} | {r["sell"] for r in promoted})
    sys.stderr.write("loading block-level prices for %d pools ...\n" % len(pools))
    bp = load_block_prices(pools)
    for p in pools:
        sys.stderr.write("  %s  blocks_with_swaps=%d\n" % (p[:12], len(bp[p])))
    ff = {p: FF(bp[p]) for p in pools}

    print()
    print("=" * 130)
    print("BLOCK-LEVEL GAP vs MINUTE-LEVEL GAP  (how much of stage 1 was bucketing artifact?)")
    print("=" * 130)
    print("%-10s %-11s %-11s %7s %10s %12s %12s %12s %12s"
          % ("symbol", "buy", "sell", "hurdle", "ev_blocks",
             "min_pct>0", "blk_pct>0", "blk_net_p99", "blk_net_max"))
    out = []
    for r in promoted:
        A, B, h = r["buy"], r["sell"], r["hurdle"]
        ev = sorted(set(x[0] for x in bp[A]) | set(x[0] for x in bp[B]))
        fa, fb = ff[A], ff[B]
        nets, sig = [], []
        for b in ev:
            pa, pb = fa.at(b), fb.at(b)
            if pa is None or pb is None or pa <= 0:
                continue
            net = 1e4 * (pb - pa) / pa - h
            nets.append(net)
            if net > 0:
                sig.append(b)
        if not nets:
            continue
        ns = sorted(nets)
        n = len(ns)
        pct = 100.0 * len(sig) / n
        print("%-10s %-11s %-11s %7.1f %10d %12.3f %12.3f %12.1f %12.1f"
              % (r["sym"], A[:10], B[:10], h, n, r["pct_net_pos"], pct,
                 ns[int(n * .99)], ns[-1]))
        out.append(dict(r, ev_blocks=n, blk_pct_pos=pct, blk_net_p99=ns[int(n * .99)],
                        blk_net_max=ns[-1], n_blk_sig=len(sig), sig=sig[:200000]))

    print()
    print("=" * 130)
    print("PERSISTENCE: given net>0 at block b, what is net at b+lag?  (mean bps, and % still >0)")
    print("=" * 130)
    print("%-10s %-11s %-11s %8s %9s %s"
          % ("symbol", "buy", "sell", "n_signal", "net@b",
             "  ".join("%14s" % ("net@b+%d" % L) for L in LAGS)))
    final = []
    for r in out:
        A, B, h = r["buy"], r["sell"], r["hurdle"]
        fa, fb = ff[A], ff[B]
        sig = r["sig"]
        if len(sig) < 100:
            continue

        def net_at(blk):
            pa, pb = fa.at(blk), fb.at(blk)
            if pa is None or pb is None or pa <= 0:
                return None
            return 1e4 * (pb - pa) / pa - h

        base = [net_at(b) for b in sig]
        base = [x for x in base if x is not None]
        cells = []
        rec = dict(sym=r["sym"], buy=A, sell=B, hurdle=h, n_signal=len(sig),
                   net_at_b=sum(base) / len(base))
        for L in LAGS:
            v = [net_at(b + L) for b in sig]
            v = [x for x in v if x is not None]
            if not v:
                cells.append("   n/a"); continue
            mu = sum(v) / len(v)
            pos = 100.0 * sum(1 for x in v if x > 0) / len(v)
            cells.append("%7.2f/%5.1f%%" % (mu, pos))
            rec["net_at_b%d" % L] = mu
            rec["pct_pos_b%d" % L] = pos
        print("%-10s %-11s %-11s %8d %9.2f %s"
              % (r["sym"], A[:10], B[:10], len(sig), rec["net_at_b"],
                 "  ".join("%14s" % c for c in cells)))
        # how many distinct days do the signals span
        rec["n_sig_days"] = None
        final.append(rec)

    json.dump(final, open(os.path.join(OUT, "stage2.json"), "w"), indent=1)
    print()
    print("reading: net@b is the gap at the moment it is visible. net@b+1 is what a")
    print("non-MEV actor could actually have taken. If net@b+1 <= 0 the gap belongs")
    print("to faster participants, not to us.")


if __name__ == "__main__":
    main()
