CREATE TABLE IF NOT EXISTS eatlq.erc20_transfers
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    token_address LowCardinality(String),
    sender LowCardinality(String),
    recipient LowCardinality(String),
    amount_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, token_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.erc20_transfers_mv
TO eatlq.erc20_transfers
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    token_address,
    sender,
    recipient,
    amount_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS token_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(topic2, 27, 40))) AS recipient,
    concat('0x', lower(substring(data, 3, 64))) AS amount_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(data) >= 66;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v2_swaps
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pair_address LowCardinality(String),
    sender LowCardinality(String),
    recipient LowCardinality(String),
    amount0_in_raw String,
    amount1_in_raw String,
    amount0_out_raw String,
    amount1_out_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pair_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v2_swaps_mv
TO eatlq.uniswap_v2_swaps
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    recipient,
    amount0_in_raw,
    amount1_in_raw,
    amount0_out_raw,
    amount1_out_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pair_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(topic2, 27, 40))) AS recipient,
    concat('0x', lower(substring(data, 3, 64))) AS amount0_in_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount1_in_raw,
    concat('0x', lower(substring(data, 131, 64))) AS amount0_out_raw,
    concat('0x', lower(substring(data, 195, 64))) AS amount1_out_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(data) >= 258;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v3_swaps
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pool_address LowCardinality(String),
    sender LowCardinality(String),
    recipient LowCardinality(String),
    amount0_raw String,
    amount1_raw String,
    sqrt_price_x96_raw String,
    liquidity_raw String,
    tick_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pool_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v3_swaps_mv
TO eatlq.uniswap_v3_swaps
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    sender,
    recipient,
    amount0_raw,
    amount1_raw,
    sqrt_price_x96_raw,
    liquidity_raw,
    tick_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pool_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(topic2, 27, 40))) AS recipient,
    concat('0x', lower(substring(data, 3, 64))) AS amount0_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount1_raw,
    concat('0x', lower(substring(data, 131, 64))) AS sqrt_price_x96_raw,
    concat('0x', lower(substring(data, 195, 64))) AS liquidity_raw,
    concat('0x', lower(substring(data, 259, 64))) AS tick_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(data) >= 322;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v2_mints
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pair_address LowCardinality(String),
    sender LowCardinality(String),
    amount0_raw String,
    amount1_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pair_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v2_mints_mv
TO eatlq.uniswap_v2_mints
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    amount0_raw,
    amount1_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pair_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(data, 3, 64))) AS amount0_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount1_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f'
  AND length(topic1) = 66
  AND length(data) >= 130;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v2_burns
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pair_address LowCardinality(String),
    sender LowCardinality(String),
    recipient LowCardinality(String),
    amount0_raw String,
    amount1_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pair_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v2_burns_mv
TO eatlq.uniswap_v2_burns
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    recipient,
    amount0_raw,
    amount1_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pair_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(topic2, 27, 40))) AS recipient,
    concat('0x', lower(substring(data, 3, 64))) AS amount0_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount1_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(data) >= 130;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v3_mints
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pool_address LowCardinality(String),
    sender LowCardinality(String),
    owner LowCardinality(String),
    tick_lower_raw String,
    tick_upper_raw String,
    liquidity_raw String,
    amount0_raw String,
    amount1_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pool_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v3_mints_mv
TO eatlq.uniswap_v3_mints
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    sender,
    owner,
    tick_lower_raw,
    tick_upper_raw,
    liquidity_raw,
    amount0_raw,
    amount1_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pool_address,
    concat('0x', lower(substring(data, 27, 40))) AS sender,
    concat('0x', lower(substring(topic1, 27, 40))) AS owner,
    concat('0x', lower(substring(topic2, 3, 64))) AS tick_lower_raw,
    concat('0x', lower(substring(topic3, 3, 64))) AS tick_upper_raw,
    concat('0x', lower(substring(data, 67, 64))) AS liquidity_raw,
    concat('0x', lower(substring(data, 131, 64))) AS amount0_raw,
    concat('0x', lower(substring(data, 195, 64))) AS amount1_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(topic3) = 66
  AND length(data) >= 258;

CREATE TABLE IF NOT EXISTS eatlq.uniswap_v3_burns
(
    chain_id UInt64,
    source LowCardinality(String),
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    pool_address LowCardinality(String),
    owner LowCardinality(String),
    tick_lower_raw String,
    tick_upper_raw String,
    liquidity_raw String,
    amount0_raw String,
    amount1_raw String,
    removed UInt8,
    payload_json String,
    ingested_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, pool_address, block_number, tx_hash, log_index);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.uniswap_v3_burns_mv
TO eatlq.uniswap_v3_burns
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pool_address,
    owner,
    tick_lower_raw,
    tick_upper_raw,
    liquidity_raw,
    amount0_raw,
    amount1_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pool_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS owner,
    concat('0x', lower(substring(topic2, 3, 64))) AS tick_lower_raw,
    concat('0x', lower(substring(topic3, 3, 64))) AS tick_upper_raw,
    concat('0x', lower(substring(data, 3, 64))) AS liquidity_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount0_raw,
    concat('0x', lower(substring(data, 131, 64))) AS amount1_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(topic3) = 66
  AND length(data) >= 194;

-- ── Aerodrome vAMM Swap → uniswap_v2_swaps (same data layout as Uniswap V2 Swap) ──
-- topic0: 0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b
-- Swap(address indexed sender, address indexed to, uint256 amount0In, uint256 amount1In, uint256 amount0Out, uint256 amount1Out)
CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.aerodrome_vamm_swaps_mv
TO eatlq.uniswap_v2_swaps
(
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    pair_address,
    sender,
    recipient,
    amount0_in_raw,
    amount1_in_raw,
    amount0_out_raw,
    amount1_out_raw,
    removed,
    payload_json,
    ingested_at
)
AS
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    tx_hash,
    log_index,
    lower(address) AS pair_address,
    concat('0x', lower(substring(topic1, 27, 40))) AS sender,
    concat('0x', lower(substring(topic2, 27, 40))) AS recipient,
    concat('0x', lower(substring(data, 3, 64))) AS amount0_in_raw,
    concat('0x', lower(substring(data, 67, 64))) AS amount1_in_raw,
    concat('0x', lower(substring(data, 131, 64))) AS amount0_out_raw,
    concat('0x', lower(substring(data, 195, 64))) AS amount1_out_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE topic0 = '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'
  AND length(topic1) = 66
  AND length(topic2) = 66
  AND length(data) >= 258;
