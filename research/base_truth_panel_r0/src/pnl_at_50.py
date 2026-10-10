#!/usr/bin/env python3
"""What has to be true for a $50-per-trade account to double, and how far off we are.

Written after discovering that every prior round sized trades at $5,000 when the
real size is about $50. Rather than restate results in dollars -- which is what
made the earlier numbers so easy to get wrong -- this expresses everything in two
size-free quantities:

    annual return  = (trades per capital slot per year) x (net edge per trade)
                   = (8760 / holding_hours) x net_bps / 10000

Capital cancels. A $500 account and a $50,000 account earn the same PERCENTAGE from
the same rule, as long as each is small enough not to move the pool -- and at $50
that is always true. So the target "double the account" is a statement about edge
per trade and holding period only, and the account size never enters.

The second size-free quantity is edge measured in units of the move being predicted:

    required_gross / sigma  =  required_net / sigma  +  hurdle / sigma

because the toll is a fixed rate while sigma grows as sqrt(time). This is what makes
holding period the only real lever, and it is why the answer has an interior optimum.

All inputs are measured, not assumed:
  - gas: 286 real Base receipts (out/gas_cost.json)
  - hurdle: census.pit_cost_50, the closed-form cost model validated against atomic
    round trips to within 0.2% at this size
  - sigma: edge.bars_1h_year_50, 411 pools, 366 days
  - strategy edges: the round 35-41 result files
"""
from __future__ import annotations

import json
import math
import subprocess
import sys

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800", "--max_memory_usage=12000000000"]
HOURS_PER_YEAR = 8760.0
SIZE_USD = 50.0
GAS_ROUNDTRIP_BPS = 2 * 0.0359 / SIZE_USD * 1e4


def ch(q: str) -> list[list[str]]:
    p = subprocess.run(CH + ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:2000])
        raise SystemExit("clickhouse failed")
    return [ln.split("\t") for ln in p.stdout.strip().split("\n") if ln]


def measured() -> tuple[float, float, float, float]:
    """Median and best-decile (cost, sigma_1h) over pools with a real year of data."""
    rows = ch("""
    WITH v AS (
      SELECT pool, stddevSamp(log(close / open)) * 1e4 AS s1h, count() AS bars
      FROM edge.bars_1h_year_50
      WHERE open > 0 AND close > 0 AND abs(log(close / open)) < 2.3
      GROUP BY pool HAVING bars >= 4000),
    c AS (SELECT pool, quantile(0.5)(cost_bps_med) AS c, count() AS days
          FROM census.pit_cost_50 WHERE feasible = 1 GROUP BY pool HAVING days >= 180)
    SELECT quantile(0.5)(c.c), quantile(0.5)(v.s1h),
           quantile(0.5)((2 * c.c + """ + f"{GAS_ROUNDTRIP_BPS}" + """) / v.s1h),
           quantile(0.1)((2 * c.c + """ + f"{GAS_ROUNDTRIP_BPS}" + """) / v.s1h)
    FROM v INNER JOIN c ON c.pool = v.pool
    """)
    return tuple(float(x) for x in rows[0])


