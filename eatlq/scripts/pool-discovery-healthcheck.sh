#!/usr/bin/env bash
#
# Pool discovery healthcheck (LASZLO-aligned).
#
# Verifies the pool discovery loop is producing new pools; this is the only
# upstream the strategy depends on for its universe.  No meme-specific gates
# are applied here (those were an EATLQ-only layer, now removed).
#
# Args:
#   $1 WINDOW_HOURS   window for "new pools" count (default 24)
#   $2 CHAIN_ID       chain id (default 8453 = Base)
#   $3 STREAK_DAYS    rolling-window days that must each have >0 pools
#                     for streak_*d_status=PASS (default 3)
#   $4 BLACKLIST_FILE path to mev_blacklist.yaml (default infra/config/mev_blacklist.yaml)
#
# Verdict: PASS iff discovered_pools_new_${WINDOW_HOURS}h > 0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

if [[ ! -f "${COMPOSE_DIR}/.env" ]]; then
  echo "Missing ${COMPOSE_DIR}/.env"
  echo "Copy infra/compose/.env.example first and fill real values."
  exit 1
fi

WINDOW_HOURS="${1:-24}"
CHAIN_ID="${2:-8453}"
STREAK_DAYS="${3:-3}"
BLACKLIST_FILE="${4:-${REPO_ROOT}/infra/config/mev_blacklist.yaml}"

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

discovered_pools_new="$(query_tsv "SELECT
    toUInt64(count())
  FROM eatlq.discovered_pools
  WHERE chain_id = toUInt64(${CHAIN_ID})
    AND discovered_at >= now() - INTERVAL ${WINDOW_HOURS} HOUR
  FORMAT TSVRaw")"
discovered_pools_new="$(normalize_int "${discovered_pools_new}")"

verdict="BLOCKED"
if (( discovered_pools_new > 0 )); then
  verdict="PASS"
fi

pass_days="$(query_tsv "SELECT
    toUInt64(countIf(discovered_pools_new > 0)) AS pass_days
  FROM (
    SELECT
      health_date,
      discovered_pools_new
    FROM eatlq.discovered_pool_daily_health_v1
    ORDER BY health_date DESC
    LIMIT ${STREAK_DAYS}
  )
  SETTINGS
    query_plan_max_optimizations_to_apply=0,
    max_memory_usage=5000000000,
    max_bytes_before_external_group_by=3000000000,
    max_bytes_before_external_sort=3000000000
  FORMAT TSVRaw")"
pass_days="$(normalize_int "${pass_days}")"

streak_status="BLOCKED"
if (( pass_days >= STREAK_DAYS )); then
  streak_status="PASS"
fi

echo "# Pool Discovery Healthcheck"
echo "window_hours=${WINDOW_HOURS} chain_id=${CHAIN_ID} streak_days=${STREAK_DAYS}"
echo
echo "discovered_pools_new_${WINDOW_HOURS}h=${discovered_pools_new}"
if [[ -f "${BLACKLIST_FILE}" ]]; then
  mev_blacklist_size="$(
    BLACKLIST_FILE="${BLACKLIST_FILE}" CHAIN_ID="${CHAIN_ID}" python3 - <<'PY'
import json
import os

path = os.environ.get("BLACKLIST_FILE", "")
chain_id = os.environ.get("CHAIN_ID", "8453")
try:
    payload = json.loads(open(path, "r", encoding="utf-8").read())
except Exception:
    print(0)
    raise SystemExit(0)
cp = payload.get(chain_id) or payload.get(int(chain_id) if chain_id.isdigit() else chain_id) or {}
count = 0
if isinstance(cp, dict):
    for rows in cp.values():
        if isinstance(rows, list):
            count += len(rows)
print(count)
PY
  )"
  echo "mev_blacklist_size=${mev_blacklist_size:-0}"
else
  echo "mev_blacklist_size=0"
fi
echo "pass_days_${STREAK_DAYS}d=${pass_days}"
echo "streak_${STREAK_DAYS}d_status=${streak_status}"
echo
echo "verdict=${verdict}"
