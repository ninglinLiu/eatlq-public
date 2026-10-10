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

v3_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v3_swaps_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        amount0_raw,
        toString(amount0) AS amount0,
        amount1_raw,
        toString(amount1) AS amount1,
        sqrt_price_x96_raw,
        toString(sqrt_price_x96) AS sqrt_price_x96,
        liquidity_raw,
        toString(liquidity) AS liquidity,
        tick_raw,
        toString(tick) AS tick
    FROM eatlq.uniswap_v3_swaps_decoded
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v2_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'uniswap_v2_swaps_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        amount0_in_raw,
        toString(amount0_in) AS amount0_in,
        amount1_in_raw,
        toString(amount1_in) AS amount1_in,
        amount0_out_raw,
        toString(amount0_out) AS amount0_out,
        amount1_out_raw,
        toString(amount1_out) AS amount1_out
    FROM eatlq.uniswap_v2_swaps_decoded
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
        'uniswap_v2_mints_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        amount0_raw,
        toString(amount0) AS amount0,
        amount1_raw,
        toString(amount1) AS amount1
    FROM eatlq.uniswap_v2_mints_decoded
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
        'uniswap_v2_burns_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        amount0_raw,
        toString(amount0) AS amount0,
        amount1_raw,
        toString(amount1) AS amount1
    FROM eatlq.uniswap_v2_burns_decoded
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
        'uniswap_v3_mints_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        tick_lower_raw,
        toString(tick_lower) AS tick_lower,
        tick_upper_raw,
        toString(tick_upper) AS tick_upper,
        liquidity_raw,
        toString(liquidity) AS liquidity,
        amount0_raw,
        toString(amount0) AS amount0,
        amount1_raw,
        toString(amount1) AS amount1
    FROM eatlq.uniswap_v3_mints_decoded
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
        'uniswap_v3_burns_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        tick_lower_raw,
        toString(tick_lower) AS tick_lower,
        tick_upper_raw,
        toString(tick_upper) AS tick_upper,
        liquidity_raw,
        toString(liquidity) AS liquidity,
        amount0_raw,
        toString(amount0) AS amount0,
        amount1_raw,
        toString(amount1) AS amount1
    FROM eatlq.uniswap_v3_burns_decoded
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

erc20_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        'erc20_transfers_decoded' AS surface,
        tx_hash,
        toString(log_index) AS log_index,
        amount_raw,
        toString(amount) AS amount
    FROM eatlq.erc20_transfers_decoded
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

v3_samples="$(query_json "${v3_query}")"
v2_samples="$(query_json "${v2_query}")"
v2_mint_samples="$(query_json "${v2_mint_query}")"
v2_burn_samples="$(query_json "${v2_burn_query}")"
v3_mint_samples="$(query_json "${v3_mint_query}")"
v3_burn_samples="$(query_json "${v3_burn_query}")"
erc20_samples="$(query_json "${erc20_query}")"

export SAMPLE_LIMIT
export V3_SAMPLES="${v3_samples}"
export V2_SAMPLES="${v2_samples}"
export V2_MINT_SAMPLES="${v2_mint_samples}"
export V2_BURN_SAMPLES="${v2_burn_samples}"
export V3_MINT_SAMPLES="${v3_mint_samples}"
export V3_BURN_SAMPLES="${v3_burn_samples}"
export ERC20_SAMPLES="${erc20_samples}"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import sys


def parse_unsigned(value):
    body = value[2:] if value.startswith("0x") else value
    return int(body, 16)


def parse_signed_256(value):
    number = parse_unsigned(value)
    if number >= 1 << 255:
        number -= 1 << 256
    return number


def parse_json_lines(env_name):
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def assert_equal(label, actual, expected, failures):
    if actual != expected:
        failures.append(f"{label}: expected={expected} actual={actual}")


failures = []

print("# Verify Decoded Values")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

v3_rows = parse_json_lines("V3_SAMPLES")
if not v3_rows:
    failures.append("uniswap_v3_swaps_decoded returned no sample rows")
