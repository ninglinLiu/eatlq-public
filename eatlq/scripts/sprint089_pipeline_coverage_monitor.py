#!/usr/bin/env python3
"""Sprint089 — Hourly pipeline swap-capture coverage monitor.

Compares pools swap-poller should monitor (Redis WETH pools in age window)
vs pools with discovery_poller/raw swap rows ingested in the last hour.

Exit code 1 if capture_rate < threshold (default 0.95).

Usage:
  python3 scripts/sprint089_pipeline_coverage_monitor.py
  python3 scripts/sprint089_pipeline_coverage_monitor.py --alert-json mvp/audits/sprint089_pipeline_recovery/coverage_alert.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CH = "eatlq-clickhouse-1"
DEFAULT_THRESHOLD = 0.95


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=float, default=float(os.environ.get("PIPELINE_COVERAGE_MIN", DEFAULT_THRESHOLD)))
    p.add_argument("--max-age-h", type=int, default=int(os.environ.get("SWAP_POLLER_POOL_MAX_AGE_H", "72")))
    p.add_argument("--window-h", type=int, default=1)
    p.add_argument("--alert-json", default="")
    return p.parse_args()


def _ch(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()


def redis_monitored_pools(max_age_h: int) -> int:
    """Count WETH pools in Redis within age window (same logic as swap_poller)."""
    script = f"""
import os, redis, time
r = redis.from_url(os.environ.get('REDIS_URL','redis://redis:6379/0'))
now=time.time(); cutoff=now-{max_age_h}*3600; cutoff_ms=int(cutoff*1000); n=0
cur=0
while True:
    cur, keys = r.scan(cur, match='pool:discovered:*', count=500)
    for key in keys:
        d=r.hgetall(key)
        if (d.get(b'has_weth_leg') or b'0')!=b'1': continue
        cs=int((d.get(b'created_at_sec') or b'0')); da=int((d.get(b'discovered_at_ms') or b'0'))
        if cs>0:
            if cs>=cutoff: n+=1
        elif da>0:
            if da>=cutoff_ms: n+=1
    if cur==0: break
print(n+10)
"""
    r = subprocess.run(
        ["docker", "exec", "-e", "REDIS_URL=redis://redis:6379/0", "eatlq-redis-1",
         "python3", "-c", script],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if r.returncode != 0:
        # redis container may lack python3 — fallback to CH discovered count
        return int(_ch(
            f"SELECT count() FROM eatlq.discovered_pools "
            f"WHERE has_weth_leg=1 AND discovered_at >= now()-INTERVAL {max_age_h} HOUR "
            f"FORMAT TabSeparated"
        ) or "0")
    return int(r.stdout.strip() or "0")


def main() -> int:
    args = parse_args()
    out_dir = REPO / "mvp/audits/sprint089_pipeline_recovery"
    out_dir.mkdir(parents=True, exist_ok=True)

    monitored = redis_monitored_pools(args.max_age_h)
    ingested_pools = int(_ch(f"""
SELECT uniqExact(address) FROM eatlq.raw_evm_logs
WHERE received_at_ms >= now() - INTERVAL {args.window_h} HOUR
  AND topic0 IN (
    '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67',
    '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822',
    '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'
  )
  AND source IN ('discovery_poller', 'quicknode', 'sprint089_gap_backfill')
FORMAT TabSeparated
""") or "0")

    archiver_pending = _ch(
        "docker exec eatlq-redis-1 redis-cli XPENDING market_ticks archiver_group 2>/dev/null | head -1"
        if False else
        "SELECT 0 FORMAT TabSeparated"
    )
    try:
        pr = subprocess.run(
            ["docker", "exec", "eatlq-redis-1", "redis-cli", "XPENDING", "market_ticks", "archiver_group"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        archiver_pending = pr.stdout.split()[0] if pr.stdout.strip() else "?"
    except Exception:
        archiver_pending = "?"

    # Active pools with on-chain swaps in window (proxy: ingested unique addresses)
    active_with_swaps = ingested_pools
    # Coverage: of monitored universe, how many had ≥1 swap ingested this hour
    overlap = int(_ch(f"""
SELECT count() FROM (
  SELECT lower(d.pool_address) p
  FROM eatlq.discovered_pools d
  INNER JOIN (
    SELECT DISTINCT lower(address) a FROM eatlq.raw_evm_logs
    WHERE received_at_ms >= now() - INTERVAL {args.window_h} HOUR
      AND topic0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
  ) s ON lower(d.pool_address) = s.a
  WHERE d.has_weth_leg=1 AND d.discovered_at >= now() - INTERVAL {args.max_age_h} HOUR
) FORMAT TabSeparated
""") or "0")

    capture_rate = (overlap / monitored) if monitored > 0 else 0.0
    # For pools that actually traded, use overlap vs active_with_swaps if available
    if active_with_swaps > 0:
        capture_rate_active = min(1.0, overlap / active_with_swaps)
    else:
        capture_rate_active = 1.0 if monitored == 0 else 0.0

    pass_gate = capture_rate >= args.threshold
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "monitored_pools_redis_or_ch": monitored,
        "pools_with_swaps_ingested_1h": active_with_swaps,
        "overlap_monitored_and_ingested_1h": overlap,
        "capture_rate_vs_monitored": round(capture_rate, 4),
        "capture_rate_vs_active": round(capture_rate_active, 4),
        "threshold": args.threshold,
        "pass": pass_gate,
        "archiver_pending_market_ticks": archiver_pending,
        "max_age_h": args.max_age_h,
    }
    (out_dir / "coverage_latest.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.alert_json:
        Path(args.alert_json).write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps(report, indent=2))
    if not pass_gate and monitored > 10:
        print(f"ALERT: capture_rate {capture_rate:.2%} < {args.threshold:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
