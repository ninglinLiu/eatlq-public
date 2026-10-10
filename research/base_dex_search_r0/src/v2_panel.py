"""Point-in-time $25/$50 cost for WETH-legged Uniswap-V2-style pairs on Base.

Aerodrome vAMM emits the same Swap log as Uniswap V2; this panel covers both
and tags venue from discovered_pools when the factory is known.

Method (conservative, written before scores):
  fee = 30 bps (univ2 default; aero volatile is typically 30 bps too)
  slip_bps ≈ 1e4 * size_weth / max(2 * max_single_weth_swap_30d, size_weth)
  feasible iff some swap in the prior 30 days moved at least size_weth
"""
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from ch import ch
from params import WETH


def main():
    print("[1/4] v2 WETH pairs", flush=True)
    ch("DROP TABLE IF EXISTS census.v2_weth_pairs", fmt="TSV")
    ch(f"""
        CREATE TABLE census.v2_weth_pairs
        ENGINE = MergeTree ORDER BY pool AS
        SELECT lower(pool_address) AS pool,
               lower(factory_address) AS factory,
               if(pool_type = 'aero_vamm', 'aero_vamm', 'univ2') AS venue,
               lower(token0_address) AS token0,
               lower(token1_address) AS token1,
               if(lower(token1_address) = '{WETH}', 1, 0) AS weth_is_token1,
               if(lower(token1_address) = '{WETH}', token0_decimals, token1_decimals) AS alt_dec
        FROM eatlq.discovered_pools
        WHERE chain_id = 8453 AND has_weth_leg = 1
          AND pool_type IN ('v2', 'aero_vamm')
    """, fmt="TSV")
    print("  ", ch("SELECT count() FROM census.v2_weth_pairs", fmt="TSV").strip(), "pairs")

    print("[2/4] daily WETH flow", flush=True)
    ch("DROP TABLE IF EXISTS census.v2_daily", fmt="TSV")
    ch(f"""
        CREATE TABLE census.v2_daily
        ENGINE = MergeTree ORDER BY (pool, d) AS
        SELECT s.pair_address AS pool, toDate(assumeNotNull(s.block_time)) AS d,
               count() AS n_swaps,
               sum(toFloat64(if(p.weth_is_token1 = 1,
                                s.amount1_in + s.amount1_out,
                                s.amount0_in + s.amount0_out))) / 1e18 AS weth_vol,
               max(toFloat64(if(p.weth_is_token1 = 1,
                                s.amount1_in + s.amount1_out,
                                s.amount0_in + s.amount0_out))) / 1e18 AS max_weth
        FROM eatlq.uniswap_v2_swaps_decoded s
        INNER JOIN census.v2_weth_pairs p ON p.pool = s.pair_address
        WHERE s.chain_id = 8453 AND s.block_time IS NOT NULL AND s.block_time >= '2025-08-18'
        GROUP BY pool, d
    """, fmt="TSV")
    print("  ", ch("SELECT count(), uniqExact(pool) FROM census.v2_daily", fmt="TSV").strip())

    print("[2b/4] v2 hourly last implied WETH-per-alt", flush=True)
    ch("DROP TABLE IF EXISTS census.v2_hourly", fmt="TSV")
    ch(f"""
        CREATE TABLE census.v2_hourly
        ENGINE = MergeTree ORDER BY (pool, h) AS
        SELECT s.pair_address AS pool, toStartOfHour(assumeNotNull(s.block_time)) AS h,
               argMax(
                 toFloat64(if(p.weth_is_token1=1, s.amount1_in+s.amount1_out, s.amount0_in+s.amount0_out)) / 1e18
                 / nullIf(toFloat64(if(p.weth_is_token1=1, s.amount0_in+s.amount0_out, s.amount1_in+s.amount1_out))
                          / pow(10, p.alt_dec), 0),
                 (s.block_number, s.log_index)
               ) AS px,
               count() AS n_swaps,
               sum(toFloat64(if(p.weth_is_token1=1, s.amount1_in+s.amount1_out, s.amount0_in+s.amount0_out)))/1e18 AS weth_vol
        FROM eatlq.uniswap_v2_swaps_decoded s
        INNER JOIN census.v2_weth_pairs p ON p.pool = s.pair_address
        WHERE s.chain_id = 8453 AND s.block_time IS NOT NULL AND s.block_time >= '2025-08-18'
        GROUP BY pool, h
        HAVING isFinite(px) AND px > 0
    """, fmt="TSV")
    print("  ", ch("SELECT count(), uniqExact(pool) FROM census.v2_hourly", fmt="TSV").strip())

    for size in (25, 50):
        print(f"[3-4/4] v2_pit_cost_{size}", flush=True)
        tbl = f"census.v2_pit_cost_{size}"
        ch(f"DROP TABLE IF EXISTS {tbl}", fmt="TSV")
        ch(f"""
            CREATE TABLE {tbl}
            ENGINE = MergeTree ORDER BY (pool, d) AS
            WITH ethd AS (
                SELECT toDate(bar) AS d, quantile(0.5)(eth_usd) AS eth
                FROM edge.eth_usd_1h GROUP BY d
            ),
            inv AS (
                SELECT a.pool AS pool, a.d AS d,
                       max(b.max_weth) AS max_weth_30d,
                       sum(b.n_swaps) AS n_swaps_30d
                FROM census.v2_daily a
                INNER JOIN census.v2_daily b
                  ON b.pool = a.pool AND b.d < a.d AND b.d >= a.d - 30
                GROUP BY a.pool, a.d
            )
            SELECT i.pool AS pool, i.d AS d, e.eth AS eth_usd,
                   p.venue AS venue, p.factory AS factory,
                   30.0 + 1e4 * ({size} / e.eth)
                     / greatest(2.0 * i.max_weth_30d, {size} / e.eth) AS cost_bps_med,
                   i.max_weth_30d AS max_weth_out_30d,
                   {size} / e.eth AS need_weth,
                   if(i.max_weth_30d >= {size} / e.eth, 1, 0) AS feasible,
                   i.n_swaps_30d AS n_swaps_30d
            FROM inv i
            INNER JOIN ethd e ON e.d = i.d
            INNER JOIN census.v2_weth_pairs p ON p.pool = i.pool
        """, fmt="TSV")
        print(ch(f"""
            SELECT '{size}' AS sz, count() rows, uniqExact(pool) pools,
                   uniqExactIf(pool, feasible=1 AND cost_bps_med<=100) cheap
            FROM {tbl}"""))


if __name__ == "__main__":
    main()
