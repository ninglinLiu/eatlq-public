#!/usr/bin/env python3
"""Evaluate A3 hardware trigger conditions."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import ch_query  # noqa: E402
from measure_ingest_lag import measure  # noqa: E402

OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra" / "hardware_trigger_eval.json"


def pit_timing_probe() -> dict:
    t0 = time.time()
    try:
        from compute_labels import compute_smart_money_as_of

        compute_smart_money_as_of(lookback_days=90, limit=500)
        elapsed = time.time() - t0
        oom = False
    except Exception as e:
        elapsed = time.time() - t0
        oom = "MEMORY" in str(e).upper()
    return {"smart_money_90d_sec": round(elapsed, 1), "oom": oom}


def main() -> None:
    lag = measure()
    pit = pit_timing_probe()
    mv_rows = ch_query(
        "SELECT count() AS n FROM eatlq.sender_token_ticks_light FORMAT JSONEachRow",
        timeout=30,
    )
    n_mv = int(mv_rows[0]["n"]) if mv_rows else 0

    triggered = (
        pit.get("oom")
        or pit.get("smart_money_90d_sec", 0) > 3600
        or lag.get("lag_hours", 0) > 24
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mv_rows": n_mv,
        "ingest_lag": lag,
        "pit_probe": pit,
        "triggered": triggered,
        "recommended_action": (
            "migrate_contabo_vps_30_nvme" if triggered else "stay_current_host_hourly_backfill"
        ),
        "runbook": "mvp/audits/wallet_profile_v0/infra/HARDWARE_TRIGGER.md",
    }
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
