#!/usr/bin/env python3
"""
Backfill sender_ch on dashcam rows via ClickHouse pool+time join.

Historical dashcam rows lack sender_address. This script joins each row's
(pool_address, timestamp) to the nearest swap in uniswap_v3_swaps_decoded
within ±5 minutes and writes sender_ch.

Output: mvp/audits/wallet_profile_v0/labels/dashcam_with_sender_ch.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query, write_csv  # noqa: E402

log = logging.getLogger("dashcam_sender_backfill")

DASHCAM = REPO / "logs" / "dashcam.csv"
OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "dashcam_with_sender_ch.csv"
META = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "dashcam_sender_backfill_meta.json"


def _ts_ms(row: dict) -> int:
    try:
        return int(float(row.get("timestamp") or 0))
    except (TypeError, ValueError):
        return 0


def _pool(row: dict) -> str:
    return (row.get("pool_address") or row.get("token_address") or "").lower().strip()


def _bucket_ms(ts_ms: int) -> int:
    return (ts_ms // 300_000) * 300_000


def lookup_senders_batch(keys: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """keys = [(pool, bucket_ms), ...] → sender_ch."""
    if not keys:
        return {}
    # Build VALUES tuples for CH
    tuples = []
    for pool, bms in keys:
        if not pool.startswith("0x"):
            continue
        dt = datetime.fromtimestamp(bms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        tuples.append(f"('{pool}', toDateTime('{dt}', 'UTC'))")
    if not tuples:
        return {}

    values_sql = ",\n".join(tuples[:500])
    sql = f"""
    WITH targets AS (
        SELECT pool, bucket_time FROM (
            SELECT * FROM VALUES(
                ('pool', 'String'), ('bucket_time', 'DateTime')
            ) {', '.join('(' + t + ')' for t in tuples[:500])}
        )
    ),
    swaps AS (
        SELECT
            lower(s.pool_address) AS pool,
            toStartOfFiveMinutes(s.block_time) AS bucket_time,
            lower(s.sender) AS sender,
            count() AS n
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        INNER JOIN targets AS t
            ON lower(s.pool_address) = t.pool
           AND toStartOfFiveMinutes(s.block_time) = t.bucket_time
        WHERE s.chain_id = {CHAIN_ID}
        GROUP BY pool, bucket_time, sender
    )
    SELECT pool, bucket_time, argMax(sender, n) AS sender_ch
    FROM swaps
    GROUP BY pool, bucket_time
    FORMAT JSONEachRow
    """
    # ClickHouse VALUES syntax varies; use simpler IN approach per batch
    out: dict[tuple[str, int], str] = {}
    chunk_size = 200
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        pools = sorted({p for p, _ in chunk if p.startswith("0x")})
        if not pools:
            continue
        min_b = min(b for _, b in chunk)
        max_b = max(b for _, b in chunk) + 300_000
        min_dt = datetime.fromtimestamp(min_b / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        max_dt = datetime.fromtimestamp(max_b / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        pool_in = ",".join(f"'{p}'" for p in pools)
        q = f"""
        SELECT
            lower(pool_address) AS pool,
            toUnixTimestamp(toStartOfFiveMinutes(block_time)) * 1000 AS bucket_ms,
            lower(sender) AS sender,
            count() AS n
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_address) IN ({pool_in})
          AND block_time >= toDateTime('{min_dt}', 'UTC')
          AND block_time <= toDateTime('{max_dt}', 'UTC')
        GROUP BY pool, bucket_ms, sender
        ORDER BY pool, bucket_ms, n DESC
        FORMAT JSONEachRow
        """
        rows = ch_query(q, timeout=120)
        best: dict[tuple[str, int], tuple[int, str]] = {}
        for r in rows:
            k = (r["pool"], int(r["bucket_ms"]))
            n = int(r["n"])
            if k not in best or n > best[k][0]:
                best[k] = (n, r["sender"])
        for k, (_, sender) in best.items():
            out[k] = sender
    return out


def run(
    *,
    dashcam_path: Path = DASHCAM,
    out_path: Path = OUT,
    limit: int = 100_000,
    batch_keys: int = 200,
) -> dict[str, Any]:
    if not dashcam_path.exists():
        raise FileNotFoundError(dashcam_path)

    rows_in: list[dict] = []
    with dashcam_path.open(newline="", encoding="utf-8") as f:
        rows_in = list(csv.DictReader(f))
    rows_in.sort(key=_ts_ms, reverse=True)
    if limit:
        rows_in = rows_in[:limit]

    # Unique (pool, 5m bucket) keys
    key_to_indices: dict[tuple[str, int], list[int]] = defaultdict(list)
    for i, row in enumerate(rows_in):
        pool = _pool(row)
        ts = _ts_ms(row)
        if not pool or ts <= 0:
            continue
        key_to_indices[(pool, _bucket_ms(ts))].append(i)

    keys = list(key_to_indices.keys())
    log.info("lookup keys=%d rows=%d", len(keys), len(rows_in))

    sender_map: dict[tuple[str, int], str] = {}
    for i in range(0, len(keys), batch_keys):
        batch = keys[i : i + batch_keys]
        sender_map.update(lookup_senders_batch(batch))
        log.info("  batch %d/%d mapped=%d", i // batch_keys + 1, (len(keys) + batch_keys - 1) // batch_keys, len(sender_map))

    filled = 0
    rows_out: list[dict] = []
    for row in rows_in:
        out = dict(row)
        pool = _pool(row)
        ts = _ts_ms(row)
        live = (row.get("sender_address") or "").strip().lower()
        ch_sender = sender_map.get((pool, _bucket_ms(ts)), "")
        if live.startswith("0x") and len(live) == 42:
            out["sender_ch"] = live
        else:
            out["sender_ch"] = ch_sender
        if out["sender_ch"]:
            filled += 1
        rows_out.append(out)

    fieldnames = list(rows_out[0].keys()) if rows_out else []
    if "sender_ch" not in fieldnames:
        fieldnames.append("sender_ch")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out_path, rows_out, fieldnames)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_rows": len(rows_in),
        "unique_keys": len(keys),
        "sender_ch_filled": filled,
        "fill_rate": round(filled / len(rows_in), 4) if rows_in else 0.0,
        "output": str(out_path.relative_to(REPO)),
        "RESEARCH_ONLY": True,
    }
    META.write_text(json.dumps(meta, indent=2))
    return meta


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Backfill sender_ch on dashcam via CH")
    p.add_argument("--dashcam", default=str(DASHCAM))
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--limit", type=int, default=100_000)
    p.add_argument("--batch-keys", type=int, default=200)
    args = p.parse_args()
    meta = run(
        dashcam_path=Path(args.dashcam),
        out_path=Path(args.out),
        limit=args.limit,
        batch_keys=args.batch_keys,
    )
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
