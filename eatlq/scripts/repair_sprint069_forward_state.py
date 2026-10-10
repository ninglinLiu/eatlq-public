#!/usr/bin/env python3
"""Repair corrupted sprint069 forward_dryrun_state.json by re-initializing from artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/opt/EATLQ")
STATE = REPO / "mvp/audits/sprint069_forward_dryrun/forward_dryrun_state.json"
TRADES = REPO / "mvp/audits/sprint069_forward_dryrun/forward_dryrun_trades.csv"
SCRIPT = REPO / "scripts/sprint069_forward_dryrun.py"


def main() -> int:
    if STATE.exists():
        backup = STATE.with_suffix(
            f".corrupt_backup_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        shutil.copy2(STATE, backup)
        print(f"Backed up corrupt state → {backup}")
        STATE.unlink()

    if TRADES.exists():
        n = sum(1 for _ in open(TRADES)) - 1
        print(f"Preserved trades.csv rows={n}")

    print("Re-initializing forward dry-run from paper shadow CSV + freeze...")
    rc = subprocess.call(
        [sys.executable, str(SCRIPT), "--once"],
        cwd=str(REPO),
    )
    if rc != 0:
        print("ERROR: sprint069_forward_dryrun.py failed after state reset", file=sys.stderr)
        return rc

    if STATE.exists():
        json.load(open(STATE))
        print(f"OK: state JSON valid at {STATE}")
    else:
        print("ERROR: state file not created", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
