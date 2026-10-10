"""Restrict to positions where the fee model's own assumption holds, then re-test.

The fee model assumes my position does not change the market. That assumption is
checked per position by how large my liquidity is against the pool's actual
active liquidity at the swaps I would have earned from. Where my $5,000 position
would have been a large share of the pool, the recorded volume would not have
occurred as recorded, and the fee estimate is not trustworthy.

Validity rule (computed from liquidity data only, never from returns):
    a position is MODEL-VALID if fewer than 1% of its in-range swaps had
    L_mine / L_active > 0.10

This is a measurement-validity filter, not a performance filter.
"""
import json, os, random, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
BASE = "2026-06-14"
ALPHA = 0.05 / 9
SHARE_CAP = 0.10
MAX_BAD_FRAC = 0.01


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000]); raise SystemExit("clickhouse failed")
    return p.stdout


def mean(a): return sum(a) / len(a)


def median(a):
    s = sorted(a); k = len(s)
    return s[k // 2] if k % 2 else 0.5 * (s[k // 2 - 1] + s[k // 2])


def block_boot(byd, stat, n=6000, seed=7):
    dates = list(byd)
    pooled = [v for d in dates for v in byd[d]]
    point = stat(pooled)
    if len(dates) < 5:
        return point, None, None, len(dates)
    rng = random.Random(seed); out = []
    for _ in range(n):
        samp = []
        for _ in range(len(dates)):
            samp.extend(byd[dates[rng.randrange(len(dates))]])
        out.append(stat(samp))
    out.sort()
    return point, out[int(len(out) * ALPHA / 2)], out[int(len(out) * (1 - ALPHA / 2))], len(dates)


def main():
    valid = {}
    for T in (1, 7, 30):
        sys.stderr.write("checking model validity for T=%d ...\n" % T)
        q = """
        WITH sw AS (
            SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
                   toDate('{b}') + {T}*intDiv(toDate(s.block_time)-toDate('{b}'), {T}) AS entry_d
            FROM census.cl_swaps s
            WHERE s.pool IN (SELECT DISTINCT pool FROM census.lp_pos WHERE T={T})
              AND s.sqrt_price_x96 > 0)
        SELECT p.pos_id AS pos_id,
               countIf(sw.Lact = 0 OR p.L / sw.Lact > {cap}) AS n_bad,
               count() AS n_inrange,
               round(quantile(0.5)(if(sw.Lact>0, p.L/sw.Lact, 999)), 6) AS med_share
        FROM sw INNER JOIN census.lp_pos p ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T} AND sw.tick >= p.tick_lower AND sw.tick < p.tick_upper
        GROUP BY p.pos_id""".format(b=BASE, T=T, cap=SHARE_CAP)
        for ln in ch(q).strip().split("\n")[1:]:
            if not ln:
                continue
            pid, nb, nir, msh = ln.split("\t")
            nb, nir = int(nb), int(nir)
            valid[int(pid)] = dict(bad_frac=(nb / nir if nir else 1.0),
                                   n_inrange=nir, med_share=float(msh))

    rows = json.load(open(os.path.join(OUT, "pnl.json")))
    pos = json.load(open(os.path.join(OUT, "positions.json")))
    # pnl.json preserved order of positions minus skips; rebuild the id mapping
    key2id = {(p["pool"], p["entry"], p["T"], p["c"]): i for i, p in enumerate(pos)}
    n_no_inrange = 0
    for r in rows:
        pid = key2id.get((r["pool"], r["entry"], r["T"], r["c"]))
        v = valid.get(pid)
        if v is None:
            r["model_valid"] = False
            r["bad_frac"] = 1.0
            n_no_inrange += 1
        else:
            r["model_valid"] = v["bad_frac"] <= MAX_BAD_FRAC and v["n_inrange"] > 0
            r["bad_frac"] = v["bad_frac"]
            r["med_share"] = v["med_share"]

    nv = sum(1 for r in rows if r["model_valid"])
    print("positions total          : %d" % len(rows))
    print("positions MODEL-VALID    : %d  (%.1f%%)" % (nv, 100.0 * nv / len(rows)))
    print("positions with no in-range swaps: %d" % n_no_inrange)
    print("distinct pools valid     : %d" % len({r["pool"] for r in rows if r["model_valid"]}))
    print("median cost_bps of valid : %.1f   of invalid: %.1f"
          % (median([r["cost_bps"] for r in rows if r["model_valid"]]),
             median([r["cost_bps"] for r in rows if not r["model_valid"]])))
    print()

    print("=" * 122)
    print("MODEL-VALID SUBSAMPLE — the 9 pre-registered cells")
    print("=" * 122)
    print("%-4s %-5s %7s %6s %6s %8s %8s %8s %9s %9s %11s %11s %6s %s"
          % ("T", "c", "n", "pools", "dates", "fee", "il", "gross", "cost",
             "net_med", "mean", "mean_lo", "win%", "verdict"))
    out = []
    for T in (1, 7, 30):
        for c in (0.5, 1.0, 2.0):
            g = [r for r in rows if r["T"] == T and r["c"] == c and r["model_valid"]]
            if len(g) < 20:
                print("%-4d %-5.1f %7d  (too few model-valid positions)" % (T, c, len(g)))
                continue
            byd = defaultdict(list)
            for r in g:
                byd[r["entry"]].append(r["net_bps"])
            mu, lo, hi, nd = block_boot(byd, mean)
            md = median([r["net_bps"] for r in g])
            s = sorted(r["net_bps"] for r in g); k = max(1, int(len(s) * 0.005))
            trimmed = mean(s[k:-k]) if len(s) > 2 * k else mu
            win = 100.0 * sum(1 for r in g if r["net_bps"] > 0) / len(g)
            if nd < 5:
                verdict = "UNTESTABLE_TOO_FEW_WINDOWS"
            elif mu > 0 and md > 0 and lo is not None and lo > 0:
                verdict = "LP_EDGE_CONFIRMED"
            elif mu > 0 and (md <= 0 or trimmed <= 0):
                verdict = "LOTTERY_DEPENDENT"
            elif hi is not None and hi < 0:
                verdict = "NO_LP_EDGE"
            elif lo is not None and lo < 0 < hi and (hi - lo) > 2 * abs(mu):
                verdict = "UNDERPOWERED"
            else:
                verdict = "NO_LP_EDGE"
            print("%-4d %-5.1f %7d %6d %6d %8.1f %8.1f %8.1f %9.1f %9.1f %11.1f %11s %6.1f %s"
                  % (T, c, len(g), len({r["pool"] for r in g}), nd,
                     median([r["fee_bps"] for r in g]), median([r["il_bps"] for r in g]),
                     median([r["fee_bps"] - r["il_bps"] for r in g]),
                     median([r["cost_bps_paid"] for r in g]), md, mu,
                     ("%.1f" % lo) if lo is not None else "n/a", win, verdict))
            out.append(dict(T=T, c=c, n=len(g), pools=len({r["pool"] for r in g}),
                            n_dates=nd, fee_med=median([r["fee_bps"] for r in g]),
                            il_med=median([r["il_bps"] for r in g]),
                            gross_med=median([r["fee_bps"] - r["il_bps"] for r in g]),
                            cost_med=median([r["cost_bps_paid"] for r in g]),
                            net_med=md, mean=mu, mean_lo=lo, mean_hi=hi,
                            trimmed=trimmed, win_pct=win, verdict=verdict))

    print()
    print("=" * 122)
    print("LEFT-TAIL SHAPE (model-valid, T=7) — LP is structurally short volatility")
    print("=" * 122)
    for c in (0.5, 1.0, 2.0):
        g = sorted(r["net_bps"] for r in rows if r["T"] == 7 and r["c"] == c and r["model_valid"])
        if len(g) < 20:
            continue
        qs = [g[int(len(g) * q)] for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95)]
        print("c=%-4.1f n=%-5d p01=%9.1f p05=%8.1f p25=%7.1f p50=%7.1f p75=%7.1f p95=%7.1f  worst=%.0f"
              % (c, len(g), *qs, g[0]))

    json.dump(out, open(os.path.join(OUT, "valid_verdicts.json"), "w"), indent=1)
    json.dump(rows, open(os.path.join(OUT, "pnl_flagged.json"), "w"))


if __name__ == "__main__":
    main()
