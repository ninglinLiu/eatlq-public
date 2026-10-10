DROP VIEW IF EXISTS eatlq.normalized_duplicate_summary;
DROP VIEW IF EXISTS eatlq.normalized_duplicate_events;

CREATE VIEW eatlq.normalized_duplicate_events
AS
SELECT
    'erc20_transfers' AS surface,
    'token' AS venue_type,
    chain_id,
    token_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.erc20_transfers_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v2_swaps' AS surface,
    'pair' AS venue_type,
    chain_id,
    pair_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v2_swaps_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v2_mints' AS surface,
    'pair' AS venue_type,
    chain_id,
    pair_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v2_mints_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v2_burns' AS surface,
    'pair' AS venue_type,
    chain_id,
    pair_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v2_burns_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v3_swaps' AS surface,
    'pool' AS venue_type,
    chain_id,
    pool_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v3_swaps_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v3_mints' AS surface,
    'pool' AS venue_type,
    chain_id,
    pool_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v3_mints_deduped
WHERE duplicate_rows > 1

UNION ALL

SELECT
    'uniswap_v3_burns' AS surface,
    'pool' AS venue_type,
    chain_id,
    pool_address AS venue_address,
    tx_hash,
    log_index,
    duplicate_rows,
    duplicate_rows - 1 AS duplicate_surplus,
    latest_ingested_at
FROM eatlq.uniswap_v3_burns_deduped
WHERE duplicate_rows > 1;

CREATE VIEW eatlq.normalized_duplicate_summary
AS
SELECT
    surface,
    venue_type,
    chain_id,
    venue_address,
    count() AS duplicate_events,
    sum(duplicate_surplus) AS duplicate_surplus,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.normalized_duplicate_events
GROUP BY surface, venue_type, chain_id, venue_address;
