CREATE DATABASE IF NOT EXISTS eatlq;

CREATE TABLE IF NOT EXISTS eatlq.raw_evm_logs
(
    chain_id UInt64,
    source LowCardinality(String),
    subscription_id String,
    block_time Nullable(DateTime64(3, 'UTC')),
    received_at_ms DateTime64(3, 'UTC'),
    block_date Date DEFAULT toDate(coalesce(block_time, received_at_ms)),
    block_number UInt64,
    tx_hash String,
    log_index UInt32,
    address LowCardinality(String),
    topic0 String,
    topic1 String,
    topic2 String,
    topic3 String,
    data String,
    removed UInt8,
    value_usd Nullable(Float64),
    payload_json String,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at_ms)
ORDER BY (chain_id, received_at_ms, tx_hash, log_index)
TTL toDateTime(received_at_ms) + INTERVAL 180 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
