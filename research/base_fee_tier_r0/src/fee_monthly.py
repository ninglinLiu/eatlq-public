#!/usr/bin/env python3
"""Build a point-in-time monthly fee table for the multi-tier pool universe.

census.pools.fee is one reading taken when a pool was resolved, and the fee is not
a constant. Measuring it in two windows six months apart already showed pools
moving a long way: Mog 170 -> 417 bps, one USDC pool 100 -> 5.5, cbXRP 100 -> 70,
and the cbBTC pool that produced this project's only positive market-making result
sitting at 2.6-4.4 against a recorded 25.

A single corrected number would repeat the original mistake at a different value.
What both the cost model and the revenue model actually need is a fee series, so
this measures every month separately.

The fee comes out of the pool's own arithmetic. Within one tick L is constant, so
for token0 in

    1/sqrt(p_after) - 1/sqrt(p_before) = amount0_gross * (1 - f) / L

and f is the only unknown. The reciprocal difference is taken in exact Int256
before any float appears, because sqrt_price_x96 sits near 1e23 and consecutive
values differ in their last digits.

Two guards, both learned the hard way this round:

  - Amount floors. On dust swaps f snaps to simple fractions of small integers,
    which is where the identical 1000.0 and 416.7 bps readings across twenty
    unrelated meme pools came from. Those are quantisation, not fees.
  - Interquartile spread is stored alongside the median, so a pool whose fee
    changed mid-month is visible rather than averaged into a fiction.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
Q96 = "79228162514264337593543950336."
MONTHS = ["2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01",
          "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
          "2026-08"]
POOL_CHUNKS = 2
MIN_OBS = 60
MIN_RAW = int(1e6)          # amount floor, both legs, kills the dust quantisation
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=11000000000"]


def ch(q: str, fmt: str = "TSV") -> str:
    p = subprocess.run(CH + (["--format", fmt] if fmt != "TSV" else []) +
                       ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write((p.stderr or "no stderr, likely killed")[:2500] + "\n")
        return ""
    return p.stdout


ch("DROP TABLE IF EXISTS census.pool_fee_monthly")
ch("""CREATE TABLE census.pool_fee_monthly (
  pool String, month Date, venue LowCardinality(String),
  fee_snapshot Int64, implied_bps Float64,
  implied_p25 Float64, implied_p75 Float64, n_obs UInt64
) ENGINE = MergeTree ORDER BY (pool, month)""")

UNIV = f"""
u AS (
  SELECT p.pool AS pool,
         if(p.token0 = '{WETH}', p.token1, p.token0) AS alt
  FROM census.pools p
  WHERE p.pool IN (SELECT DISTINCT pool FROM census.pit_cost_50)
    AND (p.token0 = '{WETH}' OR p.token1 = '{WETH}') AND p.fee > 0
),
mt AS (SELECT alt FROM u GROUP BY alt HAVING count() >= 2),
univ AS (SELECT u.pool AS pool FROM u INNER JOIN mt ON mt.alt = u.alt)
"""

ok = fail = 0
for mo in MONTHS:
    for k in range(POOL_CHUNKS):
        sys.stderr.write(f"[fee] {mo} chunk {k + 1}/{POOL_CHUNKS}\n")
        r = ch(f"""
        INSERT INTO census.pool_fee_monthly
        WITH {UNIV},
        step AS (
          SELECT pool, amount0, amount1, liquidity,
                 sqrt_price_x96 AS s, prev_spx AS ps
          FROM (
            SELECT pool, amount0, amount1, liquidity, tick, sqrt_price_x96,
                   any(sqrt_price_x96) OVER w AS prev_spx,
                   any(tick) OVER w AS prev_tick
            FROM census.cl_swaps
            WHERE pool IN (SELECT pool FROM univ)
              AND cityHash64(pool) % {POOL_CHUNKS} = {k}
              AND toYYYYMM(block_time) = {mo.replace('-', '')}
              AND sqrt_price_x96 > 0 AND liquidity > 0
            WINDOW w AS (PARTITION BY pool ORDER BY block_number, log_index
                         ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING)
          )
          WHERE prev_spx > 0 AND prev_tick = tick AND prev_spx != sqrt_price_x96
            AND abs(amount0) >= {MIN_RAW} AND abs(amount1) >= {MIN_RAW}
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
        SELECT f.pool AS pool, toDate('{mo}-01') AS month,
               any(p.venue) AS venue, any(p.fee) AS fee_snapshot,
               1e4 * quantile(0.5)(f.fee_frac) AS implied_bps,
               1e4 * quantile(0.25)(f.fee_frac) AS implied_p25,
               1e4 * quantile(0.75)(f.fee_frac) AS implied_p75,
               count() AS n_obs
        FROM f INNER JOIN census.pools p ON p.pool = f.pool
        WHERE f.fee_frac > -0.001 AND f.fee_frac < 0.15
        GROUP BY pool HAVING n_obs >= {MIN_OBS}
        """)
        if r == "" and "INSERT" not in r:
            pass
        n = ch("SELECT count() FROM census.pool_fee_monthly").strip()
        sys.stderr.write(f"    rows now {n}\n")

print("=" * 104)
print("census.pool_fee_monthly -- charged fee by month, multi-tier WETH pools")
print("=" * 104)
print(ch("""
SELECT toString(month) AS month, count() AS pools,
       countIf(abs(implied_bps - fee_snapshot / 100.0) <= 0.05) AS agree,
       countIf(implied_bps < 0.9 * fee_snapshot / 100.0) AS snapshot_too_high,
       countIf(implied_bps > 1.1 * fee_snapshot / 100.0) AS snapshot_too_low,
       round(quantile(0.5)(implied_bps / nullIf(fee_snapshot / 100.0, 0)), 4)
         AS median_ratio
FROM census.pool_fee_monthly GROUP BY month ORDER BY month
""", fmt="PrettyCompact"))

print("Pools whose fee actually changed during the year, ranked by how much\n")
print(ch("""
SELECT any(t.symbol) AS symbol, any(m.venue) AS venue,
       round(any(m.fee_snapshot) / 100.0, 2) AS snapshot_bps,
       round(min(m.implied_bps), 2) AS min_bps,
       round(max(m.implied_bps), 2) AS max_bps,
       round(max(m.implied_bps) - min(m.implied_bps), 2) AS range_bps,
       count() AS months, sum(m.n_obs) AS n_obs
FROM census.pool_fee_monthly m
INNER JOIN census.pools p ON p.pool = m.pool
LEFT JOIN census.tokens t
  ON t.token = if(p.token0 = '0x4200000000000000000000000000000000000006',
                  p.token1, p.token0)
GROUP BY m.pool
HAVING months >= 6 AND range_bps > 1.0
ORDER BY range_bps DESC LIMIT 30
""", fmt="PrettyCompact"))

print("How often is the snapshot wrong, weighted by months of coverage\n")
print(ch("""
SELECT round(100.0 * countIf(abs(implied_bps - fee_snapshot / 100.0) <= 0.05)
             / count(), 2) AS pct_pool_months_exact,
       round(100.0 * countIf(implied_bps < 0.9 * fee_snapshot / 100.0)
             / count(), 2) AS pct_snapshot_too_high,
       round(100.0 * countIf(implied_bps > 1.1 * fee_snapshot / 100.0)
             / count(), 2) AS pct_snapshot_too_low,
       round(avg(implied_bps - fee_snapshot / 100.0), 3) AS mean_error_bps,
       count() AS pool_months
FROM census.pool_fee_monthly
""", fmt="PrettyCompact"))
