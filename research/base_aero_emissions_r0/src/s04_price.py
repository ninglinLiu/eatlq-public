"""Step 5: hourly AERO/USD from on-chain CL swaps.

Two independent quote legs:
  * AERO/USDC pools  -> direct USD (no ETH dependency)
  * AERO/WETH pools  -> USD via edge.eth_usd_1h
Final aero_usd = median across all pool legs available in the hour.
"""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"

# (pool, quote, venue, label) -- AERO is token1 in every one of these.
POOLS = [
    ("0xe5b5f522e98b5a2baae212d4da66b865b781db97", "USDC", "univ3", "USDC/AERO 0.05%"),
    ("0xa4fdd479eda160671636e2ecf8f993cbf86258a8", "USDC", "aero_cl", "USDC/AERO ts100"),
    ("0xbe00ff35af70e8415d0eb605a286d8a45466a4c1", "USDC", "aero_cl", "USDC/AERO ts2000"),
    ("0x2426dc0a657bd481ab48f86c1616431905901238", "USDC", "univ3", "USDC/AERO 0.30%"),
    ("0x82321f3beb69f503380d6b233857d5c43562e2d0", "WETH", "aero_cl", "WETH/AERO ts200"),
    ("0x3d5d143381916280ff91407febeb52f2b60f33cf", "WETH", "univ3", "WETH/AERO 0.30%"),
    ("0x0d5959a52e7004b601f0be70618d01ac3cdce976", "WETH", "univ3", "WETH/AERO 1.00%"),
]

MIN_SWAPS_PER_BAR = 3

# Thinnest leg (~72k swaps, ~42% bar coverage, persistently ~7% off the other six).
# Kept in the leg table for diagnostics but excluded from the consensus median.
THIN_LEG = "WETH/AERO 1.00%"


