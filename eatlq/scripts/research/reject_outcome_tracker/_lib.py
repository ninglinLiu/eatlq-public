"""Shared helpers for the reject-outcome tracker.

Everything here shells out to `docker exec`/`docker logs`. No web3, no RPC,
no secrets. Read-only against live containers except for INSERT/UPDATE on
the isolated app.reject_outcome_tracking table.
"""
from __future__ import annotations

import subprocess

PG_CONTAINER = "eatlq-postgres-1"
PG_USER = "eatlq"
PG_DB = "eatlq"
CH_CONTAINER = "eatlq-clickhouse-1"
REDIS_CONTAINER = "eatlq-redis-1"
EXECUTOR_CONTAINER = "eatlq-executor-1"
WETH_BASE = "0x4200000000000000000000000000000000000006"


def run(cmd: list[str], input_text: str | None = None, timeout: int = 60):
    res = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    return res.stdout, res.stderr, res.returncode


def run_merged(cmd: list[str], timeout: int = 60) -> tuple[str, int]:
    """Like run(), but merges stdout+stderr (needed for `docker logs`, whose
    app-level log lines are typically written to the container's stderr)."""
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return res.stdout, res.returncode


def ch_query(sql: str):
    out, err, rc = run(["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", sql])
    if rc != 0:
        return None, err
    return out.strip(), None


def pg_query_rows(sql: str):
    """Run a SELECT via psql in tab-separated unaligned mode; return list[list[str]]."""
    out, err, rc = run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-At", "-F", "\t"],
        input_text=sql,
    )
    if rc != 0:
        return [], err
    rows = [line.split("\t") for line in out.strip().split("\n") if line.strip() != ""]
    return rows, None


def pg_exec(sql: str):
    out, err, rc = run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1"],
        input_text=sql,
    )
    return rc == 0, err


def pg_escape(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def redis_hgetall(key: str) -> dict:
    out, _, rc = run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "HGETALL", key])
    if rc != 0 or not out.strip():
        return {}
    lines = out.strip().split("\n")
    return dict(zip(lines[0::2], lines[1::2]))


def tick_ret_pct(token0_is_meme: bool | None, entry_tick, other_tick) -> float | None:
    """% change in the non-WETH leg's price, from entry_tick to other_tick."""
    if entry_tick is None or other_tick is None or token0_is_meme is None:
        return None
    delta = (other_tick - entry_tick) if token0_is_meme else (entry_tick - other_tick)
    return (1.0001 ** delta - 1.0) * 100.0
