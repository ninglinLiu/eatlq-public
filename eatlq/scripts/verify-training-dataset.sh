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

day_rollup_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(trade_count) AS trade_count,
        toString(v2_trade_count) AS v2_trade_count,
        toString(v3_trade_count) AS v3_trade_count,
        toString(distinct_venues) AS distinct_venues,
        toString(distinct_recipients) AS distinct_recipients,
        toString(distinct_sold_assets) AS distinct_sold_assets,
        toString(distinct_bought_assets) AS distinct_bought_assets
    FROM eatlq.sender_trade_day_rollup_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

training_dataset_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        toString(activity_date) AS activity_date,
        sender_address,
        toString(trade_count) AS trade_count,
        toString(v2_trade_count) AS v2_trade_count,
        toString(v3_trade_count) AS v3_trade_count,
        toString(v2_trade_share) AS v2_trade_share,
        toString(v3_trade_share) AS v3_trade_share,
        toString(distinct_venues) AS distinct_venues,
        toString(distinct_recipients) AS distinct_recipients,
        toString(distinct_sold_assets) AS distinct_sold_assets,
        toString(distinct_bought_assets) AS distinct_bought_assets,
        toString(top_venue_trade_count) AS top_venue_trade_count,
        toString(venue_concentration_ratio) AS venue_concentration_ratio,
        toString(top_token_trade_touch_count) AS top_token_trade_touch_count,
        toString(total_token_touches) AS total_token_touches,
        toString(token_concentration_ratio) AS token_concentration_ratio,
        protocol_preference,
        venue_concentration_band,
        token_concentration_band,
        activity_band,
        recipient_diversity_band,
        toString(cross_protocol_flag) AS cross_protocol_flag,
        toString(multi_venue_flag) AS multi_venue_flag
    FROM eatlq.sender_training_dataset_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export DAY_ROLLUP_SAMPLES="$(query_json "${day_rollup_query}")"
export TRAINING_DATASET_SAMPLES="$(query_json "${training_dataset_query}")"

"${PYTHON_BIN}" - <<'PY'
import json
import os
import subprocess
import sys
from decimal import Decimal


EPSILON = Decimal("1e-12")


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


def decimal_equal(expected_text, actual_text):
    expected = Decimal(expected_text)
    actual = Decimal(actual_text)
    tolerance = max(EPSILON, abs(actual) * EPSILON)
    return abs(expected - actual) <= tolerance, tolerance


def protocol_preference(v2_count, v3_count, v2_share, v3_share):
    if v2_count > 0 and v3_count == 0:
        return "v2_only"
    if v3_count > 0 and v2_count == 0:
        return "v3_only"
    if v2_share >= Decimal("0.8"):
        return "v2_leaning"
    if v3_share >= Decimal("0.8"):
        return "v3_leaning"
    return "mixed"


def venue_band(value):
    if value >= Decimal("0.9"):
        return "single_venue"
    if value >= Decimal("0.6"):
        return "concentrated"
    if value >= Decimal("0.35"):
        return "multi_venue"
    return "distributed"


def token_band(value):
    if value >= Decimal("0.9"):
        return "single_token"
    if value >= Decimal("0.6"):
        return "concentrated"
    if value >= Decimal("0.35"):
        return "multi_token"
    return "distributed"


def activity_band(trade_count):
    if trade_count >= 20:
        return "hyperactive"
    if trade_count >= 5:
        return "active"
    if trade_count >= 1:
        return "steady"
    return "sporadic"


def recipient_band(recipient_count):
    if recipient_count >= 20:
        return "high"
    if recipient_count >= 5:
        return "medium"
    if recipient_count >= 2:
        return "low"
    return "single"


failures = []

print("# Verify Training Dataset")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

day_rollups = parse_json_lines("DAY_ROLLUP_SAMPLES")
if not day_rollups:
    failures.append("sender_trade_day_rollup_v1 returned no sample rows")
else:
    print("## sender_trade_day_rollup_v1")
    for row in day_rollups:
        sql = f"""
SELECT
    count(),
    countIf(protocol_family = 'uniswap_v2'),
    countIf(protocol_family = 'uniswap_v3'),
    uniqExact(venue_address),
    uniqExact(recipient_address),
    uniqExact(sold_token_address),
    uniqExact(bought_token_address)
FROM eatlq.swap_trade_events_all
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND toDate(block_time) = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "trade_count",
            "v2_trade_count",
            "v3_trade_count",
            "distinct_venues",
            "distinct_recipients",
            "distinct_sold_assets",
            "distinct_bought_assets",
        ]
        expected = [row[label] for label in labels]
        for label, actual_value, expected_value in zip(labels, actual, expected):
            if actual_value != expected_value:
                failures.append(
                    f"sender_trade_day_rollup_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={expected_value} actual={actual_value}"
                )
        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"trades={row['trade_count']} venues={row['distinct_venues']}"
        )
    print()

training_rows = parse_json_lines("TRAINING_DATASET_SAMPLES")
if not training_rows:
    failures.append("sender_training_dataset_v1 returned no sample rows")
else:
    print("## sender_training_dataset_v1")
    for row in training_rows:
        rollup_sql = f"""
