#!/usr/bin/env python3
"""Analyze worst sender-gated signals for common failure patterns (R5)."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "mvp/audits/wallet_profile_v0/sender_gated/sender_gated_paper_shadow.csv"
OUT = REPO / "mvp/audits/wallet_profile_v0/sender_gated/loss_analysis.json"


def main() -> None:
    rows = list(csv.DictReader(SRC.open(newline="", encoding="utf-8")))
    losses = sorted(
        [r for r in rows if float(r["net_return_30m_tick"]) < 0],
        key=lambda r: float(r["net_return_30m_tick"]),
    )
    wins = [r for r in rows if float(r["net_return_30m_tick"]) >= 0]

    def stats(subset: list[dict]) -> dict[str, Any]:
        heats = [int(r["onset_heat"]) for r in subset if r.get("onset_heat")]
        return {
            "n": len(subset),
            "heat_median": sorted(heats)[len(heats) // 2] if heats else None,
            "heat_mean": sum(heats) / len(heats) if heats else None,
            "heat_le_7": sum(1 for h in heats if h <= 7),
            "top_senders": Counter(r["smart_sender"] for r in subset).most_common(5),
        }

    worst15 = losses[:15]
    sender_loss_counts = Counter(r["smart_sender"] for r in losses)
    report = {
        "total_signals": len(rows),
        "losses": len(losses),
        "wins": len(wins),
        "loss_stats": stats(losses),
        "win_stats": stats(wins),
        "worst_15": [
            {
                "signal_time": r["signal_time"],
                "pool": r["pool"],
                "onset_heat": int(r["onset_heat"]),
                "net_return_30m_tick": float(r["net_return_30m_tick"]),
                "smart_sender": r["smart_sender"],
            }
            for r in worst15
        ],
        "repeat_loss_senders": sender_loss_counts.most_common(10),
        "recommendations": [],
    }

    ls = report["loss_stats"]
    ws = report["win_stats"]
    if ls.get("heat_median") and ws.get("heat_median") and ls["heat_median"] < ws["heat_median"]:
        report["recommendations"].append("Try min_heat >= 10 gate (losses skew lower heat)")
    if ls.get("heat_le_7", 0) > len(losses) * 0.4:
        report["recommendations"].append("Many losses at heat<=7; filter heat>=8")

    top_loss_sender, top_loss_n = report["repeat_loss_senders"][0] if report["repeat_loss_senders"] else ("", 0)
    if top_loss_n >= 3:
        report["recommendations"].append(f"Sender {top_loss_sender[:12]}... appears in {top_loss_n} losses")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
