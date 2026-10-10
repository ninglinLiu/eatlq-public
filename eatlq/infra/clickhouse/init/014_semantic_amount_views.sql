DROP VIEW IF EXISTS eatlq.uniswap_v3_burns_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v3_mints_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v3_swaps_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v2_burns_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v2_mints_semantic;
DROP VIEW IF EXISTS eatlq.uniswap_v2_swaps_semantic;
DROP VIEW IF EXISTS eatlq.erc20_transfers_semantic;

DROP FUNCTION IF EXISTS eatlq_scale_int256;
DROP FUNCTION IF EXISTS eatlq_scale_uint256;

CREATE FUNCTION eatlq_scale_uint256 AS (amount_value, decimals_value) ->
    if(
        isNull(decimals_value),
        CAST(NULL, 'Nullable(Float64)'),
        toFloat64(toString(amount_value)) / pow(10, toFloat64(decimals_value))
    );

CREATE FUNCTION eatlq_scale_int256 AS (amount_value, decimals_value) ->
    if(
        isNull(decimals_value),
        CAST(NULL, 'Nullable(Float64)'),
        toFloat64(toString(amount_value)) / pow(10, toFloat64(decimals_value))
    );

CREATE VIEW eatlq.erc20_transfers_semantic
AS
SELECT
    t.chain_id,
    t.source,
    t.block_time,
    t.received_at_ms,
    t.block_date,
    t.block_number,
    t.tx_hash,
    t.log_index,
    t.token_address,
    r.symbol AS token_symbol,
    r.name AS token_name,
    r.decimals AS token_decimals,
    r.is_core AS token_is_core,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    t.sender,
    t.recipient,
    t.amount_raw,
    t.amount,
    eatlq_scale_uint256(t.amount, r.decimals) AS amount_normalized,
    t.removed,
    t.payload_json,
    t.latest_ingested_at,
    t.duplicate_rows
FROM eatlq.erc20_transfers_decoded AS t
LEFT JOIN eatlq.token_registry AS r
    ON t.chain_id = r.chain_id AND t.token_address = r.token_address;

CREATE VIEW eatlq.uniswap_v2_swaps_semantic
AS
SELECT
    s.chain_id,
    s.source,
    s.block_time,
    s.received_at_ms,
    s.block_date,
    s.block_number,
    s.tx_hash,
    s.log_index,
    s.pair_address,
    p.pair_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    s.sender,
    s.recipient,
    s.amount0_in_raw,
    s.amount0_in,
    eatlq_scale_uint256(s.amount0_in, t0.decimals) AS amount0_in_normalized,
    s.amount1_in_raw,
    s.amount1_in,
    eatlq_scale_uint256(s.amount1_in, t1.decimals) AS amount1_in_normalized,
    s.amount0_out_raw,
    s.amount0_out,
    eatlq_scale_uint256(s.amount0_out, t0.decimals) AS amount0_out_normalized,
    s.amount1_out_raw,
    s.amount1_out,
    eatlq_scale_uint256(s.amount1_out, t1.decimals) AS amount1_out_normalized,
    s.removed,
    s.payload_json,
    s.latest_ingested_at,
    s.duplicate_rows
FROM eatlq.uniswap_v2_swaps_decoded AS s
LEFT JOIN eatlq.uniswap_v2_pair_registry AS p
    ON s.chain_id = p.chain_id AND s.pair_address = p.pair_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;

CREATE VIEW eatlq.uniswap_v2_mints_semantic
AS
SELECT
    m.chain_id,
    m.source,
    m.block_time,
    m.received_at_ms,
    m.block_date,
    m.block_number,
    m.tx_hash,
    m.log_index,
    m.pair_address,
    p.pair_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    m.sender,
    m.amount0_raw,
    m.amount0,
    eatlq_scale_uint256(m.amount0, t0.decimals) AS amount0_normalized,
    m.amount1_raw,
    m.amount1,
    eatlq_scale_uint256(m.amount1, t1.decimals) AS amount1_normalized,
    m.removed,
    m.payload_json,
    m.latest_ingested_at,
    m.duplicate_rows
FROM eatlq.uniswap_v2_mints_decoded AS m
LEFT JOIN eatlq.uniswap_v2_pair_registry AS p
    ON m.chain_id = p.chain_id AND m.pair_address = p.pair_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;

