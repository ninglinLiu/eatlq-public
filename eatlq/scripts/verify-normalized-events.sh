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

WINDOW_MINUTES="${1:-10}"

docker compose \
  --env-file "${COMPOSE_DIR}/.env" \
  -f "${COMPOSE_DIR}/compose.yaml" \
  exec -T clickhouse \
  clickhouse-client --query "
SELECT
    ${WINDOW_MINUTES} AS window_minutes,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_transfer,
    (SELECT count() FROM eatlq.erc20_transfers WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_transfer,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 IN ('0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822', '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b') AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v2_swap,
    (SELECT count() FROM eatlq.uniswap_v2_swaps WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v2_swap,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v3_swap,
    (SELECT count() FROM eatlq.uniswap_v3_swaps WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v3_swap,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v2_mint,
    (SELECT count() FROM eatlq.uniswap_v2_mints WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v2_mint,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v2_burn,
    (SELECT count() FROM eatlq.uniswap_v2_burns WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v2_burn,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v3_mint,
    (SELECT count() FROM eatlq.uniswap_v3_mints WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v3_mint,
    (SELECT count() FROM eatlq.raw_evm_logs WHERE topic0 = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c' AND received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS raw_v3_burn,
    (SELECT count() FROM eatlq.uniswap_v3_burns WHERE received_at_ms >= now() - INTERVAL ${WINDOW_MINUTES} MINUTE) AS normalized_v3_burn
"
