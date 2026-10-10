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

sender_venue_profile_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        protocol_family,
        venue_address,
        venue_label,
        toString(trade_count) AS trade_count,
        toString(distinct_txs) AS distinct_txs,
        toString(distinct_sold_assets) AS distinct_sold_assets,
        toString(distinct_bought_assets) AS distinct_bought_assets
    FROM eatlq.sender_venue_trade_profile
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

sender_feature_store_query="$(cat <<EOF
SELECT *
FROM (
    SELECT
        toString(chain_id) AS chain_id,
        sender_address,
        toString(trade_count) AS trade_count,
        toString(v2_trade_count) AS v2_trade_count,
        toString(v3_trade_count) AS v3_trade_count,
        toString(v2_trade_share) AS v2_trade_share,
        toString(v3_trade_share) AS v3_trade_share,
        toString(distinct_venues) AS distinct_venues,
        toString(distinct_sold_assets) AS distinct_sold_assets,
        toString(distinct_bought_assets) AS distinct_bought_assets,
        toString(active_days) AS active_days,
        toString(active_hours) AS active_hours,
        toString(distinct_recipients) AS distinct_recipients,
        toString(avg_trades_per_active_day) AS avg_trades_per_active_day,
        toString(avg_trades_per_active_hour) AS avg_trades_per_active_hour,
        toString(top_venue_trade_count) AS top_venue_trade_count,
        toString(top_token_trade_touch_count) AS top_token_trade_touch_count,
        toString(total_token_touches) AS total_token_touches,
        toString(venue_concentration_ratio) AS venue_concentration_ratio,
        toString(token_concentration_ratio) AS token_concentration_ratio,
        toString(net_buyer_token_count) AS net_buyer_token_count,
        toString(net_seller_token_count) AS net_seller_token_count,
        toString(net_flat_token_count) AS net_flat_token_count,
        toString(cross_protocol_flag) AS cross_protocol_flag,
        toString(multi_venue_flag) AS multi_venue_flag,
        toString(first_trade_at) AS first_trade_at,
        toString(latest_trade_at) AS latest_trade_at
    FROM eatlq.sender_feature_store_v1
    ORDER BY latest_ingested_at DESC
    LIMIT ${SAMPLE_LIMIT}
)
FORMAT JSONEachRow
EOF
)"

export SAMPLE_LIMIT
export SENDER_VENUE_PROFILE_SAMPLES="$(query_json "${sender_venue_profile_query}")"
export SENDER_FEATURE_STORE_SAMPLES="$(query_json "${sender_feature_store_query}")"

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


failures = []

print("# Verify Wallet Features")
print(f"sample_limit={os.environ.get('SAMPLE_LIMIT', '3')}")
print()

venue_profiles = parse_json_lines("SENDER_VENUE_PROFILE_SAMPLES")
if not venue_profiles:
    failures.append("sender_venue_trade_profile returned no sample rows")
