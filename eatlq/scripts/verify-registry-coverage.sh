#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

if [[ ! -f "${COMPOSE_DIR}/.env" ]]; then
  echo "Missing ${COMPOSE_DIR}/.env"
  echo "Copy infra/compose/.env.example first and fill real values."
  exit 1
fi

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  exec -T clickhouse \
  clickhouse-client --query "
SELECT
    surface,
    registry_entries,
    seen_addresses,
    covered_addresses,
    seen_addresses - covered_addresses AS uncovered_addresses
FROM
(
    SELECT
        'erc20_transfers' AS surface,
        (SELECT count() FROM eatlq.token_registry) AS registry_entries,
        uniqExact(token_address) AS seen_addresses,
        uniqExactIf(token_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.erc20_transfers_enriched

    UNION ALL

    SELECT
        'uniswap_v2_swaps' AS surface,
        (SELECT count() FROM eatlq.uniswap_v2_pair_registry) AS registry_entries,
        uniqExact(pair_address) AS seen_addresses,
        uniqExactIf(pair_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v2_swaps_enriched

    UNION ALL

    SELECT
        'uniswap_v2_mints' AS surface,
        (SELECT count() FROM eatlq.uniswap_v2_pair_registry) AS registry_entries,
        uniqExact(pair_address) AS seen_addresses,
        uniqExactIf(pair_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v2_mints_enriched

    UNION ALL

    SELECT
        'uniswap_v2_burns' AS surface,
        (SELECT count() FROM eatlq.uniswap_v2_pair_registry) AS registry_entries,
        uniqExact(pair_address) AS seen_addresses,
        uniqExactIf(pair_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v2_burns_enriched

    UNION ALL

    SELECT
        'uniswap_v3_swaps' AS surface,
        (SELECT count() FROM eatlq.uniswap_v3_pool_registry) AS registry_entries,
        uniqExact(pool_address) AS seen_addresses,
        uniqExactIf(pool_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v3_swaps_enriched

    UNION ALL

    SELECT
        'uniswap_v3_mints' AS surface,
        (SELECT count() FROM eatlq.uniswap_v3_pool_registry) AS registry_entries,
        uniqExact(pool_address) AS seen_addresses,
        uniqExactIf(pool_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v3_mints_enriched

    UNION ALL

    SELECT
        'uniswap_v3_burns' AS surface,
        (SELECT count() FROM eatlq.uniswap_v3_pool_registry) AS registry_entries,
        uniqExact(pool_address) AS seen_addresses,
        uniqExactIf(pool_address, has_registry_match = 1) AS covered_addresses
    FROM eatlq.uniswap_v3_burns_enriched
)
ORDER BY surface
FORMAT PrettyCompact
"
