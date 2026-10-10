#!/usr/bin/env bash
# Phase 4: bring up executor on VPS and smoke-test (dry-run first, then tiny live trade).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${ROOT}/infra/compose"

echo "=== 1) Pull + ensure Redis consumer group ==="
cd "${ROOT}"
git pull
docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" exec -T redis \
  redis-cli XGROUP CREATE signals execution_group 0 MKSTREAM 2>/dev/null || true

echo "=== 2) Rebuild strategy (enriched signals) + executor ==="
docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" \
  --profile strategy --profile executor up -d --build --force-recreate strategy executor

echo "=== 3) Logs: executor should show executor_started ==="
sleep 3
docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" \
  logs --tail=20 executor

echo "=== Done. For ~\$5 tests set in .env: EXECUTOR_MAX_POSITION_USD=5 EXECUTOR_ETH_PRICE_USD=<spot> ==="
echo "Optional dry-run: EXECUTOR_DRY_RUN=1 (no private key required for signal preview only)."
