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
HOURS="${2:-1}"
LIMIT="${3:-10}"

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
    "top erc20 transfer tokens last ${HOURS}h" \
    "SELECT token_address, sum(transfer_count) AS transfers, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.erc20_transfers_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY token_address ORDER BY transfers DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2" ]]; then
  run_query \
    "top uniswap v2 pairs last ${HOURS}h" \
    "SELECT pair_address, sum(swap_count) AS swaps, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_swaps_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pair_address ORDER BY swaps DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3" ]]; then
  run_query \
    "top uniswap v3 pools last ${HOURS}h" \
    "SELECT pool_address, sum(swap_count) AS swaps, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_swaps_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pool_address ORDER BY swaps DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-mint" ]]; then
  run_query \
    "top uniswap v2 mint pairs last ${HOURS}h" \
    "SELECT pair_address, sum(mint_count) AS mints, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_mints_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pair_address ORDER BY mints DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-burn" ]]; then
  run_query \
    "top uniswap v2 burn pairs last ${HOURS}h" \
    "SELECT pair_address, sum(burn_count) AS burns, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v2_burns_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pair_address ORDER BY burns DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-mint" ]]; then
  run_query \
    "top uniswap v3 mint pools last ${HOURS}h" \
    "SELECT pool_address, sum(mint_count) AS mints, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_mints_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pool_address ORDER BY mints DESC, unique_txs DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-burn" ]]; then
  run_query \
    "top uniswap v3 burn pools last ${HOURS}h" \
    "SELECT pool_address, sum(burn_count) AS burns, sum(unique_tx_count) AS unique_txs, max(latest_ingested_at) AS latest_ingested_at FROM eatlq.uniswap_v3_burns_hourly WHERE hour >= toStartOfHour(now() - INTERVAL ${HOURS} HOUR) GROUP BY pool_address ORDER BY burns DESC, unique_txs DESC LIMIT ${LIMIT}"
fi
