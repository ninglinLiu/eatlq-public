-- Materialize sprint089_gap_backfill rows from raw_evm_logs into normalized swap tables.
--
-- Gap backfill wrote topic*/data/tx_hash/address WITHOUT `0x` prefix (length 64),
-- so uniswap_v{2,3}_swaps_mv silently dropped every row. This script bypasses MVs.
--
-- Idempotent: skips rows already present in target tables (same dedup key).

-- V3 Swap → uniswap_v3_swaps
INSERT INTO eatlq.uniswap_v3_swaps (
    chain_id, source, block_time, received_at_ms, block_number, tx_hash,
    log_index, pool_address, sender, recipient,
    amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw,
    removed, payload_json, ingested_at
)
SELECT
    r.chain_id,
    r.source,
    r.block_time,
    r.received_at_ms,
    r.block_number,
    concat('0x', r.tx_hash) AS tx_hash,
    r.log_index,
    concat('0x', lower(r.address)) AS pool_address,
    concat('0x', lower(substring(r.topic1, length(r.topic1) - 39, 40))) AS sender,
    concat('0x', lower(substring(r.topic2, length(r.topic2) - 39, 40))) AS recipient,
    concat('0x', lower(substring(r.data, 1, 64))) AS amount0_raw,
    concat('0x', lower(substring(r.data, 65, 64))) AS amount1_raw,
    concat('0x', lower(substring(r.data, 129, 64))) AS sqrt_price_x96_raw,
    concat('0x', lower(substring(r.data, 193, 64))) AS liquidity_raw,
    concat('0x', lower(substring(r.data, 257, 64))) AS tick_raw,
    r.removed,
    r.payload_json,
    r.ingested_at
FROM eatlq.raw_evm_logs AS r
LEFT JOIN eatlq.uniswap_v3_swaps AS s
    ON s.chain_id = r.chain_id
   AND s.block_number = r.block_number
   AND s.log_index = r.log_index
   AND lower(s.pool_address) = concat('0x', lower(r.address))
   AND lower(replace(s.tx_hash, '0x', '')) = lower(r.tx_hash)
WHERE r.chain_id = 8453
  AND r.source = 'sprint089_gap_backfill'
  AND r.topic0 = 'c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
  AND length(r.topic1) = 64
  AND length(r.topic2) = 64
  AND length(r.data) >= 320
  AND r.block_number BETWEEN 46150416 AND 46281863
  AND s.tx_hash IS NULL;

-- V2 Swap + Aerodrome vAMM Swap → uniswap_v2_swaps
INSERT INTO eatlq.uniswap_v2_swaps (
    chain_id, source, block_time, received_at_ms, block_number, tx_hash,
    log_index, pair_address, sender, recipient,
    amount0_in_raw, amount1_in_raw, amount0_out_raw, amount1_out_raw,
    removed, payload_json, ingested_at
)
SELECT
    r.chain_id,
    r.source,
    r.block_time,
    r.received_at_ms,
    r.block_number,
    concat('0x', r.tx_hash) AS tx_hash,
    r.log_index,
    concat('0x', lower(r.address)) AS pair_address,
    concat('0x', lower(substring(r.topic1, length(r.topic1) - 39, 40))) AS sender,
    concat('0x', lower(substring(r.topic2, length(r.topic2) - 39, 40))) AS recipient,
    concat('0x', lower(substring(r.data, 1, 64))) AS amount0_in_raw,
    concat('0x', lower(substring(r.data, 65, 64))) AS amount1_in_raw,
    concat('0x', lower(substring(r.data, 129, 64))) AS amount0_out_raw,
    concat('0x', lower(substring(r.data, 193, 64))) AS amount1_out_raw,
    r.removed,
    r.payload_json,
    r.ingested_at
FROM eatlq.raw_evm_logs AS r
LEFT JOIN eatlq.uniswap_v2_swaps AS s
    ON s.chain_id = r.chain_id
   AND s.block_number = r.block_number
   AND s.log_index = r.log_index
   AND lower(s.pair_address) = concat('0x', lower(r.address))
   AND lower(replace(s.tx_hash, '0x', '')) = lower(r.tx_hash)
WHERE r.chain_id = 8453
  AND r.source = 'sprint089_gap_backfill'
  AND r.topic0 IN (
    'd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822',
    'b3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'
  )
  AND length(r.topic1) = 64
  AND length(r.topic2) = 64
  AND length(r.data) >= 256
  AND r.block_number BETWEEN 46150416 AND 46281863
  AND s.tx_hash IS NULL;
