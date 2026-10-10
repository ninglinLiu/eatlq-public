DROP VIEW IF EXISTS eatlq.uniswap_v3_burns_decoded;
DROP VIEW IF EXISTS eatlq.uniswap_v3_mints_decoded;
DROP VIEW IF EXISTS eatlq.uniswap_v2_burns_decoded;
DROP VIEW IF EXISTS eatlq.uniswap_v2_mints_decoded;
DROP VIEW IF EXISTS eatlq.uniswap_v3_swaps_decoded;
DROP VIEW IF EXISTS eatlq.uniswap_v2_swaps_decoded;
DROP VIEW IF EXISTS eatlq.erc20_transfers_decoded;

DROP FUNCTION IF EXISTS eatlq_hex_to_int256;
DROP FUNCTION IF EXISTS eatlq_hex_to_uint256;

CREATE FUNCTION eatlq_hex_to_uint256 AS (hex_value) ->
    reinterpretAsUInt256(
        reverse(
            unhex(
                if(startsWith(hex_value, '0x'), substring(hex_value, 3), hex_value)
            )
        )
    );

CREATE FUNCTION eatlq_hex_to_int256 AS (hex_value) ->
    reinterpretAsInt256(
        reverse(
            unhex(
                if(startsWith(hex_value, '0x'), substring(hex_value, 3), hex_value)
            )
        )
    );

CREATE VIEW eatlq.erc20_transfers_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    token_address,
    sender,
    recipient,
    amount_raw,
    eatlq_hex_to_uint256(amount_raw) AS amount,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.erc20_transfers_deduped;

CREATE VIEW eatlq.uniswap_v2_swaps_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    recipient,
    amount0_in_raw,
    eatlq_hex_to_uint256(amount0_in_raw) AS amount0_in,
    amount1_in_raw,
    eatlq_hex_to_uint256(amount1_in_raw) AS amount1_in,
    amount0_out_raw,
    eatlq_hex_to_uint256(amount0_out_raw) AS amount0_out,
    amount1_out_raw,
    eatlq_hex_to_uint256(amount1_out_raw) AS amount1_out,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v2_swaps_deduped;

CREATE VIEW eatlq.uniswap_v2_mints_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    amount0_raw,
    eatlq_hex_to_uint256(amount0_raw) AS amount0,
    amount1_raw,
    eatlq_hex_to_uint256(amount1_raw) AS amount1,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v2_mints_deduped;

CREATE VIEW eatlq.uniswap_v2_burns_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    recipient,
    amount0_raw,
    eatlq_hex_to_uint256(amount0_raw) AS amount0,
    amount1_raw,
    eatlq_hex_to_uint256(amount1_raw) AS amount1,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v2_burns_deduped;

CREATE VIEW eatlq.uniswap_v3_swaps_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    sender,
    recipient,
    amount0_raw,
    eatlq_hex_to_int256(amount0_raw) AS amount0,
    amount1_raw,
    eatlq_hex_to_int256(amount1_raw) AS amount1,
    sqrt_price_x96_raw,
    eatlq_hex_to_uint256(sqrt_price_x96_raw) AS sqrt_price_x96,
    liquidity_raw,
    eatlq_hex_to_uint256(liquidity_raw) AS liquidity,
    tick_raw,
    toInt32(eatlq_hex_to_int256(tick_raw)) AS tick,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v3_swaps_deduped;

CREATE VIEW eatlq.uniswap_v3_mints_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    sender,
    owner,
    tick_lower_raw,
    toInt32(eatlq_hex_to_int256(tick_lower_raw)) AS tick_lower,
    tick_upper_raw,
    toInt32(eatlq_hex_to_int256(tick_upper_raw)) AS tick_upper,
    liquidity_raw,
    eatlq_hex_to_uint256(liquidity_raw) AS liquidity,
    amount0_raw,
    eatlq_hex_to_uint256(amount0_raw) AS amount0,
    amount1_raw,
    eatlq_hex_to_uint256(amount1_raw) AS amount1,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v3_mints_deduped;

CREATE VIEW eatlq.uniswap_v3_burns_decoded
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_date,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    owner,
    tick_lower_raw,
    toInt32(eatlq_hex_to_int256(tick_lower_raw)) AS tick_lower,
    tick_upper_raw,
    toInt32(eatlq_hex_to_int256(tick_upper_raw)) AS tick_upper,
    liquidity_raw,
    eatlq_hex_to_uint256(liquidity_raw) AS liquidity,
    amount0_raw,
    eatlq_hex_to_uint256(amount0_raw) AS amount0,
    amount1_raw,
    eatlq_hex_to_uint256(amount1_raw) AS amount1,
    removed,
    payload_json,
    latest_ingested_at,
    duplicate_rows
FROM eatlq.uniswap_v3_burns_deduped;
