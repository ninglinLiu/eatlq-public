#!/usr/bin/env python3
"""A2 acceptance: dashcam sender_address overlap with wallet label sets."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHCAM = REPO / "logs" / "dashcam.csv"
LABELS = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "all_labels.csv"
OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra" / "dashcam_sender_overlap.json"

QUOTE_ROUTER = {
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x6ff5693b99212da76ad316178a184ab56d299b43",
}


def load_wallet_tags() -> set[str]:
    tags: set[str] = set()
    if not LABELS.exists():
        return tags
    with LABELS.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addr = row["sender_address"].lower()
            if any(row.get(k, "").lower() in ("true", "1") for k in (
                "is_smart_money", "is_whale", "is_sniper", "is_diamond_hand", "is_paper_hand"
            )):
                tags.add(addr)
    return tags


def main() -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    since_ts = since.timestamp()

    if not DASHCAM.exists():
        print(json.dumps({"error": "dashcam.csv missing"}, indent=2))
        sys.exit(1)

    senders: set[str] = set()
    quote_hits = 0
    rows_24h = 0
    with DASHCAM.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts = float(row.get("timestamp") or 0)
            except ValueError:
                continue
            if ts > 1e12:
                ts /= 1000.0
            if ts < since_ts:
                continue
            rows_24h += 1
            s = (row.get("sender_address") or row.get("tx_from") or "").lower().strip()
            if not s.startswith("0x") or len(s) != 42:
                continue
            if s in QUOTE_ROUTER:
                quote_hits += 1
                continue
            senders.add(s)

    tags = load_wallet_tags()
    overlap = senders & tags
    overlap_pct = len(overlap) / len(senders) * 100 if senders else 0.0

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": 24,
        "dashcam_rows_24h": rows_24h,
        "distinct_senders_24h": len(senders),
        "quote_router_hits": quote_hits,
        "wallet_tagged_total": len(tags),
        "overlap_senders": len(overlap),
        "overlap_pct": round(overlap_pct, 4),
        "target_overlap_pct": 0.5,
        "acceptance_pass": overlap_pct >= 0.5 and len(senders) >= 10,
        "sample_overlap": sorted(overlap)[:10],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
