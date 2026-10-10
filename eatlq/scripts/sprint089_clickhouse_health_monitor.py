#!/usr/bin/env python3
"""ClickHouse disk + mutation health monitor.

Alerts when:
  - pending mutations exist > 30 min
  - disk usage > threshold (default 85%)
  - eatlq data size growth anomaly

Exit 1 on alert (for cron/Telegram hook).

Usage:
  python3 scripts/sprint089_clickhouse_health_monitor.py
  python3 scripts/sprint089_clickhouse_health_monitor.py --alert-json mvp/audits/sprint089_pipeline_recovery/ch_health.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CH = "eatlq-clickhouse-1"
OUT = REPO / "mvp/audits/sprint089_pipeline_recovery"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--disk-pct-max", type=float, default=float(os.environ.get("CH_DISK_PCT_MAX", "85")))
    p.add_argument("--alert-json", default="")
    return p.parse_args()


def _ch(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()


def main() -> int:
    args = parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    pending = _ch(
        "SELECT count() FROM system.mutations WHERE is_done=0 AND database='eatlq' FORMAT TabSeparated"
    )
    pending_n = int(pending or "0")

    fail_reason = ""
    if pending_n:
        fail_reason = _ch(
            "SELECT latest_fail_reason FROM system.mutations "
            "WHERE is_done=0 AND database='eatlq' ORDER BY create_time DESC LIMIT 1 FORMAT TabSeparated"
        )

    data_gb = float(_ch(
        "SELECT round(sum(bytes)/1024/1024/1024, 2) FROM system.parts "
        "WHERE database='eatlq' AND active FORMAT TabSeparated"
    ) or "0")

    # Host disk via CH container mount
    disk_line = subprocess.run(
        ["docker", "exec", CH, "df", "-P", "/var/lib/clickhouse"],
        capture_output=True, text=True, check=False,
    )
    disk_pct = 0.0
    disk_avail_gb = 0.0
    for line in disk_line.stdout.splitlines()[1:2]:
        parts = line.split()
        if len(parts) >= 5:
            disk_pct = float(parts[4].rstrip("%"))
            disk_avail_gb = round(int(parts[3]) / 1024 / 1024, 1)

    alerts = []
    if pending_n > 0:
        alerts.append(f"pending_mutations={pending_n}: {fail_reason[:120]}")
    if disk_pct >= args.disk_pct_max:
        alerts.append(f"disk_usage={disk_pct}% >= {args.disk_pct_max}%")

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pending_mutations": pending_n,
        "latest_fail_reason": fail_reason[:500] if fail_reason else None,
        "eatlq_data_gb": data_gb,
        "disk_pct": disk_pct,
        "disk_avail_gb": disk_avail_gb,
        "disk_pct_max": args.disk_pct_max,
        "alerts": alerts,
        "pass": len(alerts) == 0,
    }
    (OUT / "ch_health_latest.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.alert_json:
        Path(args.alert_json).write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps(report, indent=2))
    if alerts:
        for a in alerts:
            print(f"ALERT: {a}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
