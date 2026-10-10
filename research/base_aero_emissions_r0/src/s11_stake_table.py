"""Build census.aero_gauge_stake from the sampled in-range staked liquidity.

USD conversion
--------------
Every one of the 157 gauge pools is an ALT/WETH pair (verified against census.pools),
so active liquidity can be valued in WETH with no extra price feed.

For liquidity L at sqrt price s = sqrtPriceX96 / 2**96, the virtual reserves are
    token0_raw = L / s      token1_raw = L * s
A full-range (x*y=k) position needs exactly those reserves. Any *concentrated* position
holding the same L over a narrower range needs strictly less capital:
    value(range [a,b]) = L*(2*sqrt(P) - P/sqrt(b) - sqrt(a)) <= 2*L*sqrt(P)
So 2 * L * s (expressed on the 18-decimal WETH leg) is a STRICT UPPER BOUND on the USD
capital standing behind the staked in-range liquidity -- which makes the resulting
emission APR a LOWER BOUND. That is the conservative direction for a profit claim.
"""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C

Q96 = 2.0 ** 96


def main():
    print(C.ch("SELECT count() n, uniqExact(gauge) g, uniqExact(epoch_start) e "
               "FROM census.aero_gauge_stake_samples"))

    # epoch-level price/liquidity context per pool, from existing census/edge tables
    C.ch("DROP TABLE IF EXISTS census.aero_pool_epoch_ctx")
    C.ch(f"""
    CREATE TABLE census.aero_pool_epoch_ctx
    ENGINE = MergeTree ORDER BY (pool, epoch_start) AS
    SELECT p.pool AS pool,
           toDateTime(intDiv(toUInt32(p.h), 604800) * 604800) AS epoch_start,
           quantileExact(0.5)(p.sp_last) AS sqrt_price_x96,
           quantileExact(0.5)(p.L_med_h) AS pool_liquidity_pit,
           quantileExact(0.5)(e.eth_usd) AS eth_usd,
           quantileExact(0.5)(a.aero_usd) AS aero_usd,
           count() AS n_hours
    FROM census.pit_hourly p
    INNER JOIN edge.eth_usd_1h e ON e.bar = p.h
    INNER JOIN census.aero_price_hourly a ON a.bar = p.h
    WHERE p.pool IN (SELECT pool FROM census.aero_gauge_map)
      AND p.sp_last > 0
    GROUP BY pool, epoch_start
    """)
    print(C.ch("SELECT count() n, uniqExact(pool) pools, min(epoch_start) mine, "
               "max(epoch_start) maxe FROM census.aero_pool_epoch_ctx"))

    C.ch("DROP TABLE IF EXISTS census.aero_gauge_stake")
    C.ch("""
    CREATE TABLE census.aero_gauge_stake (
        gauge String,
        pool String,
        epoch_start DateTime,
        staked_liquidity Float64,
        method String,
        confidence String,
        n_samples UInt8,
        staked_liquidity_min Float64,
        staked_liquidity_max Float64,
        sample_dispersion Float64,
        pool_liquidity Float64,
        staked_share Float64,
        sqrt_price_x96 Float64,
        eth_usd Float64,
        weth_is_token1 UInt8,
        staked_liquidity_usd_ub Float64
    ) ENGINE = MergeTree ORDER BY (gauge, epoch_start)
    """)

    method = ("in-range CLPool.stakedLiquidity() sampled at 4 evenly spaced blocks per "
              "epoch via archive eth_call, arithmetic mean; USD column is a STRICT UPPER "
              "BOUND 2*L*sqrtP on the capital backing that liquidity (=> APR from it is a "
              "LOWER BOUND), sqrtP = epoch-median census.pit_hourly.sp_last, "
              "ETH/USD = epoch-median edge.eth_usd_1h")

    C.ch(f"""
    INSERT INTO census.aero_gauge_stake
    SELECT s.gauge, s.pool, s.epoch_start,
           s.sl_mean AS staked_liquidity,
           '{method}' AS method,
           multiIf(s.n = 0, 'none',
                   s.sl_mean = 0, 'exact-zero: no in-range staked liquidity observed',
                   s.n >= 4 AND s.disp <= 0.25, 'high',
                   s.n >= 3 AND s.disp <= 1.0, 'medium',
                   'low') AS confidence,
           s.n AS n_samples, s.sl_min, s.sl_max, s.disp,
           s.pl_mean AS pool_liquidity,
           if(s.pl_mean > 0, s.sl_mean / s.pl_mean, 0) AS staked_share,
           c.sqrt_price_x96, c.eth_usd, u.weth_is_token1,
           if(c.sqrt_price_x96 > 0 AND c.eth_usd > 0,
              c.eth_usd * 2.0 * if(u.weth_is_token1 = 1,
                                   s.sl_mean * (c.sqrt_price_x96 / {Q96}),
                                   s.sl_mean / (c.sqrt_price_x96 / {Q96})) / 1e18,
              0) AS staked_liquidity_usd_ub
    FROM (
        SELECT gauge, pool, epoch_start,
               avg(staked_liquidity) AS sl_mean,
               min(staked_liquidity) AS sl_min,
               max(staked_liquidity) AS sl_max,
               avg(pool_liquidity) AS pl_mean,
               toUInt8(count()) AS n,
               if(avg(staked_liquidity) > 0,
                  (max(staked_liquidity) - min(staked_liquidity)) / avg(staked_liquidity),
                  0) AS disp
        FROM census.aero_gauge_stake_samples GROUP BY gauge, pool, epoch_start
    ) s
    LEFT JOIN census.aero_pool_epoch_ctx c
           ON c.pool = s.pool AND c.epoch_start = s.epoch_start
    LEFT JOIN edge.bars_universe_50 u ON u.pool = s.pool
    """)

    print("--- census.aero_gauge_stake ---")
    print(C.ch("""
    SELECT count() rows, uniqExact(gauge) gauges, uniqExact(epoch_start) epochs,
           min(epoch_start) mine, max(epoch_start) maxe,
           countIf(staked_liquidity_usd_ub > 0) with_usd
    FROM census.aero_gauge_stake"""))
    print(C.ch("""
    SELECT confidence, count() n, round(100.0*count()/(SELECT count() FROM census.aero_gauge_stake),1) pct,
           round(quantileExact(0.5)(sample_dispersion),3) med_disp
    FROM census.aero_gauge_stake GROUP BY confidence ORDER BY n DESC"""))

    print("--- cross-check: sampled pool.liquidity() vs census.pit_hourly L_med_h ---")
    print(C.ch("""
    SELECT count() n,
           round(quantileExact(0.5)(abs(s.pool_liquidity - c.pool_liquidity_pit)
                                     / nullIf(c.pool_liquidity_pit,0)), 4) AS med_rel_err,
           round(quantileExact(0.9)(abs(s.pool_liquidity - c.pool_liquidity_pit)
                                     / nullIf(c.pool_liquidity_pit,0)), 4) AS p90_rel_err
    FROM census.aero_gauge_stake s
    INNER JOIN census.aero_pool_epoch_ctx c
            ON c.pool = s.pool AND c.epoch_start = s.epoch_start
    WHERE c.pool_liquidity_pit > 0 AND s.pool_liquidity > 0"""))

    print("--- staked share of active liquidity ---")
    print(C.ch("""
    SELECT round(quantileExact(0.1)(staked_share),3) p10,
           round(quantileExact(0.5)(staked_share),3) p50,
           round(quantileExact(0.9)(staked_share),3) p90,
           countIf(staked_share > 1.0001) AS impossible_gt_1
    FROM census.aero_gauge_stake WHERE pool_liquidity > 0"""))


if __name__ == "__main__":
    main()
