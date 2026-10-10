#!/usr/bin/env python3
"""Run R3 horizon sweep (15/30/60/120m × 30d backtest)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "mvp/audits/wallet_profile_v0/sender_gated"


def main() -> None:
    results = []
    for h in (15, 30, 60, 120):
        out_json = OUT_DIR / f"baseline_{h}m.json"
        print(f"=== horizon {h}m ===", file=sys.stderr)
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/sprint091_sender_gated_paper.py"),
                "--backtest-days", "30",
                "--horizon-min", str(h),
                "--no-readout",
                "--output-json", str(out_json),
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            sys.exit(proc.returncode)
        summary = json.loads(out_json.read_text())
        results.append({
            "horizon_min": h,
            "gated_signals": summary.get("gated_signals"),
            "pf_gated_30m_tick": summary.get("pf_gated_30m_tick"),
            "mean_return_gated": summary.get("mean_return_gated"),
            "top_pool_share": summary.get("top_pool_share"),
        })
        print(json.dumps(results[-1]))

    report = {"backtest_days": 30, "horizons": results, "RESEARCH_ONLY": True}
    (OUT_DIR / "horizon_metric_sweep.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
