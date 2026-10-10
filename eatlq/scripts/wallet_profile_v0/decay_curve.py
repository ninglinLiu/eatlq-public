#!/usr/bin/env python3
"""Daily rolling PF for sender-gated smart_money (R1 decay curve)."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated" / "sender_gated_paper_shadow.csv"
OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated" / "decay_curve.csv"
OUT_JSON = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated" / "decay_curve.json"


def pf(vals: list[float]) -> Optional[float]:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def main() -> None:
    if not SRC.exists():
        print(f"missing {SRC}; run sprint091 --backtest-days 30 first", file=sys.stderr)
        sys.exit(1)

    rows = list(csv.DictReader(SRC.open(newline="", encoding="utf-8")))
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        d = r["signal_time"][:10]
        by_day[d].append(float(r["net_return_30m_tick"]))

    days = sorted(by_day)
    out: list[dict] = []
    first_below_1: Optional[str] = None
    for i, d in enumerate(days):
        win7 = [v for dd in days[max(0, i - 6) : i + 1] for v in by_day[dd]]
        pf_day = pf(by_day[d])
        pf7 = pf(win7)
        if pf7 is not None and pf7 < 1.0 and first_below_1 is None:
            first_below_1 = d
        out.append(
            {
                "day": d,
                "n_day": len(by_day[d]),
                "n_win7": len(win7),
                "pf_day": pf_day,
                "mean_day": sum(by_day[d]) / len(by_day[d]) if by_day[d] else None,
                "pf_rolling7d": pf7,
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    recent7 = [v for d in days[-7:] for v in by_day[d]] if len(days) >= 7 else []
    older7 = [v for d in days[:-7][-7:] for v in by_day[d]] if len(days) >= 14 else []
    report = {
        "source_csv": str(SRC.relative_to(REPO)),
        "total_signals": len(rows),
        "days_covered": len(days),
        "first_rolling7d_below_1.0": first_below_1,
        "pf_rolling7d_latest": pf(recent7) if recent7 else None,
        "pf_rolling7d_prior_week": pf(older7) if older7 else None,
        "rows": out,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
