CREATE VIEW IF NOT EXISTS eatlq.erc20_transfers_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    token_address,
    count() AS transfer_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.erc20_transfers_deduped
GROUP BY hour, chain_id, token_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_swaps_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pair_address,
    count() AS swap_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_swaps_deduped
GROUP BY hour, chain_id, pair_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_swaps_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pool_address,
    count() AS swap_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_swaps_deduped
GROUP BY hour, chain_id, pool_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_mints_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pair_address,
    count() AS mint_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_mints_deduped
GROUP BY hour, chain_id, pair_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v2_burns_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pair_address,
    count() AS burn_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_burns_deduped
GROUP BY hour, chain_id, pair_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_mints_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pool_address,
    count() AS mint_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_mints_deduped
GROUP BY hour, chain_id, pool_address;

CREATE VIEW IF NOT EXISTS eatlq.uniswap_v3_burns_hourly
AS
SELECT
    toStartOfHour(coalesce(block_time, received_at_ms)) AS hour,
    chain_id,
    pool_address,
    count() AS burn_count,
    uniqExact(tx_hash) AS unique_tx_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_burns_deduped
GROUP BY hour, chain_id, pool_address;
