-- 30-minute forward-looking training view.
-- Labels based on actual price movement (price_velocity) over next 30 minutes,
-- accounting for round-trip trading costs (~0.6%).
-- This replaces the self-referential "score > 0.5" label.

DROP VIEW IF EXISTS eatlq.pool_realtime_training_30m_v1;

CREATE VIEW eatlq.pool_realtime_training_30m_v1
AS
SELECT
    curr.pool_address,
    curr.chain_id,
    curr.window_start,
    curr.price_velocity_5m,
    curr.buy_pressure_ratio,
    curr.token_volatility_5m,
    curr.unique_buyer_entropy_5m,
    toFloat64(curr.activity_heat) AS activity_heat,
    -- Label: 1 if average future price velocity is positive enough
    -- to overcome ~0.6% round-trip cost (buy fee + sell fee).
    -- price_velocity is in [-1, 1]; a sustained 0.02 over 30 min
    -- roughly corresponds to >1% price appreciation.
    toUInt8(
        avg(fut.price_velocity_5m) > 0.02
        AND avg(fut.buy_pressure_ratio) > 0.50
    ) AS label,
    avg(fut.price_velocity_5m) AS future_velocity_30m,
    avg(fut.buy_pressure_ratio) AS future_pressure_30m,
    sum(fut.activity_heat) AS future_activity_30m
FROM eatlq.pool_features_5m_sliding AS curr
INNER JOIN eatlq.pool_features_5m_sliding AS fut
    ON curr.pool_address = fut.pool_address
    AND curr.chain_id = fut.chain_id
    AND fut.window_start > curr.window_start
    AND fut.window_start <= addMinutes(curr.window_start, 30)
WHERE curr.activity_heat >= 2
GROUP BY
    curr.pool_address,
    curr.chain_id,
    curr.window_start,
    curr.price_velocity_5m,
    curr.buy_pressure_ratio,
    curr.token_volatility_5m,
    curr.unique_buyer_entropy_5m,
    curr.activity_heat
HAVING count() >= 2;
