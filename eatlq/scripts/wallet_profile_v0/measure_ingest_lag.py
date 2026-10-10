#!/usr/bin/env python3
"""Measure ClickHouse ingest lag vs chain head (A4 acceptance)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402

OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra" / "ingest_lag.json"
TARGET_LAG_SEC = 300


def measure() -> dict:
    rows = ch_query(
        f"""
        SELECT
            max(block_time) AS max_block_time,
            toUnixTimestamp(now()) - toUnixTimestamp(max(block_time)) AS lag_sec,
            count() AS swap_rows
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID}
        FORMAT JSONEachRow
        """,
        timeout=60,
    )
    row = rows[0] if rows else {}
    lag = int(row.get("lag_sec") or 999999)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": CHAIN_ID,
        "max_block_time": row.get("max_block_time"),
        "lag_sec": lag,
        "lag_hours": round(lag / 3600, 2),
        "swap_rows": int(row.get("swap_rows") or 0),
        "target_lag_sec": TARGET_LAG_SEC,
        "acceptance_pass": lag < TARGET_LAG_SEC,
        "RESEARCH_ONLY": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    report = measure()
    print(json.dumps(report, indent=2))
    if not report["acceptance_pass"]:
        print(
            f"WARN: lag {report['lag_sec']}s exceeds target {TARGET_LAG_SEC}s — "
            "tune ARCHIVER_BATCH_SIZE / BACKFILL_CHUNK_SIZE / run backfill catchup",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
