#!/usr/bin/env python3
"""
Sprint078 — Batch-25 forward dry-run harness
============================================
Runs 25 SOP-selected virtual trading accounts against live paper-shadow CSV.
Includes Sprint074 Alpha Factory specs + Sprint072 exit-horizon variants.

No Redis. No live trading. Isolated state under logs/sprint078/.

Cron (offset :14):
  14-59/5 * * * * cd /opt/EATLQ && python3 scripts/sprint078_batch25_harness.py --once \\
      >> logs/sprint078/harness_cron.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forward_race_harness_core import (
    FORWARD_CUTOFF_DEFAULT,
    append_summary,
    forward_metrics,
    load_csv_split,
    run_strategy,
    utc_now,
)

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "mvp/audits/sprint078_pipeline_batch25/batch25_manifest.json"
SRC_CSV = REPO / "logs/sprint055_paper_shadow.csv"
STATE_DIR = REPO / "logs/sprint078"
OUT_DIR = REPO / "mvp/audits/sprint078_pipeline_batch25"


def log(msg: str) -> None:
    print(f"[s078] {utc_now().strftime('%H:%M:%SZ')} {msg}", flush=True)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise SystemExit(f"Missing manifest: {MANIFEST}. Run generate_batch25_manifest.py first.")
    return json.loads(MANIFEST.read_text())


def build_scoreboard_rows(specs: list[dict], states: list[dict]) -> list[dict]:
    rows = []
    for spec, state in zip(specs, states):
        fm = forward_metrics(state, state["stats"])
        horizon = spec["exit_horizon"]
        entry_desc = (
            f"age[{int(spec.get('age_min_sec', 1800))},{int(spec.get('age_max_sec', 86400))}]"
            + (f" fee≤{int(spec['fee_max_bps'])}" if spec.get("fee_max_bps") else "")
            + (" pv≥0" if spec.get("pv_min") is not None else "")
            + (" momentum" if spec.get("momentum_gate") else "")
        )
        rows.append(
            {
                "strategy_id": spec["strategy_id"],
                "harness": "sprint078_batch25",
                "source": spec.get("source"),
                "family": spec.get("family"),
                "exit_horizon": horizon,
                "entry_description": entry_desc,
                "promotion_band": spec.get("promotion_band"),
                "fwd_n": fm["forward_n"],
                "fwd_pf": fm["pf"],
                "fwd_wr": fm["wr"],
                "fwd_ev": fm["ev"],
                "fwd_top_pool_share": fm["top_pool_share"],
                "fwd_equity": fm["equity"],
                "fwd_open_positions": fm.get("open_positions", 0),
                "hard_exit_forced_count": fm.get("hard_exit_forced_count", 0),
                "last_run_utc": state["last_run"],
                "run_count": state["run_count"],
            }
        )
    rows.sort(key=lambda r: (r["fwd_n"] or 0) * -1)
    return rows


def run_once() -> None:
    manifest = load_manifest()
    specs = manifest["dryrun_strategies"]
    if len(specs) != 25:
        log(f"WARN manifest dryrun_count={len(specs)} (expected 25)")

    t0 = time.perf_counter()
    hist, fwd = load_csv_split(SRC_CSV, FORWARD_CUTOFF_DEFAULT)
    log(f"CSV split hist={len(hist)} fwd={len(fwd)} strategies={len(specs)}")

    states = []
    for spec in specs:
        st = run_strategy(spec, fwd, state_dir=STATE_DIR)
        fm = forward_metrics(st, st["stats"])
        append_summary(
            STATE_DIR,
            spec["strategy_id"],
            {
                "strategy_id": spec["strategy_id"],
                "last_run": st["last_run"],
                "forward_n": fm["forward_n"],
                "quote_fail_proxy": fm["quote_fail_proxy"],
                "open_positions": fm.get("open_positions", 0),
                "hard_exit_forced": fm.get("hard_exit_forced_count", 0),
            },
        )
        states.append(st)
        log(
            f"  {spec['strategy_id']} horizon={spec['exit_horizon']} "
            f"fwd_n={fm['forward_n']} open={fm.get('open_positions', 0)}"
        )

    rows = build_scoreboard_rows(specs, states)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scoreboard.json").write_text(json.dumps(rows, indent=2, default=str))
    (OUT_DIR / "decision_flags.json").write_text(
        json.dumps(
            {
                "sprint": "078",
                "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
                "strategy_count": len(specs),
                "max_fwd_n": max((r["fwd_n"] or 0) for r in rows) if rows else 0,
                "LIVE_ALLOWED": False,
                "TRAINING_ALLOWED": False,
                "manifest_path": str(MANIFEST),
            },
            indent=2,
        )
    )
    log(f"Scoreboard → {OUT_DIR / 'scoreboard.json'} elapsed_ms={int((time.perf_counter()-t0)*1000)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint078 Batch-25 forward dry-run harness")
    parser.add_argument("--once", action="store_true", help="Run one harness cycle")
    args = parser.parse_args()
    if not args.once:
        parser.error("Use --once (cron-safe)")
    run_once()


if __name__ == "__main__":
    main()
