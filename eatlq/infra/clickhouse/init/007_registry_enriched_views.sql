DROP VIEW IF EXISTS eatlq.uniswap_v3_burns_enriched;
DROP VIEW IF EXISTS eatlq.uniswap_v3_mints_enriched;
DROP VIEW IF EXISTS eatlq.uniswap_v3_swaps_enriched;
DROP VIEW IF EXISTS eatlq.uniswap_v2_burns_enriched;
DROP VIEW IF EXISTS eatlq.uniswap_v2_mints_enriched;
DROP VIEW IF EXISTS eatlq.uniswap_v2_swaps_enriched;
DROP VIEW IF EXISTS eatlq.erc20_transfers_enriched;

CREATE VIEW eatlq.erc20_transfers_enriched
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
    t.removed,
    t.payload_json,
    t.latest_ingested_at,
    t.duplicate_rows
FROM eatlq.erc20_transfers_deduped AS t
LEFT JOIN eatlq.token_registry AS r
    ON t.chain_id = r.chain_id AND t.token_address = r.token_address;

CREATE VIEW eatlq.uniswap_v2_swaps_enriched
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
    r.pair_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    s.sender,
    s.recipient,
    s.amount0_in_raw,
    s.amount1_in_raw,
    s.amount0_out_raw,
    s.amount1_out_raw,
    s.removed,
    s.payload_json,
    s.latest_ingested_at,
    s.duplicate_rows
FROM eatlq.uniswap_v2_swaps_deduped AS s
LEFT JOIN eatlq.uniswap_v2_pair_registry AS r
    ON s.chain_id = r.chain_id AND s.pair_address = r.pair_address;

CREATE VIEW eatlq.uniswap_v2_mints_enriched
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
    r.pair_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    m.sender,
    m.amount0_raw,
    m.amount1_raw,
    m.removed,
    m.payload_json,
    m.latest_ingested_at,
    m.duplicate_rows
FROM eatlq.uniswap_v2_mints_deduped AS m
LEFT JOIN eatlq.uniswap_v2_pair_registry AS r
    ON m.chain_id = r.chain_id AND m.pair_address = r.pair_address;

CREATE VIEW eatlq.uniswap_v2_burns_enriched
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
    r.pair_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    b.sender,
    b.recipient,
    b.amount0_raw,
    b.amount1_raw,
    b.removed,
    b.payload_json,
    b.latest_ingested_at,
    b.duplicate_rows
FROM eatlq.uniswap_v2_burns_deduped AS b
LEFT JOIN eatlq.uniswap_v2_pair_registry AS r
    ON b.chain_id = r.chain_id AND b.pair_address = r.pair_address;

CREATE VIEW eatlq.uniswap_v3_swaps_enriched
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
    r.pool_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    s.sender,
    s.recipient,
    s.amount0_raw,
    s.amount1_raw,
    s.sqrt_price_x96_raw,
    s.liquidity_raw,
    s.tick_raw,
    s.removed,
    s.payload_json,
    s.latest_ingested_at,
    s.duplicate_rows
FROM eatlq.uniswap_v3_swaps_deduped AS s
LEFT JOIN eatlq.uniswap_v3_pool_registry AS r
    ON s.chain_id = r.chain_id AND s.pool_address = r.pool_address;

CREATE VIEW eatlq.uniswap_v3_mints_enriched
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
    r.pool_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    m.sender,
    m.owner,
    m.tick_lower_raw,
    m.tick_upper_raw,
    m.liquidity_raw,
    m.amount0_raw,
    m.amount1_raw,
    m.removed,
    m.payload_json,
    m.latest_ingested_at,
    m.duplicate_rows
FROM eatlq.uniswap_v3_mints_deduped AS m
LEFT JOIN eatlq.uniswap_v3_pool_registry AS r
    ON m.chain_id = r.chain_id AND m.pool_address = r.pool_address;

CREATE VIEW eatlq.uniswap_v3_burns_enriched
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
    r.pool_label,
    r.token0_address,
    r.token1_address,
    r.token0_symbol,
    r.token1_symbol,
    r.fee_bps,
    r.is_curated,
    r.registry_source,
    if(r.registry_source != '', toUInt8(1), toUInt8(0)) AS has_registry_match,
    b.owner,
    b.tick_lower_raw,
    b.tick_upper_raw,
    b.liquidity_raw,
    b.amount0_raw,
    b.amount1_raw,
    b.removed,
    b.payload_json,
    b.latest_ingested_at,
    b.duplicate_rows
FROM eatlq.uniswap_v3_burns_deduped AS b
LEFT JOIN eatlq.uniswap_v3_pool_registry AS r
    ON b.chain_id = r.chain_id AND b.pool_address = r.pool_address;
