DROP VIEW IF EXISTS eatlq.sender_token_trade_profile;
DROP VIEW IF EXISTS eatlq.sender_swap_profile;
DROP VIEW IF EXISTS eatlq.swap_trade_events_all;

CREATE VIEW eatlq.swap_trade_events_all
AS
SELECT
    'uniswap_v2' AS protocol_family,
    v2.chain_id,
    v2.pair_address AS venue_address,
    v2.pair_label AS venue_label,
    v2.fee_bps,
    v2.sender AS sender_address,
    v2.recipient AS recipient_address,
    v2.tx_hash,
    v2.log_index,
    v2.swap_direction,
    v2.sold_token_address,
    v2.sold_token_symbol,
    v2.sold_token_decimals,
    v2.sold_amount_normalized,
    v2.bought_token_address,
    v2.bought_token_symbol,
    v2.bought_token_decimals,
    v2.bought_amount_normalized,
    v2.block_time,
    v2.received_at_ms,
    v2.latest_ingested_at,
    v2.duplicate_rows
FROM (
    SELECT
        chain_id,
        pair_address,
        pair_label,
        fee_bps,
        sender,
        recipient,
        tx_hash,
        log_index,
        swap_direction,
        sold_token_address,
        sold_token_symbol,
        sold_token_decimals,
        sold_amount_normalized,
        bought_token_address,
        bought_token_symbol,
        bought_token_decimals,
        bought_amount_normalized,
        block_time,
        received_at_ms,
        latest_ingested_at,
        duplicate_rows
    FROM eatlq.uniswap_v2_swaps_trade_semantic
    WHERE has_registry_match = 1
      AND has_clear_direction = 1
) AS v2

UNION ALL

SELECT
    'uniswap_v3' AS protocol_family,
    v3.chain_id,
    v3.pool_address AS venue_address,
    v3.pool_label AS venue_label,
    v3.fee_bps,
    v3.sender AS sender_address,
    v3.recipient AS recipient_address,
    v3.tx_hash,
    v3.log_index,
    v3.swap_direction,
    v3.sold_token_address,
    v3.sold_token_symbol,
    v3.sold_token_decimals,
    v3.sold_amount_normalized,
    v3.bought_token_address,
    v3.bought_token_symbol,
    v3.bought_token_decimals,
    v3.bought_amount_normalized,
    v3.block_time,
    v3.received_at_ms,
    v3.latest_ingested_at,
    v3.duplicate_rows
FROM (
    SELECT
        chain_id,
        pool_address,
        pool_label,
        fee_bps,
        sender,
        recipient,
        tx_hash,
        log_index,
        swap_direction,
        sold_token_address,
        sold_token_symbol,
        sold_token_decimals,
        sold_amount_normalized,
        bought_token_address,
        bought_token_symbol,
        bought_token_decimals,
        bought_amount_normalized,
        block_time,
        received_at_ms,
        latest_ingested_at,
        duplicate_rows
    FROM eatlq.uniswap_v3_swaps_trade_semantic
    WHERE has_registry_match = 1
      AND has_clear_direction = 1
) AS v3;

CREATE VIEW eatlq.sender_swap_profile
AS
SELECT
    chain_id,
    sender_address,
    count() AS trade_count,
    countIf(protocol_family = 'uniswap_v2') AS v2_trade_count,
    countIf(protocol_family = 'uniswap_v3') AS v3_trade_count,
    uniqExact(venue_address) AS distinct_venues,
    uniqExact(sold_token_address) AS distinct_sold_assets,
    uniqExact(bought_token_address) AS distinct_bought_assets,
    min(block_time) AS first_trade_at,
    max(block_time) AS latest_trade_at,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.swap_trade_events_all
GROUP BY chain_id, sender_address;

CREATE VIEW eatlq.sender_token_trade_profile
AS
SELECT
    chain_id,
    sender_address,
    token_address,
    argMax(token_symbol, row_latest_ingested_at) AS token_symbol,
    argMax(token_decimals, row_latest_ingested_at) AS token_decimals,
    count() AS trade_touch_count,
    countIf(token_role = 'sold') AS sold_trade_count,
    countIf(token_role = 'bought') AS bought_trade_count,
    sumIf(amount_normalized, token_role = 'sold') AS total_sold_amount_normalized,
    sumIf(amount_normalized, token_role = 'bought') AS total_bought_amount_normalized,
    sumIf(if(token_role = 'bought', amount_normalized, -amount_normalized), 1) AS net_amount_normalized,
    min(block_time) AS first_trade_at,
    max(block_time) AS latest_trade_at,
    max(row_latest_ingested_at) AS latest_ingested_at
FROM (
    SELECT
        chain_id,
        sender_address,
        sold_token_address AS token_address,
        sold_token_symbol AS token_symbol,
        sold_token_decimals AS token_decimals,
        'sold' AS token_role,
        sold_amount_normalized AS amount_normalized,
        block_time,
        latest_ingested_at AS row_latest_ingested_at
    FROM eatlq.swap_trade_events_all

    UNION ALL

    SELECT
        chain_id,
        sender_address,
        bought_token_address AS token_address,
        bought_token_symbol AS token_symbol,
        bought_token_decimals AS token_decimals,
        'bought' AS token_role,
        bought_amount_normalized AS amount_normalized,
        block_time,
        latest_ingested_at AS row_latest_ingested_at
    FROM eatlq.swap_trade_events_all
)
GROUP BY chain_id, sender_address, token_address;
