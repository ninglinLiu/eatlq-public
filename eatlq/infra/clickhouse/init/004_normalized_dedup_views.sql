CREATE VIEW IF NOT EXISTS eatlq.erc20_transfers_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    token_address,
    argMax(sender, ingested_at) AS sender,
    argMax(recipient, ingested_at) AS recipient,
    argMax(amount_raw, ingested_at) AS amount_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.erc20_transfers
GROUP BY chain_id, token_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_swaps_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pair_address,
    argMax(sender, ingested_at) AS sender,
    argMax(recipient, ingested_at) AS recipient,
    argMax(amount0_in_raw, ingested_at) AS amount0_in_raw,
    argMax(amount1_in_raw, ingested_at) AS amount1_in_raw,
    argMax(amount0_out_raw, ingested_at) AS amount0_out_raw,
    argMax(amount1_out_raw, ingested_at) AS amount1_out_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v2_swaps
GROUP BY chain_id, pair_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_swaps_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pool_address,
    argMax(sender, ingested_at) AS sender,
    argMax(recipient, ingested_at) AS recipient,
    argMax(amount0_raw, ingested_at) AS amount0_raw,
    argMax(amount1_raw, ingested_at) AS amount1_raw,
    argMax(sqrt_price_x96_raw, ingested_at) AS sqrt_price_x96_raw,
    argMax(liquidity_raw, ingested_at) AS liquidity_raw,
    argMax(tick_raw, ingested_at) AS tick_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v3_swaps
GROUP BY chain_id, pool_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_mints_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pair_address,
    argMax(sender, ingested_at) AS sender,
    argMax(amount0_raw, ingested_at) AS amount0_raw,
    argMax(amount1_raw, ingested_at) AS amount1_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v2_mints
GROUP BY chain_id, pair_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_burns_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pair_address,
    argMax(sender, ingested_at) AS sender,
    argMax(recipient, ingested_at) AS recipient,
    argMax(amount0_raw, ingested_at) AS amount0_raw,
    argMax(amount1_raw, ingested_at) AS amount1_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v2_burns
GROUP BY chain_id, pair_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_mints_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pool_address,
    argMax(sender, ingested_at) AS sender,
    argMax(owner, ingested_at) AS owner,
    argMax(tick_lower_raw, ingested_at) AS tick_lower_raw,
    argMax(tick_upper_raw, ingested_at) AS tick_upper_raw,
    argMax(liquidity_raw, ingested_at) AS liquidity_raw,
    argMax(amount0_raw, ingested_at) AS amount0_raw,
    argMax(amount1_raw, ingested_at) AS amount1_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v3_mints
GROUP BY chain_id, pool_address, tx_hash, log_index;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_burns_deduped
AS
SELECT
    chain_id,
    argMax(source, ingested_at) AS source,
    argMax(block_time, ingested_at) AS block_time,
    argMax(received_at_ms, ingested_at) AS received_at_ms,
    argMax(block_date, ingested_at) AS block_date,
    argMax(block_number, ingested_at) AS block_number,
    tx_hash,
    log_index,
    pool_address,
    argMax(owner, ingested_at) AS owner,
    argMax(tick_lower_raw, ingested_at) AS tick_lower_raw,
    argMax(tick_upper_raw, ingested_at) AS tick_upper_raw,
    argMax(liquidity_raw, ingested_at) AS liquidity_raw,
    argMax(amount0_raw, ingested_at) AS amount0_raw,
    argMax(amount1_raw, ingested_at) AS amount1_raw,
    argMax(removed, ingested_at) AS removed,
    argMax(payload_json, ingested_at) AS payload_json,
    max(ingested_at) AS latest_ingested_at,
    count() AS duplicate_rows
FROM eatlq.uniswap_v3_burns
GROUP BY chain_id, pool_address, tx_hash, log_index;
