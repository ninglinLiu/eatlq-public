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

v2_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        tx_hash,
        toString(log_index) AS log_index,
        token0_symbol,
        token1_symbol,
        toString(amount0_in_normalized) AS amount0_in_normalized,
        toString(amount1_in_normalized) AS amount1_in_normalized,
        toString(amount0_out_normalized) AS amount0_out_normalized,
        toString(amount1_out_normalized) AS amount1_out_normalized,
        swap_direction,
        sold_token_symbol,
        bought_token_symbol,
        toString(sold_amount_normalized) AS sold_amount_normalized,
        toString(bought_amount_normalized) AS bought_amount_normalized
    FROM eatlq.uniswap_v2_swaps_trade_semantic
    WHERE has_registry_match = 1 AND has_clear_direction = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v3_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        tx_hash,
        toString(log_index) AS log_index,
        token0_symbol,
        token1_symbol,
        toString(amount0_normalized) AS amount0_normalized,
        toString(amount1_normalized) AS amount1_normalized,
        swap_direction,
        sold_token_symbol,
        bought_token_symbol,
        toString(sold_amount_normalized) AS sold_amount_normalized,
        toString(bought_amount_normalized) AS bought_amount_normalized
    FROM eatlq.uniswap_v3_swaps_trade_semantic
    WHERE has_registry_match = 1 AND has_clear_direction = 1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export V2_TRADE_SAMPLES="$(query_json "${v2_query}")"
export V3_TRADE_SAMPLES="$(query_json "${v3_query}")"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import sys
from decimal import Decimal


def parse_json_lines(env_name):
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def approx_equal(a_text, b_value):
    actual = Decimal(a_text)
    expected = Decimal(b_value)
    tolerance = max(Decimal("1e-12"), abs(expected) * Decimal("1e-12"))
    return abs(actual - expected) <= tolerance, actual, expected, tolerance


def verify_v2(rows, failures):
    if not rows:
        failures.append("uniswap_v2_swaps_trade_semantic returned no sample rows")
        return

    print("## uniswap_v2_swaps_trade_semantic")
    for row in rows:
        amount0_in = Decimal(row["amount0_in_normalized"])
        amount1_in = Decimal(row["amount1_in_normalized"])
        amount0_out = Decimal(row["amount0_out_normalized"])
        amount1_out = Decimal(row["amount1_out_normalized"])

        if amount0_in > 0 and amount1_out > 0 and amount1_in == 0 and amount0_out == 0:
            expected_direction = "token0_to_token1"
            expected_sold_symbol = row["token0_symbol"]
            expected_bought_symbol = row["token1_symbol"]
            expected_sold_amount = amount0_in
            expected_bought_amount = amount1_out
        elif amount1_in > 0 and amount0_out > 0 and amount0_in == 0 and amount1_out == 0:
            expected_direction = "token1_to_token0"
            expected_sold_symbol = row["token1_symbol"]
            expected_bought_symbol = row["token0_symbol"]
            expected_sold_amount = amount1_in
            expected_bought_amount = amount0_out
        else:
            failures.append(f"v2 trade sample is not clear-direction: {row['tx_hash']}#{row['log_index']}")
            continue

        if row["swap_direction"] != expected_direction:
            failures.append(f"v2 direction mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_direction} actual={row['swap_direction']}")
        if row["sold_token_symbol"] != expected_sold_symbol:
            failures.append(f"v2 sold token mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_sold_symbol} actual={row['sold_token_symbol']}")
        if row["bought_token_symbol"] != expected_bought_symbol:
            failures.append(f"v2 bought token mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_bought_symbol} actual={row['bought_token_symbol']}")

        ok_sold, actual_sold, _, sold_tol = approx_equal(row["sold_amount_normalized"], expected_sold_amount)
        ok_bought, actual_bought, _, bought_tol = approx_equal(row["bought_amount_normalized"], expected_bought_amount)
        if not ok_sold:
            failures.append(f"v2 sold amount mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_sold_amount} actual={actual_sold} tolerance={sold_tol}")
        if not ok_bought:
            failures.append(f"v2 bought amount mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_bought_amount} actual={actual_bought} tolerance={bought_tol}")

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"direction={row['swap_direction']} sold={row['sold_token_symbol']}:{row['sold_amount_normalized']} "
            f"bought={row['bought_token_symbol']}:{row['bought_amount_normalized']}"
        )
    print()


def verify_v3(rows, failures):
    if not rows:
        failures.append("uniswap_v3_swaps_trade_semantic returned no sample rows")
        return

    print("## uniswap_v3_swaps_trade_semantic")
    for row in rows:
        amount0 = Decimal(row["amount0_normalized"])
        amount1 = Decimal(row["amount1_normalized"])

        if amount0 > 0 and amount1 < 0:
            expected_direction = "token0_to_token1"
            expected_sold_symbol = row["token0_symbol"]
            expected_bought_symbol = row["token1_symbol"]
            expected_sold_amount = amount0
            expected_bought_amount = abs(amount1)
        elif amount1 > 0 and amount0 < 0:
            expected_direction = "token1_to_token0"
            expected_sold_symbol = row["token1_symbol"]
            expected_bought_symbol = row["token0_symbol"]
            expected_sold_amount = amount1
            expected_bought_amount = abs(amount0)
        else:
            failures.append(f"v3 trade sample is not clear-direction: {row['tx_hash']}#{row['log_index']}")
            continue

        if row["swap_direction"] != expected_direction:
            failures.append(f"v3 direction mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_direction} actual={row['swap_direction']}")
        if row["sold_token_symbol"] != expected_sold_symbol:
            failures.append(f"v3 sold token mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_sold_symbol} actual={row['sold_token_symbol']}")
        if row["bought_token_symbol"] != expected_bought_symbol:
            failures.append(f"v3 bought token mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_bought_symbol} actual={row['bought_token_symbol']}")

        ok_sold, actual_sold, _, sold_tol = approx_equal(row["sold_amount_normalized"], expected_sold_amount)
        ok_bought, actual_bought, _, bought_tol = approx_equal(row["bought_amount_normalized"], expected_bought_amount)
        if not ok_sold:
            failures.append(f"v3 sold amount mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_sold_amount} actual={actual_sold} tolerance={sold_tol}")
        if not ok_bought:
            failures.append(f"v3 bought amount mismatch {row['tx_hash']}#{row['log_index']}: expected={expected_bought_amount} actual={actual_bought} tolerance={bought_tol}")

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"direction={row['swap_direction']} sold={row['sold_token_symbol']}:{row['sold_amount_normalized']} "
            f"bought={row['bought_token_symbol']}:{row['bought_amount_normalized']}"
        )
    print()


failures = []

print("# Verify Semantic Trades")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

verify_v2(parse_json_lines("V2_TRADE_SAMPLES"), failures)
verify_v3(parse_json_lines("V3_TRADE_SAMPLES"), failures)

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Semantic trade verification passed.")
PY
