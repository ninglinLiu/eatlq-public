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

case "${TARGET}" in
  all)
    FILTER="1"
    ;;
  erc20)
    FILTER="surface = 'erc20_transfers'"
    ;;
  v2)
    FILTER="surface = 'uniswap_v2_swaps'"
    ;;
  v2-mint)
    FILTER="surface = 'uniswap_v2_mints'"
    ;;
  v2-burn)
    FILTER="surface = 'uniswap_v2_burns'"
    ;;
  v3)
    FILTER="surface = 'uniswap_v3_swaps'"
    ;;
  v3-mint)
    FILTER="surface = 'uniswap_v3_mints'"
    ;;
  v3-burn)
    FILTER="surface = 'uniswap_v3_burns'"
    ;;
  *)
    echo "Usage: bash scripts/inspect-dedupe-anomalies.sh [all|erc20|v2|v2-mint|v2-burn|v3|v3-mint|v3-burn] [limit]"
    exit 1
    ;;
esac

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

run_query \
  "duplicate anomaly summary" \
  "SELECT
      surface,
      venue_type,
      chain_id,
      venue_address,
      duplicate_events,
      duplicate_surplus,
      latest_ingested_at
    FROM eatlq.normalized_duplicate_summary
    WHERE ${FILTER}
    ORDER BY duplicate_surplus DESC, duplicate_events DESC, latest_ingested_at DESC
    LIMIT ${LIMIT}"

run_query \
  "recent duplicate anomaly events" \
  "SELECT
      surface,
      venue_type,
      chain_id,
      venue_address,
      tx_hash,
      log_index,
      duplicate_rows,
      duplicate_surplus,
      latest_ingested_at
    FROM eatlq.normalized_duplicate_events
    WHERE ${FILTER}
    ORDER BY latest_ingested_at DESC, duplicate_rows DESC
    LIMIT ${LIMIT}"
