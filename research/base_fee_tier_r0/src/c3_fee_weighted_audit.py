#!/usr/bin/env python3
"""Where do the attributed fees actually come from?

The capacity audit in c3_cross_rate.py weighted the realized liquidity share by
swap volume, and that is the wrong weight. Attribution is

    fee_i = weth_amt_i * fee_tier * L / (L_active_i + L)

so a single swap that happens while active liquidity is near zero contributes a
share near 1. Such a swap can be small in volume terms and still dominate the fee
total, because the position is only $50 while a 1 WETH swap is roughly $2,700: at
a 5 bps tier that one swap alone pays 270 bps of the position. A volume-weighted
share divides by total volume and hides it completely.

The statistic that cannot hide it is the fee-weighted share, plus the fraction of
fees drawn from swaps whose share exceeds the 10% capacity limit. If cbBTC's 1047
bps comes from moments when nobody else's liquidity was in range, it is not
revenue -- it is a queue that would not have been there, and at those moments the
position would have been the counterparty to whatever moved the price.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]


def ch(q: str, fmt: str = "PrettyCompact") -> str:
    p = subprocess.run(CH + ["--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return p.stdout


T = 7
BASE = ch(f"SELECT toString(min(entry)) FROM census.c3x_pos WHERE T = {T}",
          fmt="TSV").strip()
print(f"T={T} base={BASE}\n")

print("=" * 116)
print("Fee-weighted liquidity share, per symbol, T=7 -- the audit statistic that "
      "volume weighting hides")
print("=" * 116)
print(ch(f"""
WITH sw AS (
  SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
         abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1))) AS wamt,
         toDate('{BASE}') + {T} * intDiv(toDate(s.block_time) - toDate('{BASE}'),
                                         {T}) AS entry_d
  FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
  WHERE s.pool IN (SELECT DISTINCT pool FROM census.c3x_pos WHERE T = {T})
    AND s.sqrt_price_x96 > 0 AND s.block_time >= toDate('{BASE}')
),
att AS (
  SELECT p.pos_id AS pos_id, p.pool AS pool,
         sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                sw.wamt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0)) AS fee_all,
         sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper
                AND p.L / (sw.Lact + p.L) > 0.10,
                sw.wamt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0)) AS fee_hot,
         sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                sw.wamt * (p.fee / 1e6) * p.L / (sw.Lact + p.L)
                * p.L / (sw.Lact + p.L), 0)) AS fee_x_share,
         countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper
                 AND p.L / (sw.Lact + p.L) > 0.10) AS n_hot,
         countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in
  FROM sw INNER JOIN census.c3x_pos p
    ON p.pool = sw.pool AND p.entry = sw.entry_d
  WHERE p.T = {T}
  GROUP BY pos_id, pool
)
SELECT t.symbol AS symbol,
       count() AS positions,
       round(quantile(0.5)(att.fee_x_share / nullIf(att.fee_all, 0)), 5)
         AS fee_wtd_share_p50,
       round(quantile(0.9)(att.fee_x_share / nullIf(att.fee_all, 0)), 4)
         AS fee_wtd_share_p90,
       round(max(att.fee_x_share / nullIf(att.fee_all, 0)), 4)
         AS fee_wtd_share_max,
       round(100.0 * quantile(0.5)(att.fee_hot / nullIf(att.fee_all, 0)), 2)
         AS pct_fees_from_hot_p50,
       round(100.0 * avg(att.fee_hot / nullIf(att.fee_all, 0)), 2)
         AS pct_fees_from_hot_mean,
       round(100.0 * max(att.fee_hot / nullIf(att.fee_all, 0)), 1)
         AS pct_fees_from_hot_max,
       round(100.0 * countIf(att.fee_hot / nullIf(att.fee_all, 0) > 0.5)
             / count(), 1) AS pct_positions_majority_hot
FROM att
INNER JOIN census.pools p ON p.pool = att.pool
INNER JOIN census.tokens t
  ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
GROUP BY symbol
ORDER BY pct_fees_from_hot_mean DESC
"""))

print("=" * 116)
print("How often is active liquidity effectively absent? Distribution of "
      "instantaneous liquidity per symbol")
print("=" * 116)
print("A pool whose liquidity is sometimes near zero lets any position claim "
      "nearly the whole fee on\nwhatever trades through at that instant. The "
      "q01/q10 columns are what the capacity gate sees.\n")
print(ch(f"""
SELECT t.symbol AS symbol,
       count() AS swaps,
       round(100.0 * countIf(toFloat64(s.liquidity) < 1e12) / count(), 3)
         AS pct_liq_below_1e12,
       round(quantile(0.01)(toFloat64(s.liquidity)), 0) AS liq_q01,
       round(quantile(0.10)(toFloat64(s.liquidity)), 0) AS liq_q10,
       round(quantile(0.50)(toFloat64(s.liquidity)), 0) AS liq_p50,
       round(quantile(0.50)(toFloat64(s.liquidity))
             / nullIf(quantile(0.01)(toFloat64(s.liquidity)), 0), 1)
         AS p50_over_q01
FROM census.cl_swaps s
INNER JOIN census.pools p ON p.pool = s.pool
INNER JOIN census.tokens t
  ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
WHERE s.pool IN (SELECT DISTINCT pool FROM census.c3x_pos)
  AND s.sqrt_price_x96 > 0
GROUP BY symbol
ORDER BY pct_liq_below_1e12 DESC
"""))
