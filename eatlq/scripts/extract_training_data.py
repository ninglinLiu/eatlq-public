#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import clickhouse_connect
import pandas as pd


@dataclass
class ClickHouseConfig:
    host: str
    port: int
    username: str
    password: str
    database: str
    secure: bool


def parse_clickhouse_config(args: argparse.Namespace) -> ClickHouseConfig:
    url = args.clickhouse_url or os.getenv("CLICKHOUSE_URL")
    user = args.clickhouse_user or os.getenv("CLICKHOUSE_USER", "default")
    password = args.clickhouse_password
    if password is None:
        password = os.getenv("CLICKHOUSE_PASSWORD", "")
    database = args.clickhouse_db or os.getenv("CLICKHOUSE_DB", "eatlq")

    if url:
        parsed = urlparse(url)
        secure = parsed.scheme == "https"
        return ClickHouseConfig(
            host=parsed.hostname or "localhost",
            port=parsed.port or (8443 if secure else 8123),
            username=user,
            password=password,
            database=database,
            secure=secure,
        )

    return ClickHouseConfig(
        host=args.clickhouse_host,
        port=args.clickhouse_port,
        username=user,
        password=password,
        database=database,
        secure=args.clickhouse_secure,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Phase 3 supervised training rows from ClickHouse"
    )
    parser.add_argument(
        "--chain-id", type=int, default=None,
        help="Chain ID to extract. Auto-detects if omitted.",
    )
    parser.add_argument(
        "--chain-ids", type=str, default=None,
        help="Comma-separated chain IDs for multi-chain merge (e.g. '1,8453').",
    )
    parser.add_argument("--output", type=Path, default=Path("labeled_dataset.csv"))

    parser.add_argument("--clickhouse-url", default=None)
    parser.add_argument("--clickhouse-host", default="localhost")
    parser.add_argument("--clickhouse-port", type=int, default=8123)
    parser.add_argument("--clickhouse-user", default=None)
    parser.add_argument("--clickhouse-password", default=None)
    parser.add_argument("--clickhouse-db", default=None)
    parser.add_argument("--clickhouse-secure", action="store_true")
    return parser


def detect_chain_id(client: clickhouse_connect.driver.Client) -> int | None:
    result = client.query(
        "SELECT chain_id, count() AS cnt "
        "FROM eatlq.raw_evm_logs "
        "GROUP BY chain_id ORDER BY cnt DESC LIMIT 5"
    )
    if not result.result_rows:
        return None
    best_chain, best_count = result.result_rows[0]
    print(f"auto_detected_chain_id={best_chain} rows={best_count}")
    for row in result.result_rows[1:]:
        print(f"  other_chain_id={row[0]} rows={row[1]}")
    return int(best_chain)


_TRAIN_COLUMNS = """
        activity_date,
        sender_address,
        chain_id,
        trade_count,
        v2_trade_share,
        v3_trade_share,
        distinct_venues,
        distinct_recipients,
        venue_concentration_ratio,
        token_concentration_ratio,
        protocol_preference,
        activity_band,
        dataset_split,
        next_day_active_flag,
        next_day_higher_trade_count_flag,
        next_day_protocol_preference,
        cross_protocol_flag,
        multi_venue_flag
""".strip()

_CH_SETTINGS = {
    "query_plan_max_optimizations_to_apply": 0,
    "max_bytes_before_external_group_by": 3_000_000_000,
    "max_bytes_before_external_sort": 3_000_000_000,
    "max_memory_usage": 6_000_000_000,
}


def extract_single_chain(client, chain_id: int) -> pd.DataFrame:
    train_query = f"""
    SELECT
        {_TRAIN_COLUMNS}
    FROM eatlq.sender_training_examples_split_v1
    WHERE chain_id = %(chain_id)s
    ORDER BY activity_date ASC, sender_address ASC
    SETTINGS query_plan_max_optimizations_to_apply = 0,
             max_bytes_before_external_group_by = 3000000000,
             max_bytes_before_external_sort = 3000000000,
             max_memory_usage = 6000000000
    """
    return client.query_df(train_query, parameters={"chain_id": chain_id}, settings=_CH_SETTINGS)


