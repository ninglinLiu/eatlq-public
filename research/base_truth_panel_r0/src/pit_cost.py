"""Point-in-time execution cost, computable from the panel alone at zero RPC cost.

Replaces R2's single-block atomic measurement for any analysis that needs cost as
of a decision date. Two components, because one of them alone is not enough:

  1. CLOSED-FORM SLIPPAGE, from the CLMM identity
         sqrt(p') = sqrt(p) + V/L        =>   slippage = V / (L * sqrt(p))
     plus the fee tier. Validated against R2's 537 atomic measurements:
     median predicted/measured = 1.002 ($50), 1.014 ($500), 1.096 ($5,000);
     Spearman 0.94/0.92/0.89; exact 4-way tier agreement 93.9% at $5,000.

  2. INVENTORY FEASIBILITY GATE. The formula uses liquidity DENSITY at the current
     tick, which says nothing about how much of either token the pool actually
     holds. R2's one-way traps (BGPT: buy 1.2 bps, sell 10,000 bps) are invisible
     to it. Of the 34 pools the formula calls cheap at $5,000, 4 are traps, and
     the formula ranks them 1.2-10.8 bps. The gate that separates them perfectly
     on that sample: the largest WETH amount anyone has actually taken OUT of the
     pool over the trailing window must cover what I need to take out.
         genuinely cheap : 29/30 pass
         traps           :  0/4  pass

Both are causal: only data strictly before the decision date is used. Liquidity is
taken as the MEDIAN over the trailing window rather than a closing snapshot,
because R2 showed single-block liquidity can be off by 3-15x.

Output: census.pit_cost with one row per (pool, date).
"""
import os, subprocess, sys

WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 4000.0
SIZE_USD = 5000.0
TRAIL_DAYS = 7
INV_TRAIL_DAYS = 30


def ch(q, fmt="TabSeparatedWithNames"):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=7200", "--max_memory_usage=40000000000",
                        "--format", fmt, "--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:5000]); raise SystemExit("clickhouse failed")
    return p.stdout


