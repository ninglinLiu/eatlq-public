"""Compare RPC Mint/Burn backfill counts with existing decoded ClickHouse views."""

from __future__ import annotations

import csv
import gzip
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.backfill_collects import CHAIN_ID, END_BLOCK, OUT, POOLS, START_BLOCK


ROOT = Path(__file__).resolve().parents[1]
REPORT_CSV = OUT / "r0b_ingestion_comparison.csv"
REPORT_JSON = OUT / "r0b_ingestion_comparison.json"
STAGING_TABLE = "eatlq.r0b_rpc_position_events"


def query_rows(sql: str) -> list[dict]:
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "eatlq-clickhouse-1",
            "clickhouse-client",
            "--format",
            "JSONEachRow",
            "-q",
            sql,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return [json.loads(line) for line in completed.stdout.splitlines() if line]


def ensure_staging_table() -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "eatlq-clickhouse-1",
            "clickhouse-client",
            "-q",
            f"""
            CREATE TABLE IF NOT EXISTS {STAGING_TABLE}
            (
                chain_id UInt64,
                event_type LowCardinality(String),
                block_number UInt64,
                block_time DateTime64(3, 'UTC'),
                tx_hash String,
                log_index UInt32,
                pool_address LowCardinality(String),
                owner LowCardinality(String),
                recipient String,
                sender String,
                tick_lower Int32,
                tick_upper Int32,
                liquidity UInt256,
                amount0 UInt256,
                amount1 UInt256,
                removed UInt8,
                loaded_at DateTime64(3, 'UTC') DEFAULT now64(3),
                CONSTRAINT base_chain_only CHECK chain_id = 8453,
                CONSTRAINT position_event_only CHECK event_type IN ('mint', 'burn')
            )
            ENGINE = ReplacingMergeTree(loaded_at)
            PARTITION BY (event_type, toYYYYMM(block_time))
            ORDER BY (chain_id, event_type, pool_address, block_number, tx_hash, log_index)
            """,
        ],
        check=True,
    )


