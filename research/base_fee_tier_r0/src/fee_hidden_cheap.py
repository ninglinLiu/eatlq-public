#!/usr/bin/env python3
"""Are there cheap pools that a stale fee snapshot hid from the universe?

The strategy pools came back clean: 221 of 244 agree exactly, 11 understate the
real fee, and exactly one overstates it -- the cbBTC pool that produced the only
positive market-making result this project has ever seen. So the cost floor that
killed the directional strategies is not made of stale snapshots, and those
falsifications stand.

But that sample is selected. census.pit_cost_50 only admits pools whose modelled
cost is at or below 100 bps, and the fee tier goes straight into that cost. A pool
recorded at 100 bps that really charges 5 would be thrown out before it could ever
be measured, so a bug in exactly that direction is invisible by construction.

This looks where it would hide: WETH-legged pools with a high recorded fee and real
trading activity, whose charged fee is far below what the snapshot claims. If some
exist and they trade, the tradeable universe is bigger and cheaper than every round
so far has assumed, and that is worth more than any single strategy result.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
Q96 = "79228162514264337593543950336."
D0, D1 = "2026-03-01", "2026-03-31"
MIN_SWAPS = 2000
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=5400", "--max_memory_usage=10000000000"]


def ch(q: str, fmt: str = "PrettyCompact") -> str:
    p = subprocess.run(CH + ["--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


CAND = f"""
cand AS (
  SELECT s.pool AS pool
  FROM census.cl_swaps s
  INNER JOIN census.pools p ON p.pool = s.pool
  WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
    AND p.fee >= 2000
    AND s.block_time >= toDateTime('{D0} 00:00:00')
    AND s.block_time <  toDateTime('{D1} 00:00:00')
    AND s.sqrt_price_x96 > 0 AND s.liquidity > 0
  GROUP BY s.pool HAVING count() >= {MIN_SWAPS}
)
"""

BODY = f"""
WITH {CAND},
step AS (
  SELECT pool, amount0, amount1, liquidity, sqrt_price_x96 AS s, prev_spx AS ps
  FROM (
    SELECT pool, amount0, amount1, liquidity, tick, sqrt_price_x96,
           any(sqrt_price_x96) OVER w AS prev_spx,
           any(tick) OVER w AS prev_tick
    FROM census.cl_swaps
    WHERE pool IN (SELECT pool FROM cand)
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
  GROUP BY pool HAVING n_obs >= 200
)
"""

print("=" * 112)
print(f"High-fee WETH pools with real activity, recorded fee vs charged fee "
      f"({D0}..{D1}, >={MIN_SWAPS} swaps)")
print("=" * 112)
print(ch(BODY + """
SELECT count() AS pools_measured,
       countIf(m.implied_bps < 0.5 * p.fee / 100.0) AS charging_under_half,
       countIf(m.implied_bps < 0.25 * p.fee / 100.0) AS charging_under_quarter,
       countIf(m.implied_bps <= 10.0 AND p.fee / 100.0 >= 25.0)
         AS looks_expensive_is_cheap,
       round(quantile(0.5)(m.implied_bps / nullIf(p.fee / 100.0, 0)), 4)
         AS median_ratio
FROM m INNER JOIN census.pools p ON p.pool = m.pool
"""))

print("Every pool recorded at 25 bps or worse that actually charges 10 bps or "
      "less, and whether the universe already has it\n")
print(ch(BODY + """
SELECT t.symbol AS symbol, p.venue AS venue, m.pool AS pool,
       round(p.fee / 100.0, 2) AS snapshot_bps,
       round(m.implied_bps, 2) AS actual_bps,
       m.n_obs AS n_obs,
       if(m.pool IN (SELECT DISTINCT pool FROM census.pit_cost_50),
          'in universe', 'EXCLUDED') AS status,
       if(m.pool IN (SELECT pool FROM edge.bars_universe_50),
          'traded', '-') AS in_strategy_panel
FROM m
INNER JOIN census.pools p ON p.pool = m.pool
LEFT JOIN census.tokens t
  ON t.token = if(p.token0 = '0x4200000000000000000000000000000000000006',
                  p.token1, p.token0)
WHERE m.implied_bps <= 10.0 AND p.fee / 100.0 >= 25.0
ORDER BY m.n_obs DESC LIMIT 40
"""))
