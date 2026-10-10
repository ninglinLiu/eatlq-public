#!/usr/bin/env bash
# Alpha line health cron — run every 15 min; alert on ALIVE transitions.
set -euo pipefail
cd /opt/EATLQ

ENV_FILE="${ALPHA_HEALTH_ENV:-infra/compose/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

python3 scripts/alpha_line_health.py --once --notify
