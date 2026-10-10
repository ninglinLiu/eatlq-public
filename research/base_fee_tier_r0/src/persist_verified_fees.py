#!/usr/bin/env python3
"""Persist the charged fee for every pool that matters, so this cannot recur.

census.pools.fee is a snapshot taken when a pool was resolved, and for Aerodrome
Slipstream pools it is not a constant -- a fee manager can change it. One pool,
0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1, was recorded at 25 bps and charges
2.6, and because market-making selects the pool with the highest fee times volume
over liquidity, that single stale value is what the selection rule went looking
for. It produced the only positive market-making result this project has ever had,
and it was worth -2 bps a week once corrected.

The lesson generalises past this one field. Any quantity that enters a selection
rule as apparent revenue will be sought out by that rule wherever it is
overstated, so an error there is not diluted by averaging -- it is amplified. Such
quantities have to be measured, not looked up.

This writes the measured fee into census.pool_fee_verified for both pool sets that
have been probed: the 411 pools the directional strategies trade, and the
high-recorded-fee pools where an overstatement could hide. Anything reading a fee
as revenue should join here and fall back to the snapshot only when a pool has no
measurement, and should say so when it does.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
Q96 = "79228162514264337593543950336."
WINDOWS = [("2025-10-01", "2025-10-31"), ("2026-03-01", "2026-03-31")]
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=5400", "--max_memory_usage=10000000000"]


def ch(q: str, fmt: str = "TSV") -> str:
    p = subprocess.run(CH + (["--format", fmt] if fmt != "TSV" else []) +
                       ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


ch("DROP TABLE IF EXISTS census.pool_fee_verified")
ch("""CREATE TABLE census.pool_fee_verified (
  pool String, window_start Date, venue LowCardinality(String),
  fee_snapshot Int64, implied_bps Float64, n_obs UInt64
) ENGINE = MergeTree ORDER BY (pool, window_start)""")

for d0, d1 in WINDOWS:
    sys.stderr.write(f"[verify] {d0}..{d1}\n")
    ch(f"""
    INSERT INTO census.pool_fee_verified
    WITH cand AS (
      SELECT s.pool AS pool
      FROM census.cl_swaps s INNER JOIN census.pools p ON p.pool = s.pool
      WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
        AND (p.pool IN (SELECT pool FROM edge.bars_universe_50)
             OR p.fee >= 2000)
        AND s.block_time >= toDateTime('{d0} 00:00:00')
        AND s.block_time <  toDateTime('{d1} 00:00:00')
        AND s.sqrt_price_x96 > 0 AND s.liquidity > 0
      GROUP BY s.pool HAVING count() >= 500
    ),
    step AS (
      SELECT pool, amount0, amount1, liquidity,
             sqrt_price_x96 AS s, prev_spx AS ps
      FROM (
        SELECT pool, amount0, amount1, liquidity, tick, sqrt_price_x96,
               any(sqrt_price_x96) OVER w AS prev_spx,
               any(tick) OVER w AS prev_tick
        FROM census.cl_swaps
        WHERE pool IN (SELECT pool FROM cand)
          AND block_time >= toDateTime('{d0} 00:00:00')
          AND block_time <  toDateTime('{d1} 00:00:00')
          AND sqrt_price_x96 > 0 AND liquidity > 0
        WINDOW w AS (PARTITION BY pool ORDER BY block_number, log_index
                     ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING)
      )
      WHERE prev_spx > 0 AND prev_tick = tick
        AND ((amount0 > 0 AND amount1 < 0) OR (amount1 > 0 AND amount0 < 0))
    ),
    f AS (
      SELECT pool,
             if(amount0 > 0,
                1.0 - (toFloat64(liquidity) * {Q96}
                       * toFloat64(toInt256(ps) - toInt256(s))
                       / (toFloat64(s) * toFloat64(ps))) / toFloat64(amount0),
                1.0 - (toFloat64(liquidity)
                       * toFloat64(toInt256(s) - toInt256(ps)) / {Q96})
                      / toFloat64(amount1)) AS fee_frac
      FROM step
    )
    SELECT f.pool AS pool, toDate('{d0}') AS window_start,
           any(p.venue) AS venue, any(p.fee) AS fee_snapshot,
           1e4 * quantile(0.5)(f.fee_frac) AS implied_bps, count() AS n_obs
    FROM f INNER JOIN census.pools p ON p.pool = f.pool
    WHERE f.fee_frac > -0.001 AND f.fee_frac < 0.2
    GROUP BY pool HAVING n_obs >= 100
    """)

print("=" * 104)
print("census.pool_fee_verified -- charged fee, measured in two windows six "
      "months apart")
print("=" * 104)
print(ch("""
SELECT toString(window_start) AS window, count() AS pools,
       countIf(abs(implied_bps - fee_snapshot / 100.0) <= 0.05) AS agree_exactly,
       countIf(implied_bps < 0.9 * fee_snapshot / 100.0) AS snapshot_too_high,
       countIf(implied_bps > 1.1 * fee_snapshot / 100.0) AS snapshot_too_low,
       round(quantile(0.5)(implied_bps / nullIf(fee_snapshot / 100.0, 0)), 4)
         AS median_ratio
FROM census.pool_fee_verified GROUP BY window ORDER BY window
""", fmt="PrettyCompact"))

print("Pools whose recorded fee overstates reality in EITHER window -- the "
       "revenue-side blacklist\n")
print(ch("""
SELECT v.pool AS pool, any(v.venue) AS venue, any(t.symbol) AS symbol,
       round(any(v.fee_snapshot) / 100.0, 2) AS snapshot_bps,
       round(min(v.implied_bps), 2) AS actual_bps_min,
       round(max(v.implied_bps), 2) AS actual_bps_max,
       groupArray(toString(v.window_start)) AS windows,
       sum(v.n_obs) AS n_obs
FROM census.pool_fee_verified v
INNER JOIN census.pools p ON p.pool = v.pool
LEFT JOIN census.tokens t
  ON t.token = if(p.token0 = '0x4200000000000000000000000000000000000006',
                  p.token1, p.token0)
GROUP BY pool
HAVING max(v.implied_bps) < 0.9 * any(v.fee_snapshot) / 100.0
ORDER BY n_obs DESC
""", fmt="PrettyCompact"))

print("Is the fee stable over six months? Pools whose charged fee moved most\n")
print(ch("""
SELECT any(t.symbol) AS symbol, any(v.venue) AS venue,
       round(any(v.fee_snapshot) / 100.0, 2) AS snapshot_bps,
       round(minIf(v.implied_bps, v.window_start = '2025-10-01'), 2) AS oct_bps,
       round(minIf(v.implied_bps, v.window_start = '2026-03-01'), 2) AS mar_bps,
       round(abs(minIf(v.implied_bps, v.window_start = '2026-03-01')
                 - minIf(v.implied_bps, v.window_start = '2025-10-01')), 2)
         AS moved_bps
FROM census.pool_fee_verified v
INNER JOIN census.pools p ON p.pool = v.pool
LEFT JOIN census.tokens t
  ON t.token = if(p.token0 = '0x4200000000000000000000000000000000000006',
                  p.token1, p.token0)
GROUP BY v.pool
HAVING count() = 2 AND moved_bps > 0.5
ORDER BY moved_bps DESC LIMIT 25
""", fmt="PrettyCompact"))
