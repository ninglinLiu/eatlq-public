"""Rate-limited, resumable Base pool-event backfill from ``eth_getLogs``.

The script is deliberately self-contained (standard library only) and writes one
gzip-compressed CSV part per completed block chunk.  Collect rows are also
inserted into ``eatlq.uniswap_v3_collects``.  Mint and Burn parts are retained
locally for the independent ingestion completeness audit.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
START_BLOCK = 42_334_927
END_BLOCK = 46_266_127
CHAIN_ID = 8453
MAX_SPAN = 10_000
BASE_BLOCK_TIME = datetime(2026, 2, 19, 0, 0, 1, tzinfo=timezone.utc)
POOLS = (
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0x82321f3beb69f503380d6b233857d5c43562e2d0",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
)
TOPICS = {
    "mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
    "collect": "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0",
}
CSV_FIELDS = (
    "chain_id",
    "event_type",
    "block_number",
    "block_time",
    "tx_hash",
    "log_index",
    "pool_address",
    "owner",
    "recipient",
    "sender",
    "tick_lower",
    "tick_upper",
    "liquidity",
    "amount0",
    "amount1",
    "removed",
)


def parse_int24(value: str | bytes | int) -> int:
    """Decode an ABI-encoded signed int24, including 256-bit sign extension."""

    if isinstance(value, int):
        raw = value
    elif isinstance(value, bytes):
        raw = int.from_bytes(value, "big", signed=False)
    else:
        raw = int(value, 16)
    raw &= (1 << 24) - 1
    return raw - (1 << 24) if raw & (1 << 23) else raw


def abi_words(data: str) -> list[str]:
    payload = data[2:] if data.startswith("0x") else data
    if len(payload) % 64:
        raise ValueError(f"ABI data length is not word aligned: {len(payload)} hex chars")
    return [payload[offset : offset + 64] for offset in range(0, len(payload), 64)]


def topic_address(topic: str) -> str:
    payload = topic.removeprefix("0x")
    if len(payload) != 64:
        raise ValueError(f"invalid address topic length: {len(payload)}")
    return "0x" + payload[-40:].lower()


def word_address(word: str) -> str:
    if len(word) != 64:
        raise ValueError(f"invalid address word length: {len(word)}")
    return "0x" + word[-40:].lower()


def block_time(block_number: int) -> datetime:
    """Base produces this window's L2 blocks on the fixed two-second cadence."""

    if not START_BLOCK <= block_number <= END_BLOCK:
        raise ValueError(f"block {block_number} is outside the authorized window")
    return BASE_BLOCK_TIME + timedelta(seconds=2 * (block_number - START_BLOCK))


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")


def decode_log(event_type: str, log: dict[str, Any]) -> dict[str, Any]:
    if event_type not in TOPICS:
        raise ValueError(f"unknown event type: {event_type}")
    topics = log["topics"]
    if len(topics) != 4 or topics[0].lower() != TOPICS[event_type]:
        raise ValueError(f"unexpected {event_type} topics")
    words = abi_words(log["data"])
    expected_words = {"mint": 4, "burn": 3, "collect": 3}[event_type]
    if len(words) != expected_words:
        raise ValueError(
            f"{event_type} expected {expected_words} data words, got {len(words)}"
        )
    number = int(log["blockNumber"], 16)
    row: dict[str, Any] = {
        "chain_id": CHAIN_ID,
        "event_type": event_type,
        "block_number": number,
        "block_time": format_time(block_time(number)),
        "tx_hash": log["transactionHash"].lower(),
        "log_index": int(log["logIndex"], 16),
        "pool_address": log["address"].lower(),
        "owner": topic_address(topics[1]),
        "recipient": "",
        "sender": "",
        "tick_lower": parse_int24(topics[2]),
        "tick_upper": parse_int24(topics[3]),
        "liquidity": "",
        "amount0": "",
        "amount1": "",
        "removed": int(bool(log.get("removed", False))),
    }
    if row["pool_address"] not in POOLS:
        raise ValueError(f"RPC returned an unauthorized pool: {row['pool_address']}")
    if event_type == "mint":
        row.update(
            sender=word_address(words[0]),
            liquidity=str(int(words[1], 16)),
            amount0=str(int(words[2], 16)),
            amount1=str(int(words[3], 16)),
        )
    elif event_type == "burn":
        row.update(
            liquidity=str(int(words[0], 16)),
            amount0=str(int(words[1], 16)),
            amount1=str(int(words[2], 16)),
        )
    else:
        row.update(
            recipient=word_address(words[0]),
            amount0=str(int(words[1], 16)),
            amount1=str(int(words[2], 16)),
        )
    return row


