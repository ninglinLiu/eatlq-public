DROP VIEW IF EXISTS eatlq.uniswap_v3_swaps_trade_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v2_swaps_trade_semantic;

CREATE VIEW eatlq.uniswap_v2_swaps_trade_semantic
AS
SELECT
    s.chain_id AS chain_id,
    s.source AS source,
    s.block_time AS block_time,
    s.received_at_ms AS received_at_ms,
    s.block_date AS block_date,
    s.block_number AS block_number,
    s.tx_hash AS tx_hash,
    s.log_index AS log_index,
    s.pair_address AS pair_address,
    s.pair_label AS pair_label,
    s.token0_address AS token0_address,
    s.token1_address AS token1_address,
    s.token0_symbol AS token0_symbol,
    s.token1_symbol AS token1_symbol,
    s.token0_decimals AS token0_decimals,
    s.token1_decimals AS token1_decimals,
    s.fee_bps AS fee_bps,
    s.is_curated AS is_curated,
    s.has_registry_match AS has_registry_match,
    s.sender AS sender,
    s.recipient AS recipient,
    multiIf(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0, 'token0_to_token1',
        s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0, 'token1_to_token0',
        'complex'
    ) AS swap_direction,
    toUInt8(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0
        OR s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0
    ) AS has_clear_direction,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.token0_address,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.token1_address,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS sold_token_address,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.token0_symbol,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.token1_symbol,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS sold_token_symbol,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        CAST(s.token0_decimals, 'Nullable(UInt8)'),
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            CAST(s.token1_decimals, 'Nullable(UInt8)'),
            CAST(NULL, 'Nullable(UInt8)')
        )
    ) AS sold_token_decimals,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.token1_address,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.token0_address,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS bought_token_address,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.token1_symbol,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.token0_symbol,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS bought_token_symbol,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        CAST(s.token1_decimals, 'Nullable(UInt8)'),
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            CAST(s.token0_decimals, 'Nullable(UInt8)'),
            CAST(NULL, 'Nullable(UInt8)')
        )
    ) AS bought_token_decimals,
    s.amount0_in AS amount0_in,
    s.amount0_in_normalized AS amount0_in_normalized,
    s.amount1_in AS amount1_in,
    s.amount1_in_normalized AS amount1_in_normalized,
    s.amount0_out AS amount0_out,
    s.amount0_out_normalized AS amount0_out_normalized,
    s.amount1_out AS amount1_out,
    s.amount1_out_normalized AS amount1_out_normalized,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.amount0_in_normalized,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.amount1_in_normalized,
            CAST(NULL, 'Nullable(Float64)')
        )
    ) AS sold_amount_normalized,
    if(
        s.amount0_in > 0 AND s.amount1_out > 0 AND s.amount1_in = 0 AND s.amount0_out = 0,
        s.amount1_out_normalized,
        if(
            s.amount1_in > 0 AND s.amount0_out > 0 AND s.amount0_in = 0 AND s.amount1_out = 0,
            s.amount0_out_normalized,
            CAST(NULL, 'Nullable(Float64)')
        )
    ) AS bought_amount_normalized,
    s.removed AS removed,
    s.payload_json AS payload_json,
    s.latest_ingested_at AS latest_ingested_at,
    s.duplicate_rows AS duplicate_rows
FROM eatlq.uniswap_v2_swaps_semantic AS s;

CREATE VIEW eatlq.uniswap_v3_swaps_trade_semantic
AS
SELECT
    s.chain_id AS chain_id,
    s.source AS source,
    s.block_time AS block_time,
    s.received_at_ms AS received_at_ms,
    s.block_date AS block_date,
    s.block_number AS block_number,
    s.tx_hash AS tx_hash,
    s.log_index AS log_index,
    s.pool_address AS pool_address,
    s.pool_label AS pool_label,
    s.token0_address AS token0_address,
    s.token1_address AS token1_address,
    s.token0_symbol AS token0_symbol,
    s.token1_symbol AS token1_symbol,
    s.token0_decimals AS token0_decimals,
    s.token1_decimals AS token1_decimals,
    s.fee_bps AS fee_bps,
    s.is_curated AS is_curated,
    s.has_registry_match AS has_registry_match,
    s.sender AS sender,
    s.recipient AS recipient,
    multiIf(
        s.amount0 > 0 AND s.amount1 < 0, 'token0_to_token1',
        s.amount1 > 0 AND s.amount0 < 0, 'token1_to_token0',
        'complex'
    ) AS swap_direction,
    toUInt8(
        s.amount0 > 0 AND s.amount1 < 0
        OR s.amount1 > 0 AND s.amount0 < 0
    ) AS has_clear_direction,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        s.token0_address,
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            s.token1_address,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS sold_token_address,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        s.token0_symbol,
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            s.token1_symbol,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS sold_token_symbol,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        CAST(s.token0_decimals, 'Nullable(UInt8)'),
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            CAST(s.token1_decimals, 'Nullable(UInt8)'),
            CAST(NULL, 'Nullable(UInt8)')
        )
    ) AS sold_token_decimals,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        s.token1_address,
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            s.token0_address,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS bought_token_address,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        s.token1_symbol,
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            s.token0_symbol,
            CAST(NULL, 'Nullable(String)')
        )
    ) AS bought_token_symbol,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        CAST(s.token1_decimals, 'Nullable(UInt8)'),
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            CAST(s.token0_decimals, 'Nullable(UInt8)'),
            CAST(NULL, 'Nullable(UInt8)')
        )
    ) AS bought_token_decimals,
    s.amount0 AS amount0,
    s.amount0_normalized AS amount0_normalized,
    s.amount1 AS amount1,
    s.amount1_normalized AS amount1_normalized,
    s.sqrt_price_x96 AS sqrt_price_x96,
    s.liquidity AS liquidity,
    s.tick AS tick,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        s.amount0_normalized,
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            s.amount1_normalized,
            CAST(NULL, 'Nullable(Float64)')
        )
    ) AS sold_amount_normalized,
    if(
        s.amount0 > 0 AND s.amount1 < 0,
        abs(s.amount1_normalized),
        if(
            s.amount1 > 0 AND s.amount0 < 0,
            abs(s.amount0_normalized),
            CAST(NULL, 'Nullable(Float64)')
        )
    ) AS bought_amount_normalized,
    s.removed AS removed,
    s.payload_json AS payload_json,
    s.latest_ingested_at AS latest_ingested_at,
    s.duplicate_rows AS duplicate_rows
FROM eatlq.uniswap_v3_swaps_semantic AS s;
