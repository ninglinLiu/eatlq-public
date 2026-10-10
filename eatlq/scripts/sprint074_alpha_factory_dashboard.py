#!/usr/bin/env python3
"""Refresh rolling Alpha Factory dashboard JSON (no secrets)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FACTORY_DIR = REPO / "mvp" / "audits" / "sprint074_alpha_factory"
SCORE = FACTORY_DIR / "alpha_factory_scoreboard.json"
L073 = REPO / "mvp" / "audits" / "sprint073_live_review" / "sprint073_gate_status.json"
OUT = FACTORY_DIR / "alpha_factory_rolling_dashboard.json"


def main() -> None:
    FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    scoreboard = json.loads(SCORE.read_text()) if SCORE.exists() else []
    gates = json.loads(L073.read_text()) if L073.exists() else {}

    hist = {
        "total_strategies": len(scoreboard),
        "bands": {},
    }
    for row in scoreboard:
        b = row.get("promotion_band", "?")
        hist["bands"][b] = hist["bands"].get(b, 0) + 1

    OUT.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "global_gates_L073_summary": {
                    "L0_passed": gates.get("L0", {}).get("passed"),
                    "L1_forward_trades": gates.get("L1", {}).get("forward_trades"),
                    "L2_passed": gates.get("L2", {}).get("passed"),
                },
                "factory_band_histogram": hist["bands"],
                "top_5_by_composite": sorted(
                    scoreboard, key=lambda x: -x.get("composite_score", 0)
                )[:5],
                "note": "Re-run sprint074_alpha_factory_runner.py after CSV refresh.",
            },
            indent=2,
            default=str,
        )
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
