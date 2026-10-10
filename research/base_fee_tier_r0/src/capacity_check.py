#!/usr/bin/env python3
"""Self-check on the tier comparison: is the winning tier one I could actually enter?

Stage 1 ranked tiers by fee revenue per unit of my liquidity. That quantity is
proportional to 1 / L_active, so it is maximised in the emptiest pool -- and the
stage 1 table duly shows "winners" that carry 0.1% of the pair's volume. Round 36
made exactly this mistake (ledger error #4): the fee formula
`fee = volume * rate * L_mine/(L_active+L_mine)` looks like arithmetic but
embeds the assumption that joining does not change the pool.

So compute L_mine directly. For a position of value V (in WETH) over a range
[p(1-w), p(1+w)] at current price p, the standard concentrated-liquidity
identity gives

    V = L * (2*sqrt(p) - sqrt(pa) - p/sqrt(pb))
      = L * sqrt(p) * (2 - sqrt(1-w) - 1/sqrt(1+w))

so  L_mine = V / (sqrt(p) * kappa(w)),  kappa(0.05) = 0.04936.

p here is the raw pool price (token1 per token0 in raw units), which is what
sqrt_price_x96 encodes, so no decimal handling is needed as long as V is in the
same raw units as the WETH side.

A tier only counts as deployable if L_mine stays a small fraction of L_active.
The threshold is set at 10% before looking at any result.
"""
from __future__ import annotations

import collections
import json
import math
import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
MIN_BLOCK = 46_000_000
SIZE_USD = 5000.0
RANGE_W = 0.05
MAX_SHARE = 0.10          # L_mine / L_active above this is not deployable
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=20000000000"]
KAPPA = 2.0 - math.sqrt(1 - RANGE_W) - 1.0 / math.sqrt(1 + RANGE_W)


