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

sender_preference_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        top_protocol_family,
        top_venue_address,
        top_venue_label,
        toString(top_venue_fee_bps) AS top_venue_fee_bps,
        toString(top_venue_trade_count) AS top_venue_trade_count,
        toString(top_venue_trade_share) AS top_venue_trade_share,
        top_token_address,
        top_token_symbol,
        toString(top_token_decimals) AS top_token_decimals,
        toString(top_token_trade_touch_count) AS top_token_trade_touch_count,
        toString(top_token_trade_share) AS top_token_trade_share,
        toString(cross_protocol_flag) AS cross_protocol_flag,
        toString(multi_venue_flag) AS multi_venue_flag
    FROM eatlq.sender_preference_profile_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

sender_behavior_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        protocol_preference,
        venue_concentration_band,
        token_concentration_band,
        activity_band,
        recipient_diversity_band,
        flow_bias,
        toString(cross_protocol_flag) AS cross_protocol_flag,
        toString(multi_venue_flag) AS multi_venue_flag
    FROM eatlq.sender_behavior_labels_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export SENDER_PREFERENCE_SAMPLES="$(query_json "${sender_preference_query}")"
export SENDER_BEHAVIOR_SAMPLES="$(query_json "${sender_behavior_query}")"

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


def concentration_band(value, high_label):
    if value >= Decimal("0.9"):
        return high_label
    if value >= Decimal("0.6"):
        return "concentrated"
    if value >= Decimal("0.35"):
        return "multi_venue" if high_label == "single_venue" else "multi_token"
    return "distributed"


def activity_band(value):
    if value >= Decimal("20"):
        return "hyperactive"
    if value >= Decimal("5"):
        return "active"
    if value >= Decimal("1"):
        return "steady"
    return "sporadic"


def recipient_diversity_band(value):
    if value >= 20:
        return "high"
    if value >= 5:
        return "medium"
    if value >= 2:
        return "low"
    return "single"


def flow_bias(net_buyer_count, net_seller_count):
    if net_buyer_count > net_seller_count * 2 and net_buyer_count > 0:
        return "net_buyer"
    if net_seller_count > net_buyer_count * 2 and net_seller_count > 0:
        return "net_seller"
    return "balanced"


failures = []

print("# Verify Wallet Behavior")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

preference_rows = parse_json_lines("SENDER_PREFERENCE_SAMPLES")
if not preference_rows:
    failures.append("sender_preference_profile_v1 returned no sample rows")
else:
    print("## sender_preference_profile_v1")
    for row in preference_rows:
        venue_sql = f"""
SELECT
    protocol_family,
    venue_address,
    venue_label,
    toString(fee_bps),
    toString(trade_count)
FROM eatlq.sender_venue_trade_profile
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
ORDER BY trade_count DESC, latest_ingested_at DESC, venue_address DESC
LIMIT 1
FORMAT TSVRaw
"""
        venue_actual = query_clickhouse(venue_sql)[0].split('\t')
        venue_labels = [
            "top_protocol_family",
            "top_venue_address",
            "top_venue_label",
            "top_venue_fee_bps",
            "top_venue_trade_count",
        ]
        for label, actual_value in zip(venue_labels, venue_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_preference_profile_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        token_sql = f"""
SELECT
    token_address,
    token_symbol,
    toString(token_decimals),
    toString(trade_touch_count)
FROM eatlq.sender_token_trade_profile
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
ORDER BY trade_touch_count DESC, latest_ingested_at DESC, token_address DESC
LIMIT 1
FORMAT TSVRaw
"""
        token_actual = query_clickhouse(token_sql)[0].split('\t')
        token_labels = [
            "top_token_address",
            "top_token_symbol",
            "top_token_decimals",
            "top_token_trade_touch_count",
        ]
        for label, actual_value in zip(token_labels, token_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_preference_profile_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        feature_sql = f"""
SELECT
    toString(venue_concentration_ratio),
    toString(token_concentration_ratio),
    toString(cross_protocol_flag),
    toString(multi_venue_flag)
FROM eatlq.sender_feature_store_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
FORMAT TSVRaw
"""
        feature_actual = query_clickhouse(feature_sql)[0].split('\t')
        feature_labels = [
            "top_venue_trade_share",
            "top_token_trade_share",
            "cross_protocol_flag",
            "multi_venue_flag",
        ]
        for label, actual_value in zip(feature_labels, feature_actual):
            if label.endswith("_share"):
                ok, tolerance = decimal_equal(row[label], actual_value)
                if not ok:
                    failures.append(
                        f"sender_preference_profile_v1 {row['sender_address']} {label}: "
                        f"expected={row[label]} actual={actual_value} tolerance={tolerance}"
                    )
            elif actual_value != row[label]:
                failures.append(
                    f"sender_preference_profile_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        print(
            f"PASS sender={row['sender_address']} top_venue={row['top_venue_label']} "
            f"top_token={row['top_token_symbol']}"
        )
    print()

behavior_rows = parse_json_lines("SENDER_BEHAVIOR_SAMPLES")
if not behavior_rows:
    failures.append("sender_behavior_labels_v1 returned no sample rows")
else:
    print("## sender_behavior_labels_v1")
    for row in behavior_rows:
        feature_sql = f"""
SELECT
    toString(v2_trade_count),
    toString(v3_trade_count),
    toString(v2_trade_share),
    toString(v3_trade_share),
    toString(venue_concentration_ratio),
    toString(token_concentration_ratio),
    toString(avg_trades_per_active_day),
    toString(distinct_recipients),
    toString(net_buyer_token_count),
    toString(net_seller_token_count),
    toString(cross_protocol_flag),
    toString(multi_venue_flag)
FROM eatlq.sender_feature_store_v1
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
FORMAT TSVRaw
"""
        actual = query_clickhouse(feature_sql)[0].split('\t')
        v2_count = int(actual[0])
        v3_count = int(actual[1])
        v2_share = Decimal(actual[2])
        v3_share = Decimal(actual[3])
        venue_ratio = Decimal(actual[4])
        token_ratio = Decimal(actual[5])
        trades_per_day = Decimal(actual[6])
        recipient_count = int(actual[7])
        net_buyer_count = int(actual[8])
        net_seller_count = int(actual[9])
        expected = {
            "protocol_preference": protocol_preference(v2_count, v3_count, v2_share, v3_share),
            "venue_concentration_band": concentration_band(venue_ratio, "single_venue"),
            "token_concentration_band": concentration_band(token_ratio, "single_token"),
            "activity_band": activity_band(trades_per_day),
            "recipient_diversity_band": recipient_diversity_band(recipient_count),
            "flow_bias": flow_bias(net_buyer_count, net_seller_count),
            "cross_protocol_flag": actual[10],
            "multi_venue_flag": actual[11],
        }
        for label, expected_value in expected.items():
            if row[label] != expected_value:
                failures.append(
                    f"sender_behavior_labels_v1 {row['sender_address']} {label}: "
                    f"expected={expected_value} actual={row[label]}"
                )
        print(
            f"PASS sender={row['sender_address']} protocol={row['protocol_preference']} "
            f"activity={row['activity_band']} flow={row['flow_bias']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Wallet behavior verification passed.")
PY
