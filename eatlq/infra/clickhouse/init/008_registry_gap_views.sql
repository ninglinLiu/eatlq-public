DROP VIEW IF EXISTS eatlq.registry_candidate_summary;
DROP VIEW IF EXISTS eatlq.registry_gap_summary;

CREATE VIEW eatlq.registry_gap_summary
AS
SELECT
    'token' AS candidate_type,
    'erc20_transfers' AS surface,
    token_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.erc20_transfers_enriched
WHERE has_registry_match = 0
GROUP BY token_address

UNION ALL

SELECT
    'uniswap_v2_pair' AS candidate_type,
    'uniswap_v2_swaps' AS surface,
    pair_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_swaps_enriched
WHERE has_registry_match = 0
GROUP BY pair_address

UNION ALL

SELECT
    'uniswap_v2_pair' AS candidate_type,
    'uniswap_v2_mints' AS surface,
    pair_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_mints_enriched
WHERE has_registry_match = 0
GROUP BY pair_address

UNION ALL

SELECT
    'uniswap_v2_pair' AS candidate_type,
    'uniswap_v2_burns' AS surface,
    pair_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v2_burns_enriched
WHERE has_registry_match = 0
GROUP BY pair_address

UNION ALL

SELECT
    'uniswap_v3_pool' AS candidate_type,
    'uniswap_v3_swaps' AS surface,
    pool_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_swaps_enriched
WHERE has_registry_match = 0
GROUP BY pool_address

UNION ALL

SELECT
    'uniswap_v3_pool' AS candidate_type,
    'uniswap_v3_mints' AS surface,
    pool_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_mints_enriched
WHERE has_registry_match = 0
GROUP BY pool_address

UNION ALL

SELECT
    'uniswap_v3_pool' AS candidate_type,
    'uniswap_v3_burns' AS surface,
    pool_address AS candidate_address,
    count() AS rows,
    uniqExact(tx_hash) AS unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.uniswap_v3_burns_enriched
WHERE has_registry_match = 0
GROUP BY pool_address;

CREATE VIEW eatlq.registry_candidate_summary
AS
SELECT
    candidate_type,
    candidate_address,
    groupUniqArray(surface) AS surfaces,
    sum(rows) AS total_rows,
    sum(unique_txs) AS total_unique_txs,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.registry_gap_summary
GROUP BY candidate_type, candidate_address;
