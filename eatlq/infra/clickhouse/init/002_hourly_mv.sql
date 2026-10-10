CREATE TABLE IF NOT EXISTS eatlq.raw_evm_logs_hourly
(
    hour DateTime('UTC'),
    chain_id UInt64,
    address LowCardinality(String),
    events_count_state AggregateFunction(count),
    unique_tx_state AggregateFunction(uniqExact, String),
    value_usd_sum_state AggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (chain_id, hour, address);

CREATE MATERIALIZED VIEW IF NOT EXISTS eatlq.raw_evm_logs_hourly_mv
TO eatlq.raw_evm_logs_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    address,
    countState() AS events_count_state,
    uniqExactState(tx_hash) AS unique_tx_state,
    sumState(coalesce(value_usd, 0.0)) AS value_usd_sum_state
FROM eatlq.raw_evm_logs
GROUP BY chain_id, hour, address;
