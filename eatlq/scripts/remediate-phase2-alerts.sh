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

SEVERITY_FILTER="warning"
APPLY=0

for arg in "$@"; do
  case "${arg}" in
    warning|critical|all)
      SEVERITY_FILTER="${arg}"
      ;;
    --apply)
      APPLY=1
      ;;
    *)
      echo "Usage: bash scripts/remediate-phase2-alerts.sh [warning|critical|all] [--apply]"
      exit 1
      ;;
  esac
done

query_tsv() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

if [[ "${SEVERITY_FILTER}" == "all" ]]; then
  WHERE_CLAUSE=""
else
  WHERE_CLAUSE="AND severity = '${SEVERITY_FILTER}'"
fi

mapfile -t TARGETS < <(
  query_tsv "SELECT DISTINCT remediation_target
    FROM eatlq.phase2_alert_events
    WHERE remediation_target IN ('erc20', 'v2', 'v3', 'v2-mint', 'v2-burn', 'v3-mint', 'v3-burn')
      ${WHERE_CLAUSE}
    ORDER BY remediation_target
    FORMAT TSVRaw"
)

echo "# Phase 2 Alert Remediation"
echo "severity_filter=${SEVERITY_FILTER}"
if (( APPLY == 1 )); then
  echo "mode=apply"
else
  echo "mode=dry-run"
fi
echo

query_tsv "SELECT
    severity,
    surface,
    alert_kind,
    actual_value,
    threshold_value,
    remediation_target,
    remediation_command
  FROM eatlq.phase2_alert_events
  WHERE remediation_target IN ('erc20', 'v2', 'v3', 'v2-mint', 'v2-burn', 'v3-mint', 'v3-burn')
    ${WHERE_CLAUSE}
  ORDER BY severity, alert_kind, surface
  FORMAT PrettyCompactMonoBlock"
echo

if (( ${#TARGETS[@]} == 0 )); then
  echo "No rebuild-driven alert remediations are currently required."
  exit 0
fi

echo "Targets selected for remediation:"
for target in "${TARGETS[@]}"; do
  echo "- ${target}"
done
echo

if (( APPLY == 0 )); then
  echo "Dry run only. Re-run with --apply to execute targeted rebuilds."
  exit 0
fi

for target in "${TARGETS[@]}"; do
  bash "${SCRIPT_DIR}/rebuild-normalized-surface.sh" "${target}"
done

echo
echo "## Post-remediation alert snapshot"
bash "${SCRIPT_DIR}/inspect-phase2-alerts.sh" "${SEVERITY_FILTER}"
echo
echo "## Post-remediation strict healthcheck"
bash "${SCRIPT_DIR}/phase2-healthcheck-strict.sh" 30 300 1
