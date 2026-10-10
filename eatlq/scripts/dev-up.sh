#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

if [[ ! -f "${COMPOSE_DIR}/.env" ]]; then
  cp "${COMPOSE_DIR}/.env.example" "${COMPOSE_DIR}/.env"
  echo "Created ${COMPOSE_DIR}/.env from .env.example"
  echo "Update EVM_WS_URL before using a real provider."
fi

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  up -d --build
