#!/usr/bin/env python3
"""Backfill eatlq.tx_from_by_hash via eth_getTransactionByHash (sender semantic alignment)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402
from sender_semantics import FORBIDDEN_BUYERS  # noqa: E402

log = logging.getLogger("build_tx_from_lookup")
DDL = REPO / "infra" / "clickhouse" / "init" / "040_tx_from_by_hash.sql"
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra"
META = AUDIT / "tx_from_lookup_meta.json"


def apply_ddl() -> None:
    import subprocess

    r = subprocess.run(
        ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client", "--multiquery"],
        input=DDL.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:500])


def pending_hashes(days: int, limit: int, chain_id: int = CHAIN_ID) -> list[dict]:
    sql = f"""
    SELECT
        lower(s.tx_hash) AS tx_hash,
        max(s.block_time) AS block_time
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    WHERE s.chain_id = {chain_id}
      AND s.block_time >= now() - INTERVAL {days} DAY
      AND length(s.tx_hash) = 66
      AND lower(s.tx_hash) NOT IN (
          SELECT lower(tx_hash) FROM eatlq.tx_from_by_hash WHERE chain_id = {chain_id}
      )
    GROUP BY tx_hash
    ORDER BY block_time DESC
    LIMIT {limit}
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=180)


def fetch_tx_from(rpc_url: str, tx_hash: str, timeout: int = 15) -> str | None:
    try:
        r = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionByHash",
                "params": [tx_hash],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            return None
        frm = (body.get("result") or {}).get("from") or ""
        frm = frm.lower().strip()
        if frm.startswith("0x") and len(frm) == 42 and frm not in FORBIDDEN_BUYERS:
            return frm
    except Exception as exc:
        log.debug("fetch_tx_from %s: %s", tx_hash[:12], exc)
        return None
    return None


def insert_rows(rows: list[dict], chain_id: int = CHAIN_ID) -> None:
    if not rows:
        return
    values = []
    for row in rows:
        tx = row["tx_hash"].replace("'", "\\'")
        frm = row["tx_from"].replace("'", "\\'")
        bt = row.get("block_time") or "1970-01-01 00:00:00"
        if isinstance(bt, str) and "T" in bt:
            bt = bt.replace("T", " ").split(".")[0]
        values.append(f"({chain_id}, '{tx}', '{frm}', toDateTime('{bt}', 'UTC'))")
    sql = f"INSERT INTO eatlq.tx_from_by_hash (chain_id, tx_hash, tx_from, block_time) VALUES {','.join(values)}"
    ch_query(sql, timeout=120)


def run(
    *,
    days: int = 90,
    limit: int = 20_000,
    workers: int = 8,
    batch_insert: int = 200,
    apply_ddl_flag: bool = False,
) -> dict:
    # Prefer EVM_HTTP_URL (ingestor); LABEL_RPC_URL often rate-limited or TLS-broken.
    rpc = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL")
    if not rpc:
        raise RuntimeError("Set EVM_HTTP_URL or LABEL_RPC_URL")

    if apply_ddl_flag:
        apply_ddl()

    pending = pending_hashes(days, limit)
    log.info("pending tx hashes: %d (days=%d limit=%d)", len(pending), days, limit)
    if not pending:
        return {"inserted": 0, "pending": 0}

    inserted = 0
    buffer: list[dict] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_tx_from, rpc, row["tx_hash"]): row for row in pending}
        for i, fut in enumerate(as_completed(futs), 1):
            row = futs[fut]
            frm = fut.result()
            if frm:
                buffer.append({"tx_hash": row["tx_hash"], "tx_from": frm, "block_time": row.get("block_time")})
            if len(buffer) >= batch_insert:
                insert_rows(buffer)
                inserted += len(buffer)
                buffer.clear()
            if i % 500 == 0:
                log.info("  fetched %d/%d inserted=%d", i, len(pending), inserted)

    if buffer:
        insert_rows(buffer)
        inserted += len(buffer)

    coverage = ch_query(
        f"""
        SELECT
            count() AS mapped,
            (SELECT uniqExact(tx_hash) FROM eatlq.uniswap_v3_swaps_decoded
             WHERE chain_id={CHAIN_ID} AND block_time >= now()-INTERVAL {days} DAY) AS total_swaps_tx
        FROM eatlq.tx_from_by_hash
        WHERE chain_id={CHAIN_ID}
        FORMAT JSONEachRow
        """,
        timeout=60,
    )
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "limit_requested": limit,
        "pending_fetched": len(pending),
        "inserted": inserted,
        "elapsed_sec": round(time.time() - t0, 1),
        "coverage": coverage[0] if coverage else {},
        "RESEARCH_ONLY": True,
    }
    AUDIT.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, indent=2))
    return meta


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Backfill tx.from lookup for buyer semantics")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--limit", type=int, default=20_000, help="Max tx hashes per run")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--apply-ddl", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(days=args.days, limit=args.limit, workers=args.workers, apply_ddl_flag=args.apply_ddl), indent=2))


if __name__ == "__main__":
    main()
