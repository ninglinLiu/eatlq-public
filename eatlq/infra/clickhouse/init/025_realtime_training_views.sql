-- Phase 3.5: Real-time model training views
-- Computes the same 4 features the FeatureStore produces at runtime,
-- but from historical decoded swap data in 5-minute windows.

DROP VIEW IF EXISTS eatlq.pool_realtime_training_v1;
DROP VIEW IF EXISTS eatlq.pool_features_5m_sliding;
DROP VIEW IF EXISTS eatlq.pool_features_5m;

CREATE VIEW eatlq.pool_features_5m
AS
SELECT
    pool_address,
    chain_id,
    window_start,
    price_velocity_5m,
    buy_pressure_ratio,
    token_volatility_5m,
    activity_heat,
    unique_buyer_entropy_5m
FROM
(
    -- V2 / Aerodrome vAMM swaps
    SELECT
        pair_address AS pool_address,
        chain_id,
        toStartOfFiveMinutes(coalesce(block_time, received_at_ms)) AS window_start,

        toFloat64(sum(toFloat64(amount0_in) - toFloat64(amount0_out)))
            / greatest(sum(greatest(toFloat64(amount0_in), toFloat64(amount0_out))), 1)
            AS price_velocity_5m,

        sumIf(
            toFloat64(greatest(amount0_in, amount1_in, amount0_out, amount1_out)),
            amount0_out > amount0_in
        ) / greatest(
            sum(toFloat64(greatest(amount0_in, amount1_in, amount0_out, amount1_out))),
            1
        )
            AS buy_pressure_ratio,

        if(
            avg(toFloat64(greatest(amount0_in, amount1_in, amount0_out, amount1_out))) > 0,
            stddevPop(toFloat64(greatest(amount0_in, amount1_in, amount0_out, amount1_out)))
                / avg(toFloat64(greatest(amount0_in, amount1_in, amount0_out, amount1_out))),
            0
        ) AS token_volatility_5m,

        count() AS activity_heat,
        toFloat64(0.0) AS unique_buyer_entropy_5m

    FROM eatlq.uniswap_v2_swaps_decoded
    GROUP BY pair_address, chain_id, window_start
    HAVING count() >= 1

    UNION ALL

    -- V3 / Aerodrome CL swaps
    SELECT
        pool_address,
        chain_id,
        toStartOfFiveMinutes(coalesce(block_time, received_at_ms)) AS window_start,

        toFloat64(sum(-toFloat64(amount0)))
            / greatest(sum(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))), 1)
            AS price_velocity_5m,

        sumIf(
            greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1))),
            amount0 < 0
        ) / greatest(
            sum(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))),
            1
        )
            AS buy_pressure_ratio,

        if(
            avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))) > 0,
            stddevPop(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1))))
                / avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))),
            0
        ) AS token_volatility_5m,

        count() AS activity_heat,
        toFloat64(0.0) AS unique_buyer_entropy_5m

    FROM eatlq.uniswap_v3_swaps_decoded
    GROUP BY pool_address, chain_id, window_start
    HAVING count() >= 1
);

-- Alias view kept for train/serve parity migration.
-- Current implementation keeps 5m bucket semantics while upstream sliding view
-- backfill is being phased in.
CREATE VIEW eatlq.pool_features_5m_sliding
AS
SELECT *
FROM eatlq.pool_features_5m;

-- Labeled training dataset: each row is a 5-min feature window
-- with a binary label derived from average buy_pressure in the
-- following 10 minutes (next 2 windows).
-- INNER JOIN drops rows without future data (tail of dataset).
CREATE VIEW eatlq.pool_realtime_training_v1
AS
SELECT
    curr.pool_address,
    curr.chain_id,
    curr.window_start,
    curr.price_velocity_5m,
    curr.buy_pressure_ratio,
    curr.token_volatility_5m,
    toFloat64(curr.activity_heat) AS activity_heat,
    toUInt8(avg(fut.buy_pressure_ratio) > 0.5) AS label,
    avg(fut.buy_pressure_ratio) AS future_buy_pressure,
    sum(fut.activity_heat) AS future_activity
FROM eatlq.pool_features_5m_sliding AS curr
INNER JOIN eatlq.pool_features_5m_sliding AS fut
    ON curr.pool_address = fut.pool_address
    AND curr.chain_id = fut.chain_id
    AND fut.window_start > curr.window_start
    AND fut.window_start <= addMinutes(curr.window_start, 10)
WHERE curr.activity_heat >= 2
GROUP BY
    curr.pool_address,
    curr.chain_id,
    curr.window_start,
    curr.price_velocity_5m,
    curr.buy_pressure_ratio,
    curr.token_volatility_5m,
    curr.activity_heat,
    curr.unique_buyer_entropy_5m;
