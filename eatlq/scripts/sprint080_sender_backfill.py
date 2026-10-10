#!/usr/bin/env python3
"""
Sprint080 — Sender Dimension Backfill
======================================
For every (pool) in sprint055_paper_shadow.csv and sprint078_shadow.csv:
  1. Find the dominant sender in the ONSET WINDOW (pool_first_seen → +1h)
     via uniswap_v3_swaps_enriched
  2. Flag whether that sender is 0x6ff5 (Uniswap Universal Router on Base)
     — historically associated with alpha_009 onset alpha

Adds columns:
  dominant_sender        — hex address with most swaps in onset window
  dom_sender_top_swaps   — swap count of dominant sender in onset window
  dom_sender_total_swaps — total onset window swaps across all senders
  dom_sender_share       — dominant_sender_share = top / total
  is_0x6ff5              — True/False

Output:
  logs/sprint055_paper_shadow.csv   — updated in-place
  logs/sprint078_shadow.csv         — updated in-place
  logs/sprint080_sender_map.json    — reusable sender cache
"""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

REPO         = Path(__file__).resolve().parent.parent
CSV_055      = REPO / "logs" / "sprint055_paper_shadow.csv"
CSV_078      = REPO / "logs" / "sprint078_shadow.csv"
SENDER_MAP   = REPO / "logs" / "sprint080_sender_map.json"
CH_CONTAINER = "eatlq-clickhouse-1"

SENDER_6FF5  = "0x6ff5693b99212da76ad316178a184ab56d299b43"

NEW_COLUMNS = [
    "dominant_sender",
    "dom_sender_top_swaps",
    "dom_sender_total_swaps",
    "dom_sender_share",
    "is_0x6ff5",
]


# ── ClickHouse helper ─────────────────────────────────────────────────────────

def ch(sql: str, fmt: str = "JSONEachRow") -> tuple[str, str]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--query", sql, f"--format={fmt}"],
        capture_output=True, text=True, timeout=120,
    )
    return r.stdout.strip(), r.stderr.strip()


# ── Build or load sender map ──────────────────────────────────────────────────

def build_sender_map(all_pools: list[str]) -> dict[str, dict]:
    """Query CH for dominant sender in first-1h onset window per pool."""
    print(f"[s080] Building sender map for {len(all_pools)} pools …")

    # Step 1: get pool first_seen timestamps in batch
    pool_list_sql = ",".join(f"'{p.lower()}'" for p in all_pools)
    out1, err1 = ch(
        f"SELECT lower(pool_address) as pool, min(toDateTime(block_time)) as first_seen "
        f"FROM eatlq.uniswap_v3_swaps_enriched "
        f"WHERE lower(pool_address) IN ({pool_list_sql}) "
        f"GROUP BY pool"
    )
    if err1:
        print(f"[s080] WARN step1 err: {err1[:200]}")

    pool_first_seen: dict[str, str] = {}
    for line in out1.splitlines():
        if line:
            d = json.loads(line)
            pool_first_seen[d["pool"]] = d["first_seen"]
    print(f"[s080] first_seen resolved for {len(pool_first_seen)}/{len(all_pools)} pools")

    # Step 2: dominant sender in onset window (UNION ALL per pool)
    # Process in batches to avoid ClickHouse query plan limit (10000 optimizations)
    BATCH = 10
    pool_items = list(pool_first_seen.items())
    all_out_lines: list[str] = []
    err2 = ""
    for i in range(0, len(pool_items), BATCH):
        batch = pool_items[i:i + BATCH]
        parts = []
        for pool, fs in batch:
            parts.append(
                f"SELECT '{pool}' as pool, sender, count(*) as cnt "
                f"FROM eatlq.uniswap_v3_swaps_enriched "
                f"WHERE lower(pool_address)='{pool}' "
                f"AND toDateTime(block_time) BETWEEN toDateTime('{fs}') "
                f"AND toDateTime('{fs}') + INTERVAL 1 HOUR "
                f"GROUP BY sender"
            )
        inner = " UNION ALL ".join(parts)
        bout, berr = ch(
            f"SELECT pool, argMax(sender,cnt) AS dom, max(cnt) AS top, sum(cnt) AS total "
            f"FROM ({inner}) GROUP BY pool ORDER BY pool"
        )
        all_out_lines.extend(l for l in bout.splitlines() if l)
        if berr:
            err2 += berr + "\n"
    out2 = "\n".join(all_out_lines)
    if err2:
        print(f"[s080] WARN step2 err: {err2[:200]}")

    sender_map: dict[str, dict] = {}
    for line in out2.splitlines():
        if line:
            d = json.loads(line)
            dom = d["dom"]
            top = int(d["top"])
            total = int(d["total"])
            sender_map[d["pool"]] = {
                "dominant_sender":        dom,
                "dom_sender_top_swaps":   top,
                "dom_sender_total_swaps": total,
                "dom_sender_share":       round(top / max(total, 1), 4),
                "is_0x6ff5":             dom.lower() == SENDER_6FF5.lower(),
            }

    n6ff5 = sum(1 for v in sender_map.values() if v["is_0x6ff5"])
    print(f"[s080] Sender data for {len(sender_map)} pools")
    print(f"[s080] Dominant=0x6ff5: {n6ff5}/{len(sender_map)} ({100*n6ff5/max(len(sender_map),1):.0f}%)")

    # Pools missing from CH (no swap history yet)
    missing = [p for p in all_pools if p.lower() not in sender_map]
    if missing:
        print(f"[s080] {len(missing)} pools with no CH swap history:")
        for p in missing:
            print(f"  {p}")

    SENDER_MAP.write_text(json.dumps(sender_map, indent=2))
    print(f"[s080] Saved → {SENDER_MAP}")
    return sender_map


