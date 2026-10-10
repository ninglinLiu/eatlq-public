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

sender_profile_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        toString(trade_count) AS trade_count,
        toString(v2_trade_count) AS v2_trade_count,
        toString(v3_trade_count) AS v3_trade_count,
        toString(distinct_venues) AS distinct_venues,
        toString(distinct_sold_assets) AS distinct_sold_assets,
        toString(distinct_bought_assets) AS distinct_bought_assets
    FROM eatlq.sender_swap_profile
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

sender_token_profile_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        token_address,
        token_symbol,
        toString(trade_touch_count) AS trade_touch_count,
        toString(sold_trade_count) AS sold_trade_count,
        toString(bought_trade_count) AS bought_trade_count,
        toString(total_sold_amount_normalized) AS total_sold_amount_normalized,
        toString(total_bought_amount_normalized) AS total_bought_amount_normalized,
        toString(net_amount_normalized) AS net_amount_normalized
    FROM eatlq.sender_token_trade_profile
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export SENDER_PROFILE_SAMPLES="$(query_json "${sender_profile_query}")"
export SENDER_TOKEN_PROFILE_SAMPLES="$(query_json "${sender_token_profile_query}")"

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


def query_clickhouse(sql):
    import subprocess

    compose_dir = os.path.join(os.getcwd(), "infra", "compose")
    cmd = [
        "docker", "compose",
        "--env-file", os.path.join(compose_dir, ".env"),
        "-f", os.path.join(compose_dir, "compose.yaml"),
        "exec", "-T", "clickhouse",
        "clickhouse-client", "--query", sql,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip().splitlines()


def decimal_equal(a_text, b_value):
    a = Decimal(a_text)
    b = Decimal(b_value)
    tolerance = max(Decimal("1e-12"), abs(b) * Decimal("1e-12"))
    return abs(a - b) <= tolerance, a, b, tolerance


failures = []

print("# Verify Wallet Profiles")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

sender_profiles = parse_json_lines("SENDER_PROFILE_SAMPLES")
if not sender_profiles:
    failures.append("sender_swap_profile returned no sample rows")
else:
    print("## sender_swap_profile")
    for row in sender_profiles:
        chain_id = row["chain_id"]
        sender_address = row["sender_address"]
        sql = f"""
SELECT
    count(),
    countIf(protocol_family = 'uniswap_v2'),
    countIf(protocol_family = 'uniswap_v3'),
    uniqExact(venue_address),
    uniqExact(sold_token_address),
    uniqExact(bought_token_address)
FROM eatlq.swap_trade_events_all
WHERE chain_id = {chain_id} AND sender_address = '{sender_address}'
FORMAT TSVRaw
"""
        result = query_clickhouse(sql)
        actual = result[0].split('\t')
        expected = [
            row["trade_count"],
            row["v2_trade_count"],
            row["v3_trade_count"],
            row["distinct_venues"],
            row["distinct_sold_assets"],
            row["distinct_bought_assets"],
        ]
        labels = [
            "trade_count",
            "v2_trade_count",
            "v3_trade_count",
            "distinct_venues",
            "distinct_sold_assets",
            "distinct_bought_assets",
        ]
        for label, actual_value, expected_value in zip(labels, actual, expected):
            if actual_value != expected_value:
                failures.append(
                    f"sender_swap_profile {sender_address} {label}: expected={expected_value} actual={actual_value}"
                )
        print(
            f"PASS sender={sender_address} trade_count={row['trade_count']} "
            f"v2={row['v2_trade_count']} v3={row['v3_trade_count']}"
        )
    print()

sender_token_profiles = parse_json_lines("SENDER_TOKEN_PROFILE_SAMPLES")
if not sender_token_profiles:
    failures.append("sender_token_trade_profile returned no sample rows")
else:
    print("## sender_token_trade_profile")
    for row in sender_token_profiles:
        chain_id = row["chain_id"]
        sender_address = row["sender_address"]
        token_address = row["token_address"]
        sql = f"""
SELECT
    count(),
    countIf(token_role = 'sold'),
    countIf(token_role = 'bought'),
    sumIf(amount_normalized, token_role = 'sold'),
    sumIf(amount_normalized, token_role = 'bought'),
    sumIf(if(token_role = 'bought', amount_normalized, -amount_normalized), 1)
FROM (
    SELECT
        'sold' AS token_role,
        sold_amount_normalized AS amount_normalized
    FROM eatlq.swap_trade_events_all
    WHERE chain_id = {chain_id} AND sender_address = '{sender_address}' AND sold_token_address = '{token_address}'

    UNION ALL

    SELECT
        'bought' AS token_role,
        bought_amount_normalized AS amount_normalized
    FROM eatlq.swap_trade_events_all
    WHERE chain_id = {chain_id} AND sender_address = '{sender_address}' AND bought_token_address = '{token_address}'
)
FORMAT TSVRaw
"""
        result = query_clickhouse(sql)
        actual = result[0].split('\t')
        count_labels = ["trade_touch_count", "sold_trade_count", "bought_trade_count"]
        for label, actual_value in zip(count_labels, actual[:3]):
            if actual_value != row[label]:
                failures.append(
                    f"sender_token_trade_profile {sender_address} {token_address} {label}: expected={row[label]} actual={actual_value}"
                )

        numeric_labels = [
            "total_sold_amount_normalized",
            "total_bought_amount_normalized",
            "net_amount_normalized",
        ]
        for label, actual_value in zip(numeric_labels, actual[3:]):
            ok, _, _, tol = decimal_equal(row[label], actual_value)
            if not ok:
                failures.append(
                    f"sender_token_trade_profile {sender_address} {token_address} {label}: expected={row[label]} actual={actual_value} tolerance={tol}"
                )
        print(
            f"PASS sender={sender_address} token={row['token_symbol']} "
            f"touches={row['trade_touch_count']} net={row['net_amount_normalized']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Wallet profile verification passed.")
PY