else:
    print("## uniswap_v3_swaps_decoded")
    for row in v3_rows:
        expected_amount0 = parse_signed_256(row["amount0_raw"])
        expected_amount1 = parse_signed_256(row["amount1_raw"])
        expected_sqrt_price = parse_unsigned(row["sqrt_price_x96_raw"])
        expected_liquidity = parse_unsigned(row["liquidity_raw"])
        expected_tick = parse_signed_256(row["tick_raw"])

        actual_amount0 = int(row["amount0"])
        actual_amount1 = int(row["amount1"])
        actual_sqrt_price = int(row["sqrt_price_x96"])
        actual_liquidity = int(row["liquidity"])
        actual_tick = int(row["tick"])

        assert_equal(f"v3 amount0 {row['tx_hash']}#{row['log_index']}", actual_amount0, expected_amount0, failures)
        assert_equal(f"v3 amount1 {row['tx_hash']}#{row['log_index']}", actual_amount1, expected_amount1, failures)
        assert_equal(f"v3 sqrt_price_x96 {row['tx_hash']}#{row['log_index']}", actual_sqrt_price, expected_sqrt_price, failures)
        assert_equal(f"v3 liquidity {row['tx_hash']}#{row['log_index']}", actual_liquidity, expected_liquidity, failures)
        assert_equal(f"v3 tick {row['tx_hash']}#{row['log_index']}", actual_tick, expected_tick, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"amount0={actual_amount0} amount1={actual_amount1} tick={actual_tick}"
        )
    print()

v2_rows = parse_json_lines("V2_SAMPLES")
if not v2_rows:
    failures.append("uniswap_v2_swaps_decoded returned no sample rows")
else:
    print("## uniswap_v2_swaps_decoded")
    for row in v2_rows:
        expected_amount0_in = parse_unsigned(row["amount0_in_raw"])
        expected_amount1_in = parse_unsigned(row["amount1_in_raw"])
        expected_amount0_out = parse_unsigned(row["amount0_out_raw"])
        expected_amount1_out = parse_unsigned(row["amount1_out_raw"])

        actual_amount0_in = int(row["amount0_in"])
        actual_amount1_in = int(row["amount1_in"])
        actual_amount0_out = int(row["amount0_out"])
        actual_amount1_out = int(row["amount1_out"])

        assert_equal(f"v2 amount0_in {row['tx_hash']}#{row['log_index']}", actual_amount0_in, expected_amount0_in, failures)
        assert_equal(f"v2 amount1_in {row['tx_hash']}#{row['log_index']}", actual_amount1_in, expected_amount1_in, failures)
        assert_equal(f"v2 amount0_out {row['tx_hash']}#{row['log_index']}", actual_amount0_out, expected_amount0_out, failures)
        assert_equal(f"v2 amount1_out {row['tx_hash']}#{row['log_index']}", actual_amount1_out, expected_amount1_out, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"amount0_in={actual_amount0_in} amount1_in={actual_amount1_in} "
            f"amount0_out={actual_amount0_out} amount1_out={actual_amount1_out}"
        )
    print()

v2_mint_rows = parse_json_lines("V2_MINT_SAMPLES")
if not v2_mint_rows:
    failures.append("uniswap_v2_mints_decoded returned no sample rows")
else:
    print("## uniswap_v2_mints_decoded")
    for row in v2_mint_rows:
        expected_amount0 = parse_unsigned(row["amount0_raw"])
        expected_amount1 = parse_unsigned(row["amount1_raw"])

        actual_amount0 = int(row["amount0"])
        actual_amount1 = int(row["amount1"])

        assert_equal(f"v2 mint amount0 {row['tx_hash']}#{row['log_index']}", actual_amount0, expected_amount0, failures)
        assert_equal(f"v2 mint amount1 {row['tx_hash']}#{row['log_index']}", actual_amount1, expected_amount1, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"amount0={actual_amount0} amount1={actual_amount1}"
        )
    print()

v2_burn_rows = parse_json_lines("V2_BURN_SAMPLES")
if not v2_burn_rows:
    failures.append("uniswap_v2_burns_decoded returned no sample rows")
else:
    print("## uniswap_v2_burns_decoded")
    for row in v2_burn_rows:
        expected_amount0 = parse_unsigned(row["amount0_raw"])
        expected_amount1 = parse_unsigned(row["amount1_raw"])

        actual_amount0 = int(row["amount0"])
        actual_amount1 = int(row["amount1"])

        assert_equal(f"v2 burn amount0 {row['tx_hash']}#{row['log_index']}", actual_amount0, expected_amount0, failures)
        assert_equal(f"v2 burn amount1 {row['tx_hash']}#{row['log_index']}", actual_amount1, expected_amount1, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"amount0={actual_amount0} amount1={actual_amount1}"
        )
    print()

