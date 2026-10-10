"""ClickHouse helpers for wallet_profile_v0 (offline CH only, no new RPC)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CH_CONTAINER = os.environ.get("CLICKHOUSE_CONTAINER", "eatlq-clickhouse-1")
CHAIN_ID = int(os.environ.get("CHAIN_ID", "8453"))

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"


def ch_query(sql: str, *, timeout: int = 300, container: str | None = None) -> list[dict[str, Any]]:
    """Run ClickHouse query via docker exec; return rows as dicts."""
    cont = container or DEFAULT_CH_CONTAINER
    fmt = "JSONEachRow"
    r = subprocess.run(
        [
            "docker", "exec", cont,
            "clickhouse-client",
            "--format", fmt,
            "--max_memory_usage", "6000000000",
            "--max_bytes_before_external_group_by", "5368709120",
            "--max_bytes_before_external_sort", "5368709120",
            "--query", sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ClickHouse error: {r.stderr.strip()[:500]}")
    rows: list[dict[str, Any]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def ch_query_tsv(sql: str, *, timeout: int = 300) -> list[list[str]]:
    r = subprocess.run(
        [
            "docker", "exec", DEFAULT_CH_CONTAINER,
            "clickhouse-client", "--format", "TabSeparated", "--query", sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ClickHouse error: {r.stderr.strip()[:500]}")
    return [line.split("\t") for line in r.stdout.splitlines() if line]


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
