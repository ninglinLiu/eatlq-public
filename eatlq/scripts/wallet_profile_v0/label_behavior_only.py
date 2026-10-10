#!/usr/bin/env python3
"""
Behavior-only dashcam labels — no forward economics (B1).

Joins wallet behavior tags to dashcam rows without computing forward returns.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels"
DASHCAM = REPO / "logs" / "dashcam.csv"
BACKFILLED = AUDIT / "dashcam_with_sender_ch.csv"
OUT = AUDIT / "dashcam_behavior_labeled.csv"
LABELS = AUDIT / "all_labels.csv"

log = logging.getLogger("label_behavior_only")


def load_wallet_labels() -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    if not LABELS.exists():
        return out
    with LABELS.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addr = row["sender_address"].lower()
            out[addr] = {
                "is_smart_money": row.get("is_smart_money", "").lower() in ("true", "1"),
                "is_sniper": row.get("is_sniper", "").lower() in ("true", "1"),
                "is_diamond_hand": row.get("is_diamond_hand", "").lower() in ("true", "1"),
                "is_paper_hand": row.get("is_paper_hand", "").lower() in ("true", "1"),
                "is_whale": row.get("is_whale", "").lower() in ("true", "1"),
            }
    return out


def resolve_sender(row: dict) -> str:
    for key in ("sender_ch", "sender_address", "sender_inferred", "tx_from", "dominant_sender"):
        v = (row.get(key) or "").strip().lower()
        if v.startswith("0x") and len(v) == 42:
            return v
    return ""


def run(
    *,
    dashcam_path: Path | None = None,
    out_path: Path = OUT,
    limit: int = 0,
) -> dict:
    labels = load_wallet_labels()
    src = dashcam_path or (BACKFILLED if BACKFILLED.exists() else DASHCAM)
    if not src.exists():
        raise FileNotFoundError(src)

    with src.open(newline="", encoding="utf-8") as f:
        rows_in = list(csv.DictReader(f))

    rows_in.sort(key=lambda r: float(r.get("timestamp") or 0), reverse=True)
    if limit:
        rows_in = rows_in[:limit]

    rows_out: list[dict] = []
    tagged = 0
    for row in rows_in:
        sender = resolve_sender(row)
        tags = labels.get(sender, {})
        is_sm = tags.get("is_smart_money", False)
        is_sn = tags.get("is_sniper", False)
        is_dh = tags.get("is_diamond_hand", False)
        is_ph = tags.get("is_paper_hand", False)
        is_wh = tags.get("is_whale", False)
        if any((is_sm, is_sn, is_dh, is_ph, is_wh)):
            tagged += 1
        rows_out.append({
            "timestamp": row.get("timestamp") or row.get("signal_time") or "",
            "pool": row.get("pool_address") or row.get("token_address") or row.get("pool") or "",
            "sender": sender,
            "is_smart_money": is_sm,
            "is_sniper": is_sn,
            "is_whale": is_wh,
            "is_diamond_hand": is_dh,
            "is_paper_hand": is_ph,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp", "pool", "sender",
        "is_smart_money", "is_sniper", "is_whale", "is_diamond_hand", "is_paper_hand",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(src.relative_to(REPO)),
        "rows_in": len(rows_in),
        "rows_out": len(rows_out),
        "tagged_rows": tagged,
        "label_leakage_forward_return": False,
        "output": str(out_path.relative_to(REPO)),
        "RESEARCH_ONLY": True,
    }
    (AUDIT / "label_behavior_only_report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Behavior-only dashcam label join (no economics)")
    p.add_argument("--dashcam", type=str, default=None)
    p.add_argument("--out", type=str, default=str(OUT))
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    report = run(
        dashcam_path=Path(args.dashcam) if args.dashcam else None,
        out_path=Path(args.out),
        limit=args.limit,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
