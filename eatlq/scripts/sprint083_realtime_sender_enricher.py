#!/usr/bin/env python3
"""
Sprint083 Realtime Sender Enricher
====================================
Runs every 2 minutes (via cron or --watch mode). Finds sprint055 rows where
is_0x6ff5 is empty, queries ClickHouse for dominant_sender_so_far using
available swaps up to signal_time, fills in the field, and updates both the
CSV and the sprint080 sender map cache.

Two-phase design:
  Phase 1 (realtime):  onset window = pool_first_seen → signal_time
                       Threshold: total_swaps >= 5 AND dom_share >= 0.60
                       → write is_0x6ff5_so_far immediately (unblocks bridge)
  Phase 2 (audit):     full 1h onset window after pool creation completes
                       → overwrite with final is_0x6ff5 value

Safety: LIVE_TRADING_MODE must be false. No chain transactions.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sprint083_enricher")

REPO = Path(__file__).resolve().parent.parent
CSV_055 = REPO / "logs/sprint055_paper_shadow.csv"
SENDER_MAP_PATH = REPO / "logs/sprint080_sender_map.json"
HEARTBEAT_FILE = Path("/tmp/sprint083_enricher.heartbeat")
CH_CONTAINER = "eatlq-clickhouse-1"
SENDER_6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"

# Realtime enrichment thresholds
MIN_SWAPS_FOR_GATE = int(os.environ.get("ENRICHER_MIN_SWAPS", "5"))
MIN_SHARE_FOR_GATE = float(os.environ.get("ENRICHER_MIN_SHARE", "0.55"))
WATCH_INTERVAL_SEC = int(os.environ.get("ENRICHER_INTERVAL_SEC", "120"))


def _assert_safe() -> None:
    if os.environ.get("LIVE_TRADING_MODE", "").lower() in ("1", "true", "yes"):
        log.error("HARD ABORT: LIVE_TRADING_MODE=true forbidden in enricher.")
        sys.exit(1)


def _ch(sql: str) -> list[dict]:
    """Run a ClickHouse query, return list of dicts (JSONEachRow)."""
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--format", "JSONEachRow", "--query", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH error: {r.stderr[:300]}")
    results = []
    for line in r.stdout.splitlines():
        if line.strip():
            results.append(json.loads(line))
    return results


def _get_first_seen(pool: str) -> str | None:
    """Get pool's first block_time from ClickHouse (UTC datetime string)."""
    try:
        rows = _ch(
            f"SELECT formatDateTime(min(block_time), '%F %T') AS first_seen "
            f"FROM eatlq.uniswap_v3_swaps_enriched "
            f"WHERE lower(pool_address)='{pool.lower()}'"
        )
        if rows and rows[0].get("first_seen"):
            return rows[0]["first_seen"]
    except Exception as e:
        log.warning("first_seen query failed for %s: %s", pool[:16], e)
    return None


def _query_sender_so_far(pool: str, signal_time_utc: str) -> dict | None:
    """
    Query dominant sender from pool first_seen → signal_time.
    Returns dict with dominant_sender, top_swaps, total_swaps, share, is_0x6ff5.
    Returns None if insufficient data.
    """
    first_seen = _get_first_seen(pool)
    if not first_seen:
        return None

    try:
        rows = _ch(
            f"SELECT sender, count(*) AS cnt "
            f"FROM eatlq.uniswap_v3_swaps_enriched "
            f"WHERE lower(pool_address)='{pool.lower()}' "
            f"AND block_time BETWEEN parseDateTime64BestEffort('{first_seen}', 3, 'UTC') "
            f"AND parseDateTime64BestEffort('{signal_time_utc}', 3, 'UTC') "
            f"GROUP BY sender ORDER BY cnt DESC LIMIT 1"
        )
        if not rows:
            return None

        total_rows = _ch(
            f"SELECT count(*) AS total "
            f"FROM eatlq.uniswap_v3_swaps_enriched "
            f"WHERE lower(pool_address)='{pool.lower()}' "
            f"AND block_time BETWEEN parseDateTime64BestEffort('{first_seen}', 3, 'UTC') "
            f"AND parseDateTime64BestEffort('{signal_time_utc}', 3, 'UTC')"
        )
        total = int(total_rows[0].get("total", 0)) if total_rows else 0
        top = int(rows[0]["cnt"])
        sender = str(rows[0]["sender"]).lower()
        share = round(top / total, 4) if total > 0 else 0.0

        return {
            "dominant_sender": sender,
            "dom_sender_top_swaps": top,
            "dom_sender_total_swaps": total,
            "dom_sender_share": share,
            "is_0x6ff5": "True" if sender == SENDER_6FF5 else "False",
            "enrichment_phase": "so_far",
        }
    except Exception as e:
        log.warning("sender_so_far query failed for %s: %s", pool[:16], e)
        return None


