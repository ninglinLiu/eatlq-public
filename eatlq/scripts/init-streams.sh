#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" exec -T redis \
  redis-cli XGROUP CREATE market_ticks archiver_group 0 MKSTREAM || true

docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" exec -T redis \
  redis-cli XGROUP CREATE market_ticks strategy_group 0 MKSTREAM || true

docker compose --env-file "${COMPOSE_DIR}/.env" -f "${COMPOSE_DIR}/compose.yaml" exec -T redis \
  redis-cli XGROUP CREATE signals execution_group 0 MKSTREAM || true

echo "Redis consumer groups are initialized."
