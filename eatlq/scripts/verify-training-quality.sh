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

split_summary_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        dataset_split,
        toString(example_count) AS example_count,
        toString(next_day_active_examples) AS next_day_active_examples,
        toString(next_day_higher_trade_examples) AS next_day_higher_trade_examples,
        toString(next_day_cross_protocol_examples) AS next_day_cross_protocol_examples
    FROM eatlq.sender_training_split_summary_v1
    ORDER BY dataset_split
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

daily_summary_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        toString(example_count) AS example_count,
        toString(train_examples) AS train_examples,
        toString(validation_examples) AS validation_examples,
        toString(test_examples) AS test_examples,
        toString(next_day_active_examples) AS next_day_active_examples,
        toString(next_day_higher_trade_examples) AS next_day_higher_trade_examples,
        toString(next_day_cross_protocol_examples) AS next_day_cross_protocol_examples
    FROM eatlq.sender_training_daily_dataset_summary_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export SPLIT_SUMMARY_SAMPLES="$(query_json "${split_summary_query}")"
export DAILY_SUMMARY_SAMPLES="$(query_json "${daily_summary_query}")"

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

print("# Verify Training Quality")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

split_rows = parse_json_lines("SPLIT_SUMMARY_SAMPLES")
if not split_rows:
    failures.append("sender_training_split_summary_v1 returned no sample rows")
else:
    print("## sender_training_split_summary_v1")
    for row in split_rows:
        sql = f"""
SELECT
    count(),
    countIf(next_day_active_flag = 1),
    countIf(next_day_higher_trade_count_flag = 1),
    countIf(next_day_cross_protocol_flag = 1)
FROM eatlq.sender_training_examples_split_v1
WHERE chain_id = {row['chain_id']}
  AND dataset_split = '{row['dataset_split']}'
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "example_count",
            "next_day_active_examples",
            "next_day_higher_trade_examples",
            "next_day_cross_protocol_examples",
        ]
        for label, actual_value in zip(labels, actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_split_summary_v1 {row['dataset_split']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS split={row['dataset_split']} examples={row['example_count']} "
            f"next_day_active={row['next_day_active_examples']}"
        )
    print()

daily_rows = parse_json_lines("DAILY_SUMMARY_SAMPLES")
if not daily_rows:
    failures.append("sender_training_daily_dataset_summary_v1 returned no sample rows")
else:
    print("## sender_training_daily_dataset_summary_v1")
    for row in daily_rows:
        sql = f"""
SELECT
    count(),
    countIf(dataset_split = 'train'),
    countIf(dataset_split = 'validation'),
    countIf(dataset_split = 'test'),
    countIf(next_day_active_flag = 1),
    countIf(next_day_higher_trade_count_flag = 1),
    countIf(next_day_cross_protocol_flag = 1)
FROM eatlq.sender_training_examples_split_v1
WHERE chain_id = {row['chain_id']}
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "example_count",
            "train_examples",
            "validation_examples",
            "test_examples",
            "next_day_active_examples",
            "next_day_higher_trade_examples",
            "next_day_cross_protocol_examples",
        ]
        for label, actual_value in zip(labels, actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_daily_dataset_summary_v1 {row['activity_date']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS day={row['activity_date']} examples={row['example_count']} "
            f"train={row['train_examples']} validation={row['validation_examples']} test={row['test_examples']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Training quality verification passed.")
PY
