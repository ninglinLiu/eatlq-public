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

echo "# Phase 2 Status"
echo

run_query \
  "raw archive status" \
  "SELECT raw_rows, latest_received_at_ms, raw_lag_seconds FROM eatlq.phase2_raw_status FORMAT Vertical"

run_query \
  "surface status" \
  "SELECT
      surface,
      venue_type,
      recent_window_minutes,
      raw_total_rows,
      raw_projectable_total_rows,
      raw_projectable_unique_total_rows,
      raw_projectable_duplicate_total_rows,
      raw_unprojectable_total_rows,
      normalized_total_rows,
      total_gap,
      projectable_total_gap,
      raw_recent_rows,
      raw_projectable_recent_rows,
      raw_projectable_unique_recent_rows,
      raw_projectable_duplicate_recent_rows,
      raw_unprojectable_recent_rows,
      normalized_recent_rows,
      recent_gap,
      projectable_recent_gap,
      duplicate_surplus,
      uncovered_registry_gap,
      latest_block_time,
      latest_ingested_at
    FROM eatlq.phase2_surface_status
    ORDER BY surface
    FORMAT PrettyCompactMonoBlock"
