#!/usr/bin/env python3
"""Targeted sidecar coverage repair for expanded_v2 — no full builder rerun."""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse builder CH/sidecar helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_smart_wallet_roundtrip_dataset_builder import (  # noqa: E402
    CHAIN_ID,
    CH_CONTAINER,
    POOL_FEATURE_SIDECAR_TABLE,
    as_float,
    fetch_existing_sidecar_slot_keys,
    fetch_sidecar_features_for_slots,
    floor_5m,
    parse_ch_dt,
    q,
    run_ch,
    sql_dt,
)
import subprocess

OUT_DIR = Path(
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2"
)
DATASET = OUT_DIR / "dataset_sample.csv"
QUERY_TIMEOUT = 300
DATE_SHARD_DAYS = 1
POOL_BATCH = 2
INSERT_BATCH = 100


def materialize_slots_granular(
    missing_slots: dict[str, set[datetime]],
    timeout: int = 120,
) -> int:
    """One pool + one 5m window per query to stay under CH memory limits."""
    pending: list[dict] = []
    inserted_total = 0
    tsv_header = [
        "chain_id",
        "pool_address",
        "window_start",
        "price_velocity_5m",
        "buy_pressure_ratio",
        "token_volatility_5m",
        "activity_heat",
    ]

    def flush_batch() -> int:
        nonlocal pending, inserted_total
        if not pending:
            return 0
        uniq: dict[tuple[str, str], dict] = {}
        for row in pending:
            uniq[(row["pool_address"], row["window_start"])] = row
        final_rows = list(uniq.values())
        payload_lines = ["\t".join(tsv_header)]
        for row in final_rows:
            payload_lines.append("\t".join(str(row[h]) for h in tsv_header))
        payload = "\n".join(payload_lines) + "\n"
        cmd = [
            "docker",
            "exec",
            "-i",
            CH_CONTAINER,
            "clickhouse-client",
            "--database",
            "eatlq",
            "--query",
            (
                f"INSERT INTO {POOL_FEATURE_SIDECAR_TABLE} "
                "(chain_id,pool_address,window_start,price_velocity_5m,"
                "buy_pressure_ratio,token_volatility_5m,activity_heat) "
                "FORMAT TSVWithNames"
            ),
        ]
        cp = subprocess.run(cmd, input=payload, text=True, capture_output=True, timeout=timeout)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or cp.stdout).strip()[:800])
        n = len(final_rows)
        inserted_total += n
        pending = []
        return n

    total = sum(len(v) for v in missing_slots.values())
    done = 0
    for pool in sorted(missing_slots):
        for slot in sorted(missing_slots[pool]):
            done += 1
            if done % 50 == 0:
                print(f"[granular] {done}/{total} slots processed, inserted_total={inserted_total}", flush=True)
            slot_end = slot + timedelta(minutes=5)
            rows = run_ch(
                f"""
                SELECT
                  lower(pool_address) AS pool_address,
                  toStartOfFiveMinutes(coalesce(block_time, received_at_ms)) AS window_start,
                  toFloat64(sum(-toFloat64(amount0))) / greatest(sum(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))), 1) AS price_velocity_5m,
                  (countIf(amount0 < 0) * 1.) / greatest(count(), 1) AS buy_pressure_ratio,
                  if(avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))) > 0,
                     stddevPop(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))) / avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))),
                     0) AS token_volatility_5m,
                  count() AS activity_heat
                FROM eatlq.uniswap_v3_swaps_decoded
                WHERE chain_id = {CHAIN_ID}
                  AND lower(pool_address) = {q(pool)}
                  AND coalesce(block_time, received_at_ms) >= toDateTime64({q(sql_dt(slot))}, 3, 'UTC')
                  AND coalesce(block_time, received_at_ms) < toDateTime64({q(sql_dt(slot_end))}, 3, 'UTC')
                GROUP BY pool_address, window_start
                HAVING count() >= 1
                FORMAT TSVWithNames
                """,
                timeout=timeout,
            )
            if not rows:
                continue
            r = rows[0]
            pending.append(
                {
                    "chain_id": CHAIN_ID,
                    "pool_address": pool,
                    "window_start": slot.strftime("%Y-%m-%d %H:%M:%S"),
                    "price_velocity_5m": as_float(r.get("price_velocity_5m")),
                    "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
                    "token_volatility_5m": as_float(r.get("token_volatility_5m")),
                    "activity_heat": int(float(r.get("activity_heat") or 0)),
                }
            )
            if len(pending) >= INSERT_BATCH:
                flush_batch()
    flush_batch()
    return inserted_total


