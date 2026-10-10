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

WINDOW_MINUTES="${1:-30}"
STALE_SECONDS="${2:-300}"
ALLOWED_V2_MINT_UNCOVERED="${3:-1}"

FAILURES=0

query_tsv() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

pass() {
  echo "PASS: $1"
}

fail() {
  echo "FAIL: $1"
  FAILURES=$((FAILURES + 1))
}

assert_eq() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${label} (${actual})"
  else
    fail "${label} expected=${expected} actual=${actual}"
  fi
}

assert_le() {
  local label="$1"
  local actual="$2"
  local limit="$3"
  if (( actual <= limit )); then
    pass "${label} actual=${actual} limit=${limit}"
  else
    fail "${label} actual=${actual} limit=${limit}"
  fi
}

assert_ge() {
  local label="$1"
  local actual="$2"
  local limit="$3"
  if (( actual >= limit )); then
    pass "${label} actual=${actual} limit=${limit}"
  else
    fail "${label} actual=${actual} limit=${limit}"
  fi
}

normalize_int() {
  local value="${1:-0}"
  if [[ -z "${value}" || "${value}" == "\\N" ]]; then
    echo 0
  else
    echo "${value%%.*}"
  fi
}

echo "# Phase 2 Strict Healthcheck"
echo "window_minutes=${WINDOW_MINUTES} stale_seconds=${STALE_SECONDS} allowed_v2_mint_uncovered=${ALLOWED_V2_MINT_UNCOVERED}"
echo

IFS=$'\t' read -r raw_rows raw_lag_seconds < <(
  query_tsv "SELECT
      count() AS raw_rows,
      intDiv(toInt64(toUnixTimestamp64Milli(now64(3)) - toUnixTimestamp64Milli(max(received_at_ms))), 1000) AS raw_lag_seconds
    FROM eatlq.raw_evm_logs
    FORMAT TSVRaw"
)

raw_rows="$(normalize_int "${raw_rows}")"
raw_lag_seconds="$(normalize_int "${raw_lag_seconds}")"

assert_ge "raw archive has rows" "${raw_rows}" 1
assert_le "raw archive freshness" "${raw_lag_seconds}" "${STALE_SECONDS}"

IFS=$'\t' read -r _window raw_transfer normalized_transfer raw_v2_swap normalized_v2_swap raw_v3_swap normalized_v3_swap raw_v2_mint normalized_v2_mint raw_v2_burn normalized_v2_burn raw_v3_mint normalized_v3_mint raw_v3_burn normalized_v3_burn < <(
  query_tsv "SELECT
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
    FORMAT TSVRaw"
)

assert_eq "transfer parity" "$(normalize_int "${raw_transfer}")" "$(normalize_int "${normalized_transfer}")"
assert_eq "v2 swap parity" "$(normalize_int "${raw_v2_swap}")" "$(normalize_int "${normalized_v2_swap}")"
assert_eq "v3 swap parity" "$(normalize_int "${raw_v3_swap}")" "$(normalize_int "${normalized_v3_swap}")"
assert_eq "v2 mint parity" "$(normalize_int "${raw_v2_mint}")" "$(normalize_int "${normalized_v2_mint}")"
assert_eq "v2 burn parity" "$(normalize_int "${raw_v2_burn}")" "$(normalize_int "${normalized_v2_burn}")"
assert_eq "v3 mint parity" "$(normalize_int "${raw_v3_mint}")" "$(normalize_int "${normalized_v3_mint}")"
assert_eq "v3 burn parity" "$(normalize_int "${raw_v3_burn}")" "$(normalize_int "${normalized_v3_burn}")"

IFS=$'\t' read -r transfer_surplus v2_surplus v3_surplus v2_mint_surplus v2_burn_surplus v3_mint_surplus v3_burn_surplus < <(
  query_tsv "SELECT
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.erc20_transfers_deduped WHERE duplicate_rows > 1), 0) AS transfer_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_swaps_deduped WHERE duplicate_rows > 1), 0) AS v2_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_swaps_deduped WHERE duplicate_rows > 1), 0) AS v3_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_mints_deduped WHERE duplicate_rows > 1), 0) AS v2_mint_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v2_burns_deduped WHERE duplicate_rows > 1), 0) AS v2_burn_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_mints_deduped WHERE duplicate_rows > 1), 0) AS v3_mint_surplus,
      coalesce((SELECT sum(duplicate_rows - 1) FROM eatlq.uniswap_v3_burns_deduped WHERE duplicate_rows > 1), 0) AS v3_burn_surplus
    FORMAT TSVRaw"
)

assert_eq "transfer duplicate surplus" 0 "$(normalize_int "${transfer_surplus}")"
assert_eq "v2 swap duplicate surplus" 0 "$(normalize_int "${v2_surplus}")"
assert_eq "v3 swap duplicate surplus" 0 "$(normalize_int "${v3_surplus}")"
assert_eq "v2 mint duplicate surplus" 0 "$(normalize_int "${v2_mint_surplus}")"
assert_eq "v2 burn duplicate surplus" 0 "$(normalize_int "${v2_burn_surplus}")"
assert_eq "v3 mint duplicate surplus" 0 "$(normalize_int "${v3_mint_surplus}")"
assert_eq "v3 burn duplicate surplus" 0 "$(normalize_int "${v3_burn_surplus}")"

IFS=$'\t' read -r v2_mint_uncovered v2_burn_uncovered v3_mint_uncovered v3_burn_uncovered < <(
  query_tsv "SELECT
      (SELECT uniqExact(pair_address) - uniqExactIf(pair_address, has_registry_match = 1) FROM eatlq.uniswap_v2_mints_enriched) AS v2_mint_uncovered,
      (SELECT uniqExact(pair_address) - uniqExactIf(pair_address, has_registry_match = 1) FROM eatlq.uniswap_v2_burns_enriched) AS v2_burn_uncovered,
      (SELECT uniqExact(pool_address) - uniqExactIf(pool_address, has_registry_match = 1) FROM eatlq.uniswap_v3_mints_enriched) AS v3_mint_uncovered,
      (SELECT uniqExact(pool_address) - uniqExactIf(pool_address, has_registry_match = 1) FROM eatlq.uniswap_v3_burns_enriched) AS v3_burn_uncovered
    FORMAT TSVRaw"
)

assert_le "v2 mint uncovered registry gap" "$(normalize_int "${v2_mint_uncovered}")" "${ALLOWED_V2_MINT_UNCOVERED}"
assert_eq "v2 burn uncovered registry gap" 0 "$(normalize_int "${v2_burn_uncovered}")"
assert_eq "v3 mint uncovered registry gap" 0 "$(normalize_int "${v3_mint_uncovered}")"
assert_eq "v3 burn uncovered registry gap" 0 "$(normalize_int "${v3_burn_uncovered}")"

echo
if (( FAILURES > 0 )); then
  echo "Phase 2 strict healthcheck failed with ${FAILURES} issue(s)."
  exit 1
fi

echo "Phase 2 strict healthcheck passed."
