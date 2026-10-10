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

run_query() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

query_scalar() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

rebuild_table() {
  local target_table="$1"
  local insert_columns="$2"
  local source_sql="$3"
  local tmp_table="${target_table}_rebuild_tmp"
  local old_table="${target_table}_rebuild_old"
  local expected_rows=""
  local actual_rows=""

  echo "Rebuilding ${target_table} via temporary table swap..."
  echo "Warning: ${target_table} will only be replaced after temp-table validation succeeds."

  run_query "DROP TABLE IF EXISTS ${tmp_table}"
  run_query "DROP TABLE IF EXISTS ${old_table}"
  run_query "CREATE TABLE ${tmp_table} AS ${target_table}"

  expected_rows="$(query_scalar "SELECT count() FROM (${source_sql}) FORMAT TSVRaw")"
  run_query "INSERT INTO ${tmp_table} (${insert_columns}) ${source_sql}"
  actual_rows="$(query_scalar "SELECT count() FROM ${tmp_table} FORMAT TSVRaw")"

  if [[ "${actual_rows}" != "${expected_rows}" ]]; then
    echo "Temp-table row count validation failed for ${target_table}: expected=${expected_rows} actual=${actual_rows}"
    echo "Original table was left untouched."
    exit 1
  fi

  run_query "RENAME TABLE ${target_table} TO ${old_table}, ${tmp_table} TO ${target_table}"
  run_query "DROP TABLE ${old_table}"

  echo "Rebuilt ${target_table} rows=${actual_rows}."
}

rebuild_erc20() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, token_address, sender, recipient, amount_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS token_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(topic2, 27, 40))), ingested_at) AS recipient,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS amount_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(data) >= 66
    GROUP BY chain_id, token_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.erc20_transfers" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, token_address, sender, recipient, amount_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v2_swaps() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, recipient, amount0_in_raw, amount1_in_raw, amount0_out_raw, amount1_out_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pair_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(topic2, 27, 40))), ingested_at) AS recipient,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS amount0_in_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS amount1_in_raw,
        argMax(concat('0x', lower(substring(data, 131, 64))), ingested_at) AS amount0_out_raw,
        argMax(concat('0x', lower(substring(data, 195, 64))), ingested_at) AS amount1_out_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 IN (
        '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822',
        '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'
    )
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(data) >= 258
    GROUP BY chain_id, pair_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v2_swaps" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, recipient, amount0_in_raw, amount1_in_raw, amount0_out_raw, amount1_out_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v3_swaps() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, sender, recipient, amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pool_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(topic2, 27, 40))), ingested_at) AS recipient,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS amount0_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS amount1_raw,
        argMax(concat('0x', lower(substring(data, 131, 64))), ingested_at) AS sqrt_price_x96_raw,
        argMax(concat('0x', lower(substring(data, 195, 64))), ingested_at) AS liquidity_raw,
        argMax(concat('0x', lower(substring(data, 259, 64))), ingested_at) AS tick_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(data) >= 322
    GROUP BY chain_id, pool_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v3_swaps" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, sender, recipient, amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v2_mints() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, amount0_raw, amount1_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pair_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS amount0_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS amount1_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f'
      AND length(topic1) = 66
      AND length(data) >= 130
    GROUP BY chain_id, pair_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v2_mints" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, amount0_raw, amount1_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v2_burns() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, recipient, amount0_raw, amount1_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pair_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(topic2, 27, 40))), ingested_at) AS recipient,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS amount0_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS amount1_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496'
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(data) >= 130
    GROUP BY chain_id, pair_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v2_burns" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pair_address, sender, recipient, amount0_raw, amount1_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v3_mints() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, sender, owner, tick_lower_raw, tick_upper_raw, liquidity_raw, amount0_raw, amount1_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pool_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(data, 27, 40))), ingested_at) AS sender,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS owner,
        argMax(concat('0x', lower(substring(topic2, 3, 64))), ingested_at) AS tick_lower_raw,
        argMax(concat('0x', lower(substring(topic3, 3, 64))), ingested_at) AS tick_upper_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS liquidity_raw,
        argMax(concat('0x', lower(substring(data, 131, 64))), ingested_at) AS amount0_raw,
        argMax(concat('0x', lower(substring(data, 195, 64))), ingested_at) AS amount1_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(topic3) = 66
      AND length(data) >= 258
    GROUP BY chain_id, pool_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v3_mints" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, sender, owner, tick_lower_raw, tick_upper_raw, liquidity_raw, amount0_raw, amount1_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

rebuild_v3_burns() {
  local source_sql
  source_sql="$(cat <<'EOF'
SELECT chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, owner, tick_lower_raw, tick_upper_raw, liquidity_raw, amount0_raw, amount1_raw, removed, payload_json, latest_ingested_at AS ingested_at
FROM (
    SELECT
        chain_id,
        lower(address) AS pool_address,
        tx_hash,
        log_index,
        argMax(source, ingested_at) AS source,
        argMax(block_time, ingested_at) AS block_time,
        argMax(received_at_ms, ingested_at) AS received_at_ms,
        argMax(block_number, ingested_at) AS block_number,
        argMax(concat('0x', lower(substring(topic1, 27, 40))), ingested_at) AS owner,
        argMax(concat('0x', lower(substring(topic2, 3, 64))), ingested_at) AS tick_lower_raw,
        argMax(concat('0x', lower(substring(topic3, 3, 64))), ingested_at) AS tick_upper_raw,
        argMax(concat('0x', lower(substring(data, 3, 64))), ingested_at) AS liquidity_raw,
        argMax(concat('0x', lower(substring(data, 67, 64))), ingested_at) AS amount0_raw,
        argMax(concat('0x', lower(substring(data, 131, 64))), ingested_at) AS amount1_raw,
        argMax(removed, ingested_at) AS removed,
        argMax(payload_json, ingested_at) AS payload_json,
        max(ingested_at) AS latest_ingested_at
    FROM eatlq.raw_evm_logs
    WHERE topic0 = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'
      AND length(topic1) = 66
      AND length(topic2) = 66
      AND length(topic3) = 66
      AND length(data) >= 194
    GROUP BY chain_id, pool_address, tx_hash, log_index
)
EOF
)"
  rebuild_table \
    "eatlq.uniswap_v3_burns" \
    "chain_id, source, block_time, received_at_ms, block_number, tx_hash, log_index, pool_address, owner, tick_lower_raw, tick_upper_raw, liquidity_raw, amount0_raw, amount1_raw, removed, payload_json, ingested_at" \
    "${source_sql}"
}

case "${TARGET}" in
  all)
    rebuild_erc20
    rebuild_v2_swaps
    rebuild_v3_swaps
    rebuild_v2_mints
    rebuild_v2_burns
    rebuild_v3_mints
    rebuild_v3_burns
    ;;
  erc20)
    rebuild_erc20
    ;;
  v2)
    rebuild_v2_swaps
    ;;
  v3)
    rebuild_v3_swaps
    ;;
  v2-mint)
    rebuild_v2_mints
    ;;
  v2-burn)
    rebuild_v2_burns
    ;;
  v3-mint)
    rebuild_v3_mints
    ;;
  v3-burn)
    rebuild_v3_burns
    ;;
  *)
    echo "Usage: bash scripts/rebuild-normalized-surface.sh [all|erc20|v2|v3|v2-mint|v2-burn|v3-mint|v3-burn]"
    exit 1
    ;;
esac

echo "Normalized surface rebuild finished for target=${TARGET}."
