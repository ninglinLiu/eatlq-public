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

WINDOW_MINUTES="${1:-30}"
ACTIVITY_HOURS="${2:-24}"
TOP_LIMIT="${3:-10}"

run_step() {
  local label="$1"
  shift

  echo "## ${label}"
  "$@"
  echo
}

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

echo "# Phase 2 Healthcheck"
echo "window_minutes=${WINDOW_MINUTES} activity_hours=${ACTIVITY_HOURS} top_limit=${TOP_LIMIT}"
echo

run_query \
  "raw archive heartbeat" \
  "SELECT count() AS raw_rows, max(received_at_ms) AS latest_received_at_ms FROM eatlq.raw_evm_logs"

run_query \
  "normalized table totals" \
  "SELECT
      (SELECT count() FROM eatlq.erc20_transfers) AS transfers,
      (SELECT count() FROM eatlq.uniswap_v2_swaps) AS v2_swaps,
      (SELECT count() FROM eatlq.uniswap_v3_swaps) AS v3_swaps,
      (SELECT count() FROM eatlq.uniswap_v2_mints) AS v2_mints,
      (SELECT count() FROM eatlq.uniswap_v2_burns) AS v2_burns,
      (SELECT count() FROM eatlq.uniswap_v3_mints) AS v3_mints,
      (SELECT count() FROM eatlq.uniswap_v3_burns) AS v3_burns"

run_step \
  "recent raw vs normalized parity" \
  bash "${SCRIPT_DIR}/verify-normalized-events.sh" "${WINDOW_MINUTES}"

run_step \
  "dedupe summary" \
  bash "${SCRIPT_DIR}/verify-normalized-dedupe.sh"

run_step \
  "registry coverage" \
  bash "${SCRIPT_DIR}/verify-registry-coverage.sh"

run_step \
  "top v3 swap activity" \
  bash "${SCRIPT_DIR}/inspect-normalized-activity.sh" v3 "${ACTIVITY_HOURS}" "${TOP_LIMIT}"

run_step \
  "top v3 mint activity" \
  bash "${SCRIPT_DIR}/inspect-normalized-activity.sh" v3-mint "${ACTIVITY_HOURS}" "${TOP_LIMIT}"

run_step \
  "top v3 burn activity" \
  bash "${SCRIPT_DIR}/inspect-normalized-activity.sh" v3-burn "${ACTIVITY_HOURS}" "${TOP_LIMIT}"

run_step \
  "top v2 mint activity" \
  bash "${SCRIPT_DIR}/inspect-normalized-activity.sh" v2-mint "${ACTIVITY_HOURS}" "${TOP_LIMIT}"

run_step \
  "top v2 burn activity" \
  bash "${SCRIPT_DIR}/inspect-normalized-activity.sh" v2-burn "${ACTIVITY_HOURS}" "${TOP_LIMIT}"
