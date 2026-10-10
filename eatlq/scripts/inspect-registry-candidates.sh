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

TARGET="${1:-all}"
LIMIT="${2:-20}"

if [[ "${TARGET}" == "all" ]]; then
  FILTER="1"
elif [[ "${TARGET}" == "tokens" ]]; then
  FILTER="candidate_type = 'token'"
elif [[ "${TARGET}" == "v2" ]]; then
  FILTER="candidate_type = 'uniswap_v2_pair'"
elif [[ "${TARGET}" == "v3" ]]; then
  FILTER="candidate_type = 'uniswap_v3_pool'"
else
  echo "Unsupported target: ${TARGET}"
  echo "Use one of: all, tokens, v2, v3"
  exit 1
fi

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  exec -T clickhouse \
  clickhouse-client --query "
SELECT
    candidate_type,
    candidate_address,
    surfaces,
    total_rows,
    total_unique_txs,
    latest_ingested_at
FROM eatlq.registry_candidate_summary
WHERE ${FILTER}
ORDER BY total_rows DESC, total_unique_txs DESC, latest_ingested_at DESC
LIMIT ${LIMIT}
FORMAT PrettyCompact
"
