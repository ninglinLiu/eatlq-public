#!/usr/bin/env python3
"""
Sprint077 — Hard horizon exit smoke (paper/dry-run contract)
============================================================
Proves that when a long-hold position passes horizon + label grace without a
CSV label, the harness emits a SCHEDULED_CLOSE_SYNTHETIC_FLAT intent record
(dry_run=True, redis_writes=False). No production signals.

Writes:
  mvp/audits/sprint077_forward_expansion/hard_exit_smoke_result.json
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forward_race_harness_core import (  # noqa: E402
    HARD_EXIT_LABEL_WAIT_SEC,
    horizon_delta,
    load_state,
    parse_dt,
    run_strategy,
    save_state,
)

REPO = SCRIPT_DIR.parent
OUT_DIR = REPO / "mvp" / "audits" / "sprint077_forward_expansion"
TMP = REPO / "logs" / "sprint077" / "_smoke_tmp_state"
SID = "S077_HARD_EXIT_SMOKE"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    sig = "2026-05-16T00:00:00+00:00"
    pool = "0x0000000000000000000000000000000000000dec"
    sk = f"{pool}::{sig}"
    exp = parse_dt(sig) + horizon_delta("4h")

    spec = {
        "strategy_id": SID,
        "exit_horizon": "4h",
        "fee_max_bps": None,
        "pv_min": None,
        "momentum_gate": False,
        "hard_horizon_exit": True,
    }

    fwd_row = {
        "pool": pool,
        "signal_time": sig,
        "fee_bps": 100.0,
        "pool_age_sec": 5000.0,
        "activity_heat": 10.0,
        "buy_pressure_ratio": 0.6,
        "price_velocity_5m": 0.01,
        "ok_4h": False,
        "retag_4h": None,
    }
    for h in ("30m", "1h", "2h", "6h", "12h", "24h"):
        fwd_row[f"ok_{h}"] = False
        fwd_row[f"retag_{h}"] = None

    st = load_state(TMP, SID)
    st["open_positions"][sk] = {
        "sig_key": sk,
        "pool": pool,
        "signal_time": sig,
        "expected_exit": exp.isoformat(),
        "fee_bps": 100.0,
        "pool_age_sec": 5000.0,
        "bpr": 0.6,
        "pv": 0.01,
    }
    save_state(TMP, st)

    t_fire = exp.timestamp() + HARD_EXIT_LABEL_WAIT_SEC + 60
    dt_fire = datetime.fromtimestamp(t_fire, tz=timezone.utc)

    with patch("forward_race_harness_core.utc_now", return_value=dt_fire):
        run_strategy(spec, [fwd_row], state_dir=TMP)

    st2 = load_state(TMP, SID)
    intents = st2.get("hard_exit_intents") or []
    trades = st2.get("trades") or []
    ok_intent = any(i.get("intent") == "SCHEDULED_CLOSE_SYNTHETIC_FLAT" for i in intents)
    ok_trade = any(t.get("exit_kind") == "HARD_SYNTHETIC_ZERO" for t in trades)

    result = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "mock_clock_utc": dt_fire.isoformat(),
        "hard_exit_label_wait_sec": HARD_EXIT_LABEL_WAIT_SEC,
        "intent_emitted": ok_intent,
        "trade_closed_hard_synthetic": ok_trade,
        "redis_writes": False,
        "production_signals": False,
        "PASS": bool(ok_intent and ok_trade),
        "last_intent": intents[-1] if intents else None,
        "last_trade": trades[-1] if trades else None,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "hard_exit_smoke_result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
