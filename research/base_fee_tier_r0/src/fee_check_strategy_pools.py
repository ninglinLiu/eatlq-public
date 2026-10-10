#!/usr/bin/env python3
"""Were the directional falsifications run against a hurdle that exists?

Every directional strategy in this project trades the pools in
edge.bars_universe_50, and its hurdle is twice the point-in-time cost plus gas.
Point-in-time cost is closed-form slippage plus the fee tier, and the fee tier
comes from census.pools.fee -- the same snapshot that turned out to be 6.6x wrong
for the one pool carrying C3's result.

For revenue a stale-high fee invents money. For cost it invents a hurdle, which is
the direction that matters here: A2, A3 and A5 were all rejected against a cost
floor, and any part of that floor made of stale snapshots was never real.

Method as in fee_snapshot_audit.py, but restricted to a 30-day slice so the window
function sees every swap in sequence rather than a sample. Adjacency is then exact
except at the two slice boundaries, which is a few pairs out of millions.
"""
from __future__ import annotations

import subprocess
import sys

Q96 = "79228162514264337593543950336."
D0, D1 = "2026-03-01", "2026-03-31"
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=10000000000"]


def ch(q: str, fmt: str = "PrettyCompact") -> str:
    p = subprocess.run(CH + ["--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


BODY = f"""
WITH step AS (
  SELECT pool, amount0, amount1, liquidity, sqrt_price_x96 AS s, prev_spx AS ps
  FROM (
    SELECT pool, amount0, amount1, liquidity, tick, sqrt_price_x96,
           any(sqrt_price_x96) OVER w AS prev_spx,
           any(tick) OVER w AS prev_tick
    FROM census.cl_swaps
    WHERE pool IN (SELECT pool FROM edge.bars_universe_50)
      AND block_time >= toDateTime('{D0} 00:00:00')
      AND block_time <  toDateTime('{D1} 00:00:00')
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
),
m AS (
  SELECT pool, 1e4 * quantile(0.5)(fee_frac) AS implied_bps, count() AS n_obs
  FROM f WHERE fee_frac > -0.001 AND fee_frac < 0.2
  GROUP BY pool HAVING n_obs >= 50
)
"""

print("=" * 108)
print(f"Fee snapshot vs charged fee, the 411 pools the directional strategies "
      f"trade ({D0}..{D1})")
print("=" * 108)
print(ch(BODY + """
SELECT count() AS pools_measured,
       countIf(abs(m.implied_bps - u.fee / 100.0) <= 0.05) AS exact_agreement,
       countIf(m.implied_bps < 0.9 * u.fee / 100.0) AS snapshot_too_HIGH,
       countIf(m.implied_bps > 1.1 * u.fee / 100.0) AS snapshot_too_LOW,
       round(quantile(0.5)(u.fee / 100.0 - m.implied_bps), 3)
         AS median_overstatement_bps,
       round(avg(u.fee / 100.0 - m.implied_bps), 3) AS mean_overstatement_bps,
       round(sum(u.fee / 100.0 - m.implied_bps), 1) AS total_overstatement_bps
FROM m INNER JOIN edge.bars_universe_50 u ON u.pool = m.pool
"""))

print("Pools where the recorded fee overstates the real one by more than 10% -- "
      "these had an inflated hurdle\n")
print(ch(BODY + """
SELECT u.symbol AS symbol, u.venue AS venue,
       round(u.fee / 100.0, 2) AS snapshot_bps,
       round(m.implied_bps, 2) AS actual_bps,
       round(u.fee / 100.0 - m.implied_bps, 2) AS overstated_by_bps,
       round(u.cost_p50, 1) AS cost_p50_used,
       round(u.cost_p50 - (u.fee / 100.0 - m.implied_bps), 1) AS cost_p50_corrected,
       u.tradeable_days AS days, m.n_obs AS n_obs
FROM m INNER JOIN edge.bars_universe_50 u ON u.pool = m.pool
WHERE m.implied_bps < 0.9 * u.fee / 100.0
ORDER BY overstated_by_bps DESC LIMIT 30
"""))

print("Does correcting the fee move the cost floor? Distribution of "
      "point-in-time cost, as used vs corrected\n")
print(ch(BODY + """
SELECT round(quantile(0.10)(u.cost_p50), 1) AS cost_q10,
       round(quantile(0.10)(u.cost_p50 - (u.fee / 100.0 - m.implied_bps)), 1)
         AS cost_q10_corrected,
       round(quantile(0.50)(u.cost_p50), 1) AS cost_p50,
       round(quantile(0.50)(u.cost_p50 - (u.fee / 100.0 - m.implied_bps)), 1)
         AS cost_p50_corrected,
       round(quantile(0.90)(u.cost_p50), 1) AS cost_q90,
       round(quantile(0.90)(u.cost_p50 - (u.fee / 100.0 - m.implied_bps)), 1)
         AS cost_q90_corrected,
       countIf(u.cost_p50 <= 30) AS n_under_30bps,
       countIf(u.cost_p50 - (u.fee / 100.0 - m.implied_bps) <= 30)
         AS n_under_30bps_corrected
FROM m INNER JOIN edge.bars_universe_50 u ON u.pool = m.pool
"""))
