#!/usr/bin/env python3
"""Run R4 label freshness scan."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "mvp/audits/wallet_profile_v0/sender_gated"
LABELS_DIR = REPO / "mvp/audits/wallet_profile_v0/labels"


def main() -> None:
    results = []
    for lb in (14, 30, 60, 90):
        suffix = f"_lb{lb}d"
        print(f"=== lookback {lb}d ===", file=sys.stderr)
        subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/wallet_profile_v0/compute_labels.py"),
                "--only", "smart_money",
                "--lookback-days", str(lb),
                "--output-suffix", suffix,
                "--redis-suffix", suffix,
            ],
            cwd=str(REPO),
            check=True,
        )
        out_json = OUT_DIR / f"backtest_lb{lb}d_14d.json"
        subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/sprint091_sender_gated_paper.py"),
                "--backtest-days", "14",
                "--no-readout",
                "--labels-csv", str(LABELS_DIR / f"all_labels{suffix}.csv"),
                "--output-json", str(out_json),
            ],
            cwd=str(REPO),
            check=True,
        )
        summary = json.loads(out_json.read_text())
        row = {
            "lookback_days": lb,
            "smart_money_count": summary.get("labels_csv"),
            "gated_signals": summary.get("gated_signals"),
            "pf_gated_30m_tick": summary.get("pf_gated_30m_tick"),
            "mean_return_gated": summary.get("mean_return_gated"),
            "top_pool_share": summary.get("top_pool_share"),
        }
        labels_csv = LABELS_DIR / f"all_labels{suffix}.csv"
        if labels_csv.exists():
            import csv
            row["smart_money_n"] = sum(
                1 for r in csv.DictReader(labels_csv.open())
                if str(r.get("is_smart_money", "")).lower() in ("true", "1")
            )
        results.append(row)
        print(json.dumps(row))

    report = {"backtest_days": 14, "lookbacks": results, "RESEARCH_ONLY": True}
    (OUT_DIR / "lookback_scan.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
