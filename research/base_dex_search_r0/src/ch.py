"""Tiny ClickHouse helper."""
import subprocess


def ch(query, fmt="TabSeparatedWithNames", timeout=3600, mem=30_000_000_000):
    p = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         f"--max_memory_usage={mem}", "--max_execution_time=14400",
         "--query", f"{query} FORMAT {fmt}"],
        capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-4000:])
    return p.stdout


def ch_ok(query, timeout=3600):
    return ch(query, fmt="TSV", timeout=timeout).strip()
