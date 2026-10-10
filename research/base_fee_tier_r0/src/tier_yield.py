#!/usr/bin/env python3
"""C2: hold the pair fixed, vary the fee tier, compare fee yield per unit liquidity.

Pre-registration: ../GOAL.md. The decisive quantity is

    yield = sum over swaps of  fee_rate * |weth amount| / active liquidity

For a fixed pair and a fixed price range, the liquidity a dollar buys is the
same in every tier, and the in-range swap set is shared because the tiers track
the same price. So the ratio of yields between two tiers of one pair is the
ratio of fee revenue per dollar of position, and impermanent loss -- the thing
that killed round 36 -- cancels exactly between them.

Volume share and active liquidity are reported separately so the ordering can be
attributed: a cheap tier can lose either because flow does not come to it, or
because so much liquidity is already parked there that a new dollar is diluted.
"""
from __future__ import annotations

import collections
import json
import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
MIN_BLOCK = 46_000_000
MIN_SWAPS, MIN_DAYS = 500, 60
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=20000000000"]


def q(sql: str) -> str:
    p = subprocess.run(CH + ["--format", "TabSeparatedWithNames", "--query", sql],
                       capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def main() -> None:
    cand = {p["pool"]: p for p in
            json.load(open("/root/base_deep_edge_r0/out/candidate_pools.json"))}
    sym = {p["alt"]: p["symbol"] for p in cand.values()}

    raw = q(f"""
    WITH w AS (
      SELECT pool, venue, fee, tick_spacing,
             if(token0 = '{WETH}', token1, token0) AS alt,
             if(token0 = '{WETH}', 1, 0) AS w0
      FROM census.pools
      WHERE token0 = '{WETH}' OR token1 = '{WETH}'
    ),
    s AS (
      SELECT s.pool AS pool,
             count() AS n_swaps,
             uniqExact(toDate(s.block_time)) AS ndays,
             sum(abs(toFloat64(if(w.w0 = 1, s.amount0, s.amount1)))) AS vol_raw,
             sum(if(toFloat64(s.liquidity) > 0,
                    abs(toFloat64(if(w.w0 = 1, s.amount0, s.amount1)))
                    / toFloat64(s.liquidity), 0)) AS vol_over_L,
             quantile(0.5)(toFloat64(s.liquidity)) AS L_p50,
             countIf(toFloat64(s.liquidity) <= 0) AS n_zeroL
      FROM census.cl_swaps s INNER JOIN w ON s.pool = w.pool
      WHERE s.block_number >= {MIN_BLOCK}
      GROUP BY s.pool
    )
    SELECT w.alt AS alt, w.venue AS venue, w.fee AS fee,
           w.tick_spacing AS tick_spacing, w.pool AS pool,
           s.n_swaps AS n_swaps, s.ndays AS ndays, s.vol_raw AS vol_raw,
           s.vol_over_L AS vol_over_L, s.L_p50 AS L_p50, s.n_zeroL AS n_zeroL
    FROM w INNER JOIN s ON s.pool = w.pool
    WHERE s.n_swaps >= {MIN_SWAPS} AND s.ndays >= {MIN_DAYS} AND w.fee > 0
    ORDER BY alt, fee
    """)

    byalt: dict[str, list[dict]] = collections.defaultdict(list)
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        alt, ven, fee, tsp, pool, ns, nd, vol, vL, Lp, nz = ln.split("\t")
        byalt[alt].append(dict(
            alt=alt, venue=ven, fee=int(fee), tsp=int(tsp), pool=pool,
            n=int(ns), nd=int(nd), vol=float(vol), vol_over_L=float(vL),
            L=float(Lp), nzero=int(nz),
            # fee_rate * sum(vol / L_active): revenue per unit of my liquidity
            yield_=int(fee) / 1e6 * float(vL)))

    pairs = {a: v for a, v in byalt.items()
             if len(v) >= 2 and any(p["pool"] in cand for p in v)}
    print(f"alts with >=2 qualifying pools and >=1 in the depth universe: {len(pairs)}")

    cheap_wins = exp_wins = 0
    rows = []
    print("\n" + "=" * 122)
    print("Fee yield per unit of liquidity, by tier, within pair. yield_rel: best tier "
          "in the pair = 1.00")
    print("=" * 122)
    for a, v in sorted(pairs.items(), key=lambda x: -sum(p["vol"] for p in x[1])):
        v = [p for p in v if p["yield_"] > 0]
        if len(v) < 2:
            continue
        tot_vol = sum(p["vol"] for p in v)
        best = max(p["yield_"] for p in v)
        cheapest = min(v, key=lambda p: p["fee"])
        dearest = max(v, key=lambda p: p["fee"])
        if cheapest["fee"] == dearest["fee"]:
            continue
        win = max(v, key=lambda p: p["yield_"])
        if win["fee"] == cheapest["fee"]:
            cheap_wins += 1
        elif win["fee"] == dearest["fee"]:
            exp_wins += 1
        s = sym.get(a, a[:10])
        print(f"\n{s}  ({len(v)} tiers, winner = {win['fee'] / 100:.2f} bps "
              f"{'CHEAPEST' if win is cheapest else ('DEAREST' if win is dearest else 'middle')})")
        print(f"  {'venue':<9}{'fee_bps':>8}{'tick':>6}{'n_swaps':>9}{'vol_share':>11}"
              f"{'L_p50':>13}{'yield_rel':>11}  pool")
        for p in sorted(v, key=lambda x: x["fee"]):
            rel = p["yield_"] / best if best > 0 else 0.0
            mark = "*" if p["pool"] in cand else " "
            print(f" {mark}{p['venue']:<9}{p['fee'] / 100:>8.2f}{p['tsp']:>6}{p['n']:>9}"
                  f"{100 * p['vol'] / tot_vol:>10.1f}%{p['L']:>13.3g}{rel:>11.3f}  "
                  f"{p['pool'][:12]}")
            rows.append(p | {"symbol": s, "yield_rel": rel,
                             "vol_share": p["vol"] / tot_vol})

    n = cheap_wins + exp_wins
    print("\n" + "=" * 122)
    print(f"pairs where the CHEAPEST tier earns the most: {cheap_wins}")
    print(f"pairs where the DEAREST  tier earns the most: {exp_wins}")
    print(f"pairs where a middle tier wins: {len(pairs) - n} "
          "(counted out of the pairs that had a strict cheapest/dearest)")
    if n:
        if exp_wins > cheap_wins:
            verdict = (f"EXPENSIVE_TIER_WINS ({exp_wins} vs {cheap_wins}). C2 is refuted "
                       "on its own terms: the cheap tier bears pointwise identical "
                       "impermanent loss and collects strictly less fee revenue.")
        elif cheap_wins > exp_wins:
            verdict = (f"CHEAP_TIER_WINS ({cheap_wins} vs {exp_wins}). Premise holds; "
                       "stage 2 must add IL and entry/exit cost for an absolute verdict.")
        else:
            verdict = "NO_TIER_ORDERING (tied). Tier choice is not a dimension to bet on."
    else:
        verdict = "NO_TIER_ORDERING (no pair had a strict cheapest vs dearest tier)."
    print("\nVERDICT = " + verdict)

    # attribution: is the ordering driven by where flow goes, or by dilution?
    import statistics as st
    fr = [(r["fee"], r["yield_rel"], r["vol_share"], r["L"]) for r in rows]
    hi = [x for x in fr if x[0] >= 2500]
    lo = [x for x in fr if x[0] <= 500]
    print("\nattribution (all pools pooled, not per pair):")
    for lbl, g in ((f"fee <= 5 bps  (n={len(lo)})", lo), (f"fee >= 25 bps (n={len(hi)})", hi)):
        if g:
            print(f"  {lbl}: median yield_rel={st.median(x[1] for x in g):.3f}  "
                  f"median vol_share={100 * st.median(x[2] for x in g):.1f}%  "
                  f"median L_p50={st.median(x[3] for x in g):.3g}")
    json.dump({"verdict": verdict, "cheap_wins": cheap_wins, "exp_wins": exp_wins,
               "rows": rows}, open("out/tier_yield.json", "w"), indent=1)


if __name__ == "__main__":
    main()
