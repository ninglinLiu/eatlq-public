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

ready_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        dataset_split,
        supervised_target_class,
        next_day_activity_bucket,
        next_day_growth_bucket,
        toString(next_day_trade_count) AS next_day_trade_count,
        next_day_protocol_preference
    FROM eatlq.sender_training_examples_ready_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

balance_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        dataset_split,
        supervised_target_class,
        next_day_activity_bucket,
        toString(example_count) AS example_count,
        toString(higher_trade_positive_examples) AS higher_trade_positive_examples,
        toString(cross_protocol_positive_examples) AS cross_protocol_positive_examples
    FROM eatlq.sender_training_label_balance_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export READY_SAMPLES="$(query_json "${ready_query}")"
export BALANCE_SAMPLES="$(query_json "${balance_query}")"

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


def next_day_activity_bucket(next_day_trade_count):
    if next_day_trade_count == 0:
        return "inactive"
    if next_day_trade_count >= 20:
        return "hyperactive"
    if next_day_trade_count >= 5:
        return "active"
    return "light"


def next_day_growth_bucket(next_day_trade_count, higher_flag):
    if next_day_trade_count == 0:
        return "inactive"
    if higher_flag == 1:
        return "higher"
    return "flat_or_lower"


def supervised_target_class(next_day_active_flag, next_day_protocol_preference):
    if next_day_active_flag == 0:
        return "inactive"
    mapping = {
        "v2_only": "active_v2_only",
        "v3_only": "active_v3_only",
        "v2_leaning": "active_v2_leaning",
        "v3_leaning": "active_v3_leaning",
    }
    return mapping.get(next_day_protocol_preference, "active_mixed")


failures = []

print("# Verify Training Readiness")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

ready_rows = parse_json_lines("READY_SAMPLES")
if not ready_rows:
    failures.append("sender_training_examples_ready_v1 returned no sample rows")
else:
    print("## sender_training_examples_ready_v1")
    for row in ready_rows:
        sql = f"""
SELECT
    toString(next_day_trade_count),
    toString(next_day_active_flag),
    toString(next_day_higher_trade_count_flag),
    next_day_protocol_preference
FROM eatlq.sender_training_examples_split_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        next_day_trade_count, next_day_active_flag, next_day_higher_flag, next_day_protocol = query_clickhouse(sql)[0].split('\t')
        expected = {
            "next_day_activity_bucket": next_day_activity_bucket(int(next_day_trade_count)),
            "next_day_growth_bucket": next_day_growth_bucket(int(next_day_trade_count), int(next_day_higher_flag)),
            "supervised_target_class": supervised_target_class(int(next_day_active_flag), next_day_protocol),
            "next_day_trade_count": next_day_trade_count,
            "next_day_protocol_preference": next_day_protocol,
        }
        for label, expected_value in expected.items():
            if row[label] != expected_value:
                failures.append(
                    f"sender_training_examples_ready_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={expected_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"target={row['supervised_target_class']} bucket={row['next_day_activity_bucket']}"
        )
    print()

balance_rows = parse_json_lines("BALANCE_SAMPLES")
if not balance_rows:
    failures.append("sender_training_label_balance_v1 returned no sample rows")
else:
    print("## sender_training_label_balance_v1")
    for row in balance_rows:
        sql = f"""
SELECT
    count(),
    countIf(next_day_higher_trade_count_flag = 1),
    countIf(next_day_cross_protocol_flag = 1)
FROM eatlq.sender_training_examples_ready_v1
WHERE chain_id = {row['chain_id']}
  AND dataset_split = '{row['dataset_split']}'
  AND supervised_target_class = '{row['supervised_target_class']}'
  AND next_day_activity_bucket = '{row['next_day_activity_bucket']}'
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "example_count",
            "higher_trade_positive_examples",
            "cross_protocol_positive_examples",
        ]
        for label, actual_value in zip(labels, actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_training_label_balance_v1 {row['dataset_split']} {row['supervised_target_class']} {row['next_day_activity_bucket']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS split={row['dataset_split']} target={row['supervised_target_class']} "
            f"bucket={row['next_day_activity_bucket']} examples={row['example_count']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Training readiness verification passed.")
PY
