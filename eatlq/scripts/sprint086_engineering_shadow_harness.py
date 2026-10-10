#!/usr/bin/env python3
"""
Sprint086 — Engineering shadow validation harness (NOT SOP alpha promotion).

Separate virtual ledgers for non-fee100-onset supply:
  - fee500  : fee_bps == 500 (0.05% tier)
  - fee3000 : fee_bps == 3000 (0.30% tier) when present
  - old_pool: pool_age_sec > 86400 (aged pools, any fee)

Reads paper-shadow CSV rows only. No Redis. No live trading. No model promotion.
Artifacts: mvp/audits/sprint086_engineering_shadow/
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forward_race_harness_core import (
    FORWARD_CUTOFF_DEFAULT,
    HORIZONS,
    forward_metrics,
    parse_dt,
    run_strategy,
    utc_now,
)

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "mvp/audits/sprint086_engineering_shadow"
STATE_DIR = REPO / "logs/sprint086_engineering"

TRACKS = {
    "fee500": {
        "label": "fee500_engineering",
        "fee_bps_in": {500},
        "min_pool_age_sec": None,
        "max_pool_age_sec": None,
        "csv_paths": [
            REPO / "logs/sprint083_fee500_paper_shadow.csv",
            REPO / "logs/sprint055_paper_shadow.csv",
        ],
    },
    "fee3000": {
        "label": "fee3000_engineering",
        "fee_bps_in": {3000},
        "min_pool_age_sec": None,
        "max_pool_age_sec": None,
        "csv_paths": [REPO / "logs/sprint055_paper_shadow.csv"],
    },
    "old_pool": {
        "label": "old_pool_engineering",
        "fee_bps_in": None,
        "min_pool_age_sec": 86400,
        "max_pool_age_sec": None,
        "csv_paths": [REPO / "logs/sprint055_paper_shadow.csv"],
    },
}

STRATEGY = {
    "strategy_id": "D_entry_anchor_30m_fixed",
    "exit_horizon": "30m",
    "age_min_sec": 1800,
    "age_max_sec": 86400,
}


def log(msg: str) -> None:
    print(f"[s086] {utc_now().strftime('%H:%M:%SZ')} {msg}", flush=True)


def load_filtered_rows(cfg: dict, forward_cutoff: str) -> tuple[list[dict], list[dict]]:
    cutoff = parse_dt(forward_cutoff)
    hist, fwd = [], []
    seen: set[str] = set()
    for csv_path in cfg["csv_paths"]:
        if not csv_path.exists():
            continue
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("entry_quote_ok") != "True":
                    continue
                key = f"{r.get('pool','')}::{r.get('signal_time','')}"
                if key in seen:
                    continue
                try:
                    fee = int(float(r.get("fee_bps") or 0))
                    age = float(r.get("pool_age_sec") or 0)
                except (TypeError, ValueError):
                    continue
                allowed = cfg.get("fee_bps_in")
                if allowed is not None and fee not in allowed:
                    continue
                if cfg.get("min_pool_age_sec") is not None and age < cfg["min_pool_age_sec"]:
                    continue
                if cfg.get("max_pool_age_sec") is not None and age > cfg["max_pool_age_sec"]:
                    continue
                seen.add(key)
                row = {
                    "pool": r["pool"],
                    "signal_time": r["signal_time"],
                    "fee_bps": float(fee),
                    "pool_age_sec": age,
                    "activity_heat": float(r.get("activity_heat") or 0),
                    "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                    "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                }
                for h in HORIZONS:
                    ok_k, ag_k = f"exit_{h}_ok", f"net_return_{h}_ag"
                    row[f"ok_{h}"] = r.get(ok_k) == "True"
                    v = r.get(ag_k)
                    row[f"retag_{h}"] = float(v) if v not in (None, "", "\\N") else None
                sig_dt = parse_dt(r["signal_time"])
                (fwd if sig_dt > cutoff else hist).append(row)
    hist.sort(key=lambda x: x["signal_time"])
    fwd.sort(key=lambda x: x["signal_time"])
    return hist, fwd


def run_track(track_key: str, cfg: dict, forward_cutoff: str) -> dict:
    hist, fwd = load_filtered_rows(cfg, forward_cutoff)
    track_state_dir = STATE_DIR / track_key
    track_state_dir.mkdir(parents=True, exist_ok=True)

    baseline_path = track_state_dir / "repair_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    state = run_strategy(STRATEGY, fwd, state_dir=track_state_dir, log_prefix=f"[s086:{track_key}]")
    fm = forward_metrics(state, state["stats"])
    forward_n = int(fm.get("forward_n") or 0)

    if not baseline:
        baseline = {
            "established_at": utc_now().isoformat(),
            "forward_n_at_baseline": forward_n,
        }
        baseline_path.write_text(json.dumps(baseline, indent=2))

    days = len({(t.get("signal_time") or "")[:10] for t in state.get("trades", [])})
    return {
        "track_key": track_key,
        "label": cfg["label"],
        "evidence_class": "ENGINEERING_ONLY",
        "sop_promotion_eligible": False,
        "signal_rows_total": len(hist) + len(fwd),
        "signal_rows_forward": len(fwd),
        "forward_n": forward_n,
        "forward_n_incremental": max(0, forward_n - int(baseline.get("forward_n_at_baseline") or 0)),
        "forward_wr": fm.get("wr"),
        "forward_pf": fm.get("pf"),
        "forward_roi_pct": round(100 * (float(state.get("equity", 100)) - 100) / 100, 2),
        "forward_days": days,
        "forward_equity": fm.get("equity"),
        "last_signal_time": max((r["signal_time"] for r in hist + fwd), default=""),
        "last_run_utc": state.get("last_run"),
        "run_count": state.get("run_count"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--forward-cutoff", default=FORWARD_CUTOFF_DEFAULT)
    args = parser.parse_args()

    scoreboard = [run_track(k, cfg, args.forward_cutoff) for k, cfg in TRACKS.items()]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scoreboard.json").write_text(json.dumps(scoreboard, indent=2))
    (OUT_DIR / "engineering_tracks.json").write_text(
        json.dumps(
            {
                "generated_at_utc": utc_now().isoformat(),
                "evidence_class": "ENGINEERING_ONLY",
                "sop_promotion_eligible": False,
                "tracks": list(TRACKS.keys()),
                "scoreboard": scoreboard,
            },
            indent=2,
        )
    )
    log(f"tracks={len(scoreboard)} scoreboard written")
    print(json.dumps({"ok": True, "scoreboard": scoreboard}, indent=2))


if __name__ == "__main__":
    main()
