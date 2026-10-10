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

SEVERITY_FILTER="${1:-all}"

case "${SEVERITY_FILTER}" in
  all)
    WHERE_CLAUSE=""
    ;;
  critical|warning)
    WHERE_CLAUSE="WHERE severity = '${SEVERITY_FILTER}'"
    ;;
  *)
    echo "Usage: bash scripts/inspect-phase2-alerts.sh [all|critical|warning]"
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

echo "# Phase 2 Alerts"
echo "severity_filter=${SEVERITY_FILTER}"
echo

run_query \
  "alert summary" \
  "SELECT
      severity,
      alert_kind,
      alert_count,
      targets
    FROM eatlq.phase2_alert_summary
    ${WHERE_CLAUSE}
    ORDER BY severity, alert_kind
    FORMAT PrettyCompactMonoBlock"

run_query \
  "alert events" \
  "SELECT
      severity,
      alert_scope,
      surface,
      alert_kind,
      actual_value,
      threshold_value,
      detail,
      remediation_target,
      remediation_command
    FROM eatlq.phase2_alert_events
    ${WHERE_CLAUSE}
    ORDER BY severity, alert_kind, surface
    FORMAT PrettyCompactMonoBlock"
