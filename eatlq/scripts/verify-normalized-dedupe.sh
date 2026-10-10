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

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  exec -T clickhouse \
  clickhouse-client --query "
SELECT
    (SELECT count() FROM eatlq.erc20_transfers) AS transfer_rows,
    (SELECT count() FROM eatlq.erc20_transfers_deduped) AS transfer_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.erc20_transfers_deduped WHERE duplicate_rows > 1) AS transfer_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v2_swaps) AS v2_rows,
    (SELECT count() FROM eatlq.uniswap_v2_swaps_deduped) AS v2_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_swaps_deduped WHERE duplicate_rows > 1) AS v2_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v3_swaps) AS v3_rows,
    (SELECT count() FROM eatlq.uniswap_v3_swaps_deduped) AS v3_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_swaps_deduped WHERE duplicate_rows > 1) AS v3_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v2_mints) AS v2_mint_rows,
    (SELECT count() FROM eatlq.uniswap_v2_mints_deduped) AS v2_mint_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_mints_deduped WHERE duplicate_rows > 1) AS v2_mint_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v2_burns) AS v2_burn_rows,
    (SELECT count() FROM eatlq.uniswap_v2_burns_deduped) AS v2_burn_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_burns_deduped WHERE duplicate_rows > 1) AS v2_burn_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v3_mints) AS v3_mint_rows,
    (SELECT count() FROM eatlq.uniswap_v3_mints_deduped) AS v3_mint_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_mints_deduped WHERE duplicate_rows > 1) AS v3_mint_duplicate_surplus,
    (SELECT count() FROM eatlq.uniswap_v3_burns) AS v3_burn_rows,
    (SELECT count() FROM eatlq.uniswap_v3_burns_deduped) AS v3_burn_deduped_rows,
    (SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_burns_deduped WHERE duplicate_rows > 1) AS v3_burn_duplicate_surplus
"