def main() -> None:
    cost_med, sigma_1h, ratio_med, ratio_p10 = measured()
    print("=" * 100)
    print("MEASURED INPUTS at $50 per trade")
    print("=" * 100)
    print(f"  median one-way cost                {cost_med:8.1f} bps")
    print(f"  gas, round trip                    {GAS_ROUNDTRIP_BPS:8.1f} bps  "
          f"(286 real receipts, mean regime)")
    print(f"  median round-trip toll             {2 * cost_med + GAS_ROUNDTRIP_BPS:8.1f} bps")
    print(f"  median hourly sigma                {sigma_1h:8.1f} bps")
    print(f"  toll / sigma_1h, median pool       {ratio_med:8.3f}")
    print(f"  toll / sigma_1h, best decile       {ratio_p10:8.3f}")

    print()
    print("=" * 100)
    print("WHAT DOUBLING REQUIRES -- and why holding period is the only lever")
    print("=" * 100)
    print("annual return = (8760/h) x net_bps/10000, so capital and trade size cancel.")
    print()
    print(f"{'hold':>8}{'trades/slot/yr':>16}{'net needed':>12}{'toll':>9}"
          f"{'gross needed':>14}{'sigma_h':>10}{'gross/sigma':>13}"
          f"{'gross/sigma':>13}")
    print(f"{'hours':>8}{'':>16}{'to double':>12}{'bps':>9}{'bps':>14}{'bps':>10}"
          f"{'median pool':>13}{'best decile':>13}")
    best = None
    for h in (1, 4, 12, 24, 72, 168, 336, 720, 2160):
        n_per_slot = HOURS_PER_YEAR / h
        net_need = 1e4 / n_per_slot
        toll_med = 2 * cost_med + GAS_ROUNDTRIP_BPS
        sigma_h = sigma_1h * math.sqrt(h)
        gross_med = net_need + toll_med
        gm = gross_med / sigma_h
        gp = (net_need + ratio_p10 * sigma_1h) / sigma_h
        if best is None or gm < best[1]:
            best = (h, gm, gp, net_need, gross_med, sigma_h)
        print(f"{h:>8}{n_per_slot:>16.0f}{net_need:>12.1f}{toll_med:>9.1f}"
              f"{gross_med:>14.1f}{sigma_h:>10.0f}{gm:>13.3f}{gp:>13.3f}")
    h, gm, gp, net_need, gross_med, sigma_h = best
    print()
    print(f"  cheapest place to try to double is a {h}-hour hold: you must predict")
    print(f"  {100 * gm:.1f}% of one standard deviation per trade on a median pool,")
    print(f"  or {100 * gp:.1f}% if you only trade the best decile of pools.")
    print(f"  In per-trade Sharpe terms that is {gm:.3f} gross, which annualises to "
          f"{gm * math.sqrt(HOURS_PER_YEAR / h):.1f};")
    print(f"  the part you keep is {net_need / sigma_h:.3f} per trade, annualising to "
          f"{net_need / sigma_h * math.sqrt(HOURS_PER_YEAR / h):.2f}.")

    print()
    print("=" * 100)
    print("WHAT WE ACTUALLY MEASURED -- every family, in the same units")
    print("=" * 100)
    # gross edge in bps and the horizon it was measured at, from the round files
    families = [
        ("A1 hourly mean reversion", 4, 3.0, "round 35, no cell cleared zero"),
        ("A2 multi-day mean reversion", 24, 23.0, "round 40/41, widest CI 376 bps"),
        ("A2 multi-day mean reversion", 168, -20.3, "round 41 at $50"),
        ("A3 multi-bar momentum", 24, -15.0, "round 38, negative across grid"),
        ("A5 order flow imbalance", 4, 24.0, "round 38 best cell at $5,000"),
        ("A5 order flow imbalance", 24, 8.0, "round 38, 24h cell"),
        ("B1 cross-venue arbitrage", 1, 0.0, "round 37, zero episodes survived"),
    ]
    print(f"{'family':<32}{'h':>5}{'gross_bps':>11}{'sigma_h':>10}"
          f"{'gross/sigma':>13}{'need':>8}{'shortfall':>11}   note")
    for name, h, gross, note in families:
        sigma_h = sigma_1h * math.sqrt(h)
        n_per_slot = HOURS_PER_YEAR / h
        need = (1e4 / n_per_slot + 2 * cost_med + GAS_ROUNDTRIP_BPS) / sigma_h
        got = gross / sigma_h
        sf = (need / got) if got > 0 else float("inf")
        sfs = f"{sf:.1f}x" if math.isfinite(sf) else "n/a"
        print(f"{name:<32}{h:>5}{gross:>11.1f}{sigma_h:>10.0f}{got:>13.3f}"
              f"{need:>8.3f}{sfs:>11}   {note}")

    print()
    print("=" * 100)
    print("BREAK-EVEN, not doubling -- the lower bar, and whether anything clears it")
    print("=" * 100)
    print("to merely not lose money, gross edge only has to beat the toll:")
    for h in (4, 24, 168, 720):
        sigma_h = sigma_1h * math.sqrt(h)
        print(f"  h={h:>4}h  toll/sigma_h median {(2 * cost_med + GAS_ROUNDTRIP_BPS) / sigma_h:.3f}"
              f"   best decile {ratio_p10 * sigma_1h / sigma_h:.3f}"
              f"   -- A5's measured 0.017-0.051 "
              f"{'CLEARS' if 0.051 > ratio_p10 * sigma_1h / sigma_h else 'does not clear'}"
              f" the best decile")

    print()
    print("=" * 100)
    print("DOLLARS, for the record -- what each 1%/yr is worth on a small account")
    print("=" * 100)
    print(f"{'capital':>10}{'slots at $50':>14}{'+10%/yr':>10}{'+50%/yr':>10}"
          f"{'+100%/yr':>10}{'gas/yr at h=24':>16}")
    for cap in (200, 500, 1000, 2000, 5000):
        slots = int(cap // SIZE_USD)
        gas_yr = slots * (HOURS_PER_YEAR / 24) * 2 * 0.0359
        print(f"{cap:>10}{slots:>14}{0.10 * cap:>10.0f}{0.50 * cap:>10.0f}"
              f"{1.00 * cap:>10.0f}{gas_yr:>16.0f}")
    print()
    print("  gas is charged per trade, not per dollar, so it is the one cost that")
    print("  genuinely punishes a small account: at $200 of capital and daily")
    print("  turnover it is a large fraction of the capital every year.")

    json.dump(dict(cost_med=cost_med, sigma_1h=sigma_1h, ratio_med=ratio_med,
                   ratio_p10=ratio_p10, gas_bps=GAS_ROUNDTRIP_BPS,
                   best_hold_h=h), open("out/pnl_at_50.json", "w"), indent=1)


if __name__ == "__main__":
    main()