SELECT
    toString(trade_count),
    toString(v2_trade_count),
    toString(v3_trade_count),
    toString(distinct_venues),
    toString(distinct_recipients),
    toString(distinct_sold_assets),
    toString(distinct_bought_assets)
FROM eatlq.sender_trade_day_rollup_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND activity_date = toDate('{row['activity_date']}')
FORMAT TSVRaw
"""
        rollup_actual = query_clickhouse(rollup_sql)[0].split('\t')
        rollup_labels = [
            "trade_count",
            "v2_trade_count",
            "v3_trade_count",
            "distinct_venues",
            "distinct_recipients",
            "distinct_sold_assets",
            "distinct_bought_assets",
        ]
        for label, actual_value in zip(rollup_labels, rollup_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_training_dataset_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        venue_sql = f"""
SELECT
    max(trade_count)
FROM (
    SELECT
        venue_address,
        count() AS trade_count
    FROM eatlq.swap_trade_events_all
    WHERE chain_id = {row['chain_id']}
      AND sender_address = '{row['sender_address']}'
      AND toDate(block_time) = toDate('{row['activity_date']}')
    GROUP BY venue_address
)
FORMAT TSVRaw
"""
        venue_actual = query_clickhouse(venue_sql)[0].split('\t')[0]
        if venue_actual != row["top_venue_trade_count"]:
            failures.append(
                f"sender_training_dataset_v1 {row['sender_address']} {row['activity_date']} top_venue_trade_count: "
                f"expected={row['top_venue_trade_count']} actual={venue_actual}"
            )

        token_sql = f"""
SELECT
    max(trade_touch_count),
    sum(trade_touch_count)
FROM (
    SELECT
        token_address,
        count() AS trade_touch_count
    FROM (
        SELECT sold_token_address AS token_address
        FROM eatlq.swap_trade_events_all
        WHERE chain_id = {row['chain_id']}
          AND sender_address = '{row['sender_address']}'
          AND toDate(block_time) = toDate('{row['activity_date']}')

        UNION ALL

        SELECT bought_token_address AS token_address
        FROM eatlq.swap_trade_events_all
        WHERE chain_id = {row['chain_id']}
          AND sender_address = '{row['sender_address']}'
          AND toDate(block_time) = toDate('{row['activity_date']}')
    )
    GROUP BY token_address
)
FORMAT TSVRaw
"""
        token_actual = query_clickhouse(token_sql)[0].split('\t')
        token_labels = ["top_token_trade_touch_count", "total_token_touches"]
        for label, actual_value in zip(token_labels, token_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_training_dataset_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        trade_count = Decimal(row["trade_count"])
        v2_trade_count = Decimal(row["v2_trade_count"])
        v3_trade_count = Decimal(row["v3_trade_count"])
        top_venue_trade_count = Decimal(row["top_venue_trade_count"])
        top_token_trade_touch_count = Decimal(row["top_token_trade_touch_count"])
        total_token_touches = Decimal(row["total_token_touches"])
        v2_share_expected = Decimal("0") if trade_count == 0 else v2_trade_count / trade_count
        v3_share_expected = Decimal("0") if trade_count == 0 else v3_trade_count / trade_count
        venue_ratio_expected = Decimal("0") if trade_count == 0 else top_venue_trade_count / trade_count
        token_ratio_expected = Decimal("0") if total_token_touches == 0 else top_token_trade_touch_count / total_token_touches

        numeric_expected = {
            "v2_trade_share": v2_share_expected,
            "v3_trade_share": v3_share_expected,
            "venue_concentration_ratio": venue_ratio_expected,
            "token_concentration_ratio": token_ratio_expected,
        }
        for label, expected_value in numeric_expected.items():
            ok, tolerance = decimal_equal(row[label], str(expected_value))
            if not ok:
                failures.append(
                    f"sender_training_dataset_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={row[label]} actual={expected_value} tolerance={tolerance}"
                )

        expected_labels = {
            "protocol_preference": protocol_preference(
                int(v2_trade_count), int(v3_trade_count), v2_share_expected, v3_share_expected
            ),
            "venue_concentration_band": venue_band(venue_ratio_expected),
            "token_concentration_band": token_band(token_ratio_expected),
            "activity_band": activity_band(int(trade_count)),
            "recipient_diversity_band": recipient_band(int(row["distinct_recipients"])),
            "cross_protocol_flag": "1" if int(v2_trade_count) > 0 and int(v3_trade_count) > 0 else "0",
            "multi_venue_flag": "1" if int(row["distinct_venues"]) > 1 else "0",
        }
        for label, expected_value in expected_labels.items():
            if row[label] != expected_value:
                failures.append(
                    f"sender_training_dataset_v1 {row['sender_address']} {row['activity_date']} {label}: "
                    f"expected={expected_value} actual={row[label]}"
                )

        print(
            f"PASS sender={row['sender_address']} day={row['activity_date']} "
            f"protocol={row['protocol_preference']} activity={row['activity_band']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Training dataset verification passed.")
PY
