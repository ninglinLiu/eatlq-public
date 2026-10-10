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

TARGET="${1:-v3}"
LIMIT="${2:-20}"

case "${TARGET}" in
  erc20)
    TOPIC0_FILTER="topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'"
    ADDRESS_EXPR="lower(address)"
    ADDRESS_ALIAS="token_address"
    ;;
  v2)
    TOPIC0_FILTER="topic0 IN ('0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822', '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b')"
    ADDRESS_EXPR="lower(address)"
    ADDRESS_ALIAS="pair_address"
    ;;
  v3)
    TOPIC0_FILTER="topic0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'"
    ADDRESS_EXPR="lower(address)"
    ADDRESS_ALIAS="pool_address"
    ;;
  *)
    echo "Usage: bash scripts/inspect-raw-duplicate-origins.sh [erc20|v2|v3] [limit]"
    exit 1
    ;;
esac

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  exec -T clickhouse \
  clickhouse-client --query "
SELECT
    chain_id,
    ${ADDRESS_EXPR} AS ${ADDRESS_ALIAS},
    block_number,
    tx_hash,
    log_index,
    count() AS duplicate_rows,
    arraySort(groupUniqArray(source)) AS sources,
    min(ingested_at) AS first_ingested_at,
    max(ingested_at) AS latest_ingested_at
FROM eatlq.raw_evm_logs
WHERE ${TOPIC0_FILTER}
GROUP BY chain_id, ${ADDRESS_ALIAS}, block_number, tx_hash, log_index
HAVING count() > 1
ORDER BY duplicate_rows DESC, latest_ingested_at DESC
LIMIT ${LIMIT}
"
