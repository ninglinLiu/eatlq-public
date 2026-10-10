"""Observer-only shadow recorder — NO trading.

Records forward signals after prospective T0 using the frozen v1 spec.
Requires live/fresh swap ingestion beyond the historical parquet end.
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import OUT, ROOT, load_freeze, utcnow


def main() -> None:
    freeze = load_freeze()
    t0_path = OUT / "PROSPECTIVE_T0.json"
    if not t0_path.exists():
        print("[shadow] PROSPECTIVE_T0.json missing — run DIAGNOSTIC_REPLAY finalize first.", flush=True)
        return
    t0 = json.loads(t0_path.read_text())
    out = OUT / "shadow"
    out.mkdir(parents=True, exist_ok=True)
    status = {
        "mode": "observer_only",
        "live_trading": False,
        "prospective_t0": t0.get("prospective_t0"),
        "arm": "LARGE_BUY_ABSORPTION",
        "code_hash": t0.get("code_hash"),
        "config_hash": t0.get("config_hash"),
        "status": "ARMED_WAITING_FOR_LIVE_INGESTION",
        "note": (
            "Historical resolved_swaps ends ~2026-07-02. Shadow accumulation starts when a live "
            "ingestion feeder appends new Base V3 WETH-pool swaps. This process does not place orders."
        ),
        "armed_at": utcnow(),
        "freeze_status": freeze.get("status"),
    }
    (out / "SHADOW_STATUS.json").write_text(json.dumps(status, indent=2) + "\n")
    # empty signal log
    sig_path = out / "shadow_signals.jsonl"
    if not sig_path.exists():
        sig_path.write_text("")
    print(json.dumps(status, indent=2), flush=True)
    print(f"[shadow] wrote {out / 'SHADOW_STATUS.json'}", flush=True)


if __name__ == "__main__":
    main()