CREATE VIEW eatlq.uniswap_v2_burns_semantic
AS
SELECT
    b.chain_id,
    b.source,
    b.block_time,
    b.received_at_ms,
    b.block_date,
    b.block_number,
    b.tx_hash,
    b.log_index,
    b.pair_address,
    p.pair_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    b.sender,
    b.recipient,
    b.amount0_raw,
    b.amount0,
    eatlq_scale_uint256(b.amount0, t0.decimals) AS amount0_normalized,
    b.amount1_raw,
    b.amount1,
    eatlq_scale_uint256(b.amount1, t1.decimals) AS amount1_normalized,
    b.removed,
    b.payload_json,
    b.latest_ingested_at,
    b.duplicate_rows
FROM eatlq.uniswap_v2_burns_decoded AS b
LEFT JOIN eatlq.uniswap_v2_pair_registry AS p
    ON b.chain_id = p.chain_id AND b.pair_address = p.pair_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;

CREATE VIEW eatlq.uniswap_v3_swaps_semantic
AS
SELECT
    s.chain_id,
    s.source,
    s.block_time,
    s.received_at_ms,
    s.block_date,
    s.block_number,
    s.tx_hash,
    s.log_index,
    s.pool_address,
    p.pool_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    s.sender,
    s.recipient,
    s.amount0_raw,
    s.amount0,
    eatlq_scale_int256(s.amount0, t0.decimals) AS amount0_normalized,
    s.amount1_raw,
    s.amount1,
    eatlq_scale_int256(s.amount1, t1.decimals) AS amount1_normalized,
    s.sqrt_price_x96_raw,
    s.sqrt_price_x96,
    s.liquidity_raw,
    s.liquidity,
    s.tick_raw,
    s.tick,
    s.removed,
    s.payload_json,
    s.latest_ingested_at,
    s.duplicate_rows
FROM eatlq.uniswap_v3_swaps_decoded AS s
LEFT JOIN eatlq.uniswap_v3_pool_registry AS p
    ON s.chain_id = p.chain_id AND s.pool_address = p.pool_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;

CREATE VIEW eatlq.uniswap_v3_mints_semantic
AS
SELECT
    m.chain_id,
    m.source,
    m.block_time,
    m.received_at_ms,
    m.block_date,
    m.block_number,
    m.tx_hash,
    m.log_index,
    m.pool_address,
    p.pool_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    m.sender,
    m.owner,
    m.tick_lower_raw,
    m.tick_lower,
    m.tick_upper_raw,
    m.tick_upper,
    m.liquidity_raw,
    m.liquidity,
    m.amount0_raw,
    m.amount0,
    eatlq_scale_uint256(m.amount0, t0.decimals) AS amount0_normalized,
    m.amount1_raw,
    m.amount1,
    eatlq_scale_uint256(m.amount1, t1.decimals) AS amount1_normalized,
    m.removed,
    m.payload_json,
    m.latest_ingested_at,
    m.duplicate_rows
FROM eatlq.uniswap_v3_mints_decoded AS m
LEFT JOIN eatlq.uniswap_v3_pool_registry AS p
    ON m.chain_id = p.chain_id AND m.pool_address = p.pool_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;

CREATE VIEW eatlq.uniswap_v3_burns_semantic
AS
SELECT
    b.chain_id,
    b.source,
    b.block_time,
    b.received_at_ms,
    b.block_date,
    b.block_number,
    b.tx_hash,
    b.log_index,
    b.pool_address,
    p.pool_label,
    p.token0_address,
    p.token1_address,
    p.token0_symbol,
    p.token1_symbol,
    t0.decimals AS token0_decimals,
    t1.decimals AS token1_decimals,
    p.fee_bps,
    p.is_curated,
    p.registry_source,
    if(p.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    b.owner,
    b.tick_lower_raw,
    b.tick_lower,
    b.tick_upper_raw,
    b.tick_upper,
    b.liquidity_raw,
    b.liquidity,
    b.amount0_raw,
    b.amount0,
    eatlq_scale_uint256(b.amount0, t0.decimals) AS amount0_normalized,
    b.amount1_raw,
    b.amount1,
    eatlq_scale_uint256(b.amount1, t1.decimals) AS amount1_normalized,
    b.removed,
    b.payload_json,
    b.latest_ingested_at,
    b.duplicate_rows
FROM eatlq.uniswap_v3_burns_decoded AS b
LEFT JOIN eatlq.uniswap_v3_pool_registry AS p
    ON b.chain_id = p.chain_id AND b.pool_address = p.pool_address
LEFT JOIN eatlq.token_registry AS t0
    ON p.chain_id = t0.chain_id AND p.token0_address = t0.token_address
LEFT JOIN eatlq.token_registry AS t1
    ON p.chain_id = t1.chain_id AND p.token1_address = t1.token_address;
