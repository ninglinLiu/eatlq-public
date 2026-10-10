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

forward_labels_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(next_day_trade_count) AS next_day_trade_count,
        toString(next_day_v2_trade_count) AS next_day_v2_trade_count,
        toString(next_day_v3_trade_count) AS next_day_v3_trade_count,
        toString(next_day_active_flag) AS next_day_active_flag,
        toString(next_day_higher_trade_count_flag) AS next_day_higher_trade_count_flag,
        toString(next_day_cross_protocol_flag) AS next_day_cross_protocol_flag,
        next_day_protocol_preference
    FROM eatlq.sender_forward_outcome_labels_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

training_examples_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(trade_count) AS trade_count,
        protocol_preference,
        activity_band,
        toString(next_day_trade_count) AS next_day_trade_count,
        toString(next_day_v2_trade_count) AS next_day_v2_trade_count,
        toString(next_day_v3_trade_count) AS next_day_v3_trade_count,
        toString(next_day_active_flag) AS next_day_active_flag,
        toString(next_day_higher_trade_count_flag) AS next_day_higher_trade_count_flag,
        toString(next_day_cross_protocol_flag) AS next_day_cross_protocol_flag,
        next_day_protocol_preference
    FROM eatlq.sender_training_examples_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export FORWARD_LABEL_SAMPLES="$(query_json "${forward_labels_query}")"
export TRAINING_EXAMPLE_SAMPLES="$(query_json "${training_examples_query}")"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import subprocess
import sys
from decimal import Decimal


def parse_json_lines(env_name):
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def query_clickhouse(sql):
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


def protocol_preference(v2_count, v3_count):
    trade_count = v2_count + v3_count
    if v2_count > 0 and v3_count == 0:
        return "v2_only"
    if v3_count > 0 and v2_count == 0:
        return "v3_only"
    if trade_count > 0 and Decimal(v2_count) / Decimal(trade_count) >= Decimal("0.8"):
        return "v2_leaning"
    if trade_count > 0 and Decimal(v3_count) / Decimal(trade_count) >= Decimal("0.8"):
        return "v3_leaning"
    return "mixed"


failures = []

print("# Verify Outcome Labels")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

forward_rows = parse_json_lines("FORWARD_LABEL_SAMPLES")
if not forward_rows:
    failures.append("sender_forward_outcome_labels_v1 returned no sample rows")
else:
    print("## sender_forward_outcome_labels_v1")
    for row in forward_rows:
        sql = f"""
SELECT
    toString(trade_count),
    toString(v2_trade_count),
    toString(v3_trade_count)
FROM eatlq.sender_trade_day_rollup_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = addDays(toDate('{row['activity_date']}'), 1)
FORMAT TSVRaw
"""
        result = query_clickhouse(sql)
        if result:
            next_day_trade_count, next_day_v2_trade_count, next_day_v3_trade_count = result[0].split('\t')
        else:
            next_day_trade_count, next_day_v2_trade_count, next_day_v3_trade_count = "0", "0", "0"

        expected = {
            "next_day_trade_count": next_day_trade_count,
            "next_day_v2_trade_count": next_day_v2_trade_count,
            "next_day_v3_trade_count": next_day_v3_trade_count,
            "next_day_active_flag": "1" if int(next_day_trade_count) > 0 else "0",
            "next_day_cross_protocol_flag": "1" if int(next_day_v2_trade_count) > 0 and int(next_day_v3_trade_count) > 0 else "0",
            "next_day_protocol_preference": protocol_preference(int(next_day_v2_trade_count), int(next_day_v3_trade_count)),
        }

        current_trade_sql = f"""
SELECT toString(trade_count)
FROM eatlq.sender_trade_day_rollup_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        current_trade_count = query_clickhouse(current_trade_sql)[0].split('\t')[0]
        expected["next_day_higher_trade_count_flag"] = "1" if int(next_day_trade_count) > int(current_trade_count) else "0"

        for label, expected_value in expected.items():
            if row[label] != expected_value:
                failures.append(
                    f"sender_forward_outcome_labels_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={expected_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"next_day_trades={row['next_day_trade_count']} next_day_protocol={row['next_day_protocol_preference']}"
        )
    print()

example_rows = parse_json_lines("TRAINING_EXAMPLE_SAMPLES")
if not example_rows:
    failures.append("sender_training_examples_v1 returned no sample rows")
else:
    print("## sender_training_examples_v1")
    for row in example_rows:
        feature_sql = f"""
SELECT
    toString(trade_count),
    protocol_preference,
    activity_band
FROM eatlq.sender_training_dataset_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        feature_actual = query_clickhouse(feature_sql)[0].split('\t')
        feature_labels = ["trade_count", "protocol_preference", "activity_band"]
        for label, actual_value in zip(feature_labels, feature_actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_examples_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )

        label_sql = f"""
SELECT
    toString(next_day_trade_count),
    toString(next_day_v2_trade_count),
    toString(next_day_v3_trade_count),
    toString(next_day_active_flag),
    toString(next_day_higher_trade_count_flag),
    toString(next_day_cross_protocol_flag),
    next_day_protocol_preference
FROM eatlq.sender_forward_outcome_labels_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        label_actual = query_clickhouse(label_sql)[0].split('\t')
        label_labels = [
            "next_day_trade_count",
            "next_day_v2_trade_count",
            "next_day_v3_trade_count",
            "next_day_active_flag",
            "next_day_higher_trade_count_flag",
            "next_day_cross_protocol_flag",
            "next_day_protocol_preference",
        ]
        for label, actual_value in zip(label_labels, label_actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_examples_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"current_protocol={row['protocol_preference']} next_day_active={row['next_day_active_flag']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Outcome label verification passed.")
PY
