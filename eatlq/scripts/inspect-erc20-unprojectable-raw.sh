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

LIMIT="${1:-20}"

run_query() {
  local label="$1"
  local sql="$2"

  echo "== ${label} =="
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
  echo
}

FILTER="topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
  AND NOT (length(topic1) = 66 AND length(topic2) = 66 AND length(data) >= 66)"

REASON_EXPR="multiIf(
  length(topic1) != 66 AND length(topic2) != 66 AND length(data) < 66, 'topic1_len+topic2_len+data_len',
  length(topic1) != 66 AND length(topic2) != 66, 'topic1_len+topic2_len',
  length(topic1) != 66 AND length(data) < 66, 'topic1_len+data_len',
  length(topic2) != 66 AND length(data) < 66, 'topic2_len+data_len',
  length(topic1) != 66, 'topic1_len',
  length(topic2) != 66, 'topic2_len',
  length(data) < 66, 'data_len',
  'other'
)"

echo "# ERC20 Unprojectable Raw Analysis"
echo "limit=${LIMIT}"
echo

run_query \
  "reason breakdown" \
  "SELECT
      ${REASON_EXPR} AS reason,
      count() AS rows,
      uniqExact(tx_hash) AS unique_txs,
      arraySort(groupUniqArray(source)) AS sources,
      min(block_number) AS first_block,
      max(block_number) AS latest_block
    FROM eatlq.raw_evm_logs
    WHERE ${FILTER}
    GROUP BY reason
    ORDER BY rows DESC, reason
    FORMAT PrettyCompactMonoBlock"

run_query \
  "source breakdown" \
  "SELECT
      source,
      count() AS rows,
      uniqExact(tx_hash) AS unique_txs,
      min(block_number) AS first_block,
      max(block_number) AS latest_block
    FROM eatlq.raw_evm_logs
    WHERE ${FILTER}
    GROUP BY source
    ORDER BY rows DESC, source
    FORMAT PrettyCompactMonoBlock"

run_query \
  "token breakdown" \
  "SELECT
      lower(address) AS token_address,
      count() AS rows,
      uniqExact(tx_hash) AS unique_txs,
      arraySort(groupUniqArray(source)) AS sources,
      max(received_at_ms) AS latest_received_at_ms
    FROM eatlq.raw_evm_logs
    WHERE ${FILTER}
    GROUP BY token_address
    ORDER BY rows DESC, latest_received_at_ms DESC
    LIMIT ${LIMIT}
    FORMAT PrettyCompactMonoBlock"

run_query \
  "recent samples" \
  "SELECT
      chain_id,
      lower(address) AS token_address,
      block_number,
      tx_hash,
      log_index,
      source,
      ${REASON_EXPR} AS reason,
      length(topic1) AS topic1_len,
      length(topic2) AS topic2_len,
      length(data) AS data_len,
      left(topic1, 18) AS topic1_prefix,
      left(topic2, 18) AS topic2_prefix,
      left(data, 18) AS data_prefix,
      received_at_ms,
      ingested_at
    FROM eatlq.raw_evm_logs
    WHERE ${FILTER}
    ORDER BY ingested_at DESC
    LIMIT ${LIMIT}
    FORMAT PrettyCompactMonoBlock"