def main():
    # One INSERT per pool: census.cl_swaps is ORDER BY (pool, ...) so each is an
    # index-range read. A single 7-way UNION + JOIN blew past available memory.
    C.ch("DROP TABLE IF EXISTS census.aero_price_leg_hourly")
    C.ch("""
    CREATE TABLE census.aero_price_leg_hourly (
        bar DateTime, pool String, quote String, label String,
        n_swaps UInt32, px_native Float64, aero_usd Float64
    ) ENGINE = MergeTree ORDER BY (bar, pool)
    """)
    for pool, quote, venue, label in POOLS:
        # price_raw = (sqrtP/2^96)^2 = token1_raw per token0_raw (AERO is token1 in all)
        if quote == "USDC":   # 1/(price_raw * 10^(6-18))
            px = "1e12 / nullIf(pow(toFloat64(sqrt_price_x96) / pow(2, 96), 2), 0)"
            usd = "l.px_native"
        else:                 # WETH, both 18 decimals
            px = "1.0 / nullIf(pow(toFloat64(sqrt_price_x96) / pow(2, 96), 2), 0)"
            usd = "l.px_native * e.eth_usd"
        C.ch(f"""
        INSERT INTO census.aero_price_leg_hourly
        SELECT l.bar, '{pool}', '{quote}', '{label}', l.n, l.px_native, {usd} AS aero_usd
        FROM (
            SELECT toStartOfHour(block_time) AS bar,
                   quantileExact(0.5)({px}) AS px_native,
                   toUInt32(count()) AS n
            FROM census.cl_swaps
            WHERE pool = '{pool}' AND sqrt_price_x96 > 0
              AND block_time >= toDateTime({C.PANEL_TS_FROM})
              AND block_time < toDateTime({C.PANEL_TS_TO})
            GROUP BY bar HAVING n >= {MIN_SWAPS_PER_BAR}
        ) AS l
        LEFT JOIN edge.eth_usd_1h AS e ON e.bar = l.bar
        WHERE {usd} > 0
        """)
        print(f"  leg done: {label}", flush=True)

    print(C.ch("""
    SELECT label, quote, count() bars, min(bar) minb, max(bar) maxb,
           round(quantileExact(0.5)(aero_usd), 6) med_usd
    FROM census.aero_price_leg_hourly GROUP BY label, quote ORDER BY bars DESC"""))

    # ---- consensus price: median across legs, plus dispersion diagnostics
    C.ch("DROP TABLE IF EXISTS census.aero_price_hourly")
    C.ch("""
    CREATE TABLE census.aero_price_hourly (
        bar DateTime,
        aero_usd Float64,
        source String,
        n_pools UInt8,
        n_usdc_pools UInt8,
        n_weth_pools UInt8,
        spread_bps Float64,
        usdc_leg Float64,
        weth_leg Float64,
        cross_check_bps Float64
    ) ENGINE = MergeTree ORDER BY bar
    """)
    src = ("median across on-chain CL pools of hourly-median sqrtPriceX96; "
           "USDC legs direct USD, WETH legs x edge.eth_usd_1h")
    C.ch(f"""
    INSERT INTO census.aero_price_hourly
    SELECT bar, px_med, '{src}',
           n_pools, n_usdc, n_weth,
           if(n_pools > 1, 1e4 * (px_max - px_min) / px_med, 0) AS spread_bps,
           usdc_leg, weth_leg,
           if(n_usdc > 0 AND n_weth > 0,
              1e4 * abs(usdc_leg - weth_leg) / usdc_leg, 0) AS cross_check_bps
    FROM (
        SELECT bar,
               quantileExact(0.5)(aero_usd) AS px_med,
               min(aero_usd) AS px_min, max(aero_usd) AS px_max,
               toUInt8(count()) AS n_pools,
               toUInt8(countIf(quote = 'USDC')) AS n_usdc,
               toUInt8(countIf(quote = 'WETH')) AS n_weth,
               quantileExactIf(0.5)(aero_usd, quote = 'USDC') AS usdc_leg,
               quantileExactIf(0.5)(aero_usd, quote = 'WETH') AS weth_leg
        FROM census.aero_price_leg_hourly
        WHERE label != '{THIN_LEG}'
        GROUP BY bar
    )
    """)

    print(C.ch("""
    SELECT count() bars, min(bar) minb, max(bar) maxb,
           round(min(aero_usd),4) lo, round(quantileExact(0.5)(aero_usd),4) med, round(max(aero_usd),4) hi,
           round(avg(n_pools),2) avg_pools,
           round(quantileExact(0.5)(spread_bps),1) med_spread_bps,
           round(quantileExact(0.9)(spread_bps),1) p90_spread_bps,
           round(quantileExact(0.5)(cross_check_bps),1) med_usdc_vs_weth_bps,
           round(quantileExact(0.9)(cross_check_bps),1) p90_usdc_vs_weth_bps
    FROM census.aero_price_hourly"""))

    # pairwise disagreement between individual pools (median |bps|)
    print("--- pairwise pool disagreement (median bps) ---")
    print(C.ch("""
    SELECT a.label AS pool_a, b.label AS pool_b, count() AS shared_bars,
           round(quantileExact(0.5)(1e4 * abs(a.aero_usd - b.aero_usd) / a.aero_usd), 1) AS med_bps
    FROM census.aero_price_leg_hourly a
    INNER JOIN census.aero_price_leg_hourly b ON a.bar = b.bar
    WHERE a.pool < b.pool
    GROUP BY pool_a, pool_b HAVING shared_bars > 500 ORDER BY med_bps"""))

    # coverage gaps
    print("--- hourly coverage over panel ---")
    print(C.ch(f"""
    WITH toUInt32(({C.PANEL_TS_TO} - {C.PANEL_TS_FROM}) / 3600) AS expected
    SELECT expected AS expected_bars, count() AS have_bars,
           round(100.0 * count() / expected, 2) AS pct
    FROM census.aero_price_hourly
    WHERE bar >= toDateTime({C.PANEL_TS_FROM}) AND bar < toDateTime({C.PANEL_TS_TO})"""))


if __name__ == "__main__":
    main()