def load_sender_map() -> dict[str, dict]:
    if SENDER_MAP.exists():
        return json.loads(SENDER_MAP.read_text())
    return {}


# ── CSV backfill ──────────────────────────────────────────────────────────────

def backfill_csv(csv_path: Path, sender_map: dict[str, dict]) -> None:
    if not csv_path.exists():
        print(f"[s080] SKIP {csv_path} (not found)")
        return

    rows = list(csv.DictReader(csv_path.open()))
    if not rows:
        print(f"[s080] SKIP {csv_path} (empty)")
        return

    existing_cols = list(rows[0].keys())

    # Add new cols if not already present
    for col in NEW_COLUMNS:
        if col not in existing_cols:
            existing_cols.append(col)

    # Fill in values
    updated = 0
    for r in rows:
        pool = r["pool"].lower()
        info = sender_map.get(pool, {})
        if info:
            r["dominant_sender"]        = info.get("dominant_sender", "")
            r["dom_sender_top_swaps"]   = info.get("dom_sender_top_swaps", "")
            r["dom_sender_total_swaps"] = info.get("dom_sender_total_swaps", "")
            r["dom_sender_share"]       = info.get("dom_sender_share", "")
            r["is_0x6ff5"]              = str(info.get("is_0x6ff5", ""))
            updated += 1
        else:
            r.setdefault("dominant_sender",        "")
            r.setdefault("dom_sender_top_swaps",   "")
            r.setdefault("dom_sender_total_swaps", "")
            r.setdefault("dom_sender_share",        "")
            r.setdefault("is_0x6ff5",              "")

    bak = csv_path.with_suffix(".csv.bak_pre_s080")
    import shutil
    shutil.copy(csv_path, bak)

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=existing_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"[s080] Updated {csv_path.name}: {updated}/{len(rows)} rows got sender data")


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_alpha_sender(csv_path: Path, label: str) -> None:
    """Print PF/WR breakdown for is_0x6ff5 vs not."""
    rows = list(csv.DictReader(csv_path.open()))
    ok_rows = [r for r in rows if r.get("entry_quote_ok") == "True"]
    if not ok_rows or "is_0x6ff5" not in ok_rows[0]:
        print(f"[s080] {label}: no sender data yet")
        return

    for flag, label_str in [("True", "0x6ff5_pools"), ("False", "non_6ff5_pools")]:
        subset = [r for r in ok_rows if r.get("is_0x6ff5") == flag]
        if not subset:
            continue

        wins_2h  = [r for r in subset if r.get("net_return_2h_ag") and float(r["net_return_2h_ag"]) > 0]
        loss_2h  = [r for r in subset if r.get("net_return_2h_ag") and float(r["net_return_2h_ag"]) <= 0]
        gross_w  = sum(float(r["net_return_2h_ag"]) for r in wins_2h)
        gross_l  = abs(sum(float(r["net_return_2h_ag"]) for r in loss_2h))
        pf       = round(gross_w / gross_l, 4) if gross_l > 0 else None
        wr       = round(len(wins_2h) / max(len(wins_2h) + len(loss_2h), 1), 4)
        n_with_exit = len(wins_2h) + len(loss_2h)
        pools   = len(set(r["pool"] for r in subset))

        print(f"  {label} | {label_str:18s}: rows={len(subset):4d} pools={pools:3d} "
              f"2h_n={n_with_exit:4d} PF={pf or '—':>8} WR={wr:.3f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-map", action="store_true",
                        help="Force rebuild sender map from ClickHouse")
    args = parser.parse_args()

    # Collect all pools from both CSVs
    all_pools: list[str] = []
    for csv_path in (CSV_055, CSV_078):
        if csv_path.exists():
            rows = list(csv.DictReader(csv_path.open()))
            all_pools += [r["pool"] for r in rows]
    all_pools = sorted(set(all_pools))

    # Build or load sender map
    if args.rebuild_map or not SENDER_MAP.exists():
        sender_map = build_sender_map(all_pools)
    else:
        sender_map = load_sender_map()
        # Refresh for any pools not in cache
        missing = [p for p in all_pools if p.lower() not in sender_map]
        if missing:
            print(f"[s080] {len(missing)} new pools not in cache — refreshing …")
            new_map = build_sender_map(missing)
            sender_map.update(new_map)
            SENDER_MAP.write_text(json.dumps(sender_map, indent=2))

    # Backfill both CSVs
    backfill_csv(CSV_055, sender_map)
    backfill_csv(CSV_078, sender_map)

    # Analysis: PF/WR by sender type
    print()
    print("=" * 65)
    print("ALPHA SENDER (0x6ff5) ANALYSIS — 2h horizon")
    print("=" * 65)
    for csv_path, label in [(CSV_055, "sprint055"), (CSV_078, "sprint078")]:
        analyze_alpha_sender(csv_path, label)


if __name__ == "__main__":
    main()
