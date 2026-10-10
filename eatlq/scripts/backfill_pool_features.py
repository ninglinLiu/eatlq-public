#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def ch_query(url: str, db: str, user: str, password: str, sql: str) -> list[dict]:
    params = urlencode(
        {
            "database": db,
            "user": user,
            "password": password,
            "default_format": "JSONEachRow",
            "max_memory_usage": "7000000000",
            "max_bytes_before_external_group_by": "5368709120",
            "max_bytes_before_external_sort": "5368709120",
        }
    )
    req = Request(f"{url}/?{params}", data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urlopen(req, timeout=600) as resp:
        body = resp.read().decode("utf-8").strip()
    if not body:
        return []
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def main() -> int:
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    out_path = Path(os.environ.get("BACKFILL_OUTPUT", "models/realtime_dataset_v2.csv"))
    days = int(os.environ.get("BACKFILL_DAYS", "90"))
    chains = os.environ.get("BACKFILL_CHAIN_IDS", "8453,1,56,42161")
    chain_ids = [int(x.strip()) for x in chains.split(",") if x.strip()]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_total = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = None
        per_chain_stats: dict[int, dict[str, int]] = {}
        for chain_id in chain_ids:
            sql = f"""
                SELECT
                    pool_address,
                    chain_id,
                    window_start,
                    price_velocity_5m,
                    buy_pressure_ratio,
                    token_volatility_5m,
                    activity_heat,
                    ifNull(unique_buyer_entropy_5m, 0.0) AS unique_buyer_entropy_5m,
                    label
                FROM eatlq.pool_realtime_training_30m_v1
                WHERE chain_id = {chain_id}
                  AND window_start >= now() - INTERVAL {days} DAY
                ORDER BY window_start ASC
            """
            try:
                rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql)
            except Exception as exc:
                print(json.dumps({"event": "backfill_query_failed", "chain_id": chain_id, "error": str(exc)}))
                continue
            if not rows:
                continue
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
            writer.writerows(rows)
            rows_total += len(rows)
            positives = sum(int(r.get("label", 0) or 0) for r in rows)
            per_chain_stats[chain_id] = {"rows": len(rows), "positives": positives}
            print(json.dumps({"event": "backfill_chain_done", "chain_id": chain_id, "rows": len(rows)}))
    for chain_id, stats in per_chain_stats.items():
        rows = max(stats["rows"], 1)
        pos_rate = stats["positives"] / rows
        print(
            json.dumps(
                {
                    "event": "backfill_chain_balance",
                    "chain_id": chain_id,
                    "rows": stats["rows"],
                    "positives": stats["positives"],
                    "positive_rate": round(pos_rate, 4),
                    "target_met": pos_rate >= 0.30,
                }
            )
        )
    print(json.dumps({"event": "backfill_done", "rows_total": rows_total, "output": str(out_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
