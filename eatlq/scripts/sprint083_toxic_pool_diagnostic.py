#!/usr/bin/env python3
"""Diagnose a single pool from sprint055 paper-shadow labels.

This is intentionally offline: it does not call RPC or ClickHouse. It turns the
existing forward-label rows into a compact risk artifact so the daily readout
does not rely on one hand-written anecdote.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_055 = REPO / "logs/sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp/audits/sprint083_fee500_paper_shadow"
DEFAULT_POOL = "0xc6822075d5ef55adce2e37585b44b5f104f4d975"
HORIZONS = ("5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h")


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_pool_rows(pool: str) -> list[dict]:
    rows = []
    with CSV_055.open(newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("pool") or "").lower() == pool.lower():
                rows.append(row)
    return sorted(rows, key=lambda r: r.get("signal_time", ""))


def summarize(rows: list[dict], pool: str) -> dict:
    horizon_summary = {}
    for h in HORIZONS:
        vals = [_f(r.get(f"net_return_{h}_ag", "")) for r in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            horizon_summary[h] = {"n": 0}
            continue
        horizon_summary[h] = {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 6),
            "median": round(statistics.median(vals), 6),
            "min": round(min(vals), 6),
            "max": round(max(vals), 6),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 6),
            "loss_le_15pct_rate": round(sum(1 for v in vals if v <= -0.15) / len(vals), 6),
            "loss_le_25pct_rate": round(sum(1 for v in vals if v <= -0.25) / len(vals), 6),
        }

    fee_counts = Counter(r.get("fee_bps", "") for r in rows)
    entry_ok = sum(1 for r in rows if r.get("entry_quote_ok") == "True")
    latest = rows[-1] if rows else {}
    risk_flags = []
    for h in ("2h", "6h", "12h"):
        hs = horizon_summary.get(h, {})
        if hs.get("n", 0) and hs.get("mean", 0) <= -0.10:
            risk_flags.append(f"MEAN_{h}_LOSS_GT_10PCT")
        if hs.get("loss_le_25pct_rate", 0) > 0:
            risk_flags.append(f"{h}_HAS_25PCT_DRAWDOWN")
    if horizon_summary.get("12h", {}).get("win_rate", 1) < 0.35:
        risk_flags.append("LOW_12H_WIN_RATE")

    verdict = "TOXIC_POOL_REVIEW" if risk_flags else "INCONCLUSIVE_SINGLE_POOL"
    return {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "pool": pool.lower(),
        "rows": len(rows),
        "entry_quote_ok_rows": entry_ok,
        "fee_bps_distribution": dict(fee_counts),
        "first_signal_time": rows[0].get("signal_time") if rows else None,
        "last_signal_time": latest.get("signal_time") if latest else None,
        "horizon_summary": horizon_summary,
        "risk_flags": sorted(set(risk_flags)),
        "verdict": verdict,
        "note": "Offline CSV-label diagnostic only; RPC sell simulation/sandwich attribution still requires chain data.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=DEFAULT_POOL)
    ap.add_argument("--out", type=Path, default=OUT_DIR / "toxic_pool_diagnostic.json")
    args = ap.parse_args()

    rows = load_pool_rows(args.pool)
    result = summarize(rows, args.pool)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
