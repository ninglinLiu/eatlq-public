#!/usr/bin/env python3
"""Is cbBTC's 1047 bps of weekly fees real, or is it an accounting artifact?

C3 falsified the mechanism that was supposed to explain cbBTC: its cross rate
against WETH is not even especially quiet (vol_ratio 0.504, while BGCI 0.345,
LCAP 0.407, tBTC 0.472 and LINK 0.489 are all quieter and all lose money). So
whatever cbBTC has, it is not "slow cross rate", and the number itself now has to
be attacked directly before it is allowed to generate a third hypothesis.

Three independent ways for a fee number like this to be wrong:

  1. Share inflation. Fees are attributed as L/(L_active + L) per swap. If the
     active liquidity beside the position is tiny, the share is large and the
     position is not actually deployable at that size.
  2. Physical bound. A position cannot earn more than the pool itself collected.
     Comparing attributed fees against the pool's total fees over the same window
     bounds the error from above regardless of how the share was computed.
  3. Decimals. cbBTC carries 8 decimals against WETH's 18. A missing 1e10 would
     inflate or deflate every derived quantity, and it would do so silently.

The comparison group is the confirmation group of C3, which loses money, so any
mechanism found here has to separate the two.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]
SYMS = ("cbBTC", "tBTC", "cbXRP", "uXRP", "cbADA", "uADA", "cbLTC", "uSOL",
        "uDOGE", "uSUI", "LINK", "AAVE", "MORPHO", "ZEN", "BGCI", "CMC20",
        "LCAP")


def ch(q: str, fmt: str = "PrettyCompact") -> str:
    p = subprocess.run(CH + ["--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


syms = ",".join(f"'{s}'" for s in SYMS)

print("=" * 108)
print("1. Fee yield per unit of liquidity, measured on the pool alone -- no "
      "position, no share, no decimals")
print("=" * 108)
print("weth_volume x fee_tier / liquidity is what a unit of liquidity in range "
      "earns. It is\ndimensionless in the position size and cannot be inflated "
      "by anything this project computes.\n")
print(ch(f"""
WITH u AS (
  SELECT p.pool AS pool,
         t.symbol AS symbol,
         p.fee AS fee,
         p.venue AS venue
  FROM census.pools p
  INNER JOIN census.tokens t
    ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
  WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
    AND t.symbol IN ({syms})
),
s AS (
  SELECT s.pool AS pool,
         sum(abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
             / 1e18) AS weth_vol,
         sum(if(toFloat64(s.liquidity) > 0,
                abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1)))
                / toFloat64(s.liquidity), 0)) AS vol_over_L,
         count() AS swaps,
         quantile(0.5)(toFloat64(s.liquidity)) AS L_p50
  FROM census.cl_swaps s
  INNER JOIN census.pools pl ON pl.pool = s.pool
  WHERE s.pool IN (SELECT pool FROM u) AND s.sqrt_price_x96 > 0
  GROUP BY s.pool
)
SELECT u.symbol AS symbol,
       count() AS pools,
       sum(s.swaps) AS swaps,
       round(sum(s.weth_vol)) AS weth_volume_year,
       round(max(u.fee) / 100, 1) AS top_fee_bps,
       round(1e4 * max(s.vol_over_L * u.fee / 1e6), 1) AS best_annual_yield_bps_per_unit_L,
       round(1e4 * max(s.vol_over_L * u.fee / 1e6) / 52.0, 1) AS implied_weekly_bps
