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

for sql_file in "${REPO_ROOT}"/infra/clickhouse/init/*.sql; do
  file_name="$(basename "${sql_file}")"
  echo "Applying ${file_name}..."
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    sh -lc "clickhouse-client < /docker-entrypoint-initdb.d/${file_name}"
done

echo "ClickHouse init SQL applied."