def run_diagnostics(client, chain_id: int) -> None:
    split_diag = client.query(
        """
        SELECT dataset_split, count()
        FROM eatlq.sender_training_examples_split_v1
        WHERE chain_id = %(chain_id)s
        GROUP BY dataset_split
        ORDER BY dataset_split
        SETTINGS query_plan_max_optimizations_to_apply = 0,
                 max_bytes_before_external_group_by = 3000000000,
                 max_memory_usage = 6000000000
        """,
        parameters={"chain_id": chain_id},
        settings=_CH_SETTINGS,
    )
    print(
        f"split_view_counts[{chain_id}]="
        + json.dumps(
            [{"dataset_split": row[0], "rows": row[1]} for row in split_diag.result_rows],
            ensure_ascii=True,
        )
    )

    try:
        label_diag = client.query(
            """
            SELECT
                dataset_split,
                supervised_target_class,
                next_day_activity_bucket,
                example_count
            FROM eatlq.sender_training_label_balance_v1
            WHERE chain_id = %(chain_id)s
            ORDER BY dataset_split, supervised_target_class, next_day_activity_bucket
            LIMIT 50
            SETTINGS query_plan_max_optimizations_to_apply = 0,
                     max_bytes_before_external_group_by = 3000000000,
                     max_memory_usage = 8000000000
            """,
            parameters={"chain_id": chain_id},
            settings={**_CH_SETTINGS, "max_memory_usage": 8_000_000_000},
        )
        print(
            f"label_balance_sample[{chain_id}]="
            + json.dumps(
                [
                    {
                        "dataset_split": row[0],
                        "supervised_target_class": row[1],
                        "next_day_activity_bucket": row[2],
                        "example_count": row[3],
                    }
                    for row in label_diag.result_rows
                ],
                ensure_ascii=True,
            )
        )
    except Exception as exc:
        print(f"WARNING: label_balance diagnostic skipped for chain_id={chain_id}: {exc}")


def main() -> int:
    args = build_parser().parse_args()
    config = parse_clickhouse_config(args)

    client = clickhouse_connect.get_client(
        host=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        database=config.database,
        secure=config.secure,
    )

    chain_ids: list[int] = []
    if args.chain_ids:
        chain_ids = [int(c.strip()) for c in args.chain_ids.split(",") if c.strip()]
    elif args.chain_id is not None:
        chain_ids = [args.chain_id]
    else:
        detected = detect_chain_id(client)
        if detected is None:
            print("ERROR: sender_training_examples_split_v1 contains no data for any chain_id")
            return 1
        chain_ids = [detected]

    frames: list[pd.DataFrame] = []
    for cid in chain_ids:
        df = extract_single_chain(client, cid)
        if df.empty:
            print(f"WARNING: no rows for chain_id={cid}, skipping")
            continue
        print(f"chain_id={cid} rows={len(df)}")
        frames.append(df)

    if not frames:
        print(f"ERROR: no rows returned from sender_training_examples_split_v1 for chain_ids={chain_ids}")
        return 1

    result = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    result["activity_date"] = pd.to_datetime(result["activity_date"]).dt.date
    result = result.sort_values(["activity_date", "sender_address"]).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)

    split_counts = result["dataset_split"].value_counts(dropna=False).to_dict()
    label_counts = result["next_day_active_flag"].value_counts(dropna=False).to_dict()
    date_min = result["activity_date"].min()
    date_max = result["activity_date"].max()

    print(f"chain_ids={chain_ids}")
    print(f"output_file={args.output}")
    print(f"total_rows={len(result)}")
    print(f"activity_date_min={date_min} activity_date_max={date_max}")
    print(f"split_sizes={json.dumps(split_counts, ensure_ascii=True)}")
    print(f"next_day_active_flag_distribution={json.dumps(label_counts, ensure_ascii=True)}")

    if len(result) < 5000:
        print("WARNING: mature supervised rows < 5000, proceed with caution.")

    for cid in chain_ids:
        run_diagnostics(client, cid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
