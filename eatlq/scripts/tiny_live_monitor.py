#!/usr/bin/env python3
"""Monitor tiny live trades vs paper shadow predictions."""
from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALERT_LOG = REPO / "mvp" / "audits" / "tiny_live" / "divergence_alerts.log"


def load_live(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def load_paper_index(path: Path) -> dict[tuple, dict]:
    idx: dict[tuple, dict] = {}
    if not path.exists():
        return idx
    with open(path) as f:
        for row in csv.DictReader(f):
            key = (row.get("signal_time"), row.get("pool"))
            idx[key] = row
    return idx


def pct(val: str | None) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v * 100 if abs(v) < 2 else v
    except ValueError:
        return None


def summarize(live_rows: list[dict]) -> dict:
    closed = [r for r in live_rows if r.get("status") in ("closed", "dry_run_closed")]
    if not closed:
        return {"n": 0, "wr": None, "total_pnl_u": 0.0, "pf": None}
    pnls = []
    for r in closed:
        try:
            pnls.append(float(r.get("pnl_u") or 0))
        except ValueError:
            pass
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    wr = len(wins) / len(pnls) if pnls else None
    pf = sum(wins) / sum(losses) if losses else None
    return {
        "n": len(closed),
        "wr": wr,
        "total_pnl_u": sum(pnls),
        "pf": pf,
    }


def check_divergence(live_rows: list[dict], paper_idx: dict) -> list[str]:
    alerts = []
    for r in live_rows:
        if r.get("status") not in ("closed", "dry_run_closed"):
            continue
        key = (r.get("signal_time"), r.get("pool"))
        paper = paper_idx.get(key)
        if not paper:
            continue
        live_pct = pct(r.get("pnl_pct"))
        paper_pct = pct(paper.get("net_return_30m_real")) or pct(paper.get("net_return_30m_ag"))
        if live_pct is None or paper_pct is None or paper_pct == 0:
            continue
        rel_err = abs(live_pct - paper_pct) / max(abs(paper_pct), 0.01)
        if rel_err > 0.5:
            msg = (
                f"{datetime.now(timezone.utc).isoformat()} "
                f"pool={r.get('pool')} live={live_pct:.2f}% paper={paper_pct:.2f}% "
                f"rel_err={rel_err:.0%}\n"
            )
            alerts.append(msg)
    return alerts


def print_report(live_path: Path, paper_path: Path) -> None:
    live = load_live(live_path)
    paper_idx = load_paper_index(paper_path)
    s = summarize(live)
    print(f"[{datetime.now(timezone.utc).isoformat()}] tiny_live monitor")
    print(f"  closed_trades={s['n']} wr={s['wr']} pf={s['pf']} total_pnl_u={s['total_pnl_u']:.3f}")
    alerts = check_divergence(live, paper_idx)
    if alerts:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_LOG, "a") as f:
            for a in alerts:
                f.write(a)
                print(f"  ALERT: {a.strip()}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--live-csv", default="logs/tiny_live_runner.csv")
    p.add_argument("--paper-csv", default="logs/sprint055_paper_shadow.csv")
    p.add_argument("--interval", type=int, default=300, help="seconds between polls (0=once)")
    args = p.parse_args()
    live_path = REPO / args.live_csv
    paper_path = REPO / args.paper_csv
    if args.interval <= 0:
        print_report(live_path, paper_path)
        return
    while True:
        print_report(live_path, paper_path)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