@dataclass
class Metrics:
    started_at_utc: str
    completed_chunks: int = 0
    decoded_logs: int = 0
    http_attempts: int = 0
    json_rpc_responses: int = 0
    retries: int = 0
    rpc_errors: int = 0
    elapsed_seconds: float = 0.0


class RpcClient:
    def __init__(
        self,
        url: str,
        *,
        max_rps: float = 10.0,
        timeout: float = 90.0,
        max_retries: int = 8,
        metrics: Metrics | None = None,
    ) -> None:
        if not 0 < max_rps <= 10:
            raise ValueError("max_rps must be in (0, 10]")
        self.url = url
        self.min_interval = 1.0 / max_rps
        self.timeout = timeout
        self.max_retries = max_retries
        self.metrics = metrics or Metrics(datetime.now(timezone.utc).isoformat())
        self.last_request_at = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        for attempt in range(self.max_retries + 1):
            delay = self.min_interval - (time.monotonic() - self.last_request_at)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(
                self.url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self.metrics.http_attempts += 1
            self.last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.load(response)
                self.metrics.json_rpc_responses += 1
                if "error" in body:
                    self.metrics.rpc_errors += 1
                    raise RuntimeError(f"JSON-RPC error: {body['error']}")
                return body["result"]
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                retriable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if not retriable or attempt == self.max_retries:
                    raise
                self.metrics.retries += 1
                backoff = min(60.0, 2.0**attempt) * random.uniform(0.8, 1.2)
                print(
                    f"RPC retry {attempt + 1}/{self.max_retries} after "
                    f"{type(exc).__name__}; sleeping {backoff:.1f}s",
                    flush=True,
                )
                time.sleep(backoff)
        raise AssertionError("unreachable")


def resolve_rpc_url() -> str:
    direct = os.environ.get("EXECUTOR_READ_RPC_URL")
    if direct:
        return direct
    raw = subprocess.check_output(
        ["docker", "inspect", "-f", "{{json .Config.Env}}", "eatlq-executor-1"],
        text=True,
    )
    for item in json.loads(raw):
        if item.startswith("EXECUTOR_READ_RPC_URL="):
            return item.split("=", 1)[1]
    raise RuntimeError("EXECUTOR_READ_RPC_URL was not found in env or executor container")


def ensure_no_open_positions() -> None:
    output = subprocess.check_output(
        [
            "docker",
            "exec",
            "eatlq-redis-1",
            "redis-cli",
            "SMEMBERS",
            "positions:open_pools",
        ],
        text=True,
    )
    if output.strip():
        raise RuntimeError(f"refusing to backfill while positions are open: {output.strip()}")


def create_collect_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS eatlq.uniswap_v3_collects
    (
        chain_id UInt64,
        block_time DateTime64(3, 'UTC'),
        block_date Date DEFAULT toDate(block_time),
        block_number UInt64,
        tx_hash String,
        log_index UInt32,
        pool_address LowCardinality(String),
        owner LowCardinality(String),
        recipient LowCardinality(String),
        tick_lower Int32,
        tick_upper Int32,
        amount0 UInt256,
        amount1 UInt256,
        removed UInt8,
        ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
        CONSTRAINT base_chain_only CHECK chain_id = 8453
    )
    ENGINE = ReplacingMergeTree(ingested_at)
    PARTITION BY toYYYYMM(block_time)
    ORDER BY (chain_id, pool_address, block_number, tx_hash, log_index)
    """
    subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "-q", sql],
        check=True,
    )


def insert_collects(rows: Iterable[dict[str, Any]]) -> None:
    columns = (
        "chain_id,block_time,block_number,tx_hash,log_index,pool_address,owner,"
        "recipient,tick_lower,tick_upper,amount0,amount1,removed"
    )
    payload_lines = []
    for row in rows:
        payload_lines.append(
            json.dumps(
                {key: row[key] for key in columns.split(",")}, separators=(",", ":")
            )
        )
    if not payload_lines:
        return
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "eatlq-clickhouse-1",
            "clickhouse-client",
            "-q",
            f"INSERT INTO eatlq.uniswap_v3_collects ({columns}) FORMAT JSONEachRow",
        ],
        input="\n".join(payload_lines) + "\n",
        text=True,
        check=True,
    )


def event_output_dir(event_type: str) -> Path:
    if event_type == "collect":
        return OUT / "collects_raw"
    return OUT / "rpc_backfill" / event_type


def part_path(event_type: str, start: int, end: int) -> Path:
    return event_output_dir(event_type) / f"{event_type}_{start}_{end}.csv.gz"


def write_part(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as raw:
        temporary = Path(raw.name)
    try:
        with gzip.open(temporary, "wt", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def state_path(event_type: str) -> Path:
    return OUT / "backfill_state" / f"{event_type}.json"


def load_state(event_type: str) -> tuple[int, Metrics]:
    path = state_path(event_type)
    if not path.exists():
        return START_BLOCK, Metrics(datetime.now(timezone.utc).isoformat())
    state = json.loads(path.read_text())
    if state["chain_id"] != CHAIN_ID or state["end_block"] != END_BLOCK:
        raise ValueError(f"checkpoint parameters do not match: {path}")
    return int(state["next_block"]), Metrics(**state["metrics"])


def save_state(event_type: str, next_block: int, metrics: Metrics) -> None:
    path = state_path(event_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chain_id": CHAIN_ID,
        "event_type": event_type,
        "start_block": START_BLOCK,
        "end_block": END_BLOCK,
        "next_block": next_block,
        "event_topic": TOPICS[event_type],
        "metrics": asdict(metrics),
        "observed_getlogs_credits": metrics.json_rpc_responses * 60,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def run_event(event_type: str, client: RpcClient, span: int) -> Metrics:
    if span > MAX_SPAN:
        raise ValueError(f"span {span} exceeds hard maximum {MAX_SPAN}")
    next_block, metrics = load_state(event_type)
    client.metrics = metrics
    started = time.monotonic()
    while next_block <= END_BLOCK:
        end = min(next_block + span - 1, END_BLOCK)
        raw_logs = client.call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(next_block),
                    "toBlock": hex(end),
                    "address": list(POOLS),
                    "topics": [TOPICS[event_type]],
                }
            ],
        )
        rows = [decode_log(event_type, log) for log in raw_logs]
        rows.sort(key=lambda row: (row["block_number"], row["log_index"]))
        destination = part_path(event_type, next_block, end)
        write_part(destination, rows)
        if event_type == "collect":
            insert_collects(rows)
        metrics.completed_chunks += 1
        metrics.decoded_logs += len(rows)
        metrics.elapsed_seconds += time.monotonic() - started
        started = time.monotonic()
        next_block = end + 1
        save_state(event_type, next_block, metrics)
        print(
            f"{event_type} {end:,}/{END_BLOCK:,}: {len(rows):,} logs; "
            f"total={metrics.decoded_logs:,}; chunks={metrics.completed_chunks}",
            flush=True,
        )
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--events",
        default="collect,mint,burn",
        help="comma-separated subset/order of collect,mint,burn",
    )
    parser.add_argument("--span", type=int, default=MAX_SPAN)
    parser.add_argument("--max-rps", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    events = [item.strip().lower() for item in args.events.split(",") if item.strip()]
    if not events or any(item not in TOPICS for item in events):
        raise ValueError(f"invalid events: {events}")
    ensure_no_open_positions()
    if "collect" in events:
        create_collect_table()
    client = RpcClient(
        resolve_rpc_url(),
        max_rps=args.max_rps,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )
    for event_type in events:
        metrics = run_event(event_type, client, args.span)
        print(json.dumps({event_type: asdict(metrics)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