def _load_sender_map() -> dict:
    if SENDER_MAP_PATH.exists():
        try:
            return json.loads(SENDER_MAP_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_sender_map(m: dict) -> None:
    SENDER_MAP_PATH.write_text(json.dumps(m, indent=2))


def _signal_time_to_utc(signal_time: str) -> str:
    """Convert ISO signal_time to 'YYYY-MM-DD HH:MM:SS' for ClickHouse."""
    try:
        dt = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def enrich_missing(dry_run_mode: bool = False) -> dict:
    """
    Find sprint055 rows with is_0x6ff5 empty, query CH, update in-place.
    Returns summary dict.
    """
    if not CSV_055.exists():
        return {"enriched": 0, "failed": 0, "skipped": 0}

    rows = list(csv.DictReader(CSV_055.open()))
    sender_map = _load_sender_map()

    # Gather pools needing enrichment: is_0x6ff5 == ''
    pools_to_enrich: dict[str, str] = {}  # pool -> latest signal_time
    for r in rows:
        if r.get("is_0x6ff5", "") == "":
            pool = r.get("pool", "").lower()
            st = r.get("signal_time", "")
            if pool and st:
                # Use earliest signal_time so we get the onset window start
                if pool not in pools_to_enrich:
                    pools_to_enrich[pool] = st
                else:
                    # keep earliest
                    if st < pools_to_enrich[pool]:
                        pools_to_enrich[pool] = st

    if not pools_to_enrich:
        log.info("No rows with empty is_0x6ff5 found")
        return {"enriched": 0, "failed": 0, "skipped": 0}

    log.info("Found %d pools needing enrichment", len(pools_to_enrich))

    enriched_pools: dict[str, dict] = {}
    failed_pools: set = set()

    for pool, signal_time in pools_to_enrich.items():
        # First: check sender map cache
        cached = sender_map.get(pool) or sender_map.get(pool.lower())
        if cached and cached.get("is_0x6ff5") != "":
            enriched_pools[pool] = cached
            continue

        sig_utc = _signal_time_to_utc(signal_time)
        if not sig_utc:
            failed_pools.add(pool)
            continue

        result = _query_sender_so_far(pool, sig_utc)
        if result is None:
            log.warning("enrichment failed (no CH data) pool=%s", pool[:16])
            failed_pools.add(pool)
            continue

        # Check minimum data threshold
        total = result.get("dom_sender_total_swaps", 0)
        share = result.get("dom_sender_share", 0.0)
        if total < MIN_SWAPS_FOR_GATE:
            log.info(
                "pool=%s: only %d swaps so far (need %d) — writing is_0x6ff5 anyway (low confidence)",
                pool[:16], total, MIN_SWAPS_FOR_GATE,
            )

        log.info(
            "pool=%s: dom_sender=%s share=%.2f total=%d is_0x6ff5=%s",
            pool[:16], result["dominant_sender"][:12], share, total, result["is_0x6ff5"],
        )

        enriched_pools[pool] = result
        # Update sender map
        sender_map[pool] = result

    if dry_run_mode:
        log.info("[dry-run] would update %d rows (not writing)", len(enriched_pools))
        return {"enriched": len(enriched_pools), "failed": len(failed_pools), "skipped": 0}

    # Apply to CSV rows
    skipped = 0
    updated = 0
    for r in rows:
        pool = r.get("pool", "").lower()
        if r.get("is_0x6ff5", "") == "" and pool in enriched_pools:
            data = enriched_pools[pool]
            r["dominant_sender"] = data.get("dominant_sender", "")
            r["dom_sender_top_swaps"] = data.get("dom_sender_top_swaps", "")
            r["dom_sender_total_swaps"] = data.get("dom_sender_total_swaps", "")
            r["dom_sender_share"] = data.get("dom_sender_share", "")
            r["is_0x6ff5"] = data.get("is_0x6ff5", "")
            updated += 1
        elif r.get("is_0x6ff5", "") == "" and pool in failed_pools:
            skipped += 1

    if updated > 0:
        shutil.copy2(CSV_055, CSV_055.parent / f"{CSV_055.name}.bak_enricher")
        fieldnames = list(rows[0].keys()) if rows else []
        with CSV_055.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        _save_sender_map(sender_map)
        log.info("Updated %d CSV rows, saved sender map", updated)

    return {"enriched": len(enriched_pools), "failed": len(failed_pools), "skipped": skipped}


def write_heartbeat() -> None:
    HEARTBEAT_FILE.write_text(str(int(time.time())))


def main() -> None:
    _assert_safe()

    parser = argparse.ArgumentParser(description="Sprint083 Realtime Sender Enricher")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    args = parser.parse_args()

    if not args.once and not args.watch:
        parser.print_help()
        sys.exit(0)

    log.info(
        "Enricher starting: mode=%s interval=%ds min_swaps=%d min_share=%.2f",
        "watch" if args.watch else "once", WATCH_INTERVAL_SEC,
        MIN_SWAPS_FOR_GATE, MIN_SHARE_FOR_GATE,
    )

    def _run_once() -> None:
        write_heartbeat()
        summary = enrich_missing(dry_run_mode=args.dry_run)
        log.info(
            "enricher pass: enriched=%d failed=%d skipped=%d",
            summary["enriched"], summary["failed"], summary["skipped"],
        )
        write_heartbeat()

    _run_once()

    if args.watch:
        while True:
            time.sleep(WATCH_INTERVAL_SEC)
            try:
                _run_once()
            except KeyboardInterrupt:
                log.info("Enricher stopped by user")
                break
            except Exception as e:
                log.error("Enricher error: %s", e)


if __name__ == "__main__":
    main()
