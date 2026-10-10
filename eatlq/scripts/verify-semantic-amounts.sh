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

SAMPLE_LIMIT="${1:-3}"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Missing python3/python runtime for verification script."
  exit 1
fi

query_json() {
  local sql="$1"
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T clickhouse \
    clickhouse-client --query "${sql}"
}

erc20_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'erc20_transfers_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount) AS amount,
        toString(token_decimals) AS token_decimals,
        toString(amount_normalized) AS amount_normalized
    FROM eatlq.erc20_transfers_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v2_swap_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v2_swaps_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0_in) AS amount0_in,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_in_normalized) AS amount0_in_normalized,
        toString(amount1_in) AS amount1_in,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_in_normalized) AS amount1_in_normalized,
        toString(amount0_out) AS amount0_out,
        toString(amount0_out_normalized) AS amount0_out_normalized,
        toString(amount1_out) AS amount1_out,
        toString(amount1_out_normalized) AS amount1_out_normalized
    FROM eatlq.uniswap_v2_swaps_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v2_mint_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v2_mints_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0) AS amount0,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1) AS amount1,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_normalized) AS amount1_normalized
    FROM eatlq.uniswap_v2_mints_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v2_burn_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v2_burns_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0) AS amount0,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1) AS amount1,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_normalized) AS amount1_normalized
    FROM eatlq.uniswap_v2_burns_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v3_swap_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v3_swaps_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0) AS amount0,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1) AS amount1,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_normalized) AS amount1_normalized
    FROM eatlq.uniswap_v3_swaps_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v3_mint_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v3_mints_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0) AS amount0,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1) AS amount1,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_normalized) AS amount1_normalized
    FROM eatlq.uniswap_v3_mints_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v3_burn_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v3_burns_semantic' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        toString(amount0) AS amount0,
        toString(token0_decimals) AS token0_decimals,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1) AS amount1,
        toString(token1_decimals) AS token1_decimals,
        toString(amount1_normalized) AS amount1_normalized
    FROM eatlq.uniswap_v3_burns_semantic
    WHERE has_registry_match = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export ERC20_SEMANTIC_SAMPLES="$(query_json "${erc20_query}")"
export V2_SWAP_SEMANTIC_SAMPLES="$(query_json "${v2_swap_query}")"
export V2_MINT_SEMANTIC_SAMPLES="$(query_json "${v2_mint_query}")"
export V2_BURN_SEMANTIC_SAMPLES="$(query_json "${v2_burn_query}")"
export V3_SWAP_SEMANTIC_SAMPLES="$(query_json "${v3_swap_query}")"
export V3_MINT_SEMANTIC_SAMPLES="$(query_json "${v3_mint_query}")"
export V3_BURN_SEMANTIC_SAMPLES="$(query_json "${v3_burn_query}")"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import sys
from decimal import Decimal, getcontext

getcontext().prec = 100


def parse_json_lines(env_name):
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def normalized_amount(amount_text, decimals_text):
    return Decimal(amount_text) / (Decimal(10) ** int(decimals_text))


def within_tolerance(actual_text, expected):
    actual = Decimal(actual_text)
    tolerance = max(Decimal("1e-12"), abs(expected) * Decimal("1e-12"))
    return abs(actual - expected) <= tolerance, actual, tolerance


def verify_rows(title, rows, specs, failures):
    if not rows:
        failures.append(f"{title} returned no sample rows")
        return

    print(f"## {title}")
    for row in rows:
        for amount_key, decimals_key, normalized_key in specs:
            expected = normalized_amount(row[amount_key], row[decimals_key])
            ok, actual, tolerance = within_tolerance(row[normalized_key], expected)
            if not ok:
                failures.append(
                    f"{title} {row['tx_hash']}#{row['log_index']} {normalized_key}: "
                    f"expected={expected} actual={actual} tolerance={tolerance}"
                )
        preview = " ".join(
            f"{normalized_key}={row[normalized_key]}"
            for _, _, normalized_key in specs
        )
        print(f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} {preview}")
    print()


failures = []

print("# Verify Semantic Amounts")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

verify_rows(
    "erc20_transfers_semantic",
    parse_json_lines("ERC20_SEMANTIC_SAMPLES"),
    [("amount", "token_decimals", "amount_normalized")],
    failures,
)
verify_rows(
    "uniswap_v2_swaps_semantic",
    parse_json_lines("V2_SWAP_SEMANTIC_SAMPLES"),
    [
        ("amount0_in", "token0_decimals", "amount0_in_normalized"),
        ("amount1_in", "token1_decimals", "amount1_in_normalized"),
        ("amount0_out", "token0_decimals", "amount0_out_normalized"),
        ("amount1_out", "token1_decimals", "amount1_out_normalized"),
    ],
    failures,
)
verify_rows(
    "uniswap_v2_mints_semantic",
    parse_json_lines("V2_MINT_SEMANTIC_SAMPLES"),
    [
        ("amount0", "token0_decimals", "amount0_normalized"),
        ("amount1", "token1_decimals", "amount1_normalized"),
    ],
    failures,
)
verify_rows(
    "uniswap_v2_burns_semantic",
    parse_json_lines("V2_BURN_SEMANTIC_SAMPLES"),
    [
        ("amount0", "token0_decimals", "amount0_normalized"),
        ("amount1", "token1_decimals", "amount1_normalized"),
    ],
    failures,
)
verify_rows(
    "uniswap_v3_swaps_semantic",
    parse_json_lines("V3_SWAP_SEMANTIC_SAMPLES"),
    [
        ("amount0", "token0_decimals", "amount0_normalized"),
        ("amount1", "token1_decimals", "amount1_normalized"),
    ],
    failures,
)
verify_rows(
    "uniswap_v3_mints_semantic",
    parse_json_lines("V3_MINT_SEMANTIC_SAMPLES"),
    [
        ("amount0", "token0_decimals", "amount0_normalized"),
        ("amount1", "token1_decimals", "amount1_normalized"),
    ],
    failures,
)
verify_rows(
    "uniswap_v3_burns_semantic",
    parse_json_lines("V3_BURN_SEMANTIC_SAMPLES"),
    [
        ("amount0", "token0_decimals", "amount0_normalized"),
        ("amount1", "token1_decimals", "amount1_normalized"),
    ],
    failures,
)

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Semantic amount verification passed.")
PY
