#!/usr/bin/env bash
# Sprint064B — null_bt scheduled backfill (cron-safe wrapper).
# Loads infra/compose/.env without printing; uses flock to prevent overlapping runs.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOCK_FILE=/tmp/eatlq_null_bt_backfill.lock
ENV_FILE="${REPO_ROOT}/infra/compose/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi
export EATLQ_NULL_BT_CRON_WRAP=1
export CHAIN_ID="${CHAIN_ID:-8453}"
export CH_CONTAINER="${CH_CONTAINER:-eatlq-clickhouse-1}"
exec /usr/bin/flock -n "${LOCK_FILE}" \
  /usr/bin/python3 "${REPO_ROOT}/scripts/maintenance/backfill_null_block_times.py" \
    --apply --max-age-hours 6 --max-blocks 5000
