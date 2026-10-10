#!/usr/bin/env python3
"""Does shrinking the trade to $50 actually lower the toll relative to the move?

The A2 re-run at $50 came back with a hurdle of about 102 bps -- the same as the
$5,000 run's 94 bps -- even though the median cost across the whole pool census
fell 41x. That is not a bug and it is the most important thing measured today, so
it gets tested directly.

The mechanism to test: at $50 slippage is negligible, so cost IS the fee tier. Fee
tiers are not assigned at random -- whoever deploys a pool picks 1bp for a stable
pair and 100bp for a volatile one, because the tier has to pay liquidity providers
for the variance they absorb. If that matching is tight, then

    hurdle / sigma  ~=  2 * fee_tier / sigma  ~=  constant

across the universe, and no reduction in trade size can improve it, because the
quantity that has to be beaten is a rate, not an impact. Round 38 found this
anti-correlation and attributed it to depth. If it survives at $50, the cause is
the fee tier, which is a far more fundamental barrier: it cannot be escaped by
trading smaller, only by not paying it (i.e. by providing liquidity instead of
consuming it, or by holding long enough that sigma grows past it).

Outputs, for the $50 universe:
  1. cost at $50 vs hourly sigma, by fee tier
  2. hurdle/sigma by fee tier and by holding period -- is it flat?
  3. the same at $5,000, to show what the size change did and did not move
  4. where the 223,044 neutralised bars live
"""
from __future__ import annotations

import subprocess
import sys

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]


def ch(q: str, fmt: str = "PrettyCompact") -> str:
    p = subprocess.run(CH + ["--format", fmt, "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


print("=" * 118)
print("1. the fee tier IS the cost at $50 -- and the tier tracks the volatility "
      "it was chosen for")
print("=" * 118)
print(ch("""
WITH v AS (
  SELECT pool, stddevSamp(log(close / open)) * 1e4 AS sigma_1h_bps, count() AS bars
  FROM edge.bars_1h_year_50
  WHERE open > 0 AND close > 0 AND abs(log(close / open)) < 2.3
  GROUP BY pool HAVING bars >= 2000
),
c50 AS (SELECT pool, quantile(0.5)(cost_bps_med) AS c50
        FROM census.pit_cost_50 WHERE feasible = 1 GROUP BY pool),
c5k AS (SELECT pool, quantile(0.5)(cost_bps_med) AS c5k
        FROM census.pit_cost WHERE feasible = 1 GROUP BY pool)
SELECT p.fee / 100 AS fee_tier_bps,
       count() AS pools,
       round(quantile(0.5)(c50.c50), 1) AS cost_50,
       round(quantile(0.5)(c5k.c5k), 1) AS cost_5000,
       round(quantile(0.5)(v.sigma_1h_bps), 1) AS sigma_1h_bps,
       round(quantile(0.5)(2 * c50.c50 + 14.4) , 1) AS hurdle_50,
       round(quantile(0.5)((2 * c50.c50 + 14.4) / v.sigma_1h_bps), 2) AS hurdle50_over_sigma1h,
       round(quantile(0.5)((2 * c5k.c5k + 0.14) / v.sigma_1h_bps), 2) AS hurdle5k_over_sigma1h
FROM v
INNER JOIN census.pools p ON p.pool = v.pool
INNER JOIN c50 ON c50.pool = v.pool
LEFT JOIN c5k ON c5k.pool = v.pool
GROUP BY fee_tier_bps HAVING pools >= 3 ORDER BY fee_tier_bps
"""))

print("=" * 118)
print("2. sigma grows with the square root of time, the toll does not -- so the "
      "only free variable left is holding period")
print("=" * 118)
print(ch("""
WITH v AS (
  SELECT pool, stddevSamp(log(close / open)) * 1e4 AS s1h, count() AS bars
  FROM edge.bars_1h_year_50
  WHERE open > 0 AND close > 0 AND abs(log(close / open)) < 2.3
  GROUP BY pool HAVING bars >= 2000
),
c50 AS (SELECT pool, quantile(0.5)(cost_bps_med) AS c FROM census.pit_cost_50
        WHERE feasible = 1 GROUP BY pool)
SELECT h AS hold_hours,
       round(quantile(0.5)(2 * c50.c + 14.4), 1) AS hurdle_50_bps,
       round(quantile(0.5)(v.s1h * sqrt(h)), 1) AS sigma_h_bps,
       round(quantile(0.5)((2 * c50.c + 14.4) / (v.s1h * sqrt(h))), 3) AS ratio,
       round(quantile(0.1)((2 * c50.c + 14.4) / (v.s1h * sqrt(h))), 3) AS ratio_best_decile
FROM v INNER JOIN c50 ON c50.pool = v.pool
ARRAY JOIN [4, 24, 72, 168, 720] AS h
GROUP BY h ORDER BY h
"""))

print("=" * 118)
print("3. the cheapest pools at $50 -- what does the toll buy access to?")
print("=" * 118)
print(ch("""
WITH v AS (
  SELECT pool, stddevSamp(log(close / open)) * 1e4 AS s1h, count() AS bars
  FROM edge.bars_1h_year_50
  WHERE open > 0 AND close > 0 AND abs(log(close / open)) < 2.3
  GROUP BY pool HAVING bars >= 4000
),
c50 AS (SELECT pool, quantile(0.5)(cost_bps_med) AS c, count() AS days
        FROM census.pit_cost_50 WHERE feasible = 1 GROUP BY pool HAVING days >= 180)
SELECT u.symbol, u.venue, p.fee / 100 AS tier_bps,
       round(c50.c, 2) AS cost_50_bps, round(v.s1h, 1) AS sigma_1h_bps,
       round((2 * c50.c + 14.4) / v.s1h, 2) AS hurdle_over_sigma_1h,
       round((2 * c50.c + 14.4) / (v.s1h * sqrt(168)), 3) AS hurdle_over_sigma_1w,
       c50.days
FROM v
INNER JOIN c50 ON c50.pool = v.pool
INNER JOIN edge.bars_universe_50 u ON u.pool = v.pool
INNER JOIN census.pools p ON p.pool = v.pool
ORDER BY hurdle_over_sigma_1h ASC LIMIT 30
"""))

print("=" * 118)
print("4. where the neutralised bars live -- is the expanded universe real data?")
print("=" * 118)
print(ch("""
SELECT u.symbol, count() AS glitch_bars,
       round(100.0 * count() / any(t.total), 1) AS pct_of_symbol_bars
FROM edge.bars_1h_year_50 b
INNER JOIN edge.bars_universe_50 u ON u.pool = b.pool
INNER JOIN (SELECT pool, count() AS total FROM edge.bars_1h_year_50 GROUP BY pool) t
       ON t.pool = b.pool
WHERE b.open > 0 AND b.close > 0 AND abs(log(b.close / b.open)) >= 2.3
GROUP BY u.symbol ORDER BY glitch_bars DESC LIMIT 15
"""))
print(ch("""
SELECT countIf(abs(log(close / open)) >= 2.3) AS glitch,
       count() AS total,
       round(100.0 * countIf(abs(log(close / open)) >= 2.3) / count(), 2) AS pct,
       uniqExactIf(pool, abs(log(close / open)) >= 2.3) AS pools_affected,
       uniqExact(pool) AS pools_total
FROM edge.bars_1h_year_50 WHERE open > 0 AND close > 0
"""))
