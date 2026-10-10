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
STALE_SECONDS="${2:-300}"
ALLOWED_V2_MINT_UNCOVERED="${3:-1}"
OUTPUT_PATH="${4:-}"

query_tsv() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

normalize_int() {
  local value="${1:-0}"
  if [[ -z "${value}" || "${value}" == "\\N" ]]; then
    echo 0
  else
    echo "${value%%.*}"
  fi
}

strict_output_file="$(mktemp)"
trap 'rm -f "${strict_output_file}"' EXIT
strict_exit_code=0
if bash "${SCRIPT_DIR}/phase2-healthcheck-strict.sh" "${WINDOW_MINUTES}" "${STALE_SECONDS}" "${ALLOWED_V2_MINT_UNCOVERED}" >"${strict_output_file}" 2>&1; then
  strict_exit_code=0
else
  strict_exit_code=$?
fi

IFS=$'\t' read -r raw_rows raw_lag_seconds critical_alerts warning_alerts critical_targets warning_targets < <(
  query_tsv "SELECT
      (SELECT raw_rows FROM eatlq.phase2_raw_status) AS raw_rows,
      (SELECT raw_lag_seconds FROM eatlq.phase2_raw_status) AS raw_lag_seconds,
      (SELECT count() FROM eatlq.phase2_alert_events WHERE severity = 'critical') AS critical_alerts,
      (SELECT count() FROM eatlq.phase2_alert_events WHERE severity = 'warning') AS warning_alerts,
      (SELECT if(count() = 0, 'none', arrayStringConcat(arraySort(groupArray(concat(alert_kind, ':', if(surface = '', alert_scope, surface)))), ',')) FROM eatlq.phase2_alert_events WHERE severity = 'critical') AS critical_targets,
      (SELECT if(count() = 0, 'none', arrayStringConcat(arraySort(groupArray(concat(alert_kind, ':', if(surface = '', alert_scope, surface)))), ',')) FROM eatlq.phase2_alert_events WHERE severity = 'warning') AS warning_targets
    FORMAT TSVRaw"
)

raw_rows="$(normalize_int "${raw_rows}")"
raw_lag_seconds="$(normalize_int "${raw_lag_seconds}")"
critical_alerts="$(normalize_int "${critical_alerts}")"
warning_alerts="$(normalize_int "${warning_alerts}")"
critical_targets="${critical_targets:-none}"
warning_targets="${warning_targets:-none}"

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
status="ok"
if (( strict_exit_code != 0 || critical_alerts > 0 )); then
  status="critical"
elif (( warning_alerts > 0 )); then
  status="warning"
fi

summary_line="timestamp_utc=${timestamp_utc} status=${status} raw_rows=${raw_rows} raw_lag_seconds=${raw_lag_seconds} critical_alerts=${critical_alerts} warning_alerts=${warning_alerts} critical_targets=${critical_targets} warning_targets=${warning_targets} strict_exit_code=${strict_exit_code}"

echo "# Phase 2 Cron Monitor"
echo "${summary_line}"
echo
echo "## Strict healthcheck output"
while IFS= read -r line; do
  echo "${line}"
done < "${strict_output_file}"

if [[ -n "${OUTPUT_PATH}" ]]; then
  mkdir -p "$(dirname "${OUTPUT_PATH}")"
  printf '%s\n' "${summary_line}" > "${OUTPUT_PATH}"
  echo
  echo "summary_written_to=${OUTPUT_PATH}"
fi

exit "${strict_exit_code}"
