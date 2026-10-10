#!/usr/bin/env python3
"""Read-only health check for Sprint084 paper-shadow expansion watch."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--expansion-dir",
        default="mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    d = REPO / args.expansion_dir
    targets = list(csv.DictReader((d / "expansion_top15_pools.csv").open()))
    target_pools = {r["pool"].lower() for r in targets}
    log = d / "sprint084_expansion_paper_shadow.csv"
    seen: set[str] = set()
    rows = 0
    if log.is_file():
        for r in csv.DictReader(log.open()):
            rows += 1
            seen.add((r.get("pool") or "").lower())
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_pools": len(target_pools),
        "pools_with_signals": len(seen & target_pools),
        "log_rows": rows,
        "log_exists": log.is_file(),
        "PAPER_FORWARD_ALLOWED": "false",
        "LIVE_ALLOWED": False,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
