from __future__ import annotations

import subprocess
from pathlib import Path

from safety import validate_readonly_query


def query_tsv(sql: str, container: str = "eatlq-clickhouse-1") -> str:
    safe_sql = validate_readonly_query(sql)
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "clickhouse-client",
            "--readonly=1",
            "--max_execution_time=180",
            "--format",
            "TabSeparatedWithNames",
            "--query",
            safe_sql,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=200,
    )
    if proc.returncode:
        raise RuntimeError(f"read-only query failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout
