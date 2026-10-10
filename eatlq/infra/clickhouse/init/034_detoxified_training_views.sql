-- Phase 3.6: De-toxified training views (mirror feature_store.py runtime filtering)
--
-- Root-cause fix for train-serve skew: the runtime `FeatureStore` excludes
-- toxic wallets (same wallet buying then selling within 60s on same pool),
-- but pool_features_5m / pool_realtime_training_v1 aggregated ALL swaps
-- including MEV bots, sandwich attackers, and market-maker wash trades.
-- On Base chain this was ~20% of all swap events, causing the model to
-- learn noise patterns absent from live feature distribution.
--
-- Design:
--   1. Tag each swap with `is_toxic_sell` via a window function that checks
--      for a same-wallet BUY on same pool in the prior 60 seconds.
--   2. Toxic wallet proxy = `recipient` (swap event's recipient topic).
--      This is the closest available EOA-like identifier in the decoded
--      tables; `sender` is the router/aggregator.
--   3. Exclude the TOXIC SELL leg (mirrors FeatureStore.update() semantics
--      exactly — see feature_store.py lines 244-253 + 293-296).
--   4. Re-aggregate 5-minute features on the clean swap stream.
--
-- Entropy is fixed at 0.0 to match the v1 MV exactly (the runtime
-- FeatureStore computes real entropy, but v1 MV does not — keeping v2 MV
-- identical for apples-to-apples comparison).
--
-- Views created:
--   * eatlq.uniswap_swaps_clean_v1    -- per-swap view tagged with is_toxic_sell
--   * eatlq.pool_features_5m_clean    -- 5-min features on clean stream
--   * eatlq.pool_realtime_training_v2 -- training dataset (same labels as v1)

DROP VIEW IF EXISTS eatlq.pool_realtime_training_v2;
DROP VIEW IF EXISTS eatlq.pool_features_5m_clean;
DROP VIEW IF EXISTS eatlq.uniswap_swaps_clean_v1;

-- ---------------------------------------------------------------------------
-- Per-swap view: every v2/v3 swap tagged with `is_toxic_sell`.
-- A sell is toxic iff same (chain, pool, recipient) had a BUY in prior 60s.
-- ---------------------------------------------------------------------------
CREATE VIEW eatlq.uniswap_swaps_clean_v1
AS
WITH unioned AS
(
    -- V2 / Aerodrome-vAMM
    -- Use coalesce(block_time, received_at_ms) to match pool_features_5m
    -- semantics; most rows have NULL block_time in the decoded table.
    SELECT
        chain_id,
        pair_address AS pool_address,
        coalesce(block_time, received_at_ms) AS event_time,
        toUnixTimestamp(coalesce(block_time, received_at_ms)) AS ts_sec,
        coalesce(recipient, '') AS wallet,
        (amount0_out > amount0_in) AS is_buy,
        toFloat64(amount0_in) AS a0_in,
        toFloat64(amount1_in) AS a1_in,
        toFloat64(amount0_out) AS a0_out,
        toFloat64(amount1_out) AS a1_out
    FROM eatlq.uniswap_v2_swaps_decoded

    UNION ALL

    -- V3 / Aerodrome-CL: amount0 < 0 means token0 left the pool (buy for token0)
    SELECT
        chain_id,
        pool_address,
        coalesce(block_time, received_at_ms) AS event_time,
        toUnixTimestamp(coalesce(block_time, received_at_ms)) AS ts_sec,
        coalesce(recipient, '') AS wallet,
        (amount0 < 0) AS is_buy,
        toFloat64(if(amount0 > 0, amount0, 0)) AS a0_in,
        toFloat64(if(amount1 > 0, amount1, 0)) AS a1_in,
        toFloat64(if(amount0 < 0, -amount0, 0)) AS a0_out,
        toFloat64(if(amount1 < 0, -amount1, 0)) AS a1_out
    FROM eatlq.uniswap_v3_swaps_decoded
)
SELECT
    chain_id,
    pool_address,
    event_time AS block_time,
    ts_sec,
    wallet,
    is_buy,
    a0_in,
    a1_in,
    a0_out,
    a1_out,
    max(if(is_buy AND wallet != '', ts_sec, toUInt32(0))) OVER (
        PARTITION BY chain_id, pool_address, wallet
        ORDER BY ts_sec
        RANGE BETWEEN 60 PRECEDING AND 1 PRECEDING
    ) AS last_buy_ts_60s,
    (
        (NOT is_buy)
        AND (wallet != '')
        AND (max(if(is_buy AND wallet != '', ts_sec, toUInt32(0))) OVER (
                PARTITION BY chain_id, pool_address, wallet
                ORDER BY ts_sec
                RANGE BETWEEN 60 PRECEDING AND 1 PRECEDING
            ) > 0)
    ) AS is_toxic_sell
FROM unioned;

-- ---------------------------------------------------------------------------
-- De-toxified 5-minute features. Matches pool_features_5m exactly except
-- the toxic-sell leg is excluded from aggregation.
-- ---------------------------------------------------------------------------
CREATE VIEW eatlq.pool_features_5m_clean
AS
SELECT
    pool_address,
    chain_id,
    toStartOfFiveMinutes(block_time) AS window_start,

    toFloat64(sum(a0_in - a0_out))
        / greatest(sum(greatest(a0_in, a1_in, a0_out, a1_out)), 1)
        AS price_velocity_5m,

    sumIf(
        greatest(a0_in, a1_in, a0_out, a1_out),
        is_buy
    ) / greatest(
        sum(greatest(a0_in, a1_in, a0_out, a1_out)),
        1
    ) AS buy_pressure_ratio,

    if(
        avg(greatest(a0_in, a1_in, a0_out, a1_out)) > 0,
        stddevPop(greatest(a0_in, a1_in, a0_out, a1_out))
            / avg(greatest(a0_in, a1_in, a0_out, a1_out)),
        0
    ) AS token_volatility_5m,

    count() AS activity_heat,

    toFloat64(0.0) AS unique_buyer_entropy_5m
FROM eatlq.uniswap_swaps_clean_v1
WHERE NOT is_toxic_sell
GROUP BY pool_address, chain_id, window_start
HAVING count() >= 1;

-- ---------------------------------------------------------------------------
-- Labeled training dataset (same semantics as pool_realtime_training_v1 but
-- built on the clean feature stream).
-- ---------------------------------------------------------------------------
CREATE VIEW eatlq.pool_realtime_training_v2
AS
SELECT
    curr.pool_address,
    curr.chain_id,
    curr.window_start,
    curr.price_velocity_5m,
    curr.buy_pressure_ratio,
    curr.token_volatility_5m,
    toFloat64(curr.activity_heat) AS activity_heat,
    curr.unique_buyer_entropy_5m,
    toUInt8(avg(fut.buy_pressure_ratio) > 0.5) AS label,
    avg(fut.buy_pressure_ratio) AS future_buy_pressure,
    sum(fut.activity_heat) AS future_activity
FROM eatlq.pool_features_5m_clean AS curr
INNER JOIN eatlq.pool_features_5m_clean AS fut
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