def q(sql: str) -> str:
    p = subprocess.run(CH + ["--format", "TabSeparatedWithNames", "--query", sql],
                       capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def main() -> None:
    prev = json.load(open("out/tier_yield.json"))
    pools = {r["pool"]: r for r in prev["rows"]}
    plist = ",".join(f"'{p}'" for p in pools)

    # median sqrt(p), median L, and the point-in-time ETH price per pool-window
    raw = q(f"""
    WITH e AS (SELECT quantile(0.5)(eth_usd) AS eth FROM edge.eth_usd_1h)
    SELECT s.pool AS pool,
           quantile(0.5)(sqrt(pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2))) AS sqp,
           quantile(0.5)(toFloat64(s.liquidity)) AS L_act,
           if(p.token0 = '{WETH}', 1, 0) AS w0,
           (SELECT eth FROM e) AS eth_usd
    FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
    WHERE s.pool IN ({plist}) AND s.block_number >= {MIN_BLOCK}
          AND s.sqrt_price_x96 > 0 AND toFloat64(s.liquidity) > 0
    GROUP BY s.pool, w0
    """)

    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, sqp, L_act, w0, eth = ln.split("\t")
        r = pools.get(pool)
        if not r:
            continue
        sqp, L_act, eth = float(sqp), float(L_act), float(eth)
        v_weth_raw = (SIZE_USD / eth) * 1e18
        # value expressed on the WETH side; when WETH is token0 the identity is
        # symmetric under p -> 1/p, so use the sqrt of whichever side WETH is on
        sq = sqp if int(w0) == 0 else 1.0 / sqp
        r["L_mine"] = v_weth_raw / (sq * KAPPA) if sq > 0 else float("inf")
        r["L_act"] = L_act
        r["share"] = r["L_mine"] / L_act if L_act > 0 else float("inf")
        r["eth_usd"] = eth

    have = [r for r in pools.values() if "share" in r]
    print(f"pools priced: {len(have)}/{len(pools)}   "
          f"real ${SIZE_USD:.0f} at point-in-time ETH "
          f"{have[0]['eth_usd']:.0f} (not the old 4000 constant)")
    print(f"range +/-{RANGE_W:.0%}, kappa={KAPPA:.5f}\n")

    sh = sorted(r["share"] for r in have)
    print("L_mine / L_active for a real $5,000 position:")
    for lbl, v in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9)):
        print(f"  {lbl} = {sh[int(len(sh) * v)]:.3g}")
    ok = [r for r in have if r["share"] <= MAX_SHARE]
    print(f"\ndeployable (share <= {MAX_SHARE:.0%}): {len(ok)}/{len(have)} pools "
          f"({100 * len(ok) / len(have):.0f}%)")

    # do the stage-1 winners survive the capacity gate?
    print("\n" + "=" * 108)
    print("Stage 1 winners re-examined: was the top-yield tier deployable?")
    print("=" * 108)
    bypair: dict[str, list] = collections.defaultdict(list)
    for r in have:
        bypair[r["symbol"]].append(r)
    kept_cheap = kept_dear = kept_mid = 0
    flipped = 0
    print(f"{'pair':<9}{'winner_fee':>11}{'win_share':>11}{'deployable':>12}"
          f"{'best_ok_fee':>13}{'ordering':>12}")
    for s, v in sorted(bypair.items()):
        v = [r for r in v if r["yield_"] > 0]
        if len(v) < 2:
            continue
        fees = [r["fee"] for r in v]
        if min(fees) == max(fees):
            continue
        win = max(v, key=lambda r: r["yield_"])
        good = [r for r in v if r["share"] <= MAX_SHARE]
        if not good:
            print(f"{s:<9}{win['fee'] / 100:>11.2f}{win['share']:>11.2g}"
                  f"{'no':>12}{'none':>13}{'-':>12}")
            continue
        bo = max(good, key=lambda r: r["yield_"])
        if bo["fee"] == min(r["fee"] for r in good):
            order, kept_cheap = "CHEAPEST", kept_cheap + 1
        elif bo["fee"] == max(r["fee"] for r in good):
            order, kept_dear = "DEAREST", kept_dear + 1
        else:
            order, kept_mid = "middle", kept_mid + 1
        if bo["pool"] != win["pool"]:
            flipped += 1
        print(f"{s:<9}{win['fee'] / 100:>11.2f}{win['share']:>11.2g}"
              f"{('yes' if win['share'] <= MAX_SHARE else 'NO'):>12}"
              f"{bo['fee'] / 100:>13.2f}{order:>12}")

    print(f"\nstage-1 winner was not deployable in {flipped} pairs -> the ranking "
          "changed once capacity was enforced")
    print(f"after the capacity gate: cheapest wins {kept_cheap}, dearest wins "
          f"{kept_dear}, middle wins {kept_mid}")

    n = kept_cheap + kept_dear
    if n:
        from math import comb
        k = min(kept_cheap, kept_dear)
        p = sum(comb(n, i) for i in range(k + 1)) / 2 ** n * 2
        print(f"two-sided binomial on {kept_cheap} vs {kept_dear}: p = {min(p, 1.0):.3f}")
    else:
        p = 1.0

    # absolute fee level, not within-pair rank -- the question C2 actually asked
    import statistics as st
    print("\n" + "=" * 108)
    print("yield_rel by ABSOLUTE fee level (within-pair normalised, deployable only)")
    print("=" * 108)
    buckets = [(0, 5), (5, 15), (15, 30), (30, 100), (100, 10 ** 9)]
    print(f"{'fee band (bps)':<18}{'pools':>7}{'yield_rel p50':>15}"
          f"{'vol_share p50':>15}{'share p50':>12}")
    for a, b in buckets:
        g = [r for r in ok if a * 100 <= r["fee"] < b * 100]
        if not g:
            continue
        band = f"{a}-{b}" if b < 10 ** 9 else f"{a}+"
        print(f"{band:<18}{len(g):>7}"
              f"{st.median(r['yield_rel'] for r in g):>15.3f}"
              f"{100 * st.median(r['vol_share'] for r in g):>14.1f}%"
              f"{st.median(r['share'] for r in g):>12.3g}")

    if n and p > 0.05:
        verdict = (f"NO_TIER_ORDERING: after enforcing capacity, cheapest wins "
                   f"{kept_cheap} and dearest wins {kept_dear} of {n} decisive pairs "
                   f"(binomial p={min(p, 1.0):.3f}), with {kept_mid} pairs won by a "
                   "middle tier. Fee tier does not order fee revenue per dollar, so "
                   "'provide in the cheap tier to capture flow' has nothing to bet on.")
    elif n and kept_cheap > kept_dear:
        verdict = f"CHEAP_TIER_WINS after capacity gate ({kept_cheap} vs {kept_dear}, p={p:.3f})"
    else:
        verdict = f"EXPENSIVE_TIER_WINS after capacity gate ({kept_dear} vs {kept_cheap}, p={p:.3f})"
    print("\nVERDICT = " + verdict)
    json.dump({"verdict": verdict, "kept_cheap": kept_cheap, "kept_dear": kept_dear,
               "kept_mid": kept_mid, "flipped": flipped, "binom_p": p,
               "deployable": len(ok), "n_pools": len(have),
               "share_p50": sh[len(sh) // 2]},
              open("out/capacity_check.json", "w"), indent=1)


if __name__ == "__main__":
    main()
