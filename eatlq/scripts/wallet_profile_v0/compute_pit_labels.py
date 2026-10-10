#!/usr/bin/env python3
"""Point-in-time smart_money label snapshots (D5 — no look-ahead)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import write_csv  # noqa: E402
from compute_labels import compute_smart_money_as_of, compute_smart_money_strict, merge_all_labels  # noqa: E402

log = logging.getLogger("compute_pit_labels")

PIT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "pit"


def build_snapshots(days: int = 14, lookback_days: int = 14, *, strict: bool = False) -> dict:
    """Build daily PIT snapshots with short lookback to fit 7GB CH host."""
    PIT_DIR.mkdir(parents=True, exist_ok=True)
    meta: list[dict] = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        as_of = (today - timedelta(days=i)).isoformat()
        log.info("PIT snapshot as_of=%s lookback=%dd strict=%s", as_of, lookback_days, strict)
        try:
            if strict:
                rows = compute_smart_money_strict(as_of, lookback_days=lookback_days, limit=3000)
                for r in rows:
                    r["is_smart_money"] = True
            else:
                rows = compute_smart_money_as_of(as_of, lookback_days=lookback_days, limit=3000)
            all_labels = merge_all_labels({"smart_money": rows})
            suffix = "_strict" if strict else ""
            out = PIT_DIR / f"all_labels_pit_{as_of}{suffix}.csv"
            write_csv(
                out,
                all_labels,
                ["sender_address", "is_smart_money", "is_sniper", "is_diamond_hand", "is_paper_hand", "is_whale"],
            )
            meta.append({"as_of": as_of, "smart_money_n": len(rows), "path": str(out.relative_to(REPO)), "strict": strict})
            log.info("  wrote %s n=%d", out.name, len(rows))
        except Exception as e:
            log.error("  %s failed: %s", as_of, e)
            meta.append({"as_of": as_of, "error": str(e)[:200], "strict": strict})
    ok = [m for m in meta if "smart_money_n" in m]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "days_requested": days,
        "strict": strict,
        "snapshots_ok": len(ok),
        "snapshots": meta,
        "RESEARCH_ONLY": True,
    }
    manifest = PIT_DIR / ("pit_manifest_strict.json" if strict else "pit_manifest.json")
    manifest.write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Point-in-time wallet label snapshots")
    p.add_argument("--days", type=int, default=14, help="Days of snapshots (default 14 for OOM host)")
    p.add_argument("--lookback-days", type=int, default=14, help="Lookback per snapshot (default 14 for OOM host)")
    p.add_argument("--strict", action="store_true", help="Use compute_smart_money_strict (v2 labels)")
    args = p.parse_args()
    report = build_snapshots(days=args.days, lookback_days=args.lookback_days, strict=args.strict)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
