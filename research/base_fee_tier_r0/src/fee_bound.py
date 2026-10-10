#!/usr/bin/env python3
"""Absolute upper bound on attributed fees: an LP cannot earn more than the pool took.

Audit check 3 showed the whole positive Test A cell is one symbol, cbBTC, at +934
bps a week, and its own numbers do not agree with each other: a median 0.4% share of
active liquidity paired with 966 bps of fee revenue. Those two cannot both be true
unless the pool collected roughly 250x my notional in fees in a week.

This check needs no model. For each position, total fees the POOL collected over the
holding window is sum(|WETH leg| x fee_rate), which is an identity, and my
attributed fee must be a fraction of it. Any position where the ratio exceeds 1 is
proof the attribution is broken; the distribution of the ratio shows whether it is a
cbBTC problem or a pipeline problem.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"


def ch(q: str, fmt: str = "TabSeparatedWithNames") -> str:
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--max_execution_time=14400",
                        "--max_memory_usage=12000000000", "--format", fmt,
                        "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def main() -> None:
    rows = json.load(open("out/stage3_pnl.json"))
    sel = [r for r in rows if r["selected"]]
    pools = sorted({r["pool"] for r in sel})
    plist = ",".join(f"'{p}'" for p in pools)

    # pool-collected fees per (pool, day), in WETH: identity, no modelling
    raw = ch(f"""
    SELECT s.pool AS pool, toString(toDate(s.block_time)) AS d,
           sum(abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
               * (pl.fee / 1e6)) / 1e18 AS pool_fees_weth,
           sum(abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1))))
               / 1e18 AS pool_vol_weth,
           quantile(0.5)(toFloat64(s.liquidity)) AS L_p50,
           quantile(0.01)(toFloat64(s.liquidity)) AS L_p01
    FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
    WHERE s.pool IN ({plist}) AND s.sqrt_price_x96 > 0
    GROUP BY pool, d
    """)
    pf, pv, lp50, lp01 = {}, {}, {}, {}
    for ln in raw.strip().split("\n")[1:]:
        if not ln:
            continue
        pool, d, f, v, l5, l1 = ln.split("\t")
        pf[(pool, d)] = float(f)
        pv[(pool, d)] = float(v)
        lp50[(pool, d)] = float(l5)
        lp01[(pool, d)] = float(l1)

    import datetime as dt

    def window_fees(pool, entry, T):
        y, m, dd = map(int, entry.split("-"))
        d0 = dt.date(y, m, dd)
        return (sum(pf.get((pool, (d0 + dt.timedelta(days=i)).isoformat()), 0.0)
                    for i in range(T)),
                sum(pv.get((pool, (d0 + dt.timedelta(days=i)).isoformat()), 0.0)
                    for i in range(T)))

    print("=" * 108)
    print("my attributed fee / total fee the pool collected, per position "
          "(must be <= 1)")
    print("=" * 108)
    out = []
    for r in sel:
        tot, vol = window_fees(r["pool"], r["entry"], r["T"])
        v_ref = 5000.0 / 1850.0        # nominal WETH scale only for reporting
        mine = r["fee_bps"] / 1e4 * v_ref
        if tot > 0:
            out.append((mine / tot, r, mine, tot, vol))
    out.sort(key=lambda x: -x[0])
    over = [x for x in out if x[0] > 1.0]
    print(f"positions checked: {len(out)}")
    print(f"positions where my fee EXCEEDS the pool's total fees: {len(over)} "
          f"({100.0*len(over)/len(out):.2f}%)")
    bysym = defaultdict(int)
    for x in over:
        bysym[x[1]["symbol"]] += 1
    print(f"the impossible ones by symbol: "
          f"{sorted(bysym.items(), key=lambda z: -z[1])[:12]}")
    print(f"\n{'ratio':>10}{'symbol':<12}{'T':>3}{'entry':>12}"
          f"{'my_fee_weth':>13}{'pool_fee_weth':>15}{'pool_vol_weth':>15}"
          f"{'share':>8}")
    for ratio, r, mine, tot, vol in out[:15]:
        print(f"{ratio:>10.1f}{r['symbol'][:11]:<12}{r['T']:>3}{r['entry']:>12}"
              f"{mine:>13.4f}{tot:>15.4f}{vol:>15.1f}{r['share']:>8.4f}")

    print("\ndistribution of the ratio over all selected positions:")
    v = sorted(x[0] for x in out)
    for q in (0.5, 0.9, 0.99, 0.999, 1.0):
        print(f"   q{q:<6} {v[min(int(q*len(v)), len(v)-1)]:>12.3f}")

    print("\nsame, excluding cbBTC:")
    v2 = sorted(x[0] for x in out if x[1]["symbol"] != "cbBTC")
    for q in (0.5, 0.9, 0.99, 0.999, 1.0):
        print(f"   q{q:<6} {v2[min(int(q*len(v2)), len(v2)-1)]:>12.3f}")

    print("\nliquidity dispersion for the worst offenders: the capacity gate uses a")
    print("trailing MEDIAN liquidity, the fee attribution uses the INSTANTANEOUS")
    print("liquidity of each swap. If those differ by orders of magnitude the gate")
    print("passes while the attribution runs away.")
    print(f"{'symbol':<12}{'pool':<14}{'L_p50':>14}{'L_p01':>14}{'p50/p01':>10}")
    seen = set()
    for ratio, r, *_ in out[:40]:
        if r["pool"] in seen:
            continue
        seen.add(r["pool"])
        k = (r["pool"], r["entry"])
        a, b = lp50.get(k), lp01.get(k)
        if a and b and b > 0:
            print(f"{r['symbol'][:11]:<12}{r['pool'][:12]:<14}{a:>14.3e}"
                  f"{b:>14.3e}{a/b:>10.1f}")


if __name__ == "__main__":
    main()
