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

candidates_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(trade_count) AS trade_count,
        protocol_preference,
        activity_band,
        inference_priority_band,
        inference_venue_shape,
        inference_token_shape,
        toString(min_activity_gate) AS min_activity_gate,
        toString(v3_bias_gate) AS v3_bias_gate,
        toString(venue_diversity_gate) AS venue_diversity_gate
    FROM eatlq.sender_inference_candidates_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

summary_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        inference_priority_band,
        protocol_preference,
        activity_band,
        toString(candidate_count) AS candidate_count,
        toString(min_activity_gate_count) AS min_activity_gate_count,
        toString(v3_bias_gate_count) AS v3_bias_gate_count,
        toString(venue_diversity_gate_count) AS venue_diversity_gate_count
    FROM eatlq.sender_inference_cohort_summary_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export CANDIDATE_SAMPLES="$(query_json "${candidates_query}")"
export SUMMARY_SAMPLES="$(query_json "${summary_query}")"

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


def priority_band(trade_count, v3_share):
    if trade_count >= 100 and v3_share >= Decimal("0.8"):
        return "high_priority_v3"
    if trade_count >= 100:
        return "high_priority_mixed"
    if trade_count >= 20:
        return "medium_priority"
    return "tail"


def concentration_band(value, single, multi):
    if value >= Decimal("0.9"):
        return single
    if value >= Decimal("0.6"):
        return "concentrated"
    if value >= Decimal("0.35"):
        return multi
    return "distributed"


failures = []

print("# Verify Inference Inputs")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

candidate_rows = parse_json_lines("CANDIDATE_SAMPLES")
if not candidate_rows:
    failures.append("sender_inference_candidates_v1 returned no sample rows")
else:
    print("## sender_inference_candidates_v1")
    max_date_sql = "SELECT toString(max(activity_date)) FROM eatlq.sender_training_dataset_v1 FORMAT TSVRaw"
    max_activity_date = query_clickhouse(max_date_sql)[0].split('\t')[0]
    for row in candidate_rows:
        if row["activity_date"] != max_activity_date:
            failures.append(
                f"sender_inference_candidates_v1 {row['sender_address']} activity_date expected={max_activity_date} actual={row['activity_date']}"
            )

        sql = f"""
SELECT
    toString(trade_count),
    toString(v3_trade_share),
    toString(venue_concentration_ratio),
    toString(token_concentration_ratio),
    protocol_preference,
    activity_band,
    toString(distinct_venues)
FROM eatlq.sender_training_dataset_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        trade_count, v3_share, venue_ratio, token_ratio, protocol_preference, activity_band, distinct_venues = query_clickhouse(sql)[0].split('\t')
        expected = {
            "trade_count": trade_count,
            "protocol_preference": protocol_preference,
            "activity_band": activity_band,
            "inference_priority_band": priority_band(int(trade_count), Decimal(v3_share)),
            "inference_venue_shape": concentration_band(Decimal(venue_ratio), "single_venue", "multi_venue"),
            "inference_token_shape": concentration_band(Decimal(token_ratio), "single_token", "multi_token"),
            "min_activity_gate": "1" if int(trade_count) >= 20 else "0",
            "v3_bias_gate": "1" if Decimal(v3_share) >= Decimal("0.8") else "0",
            "venue_diversity_gate": "1" if int(distinct_venues) >= 2 else "0",
        }
        for label, expected_value in expected.items():
            if row[label] != expected_value:
                failures.append(
                    f"sender_inference_candidates_v1 {row['sender_address']} {label}: "
                    f"expected={expected_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"priority={row['inference_priority_band']} activity={row['activity_band']}"
        )
    print()

summary_rows = parse_json_lines("SUMMARY_SAMPLES")
if not summary_rows:
    failures.append("sender_inference_cohort_summary_v1 returned no sample rows")
else:
    print("## sender_inference_cohort_summary_v1")
    for row in summary_rows:
        sql = f"""
SELECT
    count(),
    countIf(min_activity_gate = 1),
    countIf(v3_bias_gate = 1),
    countIf(venue_diversity_gate = 1)
FROM eatlq.sender_inference_candidates_v1
WHERE chain_id = {row['chain_id']}
  AND activity_date = toDate('{row['activity_date']}')
  AND inference_priority_band = '{row['inference_priority_band']}'
  AND protocol_preference = '{row['protocol_preference']}'
  AND activity_band = '{row['activity_band']}'
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "candidate_count",
            "min_activity_gate_count",
            "v3_bias_gate_count",
            "venue_diversity_gate_count",
        ]
        for label, actual_value in zip(labels, actual):
            if row[label] != actual_value:
                failures.append(
                    f"sender_inference_cohort_summary_v1 {row['inference_priority_band']} {row['protocol_preference']} {row['activity_band']} {label}: "
                    f"expected={actual_value} actual={row[label]}"
                )
        print(
            f"PASS day={row['activity_date']} priority={row['inference_priority_band']} "
            f"protocol={row['protocol_preference']} candidates={row['candidate_count']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Inference input verification passed.")
PY
