-- 039_sender_token_daily_mv.sql
-- MANUAL APPLY ONLY — lightweight sender×token×day tick rows for wallet labels (OOM fix).
--
-- Apply:
--   docker exec -i eatlq-clickhouse-1 clickhouse-client --multiquery < infra/clickhouse/init/039_sender_token_daily_mv.sql
-- Backfill:
--   python3 scripts/wallet_profile_v0/backfill_sender_token_daily.py
--
-- Refresh policy: MV appends new swaps; backfill script fills history by day partition.

DROP TABLE IF EXISTS eatlq.sender_token_ticks_light_mv;

CREATE TABLE IF NOT EXISTS eatlq.sender_token_ticks_light
(
    day Date,
    chain_id UInt64,
    sender_address String,
    token_address String,
    side Enum8('buy' = 1, 'sell' = 2),
    tick Int64,
    block_time DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (chain_id, token_address, day, sender_address, block_time)
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.sender_token_ticks_light_mv
TO eatlq.sender_token_ticks_light
AS
SELECT
    toDate(s.block_time) AS day,
    s.chain_id AS chain_id,
    lower(
        if(
            length(tf.tx_from) = 42 AND startsWith(tf.tx_from, '0x'),
            tf.tx_from,
            s.sender
        )
    ) AS sender_address,
    if(
        s.amount0 < 0,
        if(lower(dp.token0_address) = lower('0x4200000000000000000000000000000000000006'),
           lower(dp.token1_address), lower(dp.token0_address)),
        if(lower(dp.token1_address) = lower('0x4200000000000000000000000000000000000006'),
           lower(dp.token0_address), lower(dp.token1_address))
    ) AS token_address,
    if(s.amount0 < 0, 'buy', 'sell') AS side,
    s.tick AS tick,
    s.block_time AS block_time
FROM eatlq.uniswap_v3_swaps_decoded AS s
LEFT JOIN (
    SELECT chain_id, tx_hash, anyLast(tx_from) AS tx_from
    FROM eatlq.tx_from_by_hash
    GROUP BY chain_id, tx_hash
) AS tf ON s.chain_id = tf.chain_id AND lower(s.tx_hash) = lower(tf.tx_hash)
INNER JOIN eatlq.discovered_pools AS dp FINAL
    ON lower(s.pool_address) = lower(dp.pool_address) AND s.chain_id = dp.chain_id
WHERE dp.has_weth_leg = 1
  AND s.tick != 0
  AND if(
        s.amount0 < 0,
        if(lower(dp.token0_address) = lower('0x4200000000000000000000000000000000000006'),
           lower(dp.token1_address), lower(dp.token0_address)),
        if(lower(dp.token1_address) = lower('0x4200000000000000000000000000000000000006'),
           lower(dp.token0_address), lower(dp.token1_address))
    ) != '';