v3_mint_rows = parse_json_lines("V3_MINT_SAMPLES")
if not v3_mint_rows:
    failures.append("uniswap_v3_mints_decoded returned no sample rows")
else:
    print("## uniswap_v3_mints_decoded")
    for row in v3_mint_rows:
        expected_tick_lower = parse_signed_256(row["tick_lower_raw"])
        expected_tick_upper = parse_signed_256(row["tick_upper_raw"])
        expected_liquidity = parse_unsigned(row["liquidity_raw"])
        expected_amount0 = parse_unsigned(row["amount0_raw"])
        expected_amount1 = parse_unsigned(row["amount1_raw"])

        actual_tick_lower = int(row["tick_lower"])
        actual_tick_upper = int(row["tick_upper"])
        actual_liquidity = int(row["liquidity"])
        actual_amount0 = int(row["amount0"])
        actual_amount1 = int(row["amount1"])

        assert_equal(f"v3 mint tick_lower {row['tx_hash']}#{row['log_index']}", actual_tick_lower, expected_tick_lower, failures)
        assert_equal(f"v3 mint tick_upper {row['tx_hash']}#{row['log_index']}", actual_tick_upper, expected_tick_upper, failures)
        assert_equal(f"v3 mint liquidity {row['tx_hash']}#{row['log_index']}", actual_liquidity, expected_liquidity, failures)
        assert_equal(f"v3 mint amount0 {row['tx_hash']}#{row['log_index']}", actual_amount0, expected_amount0, failures)
        assert_equal(f"v3 mint amount1 {row['tx_hash']}#{row['log_index']}", actual_amount1, expected_amount1, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"tick_lower={actual_tick_lower} tick_upper={actual_tick_upper} liquidity={actual_liquidity}"
        )
    print()

v3_burn_rows = parse_json_lines("V3_BURN_SAMPLES")
if not v3_burn_rows:
    failures.append("uniswap_v3_burns_decoded returned no sample rows")
else:
    print("## uniswap_v3_burns_decoded")
    for row in v3_burn_rows:
        expected_tick_lower = parse_signed_256(row["tick_lower_raw"])
        expected_tick_upper = parse_signed_256(row["tick_upper_raw"])
        expected_liquidity = parse_unsigned(row["liquidity_raw"])
        expected_amount0 = parse_unsigned(row["amount0_raw"])
        expected_amount1 = parse_unsigned(row["amount1_raw"])

        actual_tick_lower = int(row["tick_lower"])
        actual_tick_upper = int(row["tick_upper"])
        actual_liquidity = int(row["liquidity"])
        actual_amount0 = int(row["amount0"])
        actual_amount1 = int(row["amount1"])

        assert_equal(f"v3 burn tick_lower {row['tx_hash']}#{row['log_index']}", actual_tick_lower, expected_tick_lower, failures)
        assert_equal(f"v3 burn tick_upper {row['tx_hash']}#{row['log_index']}", actual_tick_upper, expected_tick_upper, failures)
        assert_equal(f"v3 burn liquidity {row['tx_hash']}#{row['log_index']}", actual_liquidity, expected_liquidity, failures)
        assert_equal(f"v3 burn amount0 {row['tx_hash']}#{row['log_index']}", actual_amount0, expected_amount0, failures)
        assert_equal(f"v3 burn amount1 {row['tx_hash']}#{row['log_index']}", actual_amount1, expected_amount1, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} "
            f"tick_lower={actual_tick_lower} tick_upper={actual_tick_upper} liquidity={actual_liquidity}"
        )
    print()

erc20_rows = parse_json_lines("ERC20_SAMPLES")
if not erc20_rows:
    failures.append("erc20_transfers_decoded returned no sample rows")
else:
    print("## erc20_transfers_decoded")
    for row in erc20_rows:
        expected_amount = parse_unsigned(row["amount_raw"])
        actual_amount = int(row["amount"])

        assert_equal(f"erc20 amount {row['tx_hash']}#{row['log_index']}", actual_amount, expected_amount, failures)

        print(
            f"PASS tx_hash={row['tx_hash']} log_index={row['log_index']} amount={actual_amount}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Decoded value verification passed.")
PY
