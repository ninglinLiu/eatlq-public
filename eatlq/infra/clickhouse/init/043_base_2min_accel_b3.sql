-- B3 research-only tables. No live consumer reads these tables.
CREATE TABLE IF NOT EXISTS eatlq.base_pool_universe_b3
(
    chain_id UInt64,
    pool_address String,
    pool_type LowCardinality(String),
    target_token String,
    target_index UInt8,
    target_decimals UInt8,
    created_block UInt64,
    created_time DateTime64(3, 'UTC'),
    snapshot_60 UInt64,
    snapshot_120 UInt64,
    snapshot_150 UInt64,
    snapshot_300 UInt64,
    window LowCardinality(String),
    built_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(built_at)
ORDER BY (chain_id, pool_address);

CREATE TABLE IF NOT EXISTS eatlq.base_pool_transfers_b3
(
    chain_id UInt64,
    request_kind LowCardinality(String),
    request_address String,
    pool_address String,
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    token_address String,
    sender String,
    recipient String,
    amount UInt256,
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (chain_id, request_kind, request_address, pool_address, tx_hash, log_index);

CREATE TABLE IF NOT EXISTS eatlq.base_transfer_fetch_status_b3
(
    chain_id UInt64,
    request_kind LowCardinality(String),
    request_address String,
    pool_address String,
    from_block UInt64,
    to_block UInt64,
    completed UInt8,
    log_count UInt32,
    rpc_attempts UInt16,
    error String,
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (chain_id, request_kind, request_address, pool_address);

CREATE TABLE IF NOT EXISTS eatlq.base_pool_2min_snapshot_b3
(
    chain_id UInt64,
    pool_address String,
    window LowCardinality(String),
    fresh_class LowCardinality(String),
    premint_address_count UInt32,
    premint_amount_raw UInt256,
    volume_1 Float64,
    volume_2 Float64,
    reserve_60_raw Int256,
    reserve_120_raw Int256,
    new_holders_1 UInt32,
    new_holders_2 UInt32,
    new_holders_eoa_1 UInt32,
    new_holders_eoa_2 UInt32,
    proxy_recipients_1 UInt32,
    proxy_recipients_2 UInt32,
    real_signal UInt8,
    real_signal_eoa UInt8,
    proxy_signal UInt8,
    max_feature_block UInt64,
    snapshot_block UInt64,
    built_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(built_at)
ORDER BY (chain_id, pool_address);

CREATE TABLE IF NOT EXISTS eatlq.base_account_code_b3
(
    chain_id UInt64,
    address String,
    at_block UInt64,
    account_code_class LowCardinality(String),
    code_hash String,
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (chain_id, address, at_block);
