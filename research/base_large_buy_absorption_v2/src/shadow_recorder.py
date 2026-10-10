"""Observer-only shadow recorder — NO trading (v2)."""
from __future__ import annotations

import json

from .common import OUT, load_freeze, utcnow


def arm_shadow(*, upgrade: bool = False) -> dict:
    freeze = load_freeze()
    t0_path = OUT / "PROSPECTIVE_T0.json"
    t0 = json.loads(t0_path.read_text()) if t0_path.exists() else {}
    out = OUT / "shadow"
    out.mkdir(parents=True, exist_ok=True)
    status = {
        "mode": "observer_only",
        "live_trading": False,
        "upgrade_from_v1": bool(upgrade),
        "edge_claim": bool(upgrade),
        "prospective_t0": t0.get("prospective_t0"),
        "arm": "LARGE_BUY_ABSORPTION",
        "primary_timing": {
            "absorption_window_seconds": freeze["signal_timing"]["absorption_window_seconds"],
            "measured_latency_seconds": freeze["signal_timing"]["measured_latency_seconds"],
        },
        "status": (
            "ARMED_EDGE_HYPOTHESIS_ACTIVE"
            if upgrade
            else "ARMED_OBSERVER_NO_EDGE_CLAIM"
        ),
        "note": (
            "Observer-only. No orders. Shadow upgrade requires PROVEN selection + ranking; "
            "otherwise recorder stays armed without an edge claim."
        ),
        "armed_at": utcnow(),
        "freeze_status": freeze.get("status"),
    }
    (out / "SHADOW_STATUS.json").write_text(json.dumps(status, indent=2) + "\n")
    sig_path = out / "shadow_signals.jsonl"
    if not sig_path.exists():
        sig_path.write_text("")
    print(json.dumps(status, indent=2), flush=True)
    return status


def main() -> None:
    arm_shadow(upgrade=False)


if __name__ == "__main__":
    main()
