-- Backfill discovery_poller rows into uniswap_v{2,3}_swaps.
--
-- Historical swap_poller.py wrote topic*/data/tx_hash WITHOUT the `0x`
-- prefix, so the MVs `uniswap_v{2,3}_swaps_mv` (which filter on
-- `length(topicN)=66` and `topic0='0x...'`) silently dropped every row.
-- The poller is now fixed; this script backfills past 30 days directly.
--
-- Safety:
--   * writes straight to the target tables (bypasses the MV).
--   * re-running is idempotent for aggregations keyed on
--     (chain_id, pair/pool_address, block_number, tx_hash, log_index) but
--     will introduce duplicate rows. Run once.

INSERT INTO eatlq.uniswap_v2_swaps (
    chain_id, source, block_time, received_at_ms, block_number, tx_hash,
    log_index, pair_address, sender, recipient,
    amount0_in_raw, amount1_in_raw, amount0_out_raw, amount1_out_raw,
    removed, payload_json, ingested_at
)
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    concat('0x', tx_hash) AS tx_hash,
    log_index,
    lower(address) AS pair_address,
    concat('0x', lower(substring(topic1, length(topic1) - 39, 40))) AS sender,
    concat('0x', lower(substring(topic2, length(topic2) - 39, 40))) AS recipient,
    concat('0x', lower(substring(data, 1, 64)))   AS amount0_in_raw,
    concat('0x', lower(substring(data, 65, 64)))  AS amount1_in_raw,
    concat('0x', lower(substring(data, 129, 64))) AS amount0_out_raw,
    concat('0x', lower(substring(data, 193, 64))) AS amount1_out_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE chain_id = 8453
  AND source = 'discovery_poller'
  AND topic0 = 'd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'
  AND length(topic1) = 64
  AND length(topic2) = 64
  AND length(data) >= 256
  AND received_at_ms >= toDateTime64(now() - toIntervalDay(30), 3, 'UTC');

INSERT INTO eatlq.uniswap_v3_swaps (
    chain_id, source, block_time, received_at_ms, block_number, tx_hash,
    log_index, pool_address, sender, recipient,
    amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw,
    removed, payload_json, ingested_at
)
SELECT
    chain_id,
    source,
    block_time,
    received_at_ms,
    block_number,
    concat('0x', tx_hash) AS tx_hash,
    log_index,
    lower(address) AS pool_address,
    concat('0x', lower(substring(topic1, length(topic1) - 39, 40))) AS sender,
    concat('0x', lower(substring(topic2, length(topic2) - 39, 40))) AS recipient,
    concat('0x', lower(substring(data, 1, 64)))   AS amount0_raw,
    concat('0x', lower(substring(data, 65, 64)))  AS amount1_raw,
    concat('0x', lower(substring(data, 129, 64))) AS sqrt_price_x96_raw,
    concat('0x', lower(substring(data, 193, 64))) AS liquidity_raw,
    concat('0x', lower(substring(data, 257, 64))) AS tick_raw,
    removed,
    payload_json,
    ingested_at
FROM eatlq.raw_evm_logs
WHERE chain_id = 8453
  AND source = 'discovery_poller'
  AND topic0 = 'c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
  AND length(topic1) = 64
  AND length(topic2) = 64
  AND length(data) >= 320
  AND received_at_ms >= toDateTime64(now() - toIntervalDay(30), 3, 'UTC');
