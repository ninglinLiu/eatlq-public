#!/usr/bin/env python3
"""Wait for 14d sender_token_ticks rebuild, then rerun labels + overlap + pivot."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOG = logging.getLogger("post_sender_alignment")
OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra" / "post_alignment_rerun.json"
PID_FILE = Path("/tmp/ticks_backfill_14d.pid")


def _pgrep(pattern: str) -> list[str]:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [x for x in r.stdout.split() if x.strip()]


def _run(cmd: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess:
    LOG.info("run: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)


def wait_for_14d_rebuild(poll_sec: int = 60) -> None:
    pattern = "backfill_sender_token_daily.py --days 14 --no-skip-existing"
    LOG.info("waiting for: %s", pattern)
    while True:
        pids = _pgrep(pattern)
        if not pids:
            LOG.info("14d rebuild finished (no matching process)")
            return
        LOG.info("14d rebuild still running pids=%s; sleep %ds", pids, poll_sec)
        time.sleep(poll_sec)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.time()
    wait_for_14d_rebuild()

    results: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": {}}

    steps = [
        ("compute_labels", [sys.executable, "scripts/wallet_profile_v0/compute_labels.py", "--only", "smart_money", "--lookback-days", "90"]),
        ("overlap", [sys.executable, "scripts/wallet_profile_v0/check_dashcam_sender_overlap.py"]),
        ("pivot_multi", [sys.executable, "scripts/wallet_profile_v0/pivot_pool_fusion.py", "--backtest-days", "14"]),
        ("pivot_single", [sys.executable, "scripts/wallet_profile_v0/pivot_pool_fusion.py", "--backtest-days", "14", "--single-tick"]),
    ]

    for name, cmd in steps:
        r = _run(cmd)
        results["steps"][name] = {
            "returncode": r.returncode,
            "stdout_tail": (r.stdout or "")[-2000:],
            "stderr_tail": (r.stderr or "")[-1000:],
        }
        if r.returncode != 0:
            LOG.error("%s failed rc=%d stderr=%s", name, r.returncode, r.stderr[:500])
            break
        LOG.info("%s ok", name)

    results["elapsed_sec"] = round(time.time() - t0, 1)
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    LOG.info("wrote %s", OUT)
    return 0 if all(s.get("returncode") == 0 for s in results["steps"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
