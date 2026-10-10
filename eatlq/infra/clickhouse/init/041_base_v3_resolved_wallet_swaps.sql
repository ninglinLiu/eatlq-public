-- Research-only Base V3 resolved-wallet attribution layer.
-- Exclusions are synced from configs/research/base_attribution_exclusions.json.
-- Arrival time is never a fallback: block_time is map or archiver only.

CREATE TABLE IF NOT EXISTS eatlq.base_wallet_attribution_exclusions
(
    chain_id UInt64,
    address String,
    category LowCardinality(String),
    label String,
    schema_version UInt16,
    synced_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(synced_at)
ORDER BY (chain_id, address);

DROP VIEW IF EXISTS eatlq.base_v3_wallet_swaps_resolved;

CREATE VIEW eatlq.base_v3_wallet_swaps_resolved AS
WITH
    excluded AS (
        SELECT
            groupArray(lower(address)) AS addresses,
            groupArrayIf(lower(address), category = 'quote_token') AS quote_addresses
        FROM eatlq.base_wallet_attribution_exclusions FINAL
        WHERE chain_id = 8453
    ),
    tx_from AS (
        SELECT
            chain_id AS tf_chain_id,
            lower(tx_hash) AS tf_tx_hash,
            anyLast(lower(tx_from)) AS tx_from
        FROM eatlq.tx_from_by_hash
        GROUP BY tf_chain_id, tf_tx_hash
    ),
    block_times AS (
        SELECT
            chain_id AS bt_chain_id,
            block_number AS bt_block_number,
            anyLast(block_time) AS map_block_time
        FROM eatlq.block_ts_map FINAL
        GROUP BY bt_chain_id, bt_block_number
    ),
    pool_meta AS (
        SELECT
            chain_id AS pm_chain_id,
            lower(pool_address) AS pm_pool_address,
            anyLast(lower(token0_address)) AS token0_address,
            anyLast(lower(token1_address)) AS token1_address,
            anyLast(token0_decimals) AS token0_decimals,
            anyLast(token1_decimals) AS token1_decimals
        FROM eatlq.discovered_pools FINAL
        GROUP BY pm_chain_id, pm_pool_address
    ),
    enriched AS (
        SELECT
            s.*,
            tf.tx_from,
            pm.token0_address,
            pm.token1_address,
            pm.token0_decimals,
            pm.token1_decimals,
            if(bt.bt_block_number != 0, toNullable(bt.map_block_time), s.block_time)
                AS resolved_block_time,
            if(bt.bt_block_number != 0, 'block_ts_map',
               if(s.block_time IS NOT NULL, 'archiver_blockTimestamp', 'missing')) AS block_time_source,
            ex.addresses AS excluded_addresses,
            ex.quote_addresses AS quote_addresses,
            match(lower(tf.tx_from), '^0x[0-9a-f]{40}$')
                AND NOT has(ex.addresses, lower(tf.tx_from)) AS valid_tx_from,
            match(lower(s.recipient), '^0x[0-9a-f]{40}$')
                AND NOT has(ex.addresses, lower(s.recipient)) AS valid_recipient,
            match(lower(s.sender), '^0x[0-9a-f]{40}$')
                AND NOT has(ex.addresses, lower(s.sender)) AS valid_sender
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        CROSS JOIN excluded AS ex
        LEFT JOIN tx_from AS tf
            ON s.chain_id = tf.tf_chain_id AND lower(s.tx_hash) = tf.tf_tx_hash
        LEFT JOIN block_times AS bt
            ON s.chain_id = bt.bt_chain_id AND s.block_number = bt.bt_block_number
        LEFT JOIN pool_meta AS pm
            ON s.chain_id = pm.pm_chain_id AND lower(s.pool_address) = pm.pm_pool_address
        WHERE s.chain_id = 8453
    )
SELECT
    enriched.* EXCEPT (excluded_addresses, quote_addresses),
    multiIf(
        valid_tx_from, lower(tx_from),
        valid_recipient, lower(recipient),
        valid_sender, lower(sender),
        ''
    ) AS resolved_eoa,
    multiIf(valid_tx_from, 'tx_from', valid_recipient, 'recipient', valid_sender, 'sender', 'unresolved')
        AS attribution_source,
    toUInt8(valid_tx_from) AS has_tx_from,
    multiIf(
        has(excluded_addresses, lower(sender)), 'excluded_infrastructure',
        lower(sender) = resolved_eoa, 'same_as_resolved',
        'different_from_resolved'
    ) AS old_sender_status,
    multiIf(
        has(quote_addresses, token0_address) AND NOT has(quote_addresses, token1_address), token1_address,
        has(quote_addresses, token1_address) AND NOT has(quote_addresses, token0_address), token0_address,
        ''
    ) AS target_token_address,
    multiIf(
        has(quote_addresses, token0_address) AND NOT has(quote_addresses, token1_address), token1_decimals,
        has(quote_addresses, token1_address) AND NOT has(quote_addresses, token0_address), token0_decimals,
        toUInt8(0)
    ) AS target_token_decimals,
    multiIf(
        has(quote_addresses, token0_address) AND NOT has(quote_addresses, token1_address), -amount1,
        has(quote_addresses, token1_address) AND NOT has(quote_addresses, token0_address), -amount0,
        toInt256(0)
    ) AS resolved_wallet_target_delta_raw,
    multiIf(
        has(quote_addresses, token0_address) AND NOT has(quote_addresses, token1_address),
            if(amount1 < 0, 'buy', if(amount1 > 0, 'sell', 'unknown')),
        has(quote_addresses, token1_address) AND NOT has(quote_addresses, token0_address),
            if(amount0 < 0, 'buy', if(amount0 > 0, 'sell', 'unknown')),
        'unknown'
    ) AS target_trade_side
FROM enriched;
