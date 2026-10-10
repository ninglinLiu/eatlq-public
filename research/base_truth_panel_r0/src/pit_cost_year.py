#!/usr/bin/env python3
"""Point-in-time execution cost over the full year, with a real ETH price.

Two changes from src/pit_cost.py, which was written for the 94.6-day panel and
never run:

  1. ETH_USD = 4000 is replaced by the daily median of edge.eth_usd_1h. Over this
     year ETH ran 4,935 -> 1,522, so a constant is not a rounding error: a
     nominal $5,000 was really $5,571 in August 2025 and $2,094 in June 2026.
     Both the notional and the inventory gate scale with it.

  2. The universe is capped. The original ran over every pool in the panel; with
     424M swaps and a 7-day rolling join that is 10^10 join rows. Restricting to
     WETH-legged pools with >=200 swaps on >=60 distinct days first cuts it to
     3,069 pools, and nothing outside that set could carry a position anyway.

Method unchanged, because round 37 validated it against atomic execution: the
closed-form slippage from the CLMM identity sqrt(p') = sqrt(p) + V/L plus the
static fee tier came within 0.6 bps of ground truth, while back-solving the fee
from swap data was 3.6 bps optimistic. Liquidity is the median over the trailing
window, not a closing snapshot, because a single block can be off by 3-15x.

The inventory gate stays too. Slippage uses liquidity DENSITY at the current
tick, which says nothing about whether the pool holds enough of the token to pay
out; R2's one-way traps (BGPT: 1.2 bps to buy, 10,000 bps to sell) are invisible
without it.
"""
from __future__ import annotations

import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
# Trade size is the single most consequential parameter in this whole project and
# every round until now hard-coded 5000. That was wrong for this account: the real
# per-trade size is about $50, and slippage scales with size while the fee tier and
# gas do not. So the size is now an argument, the output table is suffixed with it,
# and both sizes can coexist for comparison.
#   python3 src/pit_cost_year.py 50      -> census.pit_cost_50
#   python3 src/pit_cost_year.py 5000    -> census.pit_cost   (round 40's table)
SIZE_USD = float(sys.argv[1]) if len(sys.argv) > 1 else 5000.0
SUFFIX = "" if SIZE_USD == 5000.0 else f"_{int(SIZE_USD)}"
MIN_SWAPS, MIN_DAYS = 200, 60
TRAIL_DAYS, INV_TRAIL_DAYS = 7, 30


def ch(q: str, fmt: str = "TabSeparatedWithNames") -> str:
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=14400",
                        "--max_memory_usage=30000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:5000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def exists(tbl: str) -> bool:
    db, name = tbl.split(".")
    return ch(f"SELECT count() FROM system.tables WHERE database = '{db}' "
              f"AND name = '{name}'", fmt="TSV").strip().endswith("1")