def build():
    v_weth_raw = (SIZE_USD / ETH_USD) * 1e18
    need_weth = SIZE_USD / ETH_USD

    ch("DROP TABLE IF EXISTS census.pit_cost", fmt="TSV")
    ch("""CREATE TABLE census.pit_cost (
            pool String, d Date,
            cost_bps_med Float64,      -- closed form using median trailing liquidity
            cost_bps_last Float64,     -- closed form using last observed liquidity
            max_weth_out_30d Float64,  -- inventory gate input
            feasible UInt8,            -- gate result at SIZE_USD
            n_swaps_7d UInt32,
            L_med Float64, p_last Float64, fee UInt32
          ) ENGINE = MergeTree ORDER BY (pool, d)""", fmt="TSV")

    # hourly liquidity/price per pool, then trailing aggregates per decision date.
    # Done in one pass with a range join on date to keep it causal and cheap.
    sys.stderr.write("building hourly state ...\n")
    ch("DROP TABLE IF EXISTS census.pit_hourly", fmt="TSV")
    ch("""CREATE TABLE census.pit_hourly ENGINE = MergeTree ORDER BY (pool, h) AS
          SELECT pool,
                 toStartOfHour(block_time) AS h,
                 toDate(block_time) AS d,
                 median(toFloat64(liquidity)) AS L_med_h,
                 argMax(toFloat64(sqrt_price_x96), (block_number, log_index)) AS sp_last,
                 count() AS n
          FROM census.cl_swaps
          WHERE sqrt_price_x96 > 0
          GROUP BY pool, h, d""", fmt="TSV")
    sys.stderr.write("hourly rows: %s\n" % ch("SELECT count() FROM census.pit_hourly",
                                              fmt="TSV").strip())

    # daily WETH outflow maxima (inventory gate input)
    sys.stderr.write("building daily weth outflow ...\n")
    ch("DROP TABLE IF EXISTS census.pit_wout", fmt="TSV")
    ch("""CREATE TABLE census.pit_wout ENGINE = MergeTree ORDER BY (pool, d) AS
          SELECT s.pool AS pool, toDate(s.block_time) AS d,
                 max(if(pl.token1 = '{w}',
                        if(s.amount1 < 0, -toFloat64(s.amount1), 0),
                        if(s.amount0 < 0, -toFloat64(s.amount0), 0))) / 1e18 AS max_wout
          FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
          WHERE pl.token0 = '{w}' OR pl.token1 = '{w}'
          GROUP BY s.pool, d""".format(w=WETH), fmt="TSV")

    sys.stderr.write("assembling pit_cost ...\n")
    ch("""INSERT INTO census.pit_cost
        WITH dates AS (
            SELECT DISTINCT pool, d FROM census.pit_hourly),
        trail AS (
            SELECT dt.pool AS pool, dt.d AS d,
                   median(hh.L_med_h) AS L_med,
                   argMax(hh.sp_last, hh.h) AS sp_last,
                   sum(hh.n) AS n7
            FROM dates dt
            INNER JOIN census.pit_hourly hh
                ON hh.pool = dt.pool AND hh.d < dt.d AND hh.d >= dt.d - {trail}
            GROUP BY dt.pool, dt.d),
        lastliq AS (
            SELECT dt.pool AS pool, dt.d AS d,
                   argMax(hh.L_med_h, hh.h) AS L_last
            FROM dates dt
            INNER JOIN census.pit_hourly hh
                ON hh.pool = dt.pool AND hh.d < dt.d AND hh.d >= dt.d - {trail}
            GROUP BY dt.pool, dt.d),
        inv AS (
            SELECT dt.pool AS pool, dt.d AS d, max(wo.max_wout) AS mwo
            FROM dates dt
            INNER JOIN census.pit_wout wo
                ON wo.pool = dt.pool AND wo.d < dt.d AND wo.d >= dt.d - {inv}
            GROUP BY dt.pool, dt.d)
        SELECT t.pool, t.d,
               1e4 * (pl.fee / 1e6) + 1e4 * (if(pl.token1 = '{w}', {v}, {v} * pow(t.sp_last/pow(2,96), 2)))
                     / (t.L_med * (t.sp_last / pow(2, 96)))                     AS cost_bps_med,
               1e4 * (pl.fee / 1e6) + 1e4 * (if(pl.token1 = '{w}', {v}, {v} * pow(t.sp_last/pow(2,96), 2)))
                     / (l.L_last * (t.sp_last / pow(2, 96)))                    AS cost_bps_last,
               ifNull(i.mwo, 0)                                                 AS max_weth_out_30d,
               if(ifNull(i.mwo, 0) >= {need}, 1, 0)                             AS feasible,
               toUInt32(t.n7)                                                   AS n_swaps_7d,
               t.L_med, pow(t.sp_last/pow(2,96), 2)                             AS p_last,
               pl.fee
        FROM trail t
        INNER JOIN census.pools pl ON pl.pool = t.pool
        LEFT JOIN lastliq l ON l.pool = t.pool AND l.d = t.d
        LEFT JOIN inv i ON i.pool = t.pool AND i.d = t.d
        WHERE (pl.token0 = '{w}' OR pl.token1 = '{w}')
          AND t.L_med > 0 AND t.sp_last > 0
        """.format(w=WETH, v=v_weth_raw, need=need_weth,
                   trail=TRAIL_DAYS, inv=INV_TRAIL_DAYS), fmt="TSV")

    print(ch("""SELECT count() AS rows, countDistinct(pool) AS pools,
                       min(d) AS d0, max(d) AS d1,
                       round(quantile(0.5)(cost_bps_med), 1) AS cost_p50,
                       countIf(cost_bps_med < 30) AS cheap_rows,
                       countIf(cost_bps_med < 30 AND feasible = 1) AS cheap_and_feasible,
                       round(100.0 * countIf(feasible = 1) / count(), 1) AS pct_feasible
                FROM census.pit_cost"""))


def validate():
    """Compare both liquidity variants against R2's atomic measurements."""
    print("=== validation against R2 atomic measurement (block 50123160) ===")
    print(ch("""
        WITH r2 AS (
            SELECT pool, d, cost_bps_med, cost_bps_last, feasible
            FROM census.pit_cost WHERE d = toDate('2026-08-11'))
        SELECT count() AS n,
               round(quantile(0.5)(cost_bps_med), 1)  AS pit_med_p50,
               round(quantile(0.5)(cost_bps_last), 1) AS pit_last_p50
        FROM r2"""))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        validate()
    else:
        build()
