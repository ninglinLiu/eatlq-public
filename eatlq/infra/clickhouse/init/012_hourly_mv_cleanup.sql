DROP TABLE IF EXISTS eatlq.raw_evm_logs_hourly_mv;

ALTER TABLE eatlq.raw_evm_logs_hourly
    DROP COLUMN IF EXISTS unique_address_state;

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
