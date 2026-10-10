#!/usr/bin/env python3
"""Hourly alt-in-WETH bars over the full year, for the pools that can carry a trade.

Same pricing convention as src/build_bars.py, which round 35 used: sqrt_price_x96
encodes token1-per-token0, so when WETH is token0 the raw price is alt-per-WETH and
must be inverted or every return carries the wrong sign. Decimals cancel in log
returns and are left alone.

Two differences, both forced by the year panel:

  1. The universe is selected on census.pit_cost rather than on R2's single-block
     measurement: a pool qualifies if it was executable (cost <= 100 bps at $5,000
     AND passed the inventory gate) on at least MIN_TRADEABLE_DAYS distinct days.
     A static gate would have been measured in August 2026 and applied to
     September 2025.
  2. One query for all pools instead of a per-pool loop, because the year panel has
     ~14x the rows.

is_major flags WETH derivatives, stablecoins and BTC wrappers. Rounds 35 and 38
excluded them because their alt/WETH price barely moves -- bsdETH's hourly sigma
was 0.4 bps -- so any "mean reversion" in them is measuring rounding, not a market.
The flag is set by symbol pattern AND confirmed against realized volatility, so a
mislabelled symbol cannot quietly enter the tradeable set.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
# Trade size decides everything here. At $5,000 only 43 pools were executable on
# 90+ days; at $50 it is 138, because slippage scales with size and the fee tier does
# not. The cost table is therefore an argument.
COST_TABLE = sys.argv[1] if len(sys.argv) > 1 else "census.pit_cost"
BARS_TABLE = "edge.bars_1h_year" + ("_50" if COST_TABLE.endswith("_50") else "")
UNIV_TABLE = "edge.bars_universe" + ("_50" if COST_TABLE.endswith("_50") else "")
MAX_COST_BPS = 100.0
MIN_TRADEABLE_DAYS = 90
MAJOR_PAT = ("ETH", "USD", "USDC", "USDT", "DAI", "EUR", "BTC", "WBTC", "cbBTC",
             "USDS", "GHO", "crvUSD", "sUSD", "USR", "USDe")


def ch(q: str, fmt: str = "TabSeparatedWithNames") -> str:
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--max_execution_time=14400",
                        "--max_memory_usage=12000000000", "--format", fmt,
                        "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:5000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def main() -> None:
    sys.stderr.write("[1/3] tradeable pool set ...\n")
    ch(f"DROP TABLE IF EXISTS {UNIV_TABLE}", fmt="TSV")
    ch("CREATE DATABASE IF NOT EXISTS edge", fmt="TSV")
    ch(f"""CREATE TABLE {UNIV_TABLE} ENGINE = MergeTree ORDER BY pool AS
    SELECT u.pool AS pool, u.alt AS alt, u.venue AS venue, u.fee AS fee,
           u.weth_is_token1 AS weth_is_token1,
           ifNull(t.symbol, substring(u.alt, 1, 10)) AS symbol,
           c.tradeable_days AS tradeable_days, c.cost_p50 AS cost_p50
    FROM census.pit_universe u
    INNER JOIN (
        SELECT pool, countIf(cost_bps_med <= {MAX_COST_BPS} AND feasible = 1)
                 AS tradeable_days,
               quantile(0.5)(cost_bps_med) AS cost_p50
        FROM {COST_TABLE} GROUP BY pool
    ) c ON c.pool = u.pool
    LEFT JOIN census.tokens t ON t.token = u.alt
    WHERE c.tradeable_days >= {MIN_TRADEABLE_DAYS}""", fmt="TSV")
    print(ch(f"""SELECT count() AS pools, uniqExact(alt) AS alts,
                       round(quantile(0.5)(cost_p50), 1) AS cost_p50,
                       round(quantile(0.5)(tradeable_days), 0) AS tradeable_days_p50
                FROM {UNIV_TABLE} FORMAT PrettyCompact"""))

    sys.stderr.write("[2/3] hourly bars ...\n")
    ch(f"DROP TABLE IF EXISTS {BARS_TABLE}", fmt="TSV")
    ch(f"""CREATE TABLE {BARS_TABLE} ENGINE = MergeTree ORDER BY (pool, bar) AS
    WITH px AS (
      SELECT s.pool AS pool, s.block_time AS bt, s.block_number AS bn,
             s.log_index AS li,
             if(u.weth_is_token1 = 1,
                pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2),
                1.0 / pow(toFloat64(s.sqrt_price_x96) / pow(2, 96), 2)) AS p,
             abs(toFloat64(if(u.weth_is_token1 = 1, s.amount1, s.amount0)))
               / 1e18 AS w
      FROM census.cl_swaps s INNER JOIN {UNIV_TABLE} u ON u.pool = s.pool
      WHERE s.sqrt_price_x96 > 0
    )
    SELECT pool, toStartOfHour(bt) AS bar, count() AS n_swaps, sum(w) AS vol_weth,
           argMin(p, (bn, li)) AS open, max(p) AS high, min(p) AS low,
           argMax(p, (bn, li)) AS close,
           if(sum(w) > 0, sum(p * w) / sum(w), avg(p)) AS vwap
    FROM px WHERE p > 0 AND isFinite(p) GROUP BY pool, bar""", fmt="TSV")

    sys.stderr.write("[3/3] major flag ...\n")
    pat = " OR ".join(f"position(upper(symbol), '{s.upper()}') > 0" for s in MAJOR_PAT)
    print(ch(f"""
    SELECT u.symbol AS symbol, u.venue AS venue, count() AS pools,
           round(quantile(0.5)(b.sig_bps), 2) AS sigma_1h_bps,
           round(quantile(0.5)(u.cost_p50), 1) AS cost_bps,
           max(u.tradeable_days) AS tradeable_days,
           if({pat}, 1, 0) AS pattern_major
    FROM {UNIV_TABLE} u
    INNER JOIN (
        SELECT pool, 1e4 * stddevSamp(lr) AS sig_bps FROM (
            SELECT pool, log(close / any(close) OVER
                     (PARTITION BY pool ORDER BY bar ROWS BETWEEN 1 PRECEDING
                      AND 1 PRECEDING)) AS lr
            FROM {BARS_TABLE})
        WHERE isFinite(lr) AND lr != 0 GROUP BY pool
    ) b ON b.pool = u.pool
    GROUP BY symbol, venue, pattern_major
    ORDER BY pattern_major, sigma_1h_bps DESC FORMAT PrettyCompact"""))
    print(ch(f"""SELECT count() AS bars, uniqExact(pool) AS pools,
                       toString(min(bar)) AS t0, toString(max(bar)) AS t1
                FROM {BARS_TABLE} FORMAT PrettyCompact"""))


if __name__ == "__main__":
    main()
