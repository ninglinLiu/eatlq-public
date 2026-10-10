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

TARGET="${1:-all}"
LIMIT="${2:-20}"

run_query() {
  local label="$1"
  local sql="$2"

  echo "== ${label} =="
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
  echo
}

if [[ "${TARGET}" == "all" || "${TARGET}" == "erc20" ]]; then
  run_query \
    "uncovered erc20 transfer tokens" \
    "SELECT token_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.erc20_transfers_enriched WHERE has_registry_match = 0 GROUP BY token_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2" ]]; then
  run_query \
    "uncovered uniswap v2 swap pairs" \
    "SELECT pair_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_swaps_enriched WHERE has_registry_match = 0 GROUP BY pair_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-mint" ]]; then
  run_query \
    "uncovered uniswap v2 mint pairs" \
    "SELECT pair_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_mints_enriched WHERE has_registry_match = 0 GROUP BY pair_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-burn" ]]; then
  run_query \
    "uncovered uniswap v2 burn pairs" \
    "SELECT pair_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_burns_enriched WHERE has_registry_match = 0 GROUP BY pair_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3" ]]; then
  run_query \
    "uncovered uniswap v3 swap pools" \
    "SELECT pool_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_swaps_enriched WHERE has_registry_match = 0 GROUP BY pool_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-mint" ]]; then
  run_query \
    "uncovered uniswap v3 mint pools" \
    "SELECT pool_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_mints_enriched WHERE has_registry_match = 0 GROUP BY pool_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-burn" ]]; then
  run_query \
    "uncovered uniswap v3 burn pools" \
    "SELECT pool_address, count() AS rows, uniqExact(tx_hash) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_burns_enriched WHERE has_registry_match = 0 GROUP BY pool_address ORDER BY rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi
