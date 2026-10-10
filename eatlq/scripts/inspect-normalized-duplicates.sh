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
LIMIT="${2:-10}"

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
    "erc20_transfers duplicates" \
    "SELECT chain_id, token_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.erc20_transfers GROUP BY chain_id, token_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2" ]]; then
  run_query \
    "uniswap_v2_swaps duplicates" \
    "SELECT chain_id, pair_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_swaps GROUP BY chain_id, pair_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3" ]]; then
  run_query \
    "uniswap_v3_swaps duplicates" \
    "SELECT chain_id, pool_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_swaps GROUP BY chain_id, pool_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-mint" ]]; then
  run_query \
    "uniswap_v2_mints duplicates" \
    "SELECT chain_id, pair_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_mints GROUP BY chain_id, pair_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-burn" ]]; then
  run_query \
    "uniswap_v2_burns duplicates" \
    "SELECT chain_id, pair_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_burns GROUP BY chain_id, pair_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-mint" ]]; then
  run_query \
    "uniswap_v3_mints duplicates" \
    "SELECT chain_id, pool_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_mints GROUP BY chain_id, pool_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-burn" ]]; then
  run_query \
    "uniswap_v3_burns duplicates" \
    "SELECT chain_id, pool_address, tx_hash, log_index, count() AS duplicate_rows, min(ingested_at) AS first_ingested_at, max(ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_burns GROUP BY chain_id, pool_address, tx_hash, log_index HAVING count() > 1 ORDER BY duplicate_rows DESC, latest_ingested_at DESC LIMIT ${LIMIT}"
fi
