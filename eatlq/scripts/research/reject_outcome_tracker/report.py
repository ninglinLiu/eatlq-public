#!/usr/bin/env python3
"""Reject-outcome tracker: summarize outcomes by rejection category.

Read-only report against the isolated app.reject_outcome_tracking table.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import PG_CONTAINER, PG_USER, PG_DB  # noqa: E402

SQL = """
SELECT
    reject_category                                   AS category,
    count(*)                                           AS n,
    count(*) FILTER (WHERE status = 'done')            AS n_done,
    round(avg(ret_1h_pct)::numeric, 1)                 AS avg_ret_1h,
    round(avg(ret_24h_pct)::numeric, 1)                AS avg_ret_24h,
    round(avg(peak_ret_pct)::numeric, 1)               AS avg_peak_ret,
    count(*) FILTER (WHERE ret_24h_pct > 20)            AS n_moon_24h,
    count(*) FILTER (WHERE still_trading_24h = FALSE)   AS n_pool_dead
FROM app.reject_outcome_tracking
GROUP BY reject_category
ORDER BY n DESC;
"""


def main() -> int:
    res = subprocess.run(["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-c", SQL])
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