def main() -> None:
    # pit_universe / pit_hourly / pit_wout do not depend on trade size, so a second
    # size reuses them instead of re-scanning 424M rows.
    if all(exists(t) for t in ("census.pit_universe", "census.pit_hourly",
                              "census.pit_wout")):
        sys.stderr.write("[1-3/5] reusing existing size-independent tables\n")
        return build_cost()
    sys.stderr.write("[1/5] universe ...\n")
    ch("DROP TABLE IF EXISTS census.pit_universe", fmt="TSV")
    ch(f"""CREATE TABLE census.pit_universe ENGINE = MergeTree ORDER BY pool AS
    SELECT s.pool AS pool, pl.venue AS venue, pl.fee AS fee,
           pl.tick_spacing AS tick_spacing,
           if(pl.token0 = '{WETH}', pl.token1, pl.token0) AS alt,
           if(pl.token1 = '{WETH}', 1, 0) AS weth_is_token1,
           count() AS n_swaps, uniqExact(toDate(s.block_time)) AS n_days,
           min(toDate(s.block_time)) AS d_first, max(toDate(s.block_time)) AS d_last
    FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
    WHERE (pl.token0 = '{WETH}' OR pl.token1 = '{WETH}') AND pl.fee > 0
    GROUP BY s.pool, pl.venue, pl.fee, pl.tick_spacing, alt, weth_is_token1
    HAVING n_swaps >= {MIN_SWAPS} AND n_days >= {MIN_DAYS}""", fmt="TSV")
    sys.stderr.write("  pools: " + ch("SELECT count() FROM census.pit_universe",
                                      fmt="TSV").strip() + "\n")

    sys.stderr.write("[2/5] hourly state ...\n")
    ch("DROP TABLE IF EXISTS census.pit_hourly", fmt="TSV")
    ch("""CREATE TABLE census.pit_hourly ENGINE = MergeTree ORDER BY (pool, h) AS
    SELECT s.pool AS pool, toStartOfHour(s.block_time) AS h,
           toDate(s.block_time) AS d,
           median(toFloat64(s.liquidity)) AS L_med_h,
           argMax(toFloat64(s.sqrt_price_x96), (s.block_number, s.log_index)) AS sp_last,
           count() AS n
    FROM census.cl_swaps s
    WHERE s.sqrt_price_x96 > 0
          AND s.pool IN (SELECT pool FROM census.pit_universe)
    GROUP BY s.pool, h, d""", fmt="TSV")
    sys.stderr.write("  hourly rows: " + ch("SELECT count() FROM census.pit_hourly",
                                            fmt="TSV").strip() + "\n")

    sys.stderr.write("[3/5] daily WETH outflow (inventory gate input) ...\n")
    ch("DROP TABLE IF EXISTS census.pit_wout", fmt="TSV")
    ch(f"""CREATE TABLE census.pit_wout ENGINE = MergeTree ORDER BY (pool, d) AS
    SELECT s.pool AS pool, toDate(s.block_time) AS d,
           max(if(u.weth_is_token1 = 1,
                  if(s.amount1 < 0, -toFloat64(s.amount1), 0),
                  if(s.amount0 < 0, -toFloat64(s.amount0), 0))) / 1e18 AS max_wout
    FROM census.cl_swaps s INNER JOIN census.pit_universe u ON u.pool = s.pool
    GROUP BY s.pool, d""", fmt="TSV")

    return build_cost()


def build_cost() -> None:
    sys.stderr.write("[4/5] assembling pit_cost with per-date ETH price ...\n")
    ch(f"DROP TABLE IF EXISTS census.pit_cost{SUFFIX}", fmt="TSV")
    ch(f"""CREATE TABLE census.pit_cost{SUFFIX} (
            pool String, d Date, eth_usd Float64,
            cost_bps_med Float64, cost_bps_last Float64,
            max_weth_out_30d Float64, need_weth Float64, feasible UInt8,
            n_swaps_7d UInt32, L_med Float64, p_last Float64, fee UInt32
          ) ENGINE = MergeTree ORDER BY (pool, d)""", fmt="TSV")
    ch(f"""INSERT INTO census.pit_cost{SUFFIX}
    WITH ethd AS (
        SELECT toDate(bar) AS d, quantile(0.5)(eth_usd) AS eth
        FROM edge.eth_usd_1h GROUP BY d),
    dates AS (SELECT DISTINCT pool, d FROM census.pit_hourly),
    trail AS (
        SELECT dt.pool AS pool, dt.d AS d,
               median(hh.L_med_h) AS L_med,
               argMax(hh.sp_last, hh.h) AS sp_last,
               sum(hh.n) AS n7
        FROM dates dt INNER JOIN census.pit_hourly hh
          ON hh.pool = dt.pool AND hh.d < dt.d AND hh.d >= dt.d - {TRAIL_DAYS}
        GROUP BY dt.pool, dt.d),
    lastliq AS (
        SELECT dt.pool AS pool, dt.d AS d, argMax(hh.L_med_h, hh.h) AS L_last
        FROM dates dt INNER JOIN census.pit_hourly hh
          ON hh.pool = dt.pool AND hh.d < dt.d AND hh.d >= dt.d - {TRAIL_DAYS}
        GROUP BY dt.pool, dt.d),
    inv AS (
        SELECT dt.pool AS pool, dt.d AS d, max(wo.max_wout) AS mwo
        FROM dates dt INNER JOIN census.pit_wout wo
          ON wo.pool = dt.pool AND wo.d < dt.d AND wo.d >= dt.d - {INV_TRAIL_DAYS}
        GROUP BY dt.pool, dt.d)
    SELECT t.pool, t.d, e.eth AS eth_usd,
           1e4 * (u.fee / 1e6)
             + 1e4 * (if(u.weth_is_token1 = 1, ({SIZE_USD} / e.eth) * 1e18,
                         ({SIZE_USD} / e.eth) * 1e18 * pow(t.sp_last / pow(2, 96), 2)))
                   / (t.L_med * (t.sp_last / pow(2, 96)))         AS cost_bps_med,
           1e4 * (u.fee / 1e6)
             + 1e4 * (if(u.weth_is_token1 = 1, ({SIZE_USD} / e.eth) * 1e18,
                         ({SIZE_USD} / e.eth) * 1e18 * pow(t.sp_last / pow(2, 96), 2)))
                   / (l.L_last * (t.sp_last / pow(2, 96)))        AS cost_bps_last,
           ifNull(i.mwo, 0)                                       AS max_weth_out_30d,
           {SIZE_USD} / e.eth                                     AS need_weth,
           if(ifNull(i.mwo, 0) >= {SIZE_USD} / e.eth, 1, 0)       AS feasible,
           toUInt32(t.n7)                                         AS n_swaps_7d,
           t.L_med, pow(t.sp_last / pow(2, 96), 2)                AS p_last, u.fee
    FROM trail t
    INNER JOIN census.pit_universe u ON u.pool = t.pool
    INNER JOIN ethd e ON e.d = t.d
    LEFT JOIN lastliq l ON l.pool = t.pool AND l.d = t.d
    LEFT JOIN inv i ON i.pool = t.pool AND i.d = t.d
    WHERE t.L_med > 0 AND t.sp_last > 0""", fmt="TSV")

    sys.stderr.write("[5/5] readout\n")
    print(ch(f"""SELECT count() AS rows, uniqExact(pool) AS pools,
                       toString(min(d)) AS d0, toString(max(d)) AS d1,
                       round(quantile(0.5)(cost_bps_med), 1) AS cost_p50,
                       round(100.0 * countIf(feasible = 1) / count(), 1) AS pct_feasible,
                       uniqExactIf(pool, cost_bps_med < 30 AND feasible = 1)
                         AS pools_ever_cheap_and_feasible
                FROM census.pit_cost{SUFFIX} FORMAT Vertical"""))

    print("cost by month -- does the cheap set survive the whole year?")
    print(ch(f"""SELECT toString(toStartOfMonth(d)) AS month,
                       uniqExact(pool) AS pools,
                       round(quantile(0.5)(eth_usd), 0) AS eth,
                       round(quantile(0.5)(cost_bps_med), 1) AS cost_p50,
                       uniqExactIf(pool, cost_bps_med < 30 AND feasible = 1) AS cheap_feasible
                FROM census.pit_cost{SUFFIX} GROUP BY month ORDER BY month
                FORMAT PrettyCompact"""))

    print("how many pools are cheap+feasible on at least N days of the year?")
    print(ch(f"""SELECT thr AS min_cheap_days, count() AS pools FROM (
                  SELECT pool, countIf(cost_bps_med < 30 AND feasible = 1) AS cd
                  FROM census.pit_cost{SUFFIX} GROUP BY pool)
                ARRAY JOIN [30, 90, 180, 300] AS thr
                WHERE cd >= thr GROUP BY thr ORDER BY thr FORMAT PrettyCompact"""))


if __name__ == "__main__":
    main()
