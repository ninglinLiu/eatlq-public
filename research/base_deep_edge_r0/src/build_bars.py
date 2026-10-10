#!/usr/bin/env python3
"""Hourly bars for the depth-confirmed pools, priced as alt-in-WETH.

Orientation matters: sqrt_price_x96 encodes token1-per-token0, so when WETH is
token0 the raw price is alt-per-WETH and must be inverted, otherwise every
return would carry the wrong sign. Decimals cancel in log returns so they are
left alone.

5-minute bars only cover 66% of buckets on the median pool; hourly reaches
99.7%, so hourly is the finest grid that does not require gap interpolation.
"""
from __future__ import annotations

import json
import subprocess

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800"]
WETH = "0x4200000000000000000000000000000000000006"


def q(sql: str) -> str:
    p = subprocess.run(CH + ["--query", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:600])
    return p.stdout.decode()


if __name__ == "__main__":
    pools = json.load(open("out/candidate_pools.json"))
    plist = ",".join(f"'{p['pool']}'" for p in pools)

    q("CREATE DATABASE IF NOT EXISTS edge")
    q("DROP TABLE IF EXISTS edge.bars_1h")
    q("""
    CREATE TABLE edge.bars_1h (
      pool String, bar DateTime, symbol String, is_major UInt8, venue String,
      cost_bps Float64,
      n_swaps UInt32, vol_weth Float64,
      open Float64, high Float64, low Float64, close Float64, vwap Float64
    ) ENGINE = MergeTree ORDER BY (pool, bar)
    """)

    # cost/symbol/major come from the R2 measurement, joined in via a literal map
    meta = {p["pool"]: (p["symbol"], int(p["is_major"]), p["venue"],
                        float(p["cost_5k_worst_bps"])) for p in pools}

    for i, (pool, (sym, maj, ven, cost)) in enumerate(meta.items(), 1):
        sql = f"""
        INSERT INTO edge.bars_1h
        WITH sw AS (
          SELECT block_time, block_number, log_index,
                 pow(toFloat64(sqrt_price_x96) / pow(2, 96), 2) AS raw_p,
                 abs(toFloat64(if(p.token0 = '{WETH}', s.amount0, s.amount1))) / 1e18 AS w
          FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
          WHERE s.pool = '{pool}' AND sqrt_price_x96 > 0
        ),
        px AS (
          SELECT block_time, block_number, log_index, w,
                 if((SELECT token0 FROM census.pools WHERE pool='{pool}') = '{WETH}',
                    1.0 / raw_p, raw_p) AS p
          FROM sw
        )
        SELECT '{pool}', toStartOfHour(block_time), '{sym}', {maj}, '{ven}', {cost},
               count(), sum(w),
               argMin(p, (block_number, log_index)), max(p), min(p),
               argMax(p, (block_number, log_index)),
               if(sum(w) > 0, sum(p * w) / sum(w), avg(p))
        FROM px GROUP BY toStartOfHour(block_time)
        """
        q(sql)
        if i % 10 == 0 or i == len(meta):
            print(f"[bars] {i}/{len(meta)} pools", flush=True)

    print(q("""
    SELECT count() AS bars, uniqExact(pool) AS pools, uniqExact(symbol) AS symbols,
           min(bar) AS t0, max(bar) AS t1,
           countIf(is_major = 0) AS non_major_bars
    FROM edge.bars_1h FORMAT Vertical
    """))
