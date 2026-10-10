"""Hard RPC budget guard for maintenance scripts and cron wrappers.

Usage (import):
    from scripts.lib.rpc_budget_guard import require_rpc_budget, record_rpc_call

Usage (CLI preflight):
    python3 scripts/lib/rpc_budget_guard.py --calls 1
    EATLQ_RPC_BUDGET_ENABLED=0 python3 scripts/lib/rpc_budget_guard.py  # bypass

Env:
    EATLQ_RPC_BUDGET_ENABLED   "1" (default) blocks when budget exceeded
    EATLQ_RPC_DAILY_CALL_LIMIT Max eth_* calls per UTC day (default 5000)
    EATLQ_RPC_STATE_FILE       Counter file path
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LIMIT = 5000
DEFAULT_STATE = Path("/opt/EATLQ/mvp/diagnostics/rpc_budget_state.json")


def _enabled() -> bool:
    return os.environ.get("EATLQ_RPC_BUDGET_ENABLED", "1") == "1"


def _limit() -> int:
    return int(os.environ.get("EATLQ_RPC_DAILY_CALL_LIMIT", str(DEFAULT_LIMIT)))


def _state_path() -> Path:
    return Path(os.environ.get("EATLQ_RPC_STATE_FILE", str(DEFAULT_STATE)))


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"day": _utc_day(), "calls": 0}
    try:
        data = json.loads(path.read_text())
        if data.get("day") != _utc_day():
            return {"day": _utc_day(), "calls": 0}
        return {"day": data.get("day", _utc_day()), "calls": int(data.get("calls", 0))}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {"day": _utc_day(), "calls": 0}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def get_budget_status() -> dict:
    path = _state_path()
    state = _load_state(path)
    limit = _limit()
    used = int(state.get("calls", 0))
    return {
        "day": state.get("day", _utc_day()),
        "calls_used": used,
        "calls_limit": limit,
        "calls_remaining": max(0, limit - used),
        "enabled": _enabled(),
        "state_file": str(path),
    }


def record_rpc_call(n: int = 1) -> dict:
    if n <= 0:
        return get_budget_status()
    path = _state_path()
    state = _load_state(path)
    state["calls"] = int(state.get("calls", 0)) + n
    state["day"] = _utc_day()
    _save_state(path, state)
    return get_budget_status()


def require_rpc_budget(n: int = 1) -> None:
    if not _enabled():
        return
    status = get_budget_status()
    if status["calls_used"] + n > status["calls_limit"]:
        raise RuntimeError(
            f"RPC budget exceeded for {status['day']}: "
            f"{status['calls_used']}+{n} > {status['calls_limit']} "
            f"(state={status['state_file']})"
        )


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="RPC budget guard")
    p.add_argument("--calls", type=int, default=0, help="Record N calls then exit 0")
    p.add_argument("--status", action="store_true", help="Print JSON status")
    p.add_argument("--require", type=int, default=0, help="Exit 1 if budget cannot afford N calls")
    args = p.parse_args()

    if args.status:
        print(json.dumps(get_budget_status(), indent=2))
        return 0

    if args.require > 0:
        try:
            require_rpc_budget(args.require)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.calls > 0:
        record_rpc_call(args.calls)
        print(json.dumps(get_budget_status(), indent=2))
        return 0

    print(json.dumps(get_budget_status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
