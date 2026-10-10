#!/usr/bin/env python3
"""Signed WETH flow for the $50 universe over the full year.

edge.flow_1h was built on the 3-month panel and on the 43-pool universe that a
$5,000 trade could reach. At $50 the reachable universe is 411 pools and the panel
is 366 days, so the flow table has to be rebuilt on the same footing as
edge.bars_1h_year_50 or the A5 test cannot be re-run against the smaller size.

Same signal definition as round 38, including the MIN_SWAPS_IN_WINDOW correction
that was made there before results were seen: order flow imbalance from an hour
with one or two swaps is not a flow measurement.
"""
from __future__ import annotations

import subprocess
import sys

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=7200", "--max_memory_usage=12000000000"]
WETH = "0x4200000000000000000000000000000000000006"
UNIV = "edge.bars_universe_50"
OUT = "edge.flow_1h_year_50"
MIN_BLOCK = 34_300_000


def q(sql: str, fmt: str | None = None) -> str:
    cmd = CH + (["--format", fmt] if fmt else []) + ["--query", sql]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def main() -> None:
    q(f"DROP TABLE IF EXISTS {OUT}")
    q(f"""
    CREATE TABLE {OUT} (
      pool String, bar DateTime, n_swaps UInt32,
      net_weth Float64, gross_weth Float64, ofi Float64
    ) ENGINE = MergeTree ORDER BY (pool, bar)
    """)
    sys.stderr.write("[1/2] aggregating signed flow ...\n")
    q(f"""
    INSERT INTO {OUT}
    WITH sw AS (
      SELECT s.pool AS pool, toStartOfHour(s.block_time) AS bar,
             toFloat64(if(p.token0 = '{WETH}', s.amount0, s.amount1)) / 1e18 AS w
      FROM census.cl_swaps s
      INNER JOIN {UNIV} u ON u.pool = s.pool
      INNER JOIN census.pools p ON s.pool = p.pool
      WHERE s.block_number >= {MIN_BLOCK}
    ),
    agg AS (
      SELECT pool, bar, toUInt32(count()) AS n_swaps,
             sum(w) AS net_weth, sum(abs(w)) AS gross_weth
      FROM sw GROUP BY pool, bar
    )
    SELECT pool, bar, n_swaps, net_weth, gross_weth,
           if(gross_weth > 0, net_weth / gross_weth, 0) AS ofi
    FROM agg
    """)
    sys.stderr.write("[2/2] readout\n")
    print(q(f"""
    SELECT count() AS rows, uniqExact(pool) AS pools,
           toString(min(bar)) AS t0, toString(max(bar)) AS t1,
           round(quantile(0.5)(ofi), 4) AS ofi_p50,
           round(quantile(0.05)(ofi), 3) AS ofi_p05,
           round(quantile(0.95)(ofi), 3) AS ofi_p95,
           countIf(n_swaps >= 5) AS hours_with_5plus_swaps,
           round(100.0 * countIf(n_swaps >= 5) / count(), 1) AS pct_usable
    FROM {OUT} FORMAT Vertical
    """))


if __name__ == "__main__":
    main()
