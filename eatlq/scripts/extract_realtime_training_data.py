#!/usr/bin/env python3
"""Extract Phase 3.5 real-time training data from ClickHouse.

Queries eatlq.pool_realtime_training_v1 to produce a CSV with the
same 4 features the FeatureStore computes at runtime, plus a binary
label derived from future buy pressure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import clickhouse_connect
import pandas as pd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Phase 3.5 real-time training data from ClickHouse"
    )
    parser.add_argument("--output", type=Path, default=Path("models/realtime_dataset.csv"))
    parser.add_argument("--chain-id", type=int, default=None)
    parser.add_argument("--min-activity", type=int, default=2)
    parser.add_argument("--clickhouse-url", default=None)
    parser.add_argument("--clickhouse-host", default="localhost")
    parser.add_argument("--clickhouse-port", type=int, default=8123)
    parser.add_argument("--clickhouse-user", default=None)
    parser.add_argument("--clickhouse-password", default=None)
    parser.add_argument("--clickhouse-db", default=None)
    parser.add_argument("--clickhouse-secure", action="store_true")
    parser.add_argument(
        "--view", type=str, default="eatlq.pool_realtime_training_v1",
        help="Source view. Use eatlq.pool_realtime_training_v2 for de-toxified dataset.",
    )
    return parser


def get_client(args: argparse.Namespace):
    url = args.clickhouse_url or os.getenv("CLICKHOUSE_URL")
    user = args.clickhouse_user or os.getenv("CLICKHOUSE_USER", "default")
    password = args.clickhouse_password
    if password is None:
        password = os.getenv("CLICKHOUSE_PASSWORD", "")
    database = args.clickhouse_db or os.getenv("CLICKHOUSE_DB", "eatlq")

    if url:
        parsed = urlparse(url)
        secure = parsed.scheme == "https"
        return clickhouse_connect.get_client(
            host=parsed.hostname or "localhost",
            port=parsed.port or (8443 if secure else 8123),
            username=user,
            password=password,
            database=database,
            secure=secure,
        )

    return clickhouse_connect.get_client(
        host=args.clickhouse_host,
        port=args.clickhouse_port,
        username=user,
        password=password,
        database=database,
        secure=args.clickhouse_secure,
    )


def main() -> int:
    args = build_parser().parse_args()
    client = get_client(args)

    chain_filter = ""
    params = {}
    if args.chain_id is not None:
        chain_filter = "AND chain_id = %(chain_id)s"
        params["chain_id"] = args.chain_id

    query = f"""
    SELECT
        pool_address,
        chain_id,
        window_start,
        price_velocity_5m,
        buy_pressure_ratio,
        token_volatility_5m,
        activity_heat,
        label,
        future_buy_pressure,
        future_activity
    FROM {args.view}
    WHERE activity_heat >= %(min_activity)s
      {chain_filter}
    ORDER BY window_start ASC
    SETTINGS
        max_memory_usage = 6000000000,
        max_bytes_before_external_group_by = 3000000000
    """
    params["min_activity"] = args.min_activity

    print(f"extracting realtime training data (chain_id={args.chain_id or 'all'}) ...")
    df = client.query_df(query, parameters=params)

    if df.empty:
        print("ERROR: no rows returned from pool_realtime_training_v1")
        return 1

    label_dist = df["label"].value_counts(dropna=False).to_dict()
    pool_count = df["pool_address"].nunique()
    date_min = df["window_start"].min()
    date_max = df["window_start"].max()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)

    print(f"output_file={args.output}")
    print(f"total_rows={len(df)}")
    print(f"distinct_pools={pool_count}")
    print(f"window_start_min={date_min}")
    print(f"window_start_max={date_max}")
    print(f"label_distribution={json.dumps(label_dist, ensure_ascii=True, default=str)}")

    if len(df) < 100:
        print("WARNING: fewer than 100 training rows. Model quality may be poor.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
