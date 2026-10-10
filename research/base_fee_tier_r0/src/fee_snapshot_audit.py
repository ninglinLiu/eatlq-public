#!/usr/bin/env python3
"""Solve the fee every pool in the tradeable universe actually charged.

C3 died on a stale fee snapshot. One Aerodrome pool was recorded at 25 bps and had
actually been charging 3.80 bps, and because the tier-selection rule ranks pools by
fee times volume over liquidity, that one error is exactly what pulled 42 of 47
positions into that pool. Correcting it turned +982 bps a week into -2.

That number came from census.pools.fee, read once when each pool was resolved, and
the same field feeds two different places with opposite consequences:

  - as revenue, in every market-making round, where an overstated fee invents money
  - as cost, in census.pit_cost, where an overstated fee invents a hurdle

The second is the one that matters now. The cost floor is what has killed every
directional strategy in this project, and if that floor is partly made of stale fee
snapshots then some of those falsifications were run against a hurdle that does not
exist.

The pool reveals its own fee through price impact. For a swap putting token0 in,

    1/sqrt(p_after) - 1/sqrt(p_before) = amount0_net / L,  amount0_net = gross(1-f)

and symmetrically sqrt(p_after) - sqrt(p_before) = amount1_net / L for token1 in.
Restricting to consecutive swaps in the same pool that leave the tick unchanged
keeps L constant across the step, so f is the only unknown. The reciprocal
difference is formed in exact Int256 arithmetic before touching floating point,
because sqrt_price_x96 values sit around 1e23 and differ in their last few digits.

Validation is built in: 21 of the 22 wrapped-major pools probed by hand returned
their known fee to three decimals, so a pool that disagrees is a pool whose
snapshot is wrong, not an artifact of the estimator.
"""
from __future__ import annotations

import subprocess
import sys

Q96 = "79228162514264337593543950336."
CHUNKS = 16
MIN_OBS = 50
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=14000000000"]


def ch(q: str, fmt: str = "TSV") -> str:
    p = subprocess.run(CH + (["--format", fmt] if fmt != "TSV" else []) +
                       ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


ch("""CREATE TABLE IF NOT EXISTS census.pool_fee_implied (
  pool String, venue LowCardinality(String), fee_snapshot Int64,
  implied_bps Float64, implied_p25 Float64, implied_p75 Float64,
  n_obs UInt64, measured_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree ORDER BY pool""")
ch("TRUNCATE TABLE census.pool_fee_implied")

for k in range(CHUNKS):
    sys.stderr.write(f"[fee] chunk {k + 1}/{CHUNKS}\n")
    ch(f"""
    INSERT INTO census.pool_fee_implied
      (pool, venue, fee_snapshot, implied_bps, implied_p25, implied_p75, n_obs)
    WITH step AS (
      SELECT pool, amount0, amount1, liquidity,
             sqrt_price_x96 AS s, prev_spx AS ps
      FROM (
        SELECT pool, amount0, amount1, liquidity, tick, sqrt_price_x96,
               any(sqrt_price_x96) OVER w AS prev_spx,
               any(tick) OVER w AS prev_tick
        FROM census.cl_swaps
        WHERE cityHash64(pool) % {CHUNKS} = {k}
          AND pool IN (SELECT DISTINCT pool FROM census.pit_cost_50)
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
    SELECT f.pool AS pool,
           any(p.venue) AS venue,
           any(p.fee) AS fee_snapshot,
           1e4 * quantile(0.5)(f.fee_frac) AS implied_bps,
           1e4 * quantile(0.25)(f.fee_frac) AS implied_p25,
           1e4 * quantile(0.75)(f.fee_frac) AS implied_p75,
           count() AS n_obs
    FROM f INNER JOIN census.pools p ON p.pool = f.pool
    WHERE f.fee_frac > -0.001 AND f.fee_frac < 0.2
    GROUP BY pool
    HAVING n_obs >= {MIN_OBS}
    """)

print("=" * 112)
print("Fee snapshot audit over the tradeable universe "
      "(census.pit_cost_50, 3,029 pools)")
print("=" * 112)
print(ch("""
SELECT count() AS pools_measured,
       sum(n_obs) AS observations,
       countIf(abs(implied_bps - fee_snapshot / 100.0)
               <= 0.02 * fee_snapshot / 100.0 + 0.05) AS agree_within_2pct,
       countIf(implied_bps < 0.9 * fee_snapshot / 100.0) AS snapshot_too_HIGH,
       countIf(implied_bps > 1.1 * fee_snapshot / 100.0) AS snapshot_too_LOW,
       round(100.0 * countIf(implied_bps < 0.9 * fee_snapshot / 100.0)
             / count(), 2) AS pct_too_high,
       round(100.0 * countIf(implied_bps > 1.1 * fee_snapshot / 100.0)
             / count(), 2) AS pct_too_low
FROM census.pool_fee_implied
""", fmt="PrettyCompact"))

print("By venue -- which venues have drifting fees\n")
print(ch("""
SELECT venue, count() AS pools,
       round(100.0 * countIf(implied_bps < 0.9 * fee_snapshot / 100.0)
             / count(), 1) AS pct_snapshot_too_high,
       round(100.0 * countIf(implied_bps > 1.1 * fee_snapshot / 100.0)
             / count(), 1) AS pct_snapshot_too_low,
       round(quantile(0.5)(implied_bps / nullIf(fee_snapshot / 100.0, 0)), 4)
         AS median_ratio,
       round(quantile(0.05)(implied_bps / nullIf(fee_snapshot / 100.0, 0)), 3)
         AS p05_ratio,
       round(quantile(0.95)(implied_bps / nullIf(fee_snapshot / 100.0, 0)), 3)
         AS p95_ratio
FROM census.pool_fee_implied
GROUP BY venue ORDER BY pools DESC
""", fmt="PrettyCompact"))

print("The 25 pools whose recorded fee most overstates what they charge, "
      "weighted by how much they trade\n")
print(ch("""
SELECT i.pool AS pool, i.venue AS venue,
       t.symbol AS symbol,
       round(i.fee_snapshot / 100.0, 2) AS snapshot_bps,
       round(i.implied_bps, 2) AS actual_bps,
       round(i.implied_bps / nullIf(i.fee_snapshot / 100.0, 0), 3) AS ratio,
       i.n_obs AS n_obs
FROM census.pool_fee_implied i
INNER JOIN census.pools p ON p.pool = i.pool
LEFT JOIN census.tokens t
  ON t.token = if(p.token0 = '0x4200000000000000000000000000000000000006',
                  p.token1, p.token0)
WHERE i.implied_bps < 0.9 * i.fee_snapshot / 100.0
ORDER BY i.n_obs DESC LIMIT 25
""", fmt="PrettyCompact"))

print("How much cost does the snapshot invent? Fee tier is a floor on "
      "round-trip cost.\n")
print(ch("""
SELECT round(quantile(0.5)(fee_snapshot / 100.0), 2) AS snapshot_bps_p50,
       round(quantile(0.5)(implied_bps), 2) AS actual_bps_p50,
       round(quantile(0.5)(fee_snapshot / 100.0 - implied_bps), 2)
         AS overstatement_bps_p50,
       round(avg(fee_snapshot / 100.0 - implied_bps), 2)
         AS overstatement_bps_mean,
       round(quantile(0.95)(fee_snapshot / 100.0 - implied_bps), 2)
         AS overstatement_bps_p95
FROM census.pool_fee_implied
""", fmt="PrettyCompact"))
