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

matured_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(trade_count) AS trade_count,
        toString(next_day_trade_count) AS next_day_trade_count,
        protocol_preference,
        next_day_protocol_preference
    FROM eatlq.sender_training_examples_matured_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

split_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(split_bucket) AS split_bucket,
        dataset_split,
        toString(trade_count) AS trade_count,
        toString(next_day_trade_count) AS next_day_trade_count
    FROM eatlq.sender_training_examples_split_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export MATURED_SAMPLES="$(query_json "${matured_query}")"
export SPLIT_SAMPLES="$(query_json "${split_query}")"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import subprocess
import sys


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


failures = []

print("# Verify Training Splits")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

matured_rows = parse_json_lines("MATURED_SAMPLES")
if not matured_rows:
    failures.append("sender_training_examples_matured_v1 returned no sample rows")
else:
    print("## sender_training_examples_matured_v1")
    max_date_sql = "SELECT toString(max(activity_date)) FROM eatlq.sender_trade_day_rollup_v1 FORMAT TSVRaw"
    max_activity_date = query_clickhouse(max_date_sql)[0].split('\t')[0]
    for row in matured_rows:
        if row["activity_date"] >= max_activity_date:
            failures.append(
                f"sender_training_examples_matured_v1 {row['sender_address']} {row['activity_date']} should be older than max_activity_date={max_activity_date}"
            )

        example_sql = f"""
SELECT
    toString(trade_count),
    toString(next_day_trade_count),
    protocol_preference,
    next_day_protocol_preference
FROM eatlq.sender_training_examples_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        actual = query_clickhouse(example_sql)[0].split('\t')
        labels = [
            "trade_count",
            "next_day_trade_count",
            "protocol_preference",
            "next_day_protocol_preference",
        ]
        for label, actual_value in zip(labels, actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_examples_matured_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"next_day_trades={row['next_day_trade_count']}"
        )
    print()

split_rows = parse_json_lines("SPLIT_SAMPLES")
if not split_rows:
    failures.append("sender_training_examples_split_v1 returned no sample rows")
else:
    print("## sender_training_examples_split_v1")
    for row in split_rows:
        example_sql = f"""
SELECT
    toString(trade_count),
    toString(next_day_trade_count)
FROM eatlq.sender_training_examples_matured_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        actual = query_clickhouse(example_sql)[0].split('\t')
        if row["trade_count"] != actual[0]:
            failures.append(
                f"sender_training_examples_split_v1 {row['sender_address']} {row['activity_date']} trade_count: expected={actual[0]} actual={row['trade_count']}"
            )
        if row["next_day_trade_count"] != actual[1]:
            failures.append(
                f"sender_training_examples_split_v1 {row['sender_address']} {row['activity_date']} next_day_trade_count: expected={actual[1]} actual={row['next_day_trade_count']}"
            )

        bucket_sql = f"""
SELECT
    toString(cityHash64(toString({row['chain_id']}), '{row['sender_address']}', '{row['activity_date']}') % 100)
FORMAT TSVRaw
"""
        actual_bucket = query_clickhouse(bucket_sql)[0].split('\t')[0]
        if row["split_bucket"] != actual_bucket:
            failures.append(
                f"sender_training_examples_split_v1 {row['sender_address']} {row['activity_date']} split_bucket: expected={actual_bucket} actual={row['split_bucket']}"
            )

        bucket_value = int(actual_bucket)
        expected_split = "train" if bucket_value < 80 else "validation" if bucket_value < 90 else "test"
        if row["dataset_split"] != expected_split:
            failures.append(
                f"sender_training_examples_split_v1 {row['sender_address']} {row['activity_date']} dataset_split: expected={expected_split} actual={row['dataset_split']}"
            )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"bucket={row['split_bucket']} split={row['dataset_split']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Training split verification passed.")
PY
