#!/usr/bin/env python3
"""Refresh sender_pnl materialized tables in chain_id chunks (OOM-safe)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTAINER = __import__("os").environ.get("CLICKHOUSE_CONTAINER", "eatlq-clickhouse-1")
CHAIN_ID = int(__import__("os").environ.get("CHAIN_ID", "8453"))

log = logging.getLogger("refresh_sender_pnl_mat")


def _exec(sql: str, timeout: int = 600) -> None:
    r = subprocess.run(
        [
            "docker", "exec", CONTAINER, "clickhouse-client",
            "--max_memory_usage", "5000000000",
            "--max_bytes_before_external_group_by", "4000000000",
            "--multiquery", "--query", sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:800])


def refresh() -> None:
    ddl = (REPO / "infra/clickhouse/init/030_sender_pnl_materialized.sql").read_text()
    _exec(ddl)
    log.info("tables ensured")

    _exec("TRUNCATE TABLE eatlq.sender_token_pnl_mat")
    _exec(f"""
        INSERT INTO eatlq.sender_token_pnl_mat
        SELECT
            chain_id, sender_address, token_address,
            bought_trade_count, sold_trade_count,
            total_bought_amount_normalized, total_sold_amount_normalized,
            net_amount_normalized, has_round_trip, is_win,
            first_trade_at, latest_trade_at, hold_seconds,
            now() AS refreshed_at
        FROM eatlq.sender_token_pnl_v1
        WHERE chain_id = {CHAIN_ID}
        SETTINGS max_bytes_before_external_group_by = 4000000000
    """)
    log.info("sender_token_pnl_mat refreshed for chain_id=%s", CHAIN_ID)

    _exec("TRUNCATE TABLE eatlq.sender_pnl_summary_mat")
    _exec(f"""
        INSERT INTO eatlq.sender_pnl_summary_mat
        SELECT
            chain_id, sender_address,
            tokens_traded, round_trips, wins, win_rate,
            avg_hold_seconds, first_ever_trade, latest_trade, active_span_days,
            now() AS refreshed_at
        FROM eatlq.sender_pnl_summary_v1
        WHERE chain_id = {CHAIN_ID}
        SETTINGS max_bytes_before_external_group_by = 4000000000
    """)
    log.info("sender_pnl_summary_mat refreshed")

    r = subprocess.run(
        ["docker", "exec", CONTAINER, "clickhouse-client", "--query",
         "SELECT count() FROM eatlq.sender_pnl_summary_mat"],
        capture_output=True, text=True, check=True,
    )
    log.info("sender_pnl_summary_mat rows=%s", r.stdout.strip())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        refresh()
    except RuntimeError as e:
        log.error("refresh failed (view chain may still OOM on this host): %s", e)
        log.error("DDL file is at infra/clickhouse/init/030_sender_pnl_materialized.sql")
        sys.exit(1)


if __name__ == "__main__":
    main()