def pool_feature_coverage(rows: list[dict]) -> float:
    cols = ["activity_heat", "buy_pressure_ratio", "price_velocity_5m", "token_volatility_5m"]
    n = len(rows)
    if not n:
        return 0.0
    return min(sum(1 for r in rows if as_float(r.get(c)) is not None) / n for c in cols)


def build_needed_slots_from_rows(rows: list[dict]) -> dict[str, set[datetime]]:
    slots: dict[str, set[datetime]] = defaultdict(set)
    for r in rows:
        pool = (r.get("pool") or "").lower()
        et = r.get("entry_time", "")
        if not pool or not et:
            continue
        t = datetime.fromisoformat(et.replace("Z", "+00:00"))
        slots[pool].add(floor_5m(t - timedelta(minutes=5)))
    return slots


def main() -> int:
    rows = list(csv.DictReader(DATASET.open()))
    cov_before = pool_feature_coverage(rows)

    needed = build_needed_slots_from_rows(rows)
    slots_needed = sum(len(v) for v in needed.values())

    existing = fetch_existing_sidecar_slot_keys(
        needed, QUERY_TIMEOUT, date_shard_days=DATE_SHARD_DAYS, pool_batch_size=25
    )
    slots_existing_before = len(existing)

    missing: dict[str, set[datetime]] = defaultdict(set)
    for pool, pool_slots in needed.items():
        for slot in pool_slots:
            if (pool, slot) not in existing:
                missing[pool].add(slot)
    slots_missing_before = sum(len(v) for v in missing.values())

    slots_inserted = 0
    insert_error = ""
    if slots_missing_before > 0:
        try:
            slots_inserted = materialize_slots_granular(missing, timeout=QUERY_TIMEOUT)
        except Exception as exc:
            insert_error = str(exc)[:500]

    existing_after = fetch_existing_sidecar_slot_keys(
        needed, QUERY_TIMEOUT, date_shard_days=DATE_SHARD_DAYS, pool_batch_size=25
    )
    slots_existing_after = len(existing_after)
    slots_missing_after = slots_needed - slots_existing_after

    pool_features = fetch_sidecar_features_for_slots(
        needed, QUERY_TIMEOUT, date_shard_days=DATE_SHARD_DAYS, pool_batch_size=25
    )
    slots_filled = len(pool_features)

    merged = 0
    for r in rows:
        pool = (r.get("pool") or "").lower()
        et = r.get("entry_time", "")
        if not pool or not et:
            continue
        t = datetime.fromisoformat(et.replace("Z", "+00:00"))
        slot = floor_5m(t - timedelta(minutes=5))
        feat = pool_features.get((pool, slot), {})
        if not feat:
            continue
        for k in ("activity_heat", "buy_pressure_ratio", "price_velocity_5m", "token_volatility_5m"):
            if feat.get(k) is not None:
                r[k] = feat[k]
        merged += sum(1 for k in ("activity_heat", "buy_pressure_ratio", "price_velocity_5m", "token_volatility_5m") if feat.get(k) is not None)

    cov_after = pool_feature_coverage(rows)

    fields = rows[0].keys() if rows else []
    backup = OUT_DIR / "dataset_sample.csv.bak_pre_sidecar_repair"
    if not backup.exists():
        backup.write_bytes(DATASET.read_bytes())
    with DATASET.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    repair_report = {
        "slots_needed": slots_needed,
        "slots_existing_before": slots_existing_before,
        "slots_missing_before": slots_missing_before,
        "slots_inserted": slots_inserted,
        "slots_existing_after": slots_existing_after,
        "slots_missing_after": slots_missing_after,
        "slots_filled_lookup": slots_filled,
        "pool_feature_coverage_before": cov_before,
        "pool_feature_coverage_after": cov_after,
        "insert_error": insert_error,
        "merged_feature_cells": merged,
    }
    (OUT_DIR / "sidecar_repair_report.json").write_text(
        json.dumps(repair_report, indent=2) + "\n"
    )

    # Patch decision_flags
    flags_path = OUT_DIR / "decision_flags.json"
    if flags_path.exists():
        df = json.loads(flags_path.read_text())
        df["sidecar_repair"] = repair_report
        df["feature_non_null_activity_heat"] = sum(
            1 for r in rows if as_float(r.get("activity_heat")) is not None
        )
        for c in ("buy_pressure_ratio", "price_velocity_5m", "token_volatility_5m"):
            df[f"feature_non_null_{c}"] = sum(
                1 for r in rows if as_float(r.get(c)) is not None
            )
        df["pool_feature_join_status"] = (
            f"repaired: slots_inserted={slots_inserted}, "
            f"coverage={cov_after:.4f}, missing_after={slots_missing_after}"
        )
        flags_path.write_text(json.dumps(df, indent=2, sort_keys=True) + "\n")

    print(json.dumps(repair_report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
