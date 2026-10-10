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

if [[ "${TARGET}" == "all" || "${TARGET}" == "tokens" ]]; then
  run_query \
    "token registry" \
    "SELECT token_address, symbol, name, decimals, is_core, registry_source FROM eatlq.token_registry ORDER BY symbol"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2" ]]; then
  run_query \
    "uniswap v2 pair registry" \
    "SELECT pair_address, pair_label, token0_symbol, token1_symbol, fee_bps, registry_source FROM eatlq.uniswap_v2_pair_registry ORDER BY pair_label"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3" ]]; then
  run_query \
    "uniswap v3 pool registry" \
    "SELECT pool_address, pool_label, token0_symbol, token1_symbol, fee_bps, registry_source FROM eatlq.uniswap_v3_pool_registry ORDER BY pool_label"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "pools" ]]; then
  run_query \
    "unified pool registry" \
    "SELECT protocol_family, venue_type, venue_address, venue_label, token0_symbol, token1_symbol, fee_bps FROM eatlq.pool_registry ORDER BY protocol_family, venue_label"
fi
