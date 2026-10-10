#!/usr/bin/env python3
"""Merge-refresh pool features for expanded_v2 — pool_features_5m range + sidecar lookup."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_smart_wallet_roundtrip_dataset_builder import (  # noqa: E402
    as_float,
    fetch_existing_sidecar_slot_keys,
    fetch_sidecar_features_for_slots,
    floor_5m,
    parse_ch_dt,
    q,
    run_ch,
    sql_dt,
    CHAIN_ID,
)

OUT_DIR = Path(
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2"
)
DATASET = OUT_DIR / "dataset_sample.csv"
POOL_FEAT_COLS = [
    "activity_heat",
    "buy_pressure_ratio",
    "price_velocity_5m",
    "token_volatility_5m",
]


def pool_feature_coverage(rows: list[dict]) -> float:
    n = len(rows)
    if not n:
        return 0.0
    return min(
        sum(1 for r in rows if as_float(r.get(c)) is not None) / n for c in POOL_FEAT_COLS
    )


def latest_feature(features: list[dict], entry_time: datetime) -> dict:
    cutoff = entry_time - timedelta(minutes=5)
    chosen: dict = {}
    for row in features:
        if row["window_start"] <= cutoff:
            chosen = row
        else:
            break
    return chosen


def build_needed_slots(rows: list[dict]) -> dict[str, set[datetime]]:
    slots: dict[str, set[datetime]] = defaultdict(set)
    for r in rows:
        pool = (r["pool"] or "").lower()
        et = datetime.fromisoformat(r["entry_time"].replace("Z", "+00:00"))
        slots[pool].add(floor_5m(et - timedelta(minutes=5)))
    return slots


def fetch_pool_features_5m_range(
    pool: str, start: datetime, end: datetime, timeout: int = 120
) -> list[dict]:
    rows = run_ch(
        f"""
        SELECT
          window_start,
          price_velocity_5m,
          buy_pressure_ratio,
          token_volatility_5m,
          activity_heat
        FROM pool_features_5m
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_address) = {q(pool)}
          AND window_start >= toDateTime({q(sql_dt(start))}, 'UTC')
          AND window_start <= toDateTime({q(sql_dt(end))}, 'UTC')
        ORDER BY window_start
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    return [
        {
            "window_start": parse_ch_dt(r["window_start"]),
            "price_velocity_5m": as_float(r.get("price_velocity_5m")),
            "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
            "token_volatility_5m": as_float(r.get("token_volatility_5m")),
            "activity_heat": as_float(r.get("activity_heat")),
        }
        for r in rows
    ]


def main() -> int:
    rows = list(csv.DictReader(DATASET.open()))
    cov_before = pool_feature_coverage(rows)
    needed = build_needed_slots(rows)
    slots_needed = sum(len(v) for v in needed.values())

    existing_keys = fetch_existing_sidecar_slot_keys(
        needed, 180, date_shard_days=1, pool_batch_size=25
    )
    slots_existing_before = len(existing_keys)
    slots_missing_before = slots_needed - slots_existing_before

    sidecar_map = fetch_sidecar_features_for_slots(
        needed, 180, date_shard_days=1, pool_batch_size=25
    )

    pool_ranges: dict[str, tuple[datetime, datetime]] = {}
    for r in rows:
        pool = (r["pool"] or "").lower()
        et = datetime.fromisoformat(r["entry_time"].replace("Z", "+00:00"))
        cutoff = floor_5m(et - timedelta(minutes=5))
        lo = cutoff - timedelta(hours=3)
        if pool not in pool_ranges:
            pool_ranges[pool] = (lo, cutoff)
        else:
            a, b = pool_ranges[pool]
            pool_ranges[pool] = (min(a, lo), max(b, cutoff))

    pool_series: dict[str, list[dict]] = {}
    for i, (pool, (start, end)) in enumerate(sorted(pool_ranges.items())):
        print(f"[merge] pool {i+1}/{len(pool_ranges)} {pool[:12]}...", flush=True)
        try:
            feats = fetch_pool_features_5m_range(pool, start, end)
        except Exception as exc:
            print(f"  skip pool_features_5m: {str(exc)[:120]}", flush=True)
            feats = []
        by_ws: dict[datetime, dict] = {f["window_start"]: f for f in feats}
        for (p, slot), feat in sidecar_map.items():
            if p != pool:
                continue
            by_ws[slot] = {
                "window_start": slot,
                "price_velocity_5m": feat.get("price_velocity_5m"),
                "buy_pressure_ratio": feat.get("buy_pressure_ratio"),
                "token_volatility_5m": feat.get("token_volatility_5m"),
                "activity_heat": feat.get("activity_heat"),
            }
        pool_series[pool] = sorted(by_ws.values(), key=lambda x: x["window_start"])

    filled_rows = 0
    for r in rows:
        pool = (r["pool"] or "").lower()
        et = datetime.fromisoformat(r["entry_time"].replace("Z", "+00:00"))
        had = all(as_float(r.get(c)) is not None for c in POOL_FEAT_COLS)
        feat = latest_feature(pool_series.get(pool, []), et)
        if not feat:
            continue
        for c in POOL_FEAT_COLS:
            if feat.get(c) is not None:
                r[c] = feat[c]
        if all(as_float(r.get(c)) is not None for c in POOL_FEAT_COLS) and not had:
            filled_rows += 1

    cov_after = pool_feature_coverage(rows)
    backup = OUT_DIR / "dataset_sample.csv.bak_pre_merge_refresh"
    if not backup.exists():
        backup.write_bytes(DATASET.read_bytes())
    with DATASET.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    report = {
        "slots_needed": slots_needed,
        "slots_existing_before": slots_existing_before,
        "slots_missing_before": slots_missing_before,
        "sidecar_rows_inserted": 0,
        "sidecar_insert_note": "skipped CH INSERT; merged from pool_features_5m range + existing sidecar",
        "slots_existing_after": len(sidecar_map),
        "slots_missing_after": slots_needed - len(sidecar_map),
        "pool_feature_coverage_before": cov_before,
        "pool_feature_coverage_after": cov_after,
        "rows_newly_filled_via_latest_feature": filled_rows,
    }
    (OUT_DIR / "sidecar_merge_refresh_report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