FROM s INNER JOIN u ON u.pool = s.pool
GROUP BY symbol
ORDER BY best_annual_yield_bps_per_unit_L DESC
"""))

print("=" * 108)
print("2. Physical bound: attributed fees against what the pool actually "
      "collected in the same window")
print("=" * 108)
print("A position's fees divided by the pool's total fees must be small and must "
      "not exceed the\nliquidity share. If cbBTC's row shows a share of the "
      "pool's fees that its liquidity share\ncannot support, the number is an "
      "artifact.\n")
print(ch(f"""
WITH pos AS (
  SELECT p.pos_id AS pos_id, p.pool AS pool, p.entry AS entry, p.T AS T,
         p.L AS L, p.fee AS fee, p.tick_lower AS tl, p.tick_upper AS tu
  FROM census.c3x_pos p WHERE p.T = 7
),
sw AS (
  SELECT s.pool AS pool, s.tick AS tick, toFloat64(s.liquidity) AS Lact,
         abs(toFloat64(if(pl.token0 = '{WETH}', s.amount0, s.amount1))) AS wamt,
         toDate(s.block_time) AS d
  FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
  WHERE s.pool IN (SELECT DISTINCT pool FROM pos) AND s.sqrt_price_x96 > 0
),
j AS (
  SELECT pos.pos_id AS pos_id, pos.pool AS pool,
         sum(if(sw.tick >= pos.tl AND sw.tick < pos.tu,
                sw.wamt * (pos.fee / 1e6) * pos.L / (sw.Lact + pos.L), 0))
           AS mine,
         sum(sw.wamt * (pos.fee / 1e6)) AS pool_total,
         quantile(0.5)(if(sw.tick >= pos.tl AND sw.tick < pos.tu,
                          pos.L / (sw.Lact + pos.L), NULL)) AS share_p50,
         max(if(sw.tick >= pos.tl AND sw.tick < pos.tu,
                pos.L / (sw.Lact + pos.L), 0)) AS share_max
  FROM pos INNER JOIN sw
    ON sw.pool = pos.pool AND sw.d >= pos.entry AND sw.d < pos.entry + 7
  GROUP BY pos_id, pool
)
SELECT t.symbol AS symbol,
       count() AS positions,
       round(quantile(0.5)(100.0 * j.mine / nullIf(j.pool_total, 0)), 4)
         AS pct_of_pool_fees_p50,
       round(max(100.0 * j.mine / nullIf(j.pool_total, 0)), 3)
         AS pct_of_pool_fees_max,
       round(quantile(0.5)(j.share_p50), 5) AS liq_share_p50,
       round(max(j.share_max), 4) AS liq_share_max,
       if(max(100.0 * j.mine / nullIf(j.pool_total, 0)) <= 100.0 * max(j.share_max) + 0.001,
          'within bound', 'IMPOSSIBLE') AS bound
FROM j
INNER JOIN census.pools p ON p.pool = j.pool
INNER JOIN census.tokens t
  ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
GROUP BY symbol
ORDER BY pct_of_pool_fees_p50 DESC
"""))

print("=" * 108)
print("3. Decimals: does the pool's raw price, corrected by decimals, agree "
      "with the known USD price?")
print("=" * 108)
print("cbBTC should print near 1 BTC in USD. If the decimals correction is "
      "wrong the number will\nbe off by a clean power of ten, which is the "
      "signature of this class of bug.\n")
print(ch(f"""
WITH last AS (
  SELECT s.pool AS pool,
         argMax(toFloat64(s.sqrt_price_x96), (s.block_number, s.log_index))
           AS spx,
         argMax(s.block_time, (s.block_number, s.log_index)) AS bt
  FROM census.cl_swaps s
  WHERE s.pool IN (
    SELECT p.pool FROM census.pools p
    INNER JOIN census.tokens t
      ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
    WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
      AND t.symbol IN ({syms}))
    AND s.sqrt_price_x96 > 0
  GROUP BY s.pool
)
SELECT t.symbol AS symbol, t.decimals AS dec,
       if(p.token1 = '{WETH}', 1, 0) AS weth_is_token1,
       count() AS pools,
       round(quantile(0.5)(
         multiIf(p.token1 = '{WETH}',
                 pow(last.spx / pow(2, 96), 2) / pow(10, 18 - t.decimals),
                 1.0 / (pow(last.spx / pow(2, 96), 2)
                        * pow(10, 18 - t.decimals)))), 8) AS alt_in_weth,
       round(quantile(0.5)(
         multiIf(p.token1 = '{WETH}',
                 pow(last.spx / pow(2, 96), 2) / pow(10, 18 - t.decimals),
                 1.0 / (pow(last.spx / pow(2, 96), 2)
                        * pow(10, 18 - t.decimals)))) * 2700.0, 2) AS implied_usd
FROM last
INNER JOIN census.pools p ON p.pool = last.pool
INNER JOIN census.tokens t
  ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
GROUP BY symbol, dec, weth_is_token1
ORDER BY symbol
"""))
