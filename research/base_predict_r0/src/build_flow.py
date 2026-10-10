#!/usr/bin/env python3
"""Signed WETH flow per pool-hour, for the A5 order-flow signal.

Sign convention: WETH *into* the pool means someone is buying the alt, so that
is positive. The swap event's amount for the WETH side already carries this
sign, so no inference is needed -- only the orientation (is WETH token0 or
token1) has to be resolved per pool.

Restricted to block >= 46,000,000. That block is 2026-05-14, the first day of
the frozen panel, and the one-year backfill is currently filling the range
below it. Querying below the floor would mix a complete window with a
partially-filled one.
"""
from __future__ import annotations

import json
import subprocess
import sys

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]
WETH = "0x4200000000000000000000000000000000000006"
MIN_BLOCK = 46_000_000


def q(sql: str, fmt: str | None = None) -> str:
    cmd = CH + (["--format", fmt] if fmt else []) + ["--query", sql]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def main() -> None:
    pools = json.load(open("/root/base_deep_edge_r0/out/candidate_pools.json"))
    plist = ",".join(f"'{p['pool']}'" for p in pools)

    q("DROP TABLE IF EXISTS edge.flow_1h")
    q("""
    CREATE TABLE edge.flow_1h (
      pool String, bar DateTime, n_swaps UInt32,
      net_weth Float64, gross_weth Float64, ofi Float64
    ) ENGINE = MergeTree ORDER BY (pool, bar)
    """)
    q(f"""
    INSERT INTO edge.flow_1h
    WITH sw AS (
      SELECT s.pool AS pool, toStartOfHour(s.block_time) AS bar,
             toFloat64(if(p.token0 = '{WETH}', s.amount0, s.amount1)) / 1e18 AS w
      FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
      WHERE s.pool IN ({plist}) AND s.block_number >= {MIN_BLOCK}
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

    print(q("""
    SELECT count() AS rows, uniqExact(pool) AS pools,
           toString(min(bar)) AS t0, toString(max(bar)) AS t1,
           round(quantile(0.5)(ofi), 4) AS ofi_p50,
           round(quantile(0.05)(ofi), 3) AS ofi_p05,
           round(quantile(0.95)(ofi), 3) AS ofi_p95,
           round(100.0 * countIf(abs(ofi) > 0.3) / count(), 1) AS pct_abs_gt_030,
           round(100.0 * countIf(abs(ofi) > 0.6) / count(), 1) AS pct_abs_gt_060
    FROM edge.flow_1h FORMAT Vertical
    """))

    print("coverage against bars_1h (a flow hour must exist for every bar hour):")
    print(q("""
    SELECT count() AS bar_hours, countIf(f.pool != '') AS matched_flow_hours
    FROM edge.bars_1h b LEFT JOIN edge.flow_1h f
      ON b.pool = f.pool AND b.bar = f.bar
    FORMAT PrettyCompact
    """))


if __name__ == "__main__":
    main()
