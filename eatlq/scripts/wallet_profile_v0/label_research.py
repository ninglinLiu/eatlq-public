#!/usr/bin/env python3
"""
Research-only dashcam label join — does NOT call production label_dashcam_executable.py.

Uses sender_ch (CH backfill) or sender_address (live dashcam) for wallet tag join.
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
OUT = AUDIT / "dashcam_labeled_research.csv"
LABELS = AUDIT / "all_labels.csv"

log = logging.getLogger("label_research")


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

    rows_in: list[dict] = []
    with src.open(newline="", encoding="utf-8") as f:
        rows_in = list(csv.DictReader(f))

    rows_in.sort(key=lambda r: float(r.get("timestamp") or 0), reverse=True)
    if limit:
        rows_in = rows_in[:limit]

    rows_out: list[dict] = []
    base_fields = list(rows_in[0].keys()) if rows_in else []
    extra = [
        "sender_ch", "sender_inferred", "is_smart_money", "is_sniper", "is_diamond_hand",
        "is_paper_hand", "is_whale", "wallet_tag_count", "research_only",
    ]
    tagged_rows = 0
    for row in rows_in:
        sender = resolve_sender(row)
        wl = labels.get(sender, {})
        tag_count = sum(1 for v in wl.values() if v)
        if tag_count:
            tagged_rows += 1
        out_row = dict(row)
        out_row.update({
            "sender_ch": sender or row.get("sender_ch", ""),
            "sender_inferred": sender,
            "is_smart_money": wl.get("is_smart_money", False),
            "is_sniper": wl.get("is_sniper", False),
            "is_diamond_hand": wl.get("is_diamond_hand", False),
            "is_paper_hand": wl.get("is_paper_hand", False),
            "is_whale": wl.get("is_whale", False),
            "wallet_tag_count": tag_count,
            "research_only": True,
        })
        rows_out.append(out_row)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = base_fields + [e for e in extra if e not in base_fields]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_out)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(src.relative_to(REPO)),
        "input_rows": len(rows_out),
        "wallet_labels_loaded": len(labels),
        "rows_with_wallet_tags": tagged_rows,
        "tag_hit_rate": round(tagged_rows / len(rows_out), 4) if rows_out else 0.0,
        "output": str(out_path.relative_to(REPO)),
        "RESEARCH_ONLY": True,
        "production_labeler_called": False,
    }
    (AUDIT / "dashcam_labeled_research_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--dashcam", default="", help="Override input (default: backfilled or logs/dashcam.csv)")
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    meta = run(
        dashcam_path=Path(args.dashcam) if args.dashcam else None,
        out_path=Path(args.out),
        limit=args.limit,
    )
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
