#!/usr/bin/env python3
"""Point-in-time ETH/USD, from the panel itself. Zero RPC cost.

Every round so far priced notional with `ETH_USD = 4000`, a constant. It is
wrong: round 37 established that three independent WETH/USDC pools agree on
price to 0.02%, and what they say is that ETH traded 1,475-2,302 over the
panel. So every "$5,000 notional" was really about $2,265, and the slippage
term (which scales with size) was measured on a trade roughly half the
intended size.

The fix costs nothing. For a WETH/USDC pool, sqrt_price_x96 encodes
token1-per-token0 in raw units; USDC has 6 decimals and WETH 18, so

    p_raw = (sqrt_price_x96 / 2**96) ** 2          # USDC_raw per WETH_raw
    ETH_USD = p_raw * 1e12                        # when WETH is token0
            = 1e-12 / p_raw                       # when WETH is token1

Four independent deep pools are used and the hourly median is taken, with the
cross-pool spread carried alongside as a self-check: if the four ever disagree
by more than a few bps, the series is not trustworthy and the disagreement
will say so rather than hiding inside a median.
"""
from __future__ import annotations

import subprocess
import sys

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# the four WETH/USDC pools that cleared R2's depth gate, two venues
POOLS = ["0x3fe04a59ebd38cf06080a6f60a98d124eb59392a",   # aero, 2.4 bps
         "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59",   # aero, 3.2 bps
         "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",   # univ3, 4.2 bps
         "0xd0b53d9277642d899df5c87a3966a349a798f224"]   # univ3, 5.5 bps
# The one-year backfill finished 2026-08-19 (1,579/1,579 chunks, 424.4M rows,
# 366 contiguous days). The 46,000,000 floor that earlier rounds needed -- to keep
# the frozen 94.6-day panel apart from the range still being filled -- is gone, so
# the series now covers the whole year.
MIN_BLOCK = 34_300_000


def q(sql: str, fmt: str | None = None) -> str:
    cmd = CH + (["--format", fmt] if fmt else []) + ["--query", sql]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def main() -> None:
    plist = ",".join(f"'{p}'" for p in POOLS)
    q("CREATE DATABASE IF NOT EXISTS edge")
    q("DROP TABLE IF EXISTS edge.eth_usd_1h")
    q("""
    CREATE TABLE edge.eth_usd_1h (
      bar DateTime, eth_usd Float64, n_pools UInt8,
      spread_bps Float64, n_swaps UInt32
    ) ENGINE = MergeTree ORDER BY bar
    """)
    # per pool per hour: last price in the hour, oriented to USD per ETH
    q(f"""
    INSERT INTO edge.eth_usd_1h
    WITH per AS (
      SELECT toStartOfHour(s.block_time) AS bar, s.pool AS pool,
             argMax(pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2),
                    (s.block_number, s.log_index)) AS p_raw,
             if(p.token0 = '{WETH}', 1, 0) AS w0,
             count() AS n
      FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
      WHERE s.pool IN ({plist}) AND s.block_number >= {MIN_BLOCK}
            AND s.sqrt_price_x96 > 0
      GROUP BY bar, s.pool, w0
    ),
    px AS (
      SELECT bar, pool, n,
             if(w0 = 1, p_raw * 1e12, 1e-12 / p_raw) AS px_usd
      FROM per WHERE p_raw > 0
    ),
    ok AS (SELECT * FROM px WHERE px_usd BETWEEN 100 AND 100000)
    SELECT bar, quantile(0.5)(px_usd) AS eth_usd, toUInt8(count()) AS n_pools,
           if(min(px_usd) > 0, 1e4 * (max(px_usd) - min(px_usd)) / min(px_usd), 0)
             AS spread_bps,
           toUInt32(sum(n)) AS n_swaps
    FROM ok
    GROUP BY bar
    """)

    print(q("""
    SELECT count() AS hours, min(n_pools) AS min_pools,
           round(quantile(0.5)(eth_usd), 1) AS eth_p50,
           round(min(eth_usd), 1) AS eth_min, round(max(eth_usd), 1) AS eth_max,
           round(quantile(0.5)(spread_bps), 3) AS spread_p50_bps,
           round(quantile(0.99)(spread_bps), 2) AS spread_p99_bps,
           toString(min(bar)) AS t0, toString(max(bar)) AS t1
    FROM edge.eth_usd_1h FORMAT Vertical
    """))

    print("cross-pool check on 2026-05-14 (round 37 reported 2295.8 / 2296.3 / 2296.0):")
    print(q(f"""
    WITH per AS (
      SELECT s.pool AS pool,
             argMax(pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2),
                    (s.block_number, s.log_index)) AS p_raw,
             if(p.token0 = '{WETH}', 1, 0) AS w0
      FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
      WHERE s.pool IN ({plist}) AND toDate(s.block_time) = '2026-05-14'
            AND s.sqrt_price_x96 > 0
      GROUP BY s.pool, w0
    )
    SELECT substring(pool, 1, 12) AS pool,
           round(if(w0 = 1, p_raw * 1e12, 1e-12 / p_raw), 2) AS eth_usd
    FROM per ORDER BY pool FORMAT PrettyCompact
    """))

    print("monthly median, and what a nominal $5,000 really was:")
    print(q("""
    SELECT toString(toStartOfMonth(bar)) AS month, count() AS hours,
           round(quantile(0.5)(eth_usd), 1) AS eth_p50,
           round(5000.0 / 4000.0 * quantile(0.5)(eth_usd), 0) AS real_usd_of_nominal_5k
    FROM edge.eth_usd_1h GROUP BY month ORDER BY month FORMAT PrettyCompact
    """))


if __name__ == "__main__":
    main()