else:
    print("## sender_venue_trade_profile")
    for row in venue_profiles:
        sql = f"""
SELECT
    count(),
    uniqExact(tx_hash),
    uniqExact(sold_token_address),
    uniqExact(bought_token_address)
FROM eatlq.swap_trade_events_all
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
  AND protocol_family = '{row['protocol_family']}'
  AND venue_address = '{row['venue_address']}'
FORMAT TSVRaw
"""
        actual = query_clickhouse(sql)[0].split('\t')
        labels = [
            "trade_count",
            "distinct_txs",
            "distinct_sold_assets",
            "distinct_bought_assets",
        ]
        for label, actual_value in zip(labels, actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_venue_trade_profile {row['sender_address']} {row['venue_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )
        print(
            f"PASS sender={row['sender_address']} venue={row['venue_label']} "
            f"trades={row['trade_count']} txs={row['distinct_txs']}"
        )
    print()

feature_rows = parse_json_lines("SENDER_FEATURE_STORE_SAMPLES")
if not feature_rows:
    failures.append("sender_feature_store_v1 returned no sample rows")
else:
    print("## sender_feature_store_v1")
    for row in feature_rows:
        sender_sql = f"""
SELECT
    count(),
    countIf(protocol_family = 'uniswap_v2'),
    countIf(protocol_family = 'uniswap_v3'),
    uniqExact(venue_address),
    uniqExact(sold_token_address),
    uniqExact(bought_token_address),
    uniqExactIf(toDate(block_time), isNotNull(block_time)),
    uniqExactIf(toStartOfHour(block_time), isNotNull(block_time)),
    uniqExact(recipient_address),
    toString(min(block_time)),
    toString(max(block_time))
FROM eatlq.swap_trade_events_all
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
FORMAT TSVRaw
"""
        sender_actual = query_clickhouse(sender_sql)[0].split('\t')
        sender_labels = [
            "trade_count",
            "v2_trade_count",
            "v3_trade_count",
            "distinct_venues",
            "distinct_sold_assets",
            "distinct_bought_assets",
            "active_days",
            "active_hours",
            "distinct_recipients",
            "first_trade_at",
            "latest_trade_at",
        ]
        for label, actual_value in zip(sender_labels, sender_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_feature_store_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        venue_sql = f"""
SELECT
    max(trade_count)
FROM eatlq.sender_venue_trade_profile
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
FORMAT TSVRaw
"""
        venue_actual = query_clickhouse(venue_sql)[0].split('\t')[0]
        if venue_actual != row["top_venue_trade_count"]:
            failures.append(
                f"sender_feature_store_v1 {row['sender_address']} top_venue_trade_count: "
                f"expected={row['top_venue_trade_count']} actual={venue_actual}"
            )

        token_sql = f"""
SELECT
    max(trade_touch_count),
    sum(trade_touch_count),
    countIf(net_amount_normalized > 0.000000000001),
    countIf(net_amount_normalized < -0.000000000001),
    countIf(abs(net_amount_normalized) <= 0.000000000001)
FROM eatlq.sender_token_trade_profile
WHERE chain_id = {row['chain_id']}
  AND sender_address = '{row['sender_address']}'
FORMAT TSVRaw
"""
        token_actual = query_clickhouse(token_sql)[0].split('\t')
        token_labels = [
            "top_token_trade_touch_count",
            "total_token_touches",
            "net_buyer_token_count",
            "net_seller_token_count",
            "net_flat_token_count",
        ]
        for label, actual_value in zip(token_labels, token_actual):
            if actual_value != row[label]:
                failures.append(
                    f"sender_feature_store_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={actual_value}"
                )

        expected_numeric = {
            "v2_trade_share": (
                Decimal(row["v2_trade_count"]) / Decimal(row["trade_count"])
                if Decimal(row["trade_count"]) != 0 else Decimal("0")
            ),
            "v3_trade_share": (
                Decimal(row["v3_trade_count"]) / Decimal(row["trade_count"])
                if Decimal(row["trade_count"]) != 0 else Decimal("0")
            ),
            "avg_trades_per_active_day": (
                Decimal(row["trade_count"]) / Decimal(row["active_days"])
                if Decimal(row["active_days"]) != 0 else Decimal("0")
            ),
            "avg_trades_per_active_hour": (
                Decimal(row["trade_count"]) / Decimal(row["active_hours"])
                if Decimal(row["active_hours"]) != 0 else Decimal("0")
            ),
            "venue_concentration_ratio": (
                Decimal(row["top_venue_trade_count"]) / Decimal(row["trade_count"])
                if Decimal(row["trade_count"]) != 0 else Decimal("0")
            ),
            "token_concentration_ratio": (
                Decimal(row["top_token_trade_touch_count"]) / Decimal(row["total_token_touches"])
                if Decimal(row["total_token_touches"]) != 0 else Decimal("0")
            ),
        }
        for label, expected_value in expected_numeric.items():
            ok, tolerance = decimal_equal(row[label], str(expected_value))
            if not ok:
                failures.append(
                    f"sender_feature_store_v1 {row['sender_address']} {label}: "
                    f"expected={row[label]} actual={expected_value} tolerance={tolerance}"
                )

        expected_cross_protocol = "1" if (
            int(row["v2_trade_count"]) > 0 and int(row["v3_trade_count"]) > 0
        ) else "0"
        if row["cross_protocol_flag"] != expected_cross_protocol:
            failures.append(
                f"sender_feature_store_v1 {row['sender_address']} cross_protocol_flag: "
                f"expected={expected_cross_protocol} actual={row['cross_protocol_flag']}"
            )

        expected_multi_venue = "1" if int(row["distinct_venues"]) > 1 else "0"
        if row["multi_venue_flag"] != expected_multi_venue:
            failures.append(
                f"sender_feature_store_v1 {row['sender_address']} multi_venue_flag: "
                f"expected={expected_multi_venue} actual={row['multi_venue_flag']}"
            )

        print(
            f"PASS sender={row['sender_address']} trade_count={row['trade_count']} "
            f"top_venue_share={row['venue_concentration_ratio']} "
            f"top_token_share={row['token_concentration_ratio']}"
        )
    print()

if failures:
    print("FAIL")
    for failure in failures:
        print(failure)
    sys.exit(1)

print("Wallet feature verification passed.")
PY
