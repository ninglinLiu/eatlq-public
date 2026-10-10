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
    "recent enriched erc20 transfers" \
    "SELECT if(has_registry_match = 1, token_symbol, concat('UNREGISTERED:', token_address)) AS token_ref, sender, recipient, amount_raw, has_registry_match, latest_ingested_at FROM eatlq.erc20_transfers_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2" ]]; then
  run_query \
    "recent enriched uniswap v2 swaps" \
    "SELECT if(has_registry_match = 1, pair_label, concat('UNREGISTERED:', pair_address)) AS pair_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, sender, recipient, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v2_swaps_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-mint" ]]; then
  run_query \
    "recent enriched uniswap v2 mints" \
    "SELECT if(has_registry_match = 1, pair_label, concat('UNREGISTERED:', pair_address)) AS pair_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, sender, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v2_mints_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v2-burn" ]]; then
  run_query \
    "recent enriched uniswap v2 burns" \
    "SELECT if(has_registry_match = 1, pair_label, concat('UNREGISTERED:', pair_address)) AS pair_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, sender, recipient, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v2_burns_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3" ]]; then
  run_query \
    "recent enriched uniswap v3 swaps" \
    "SELECT if(has_registry_match = 1, pool_label, concat('UNREGISTERED:', pool_address)) AS pool_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, sender, recipient, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v3_swaps_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-mint" ]]; then
  run_query \
    "recent enriched uniswap v3 mints" \
    "SELECT if(has_registry_match = 1, pool_label, concat('UNREGISTERED:', pool_address)) AS pool_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, sender, owner, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v3_mints_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi

if [[ "${TARGET}" == "all" || "${TARGET}" == "v3-burn" ]]; then
  run_query \
    "recent enriched uniswap v3 burns" \
    "SELECT if(has_registry_match = 1, pool_label, concat('UNREGISTERED:', pool_address)) AS pool_ref, if(has_registry_match = 1, token0_symbol, '-') AS token0_ref, if(has_registry_match = 1, token1_symbol, '-') AS token1_ref, if(has_registry_match = 1, toString(fee_bps), '-') AS fee_bps_ref, owner, has_registry_match, latest_ingested_at FROM eatlq.uniswap_v3_burns_enriched ORDER BY latest_ingested_at DESC LIMIT ${LIMIT}"
fi