def load_staging() -> None:
    """Idempotently load RPC CSV parts so event-key anti-joins are reproducible."""

    ensure_staging_table()
    for event_type in ("mint", "burn"):
        expected_state = OUT / "backfill_state" / f"{event_type}.json"
        if not expected_state.exists():
            raise RuntimeError(f"backfill checkpoint is missing: {expected_state}")
        expected = int(json.loads(expected_state.read_text())["metrics"]["decoded_logs"])
        current = int(
            query_rows(
                f"SELECT count() AS n FROM {STAGING_TABLE} FINAL "
                f"WHERE chain_id=8453 AND event_type='{event_type}'"
            )[0]["n"]
        )
        if current == expected:
            continue
        parts = sorted((OUT / "rpc_backfill" / event_type).glob(f"{event_type}_*.csv.gz"))
        if not parts:
            raise RuntimeError(f"no parts to load for {event_type}")
        for number, part in enumerate(parts, 1):
            process = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    "eatlq-clickhouse-1",
                    "clickhouse-client",
                    "-q",
                    f"INSERT INTO {STAGING_TABLE} FORMAT CSVWithNames",
                ],
                stdin=subprocess.PIPE,
            )
            assert process.stdin is not None
            with gzip.open(part, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    process.stdin.write(chunk)
            process.stdin.close()
            if process.wait() != 0:
                raise subprocess.CalledProcessError(process.returncode, process.args)
            if number % 50 == 0:
                print(f"loaded {event_type} staging parts: {number}/{len(parts)}", flush=True)
        final_count = int(
            query_rows(
                f"SELECT count() AS n FROM {STAGING_TABLE} FINAL "
                f"WHERE chain_id=8453 AND event_type='{event_type}'"
            )[0]["n"]
        )
        if final_count != expected:
            raise RuntimeError(
                f"staging {event_type} count {final_count} != checkpoint {expected}"
            )


def rpc_daily_counts(event_type: str) -> tuple[Counter, int, int]:
    directory = OUT / "rpc_backfill" / event_type
    parts = sorted(directory.glob(f"{event_type}_*.csv.gz"))
    if not parts:
        raise RuntimeError(f"no RPC {event_type} parts under {directory}")
    counts: Counter = Counter()
    total = 0
    removed = 0
    for part in parts:
        with gzip.open(part, "rt", newline="") as handle:
            for row in csv.DictReader(handle):
                if int(row["chain_id"]) != CHAIN_ID:
                    raise ValueError(f"non-Base row in {part}")
                if int(row["removed"]):
                    removed += 1
                    continue
                day = row["block_time"][:10]
                counts[(row["pool_address"], day)] += 1
                total += 1
    return counts, total, removed


def clickhouse_daily_counts(event_type: str) -> Counter:
    if event_type not in {"mint", "burn"}:
        raise ValueError(event_type)
    addresses = ",".join(f"'{pool}'" for pool in POOLS)
    table = f"eatlq.uniswap_v3_{event_type}s_decoded"
    rows = query_rows(
        f"""
        SELECT pool_address,
               toString(toDate(toDateTime(1771459201 + 2 * (block_number - {START_BLOCK})))) AS day,
               count() AS events
        FROM {table}
        WHERE chain_id = 8453 AND removed = 0
          AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
          AND pool_address IN ({addresses})
        GROUP BY pool_address, day
        ORDER BY pool_address, day
        """
    )
    return Counter(
        {(row["pool_address"], row["day"]): int(row["events"]) for row in rows}
    )


def compare_event(event_type: str) -> tuple[list[dict], dict]:
    rpc, rpc_total, removed = rpc_daily_counts(event_type)
    clickhouse = clickhouse_daily_counts(event_type)
    comparisons = []
    for pool, day in sorted(set(rpc) | set(clickhouse)):
        rpc_count = rpc[(pool, day)]
        ch_count = clickhouse[(pool, day)]
        delta = rpc_count - ch_count
        comparisons.append(
            {
                "event_type": event_type,
                "pool_address": pool,
                "day": day,
                "rpc_count": rpc_count,
                "clickhouse_dedup_count": ch_count,
                "rpc_minus_clickhouse": delta,
                "status": "MATCH" if delta == 0 else ("RPC_MORE" if delta > 0 else "CH_MORE"),
            }
        )
    mismatches = [row for row in comparisons if row["status"] != "MATCH"]
    summary = {
        "event_type": event_type,
        "rpc_total": rpc_total,
        "clickhouse_dedup_total": sum(clickhouse.values()),
        "rpc_removed_logs_excluded": removed,
        "pool_days_compared": len(comparisons),
        "mismatched_pool_days": len(mismatches),
        "rpc_more_pool_days": sum(row["status"] == "RPC_MORE" for row in mismatches),
        "clickhouse_more_pool_days": sum(row["status"] == "CH_MORE" for row in mismatches),
        "complete": not mismatches,
    }
    table = f"eatlq.uniswap_v3_{event_type}s_decoded"
    addresses = ",".join(f"'{pool}'" for pool in POOLS)
    rpc_only_by_pool = {}
    ch_only_by_pool = {}
    # ClickHouse 24.12 returned inconsistent EXCEPT cardinalities for these
    # LowCardinality-backed views.  ANTI JOIN preserves the event-key identity
    # and satisfies the set cardinality invariant in both directions.
    for pool in POOLS:
        rpc_only_by_pool[pool] = int(
            query_rows(
                f"""
                SELECT count() AS n
                FROM {STAGING_TABLE} AS r FINAL
                LEFT ANTI JOIN {table} AS c
                  ON r.chain_id=c.chain_id AND r.pool_address=c.pool_address
                 AND r.tx_hash=c.tx_hash AND r.log_index=c.log_index
                 AND c.removed=0 AND c.block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
                WHERE r.chain_id=8453 AND r.event_type='{event_type}' AND r.removed=0
                  AND r.block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
                  AND r.pool_address='{pool}'
                """
            )[0]["n"]
        )
        ch_only_by_pool[pool] = int(
            query_rows(
                f"""
                SELECT count() AS n
                FROM {table} AS c
                LEFT ANTI JOIN {STAGING_TABLE} AS r FINAL
                  ON r.chain_id=c.chain_id AND r.pool_address=c.pool_address
                 AND r.tx_hash=c.tx_hash AND r.log_index=c.log_index
                 AND r.event_type='{event_type}' AND r.removed=0
                 AND r.block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
                WHERE c.chain_id=8453 AND c.removed=0
                  AND c.block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
                  AND c.pool_address='{pool}'
                """
            )[0]["n"]
        )
    summary["rpc_only_event_keys_by_pool"] = rpc_only_by_pool
    summary["clickhouse_only_event_keys_by_pool"] = ch_only_by_pool
    summary["event_keys_match"] = not any(rpc_only_by_pool.values()) and not any(
        ch_only_by_pool.values()
    )
    summary["clickhouse_null_block_time_rows"] = int(
        query_rows(
            f"""
            SELECT count() AS n FROM {table}
            WHERE chain_id=8453 AND removed=0 AND block_time IS NULL
              AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
              AND pool_address IN ({addresses})
            """
        )[0]["n"]
    )
    summary["complete"] = summary["complete"] and summary["event_keys_match"]
    return comparisons, summary


def main() -> None:
    load_staging()
    all_rows = []
    summaries = []
    for event_type in ("mint", "burn"):
        rows, summary = compare_event(event_type)
        all_rows.extend(rows)
        summaries.append(summary)
    OUT.mkdir(exist_ok=True)
    with REPORT_CSV.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(all_rows)
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "chain_id": CHAIN_ID,
        "block_window_inclusive": [START_BLOCK, END_BLOCK],
        "comparison_basis": "RPC eth_getLogs versus ClickHouse *_decoded deduplicated views, per pool/day",
        "summaries": summaries,
        "overall_complete": all(item["complete"] for item in summaries),
        "csv": str(REPORT_CSV),
    }
    REPORT_JSON.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
