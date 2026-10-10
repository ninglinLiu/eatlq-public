"""Label dashcam via bulk-loaded pool_features_5m (approximate path only).

Rationale: the original label_dashcam.py queries ClickHouse once per dashcam
row. 506k rows × ~30 ms round-trip ≈ 28 h. Since pool_features_5m is small
(<20k rows / 7d), pull it all once and join in Python. Typical runtime on
500k rows: a few minutes.

Semantics match label_dashcam.py's approximate path (the executable-quote
path needed an archive RPC we do not have):

    net_return ≈ avg(future_price_velocity_5m over next 30 min) - ROUND_TRIP_COST
    label = 1 if net_return > 0 else 0

Output header + column order is kept identical to label_dashcam.py so that
retrain_model.py sees a drop-in replacement.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("label_dashcam_fast")

ROUND_TRIP_COST = 0.006
BUCKET_SEC = 300


def ch_query(url: str, db: str, user: str, password: str, sql: str,
             timeout: int = 120) -> list[dict]:
    params = urlencode({
        "database": db, "user": user, "password": password,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{url}/?{params}", data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    if not body:
        return []
    return [json.loads(line) for line in body.split("\n") if line.strip()]


def load_feature_grid(ch_url, ch_db, ch_user, ch_pw, chain_id: int,
                      min_ts_ms: int, max_ts_ms: int
                      ) -> dict[tuple[str, int], float]:
    """Return {(pool_addr_lower, bucket_start_sec): price_velocity}.

    Buckets are 5-min windows from pool_features_5m. We pull one query
    spanning [min_ts - 5min, max_ts + 35min] so every dashcam signal's
    forward window is covered.
    """
    lo_sec = min_ts_ms // 1000 - BUCKET_SEC
    hi_sec = max_ts_ms // 1000 + 35 * 60
    sql = f"""
        SELECT
            lower(pool_address) AS pool,
            toUnixTimestamp(window_start) AS ts,
            price_velocity_5m AS v
        FROM eatlq.pool_features_5m
        WHERE chain_id = {chain_id}
          AND window_start >= fromUnixTimestamp({lo_sec})
          AND window_start <= fromUnixTimestamp({hi_sec})
    """
    log.info("loading pool_features_5m (%d → %d sec) ...", lo_sec, hi_sec)
    rows = ch_query(ch_url, ch_db, ch_user, ch_pw, sql)
    grid: dict[tuple[str, int], float] = {}
    for r in rows:
        grid[(r["pool"], int(r["ts"]))] = float(r["v"])
    log.info("feature grid rows=%d, unique pools=%d",
             len(grid), len({k[0] for k in grid}))
    return grid


def forward_avg_velocity(grid: dict, pool: str, signal_ts_sec: int) -> tuple[float, int]:
    """Average price_velocity across next 6 × 5min buckets. Returns (avg, n).

    Matches label_approximate semantics (window_count >= 2 required).
    """
    # round signal_ts up to next bucket boundary so we only look at future
    start_bucket = ((signal_ts_sec // BUCKET_SEC) + 1) * BUCKET_SEC
    total = 0.0
    n = 0
    for i in range(6):
        ts = start_bucket + i * BUCKET_SEC
        v = grid.get((pool, ts))
        if v is not None:
            total += v
            n += 1
    if n < 2:
        return 0.0, n
    return total / n, n


def main() -> None:
    dashcam_path = Path(os.environ.get("DASHCAM_PATH", "logs/dashcam.csv"))
    output_path = Path(os.environ.get("LABELED_OUTPUT", "logs/labeled_dataset_v2.csv"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))

    if not dashcam_path.exists():
        log.error("%s not found", dashcam_path)
        sys.exit(1)

    log.info("reading %s ...", dashcam_path)
    rows: list[dict] = []
    min_ts = sys.maxsize
    max_ts = 0
    with dashcam_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row.get("timestamp", 0))
            except ValueError:
                continue
            if ts <= 0:
                continue
            rows.append(row)
            if ts < min_ts:
                min_ts = ts
            if ts > max_ts:
                max_ts = ts
    log.info("dashcam rows=%d, ts range %d → %d", len(rows), min_ts, max_ts)

    if not rows:
        log.error("no usable rows")
        sys.exit(1)

    grid = load_feature_grid(ch_url, ch_db, ch_user, ch_pw, chain_id, min_ts, max_ts)

    now_ms = int(time.time() * 1000)
    min_age_ms = 35 * 60 * 1000

    output_path.parent.mkdir(parents=True, exist_ok=True)
    labeled = 0
    positive = 0
    skipped_young = 0
    skipped_no_data = 0

    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "timestamp", "token_address", "symbol",
            "price_velocity_5m", "buy_pressure_ratio",
            "token_volatility_5m", "activity_heat",
            "triggered_signal",
            "label", "net_return", "label_type",
        ])
        for i, row in enumerate(rows):
            ts = int(row["timestamp"])
            if now_ms - ts < min_age_ms:
                skipped_young += 1
                continue
            pool = (row.get("token_address") or "").lower()
            if not pool:
                continue
            signal_ts_sec = ts // 1000
            velocity, n = forward_avg_velocity(grid, pool, signal_ts_sec)
            if n < 2:
                skipped_no_data += 1
                continue
            net_return = velocity - ROUND_TRIP_COST
            label = 1 if net_return > 0 else 0
            w.writerow([
                ts, pool, row.get("symbol", ""),
                row.get("momentum_val", "0"),
                row.get("whale_val", "0"),
                row.get("risk_val", "0"),
                row.get("activity_val", "0"),
                row.get("triggered_signal", "0"),
                label, round(net_return, 6), "approximate",
            ])
            labeled += 1
            if label == 1:
                positive += 1
            if (i + 1) % 50_000 == 0:
                log.info(
                    "progress %d/%d, labeled=%d (pos %d, %.1f%%) skip_young=%d skip_no_data=%d",
                    i + 1, len(rows), labeled, positive,
                    100.0 * positive / max(labeled, 1),
                    skipped_young, skipped_no_data,
                )

    rate = positive / labeled * 100 if labeled > 0 else 0
    log.info(
        "DONE labeled=%d positive=%d (%.1f%%) skip_young=%d skip_no_data=%d",
        labeled, positive, rate, skipped_young, skipped_no_data,
    )
    log.info("output=%s", output_path)
    log.warning(
        "FIREWALL: labels are approximate (velocity proxy). "
        "Model trained on these must NOT be deployed as production per P0-3."
    )


if __name__ == "__main__":
    main()
