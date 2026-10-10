#!/usr/bin/env python3
"""Sprint061 human closure preflight — no private key, no live trades."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLOSURE_DIR = REPO / "mvp" / "audits" / "sprint061_human_closure"
CHECKLIST = CLOSURE_DIR / "sprint061_human_closure_checklist.json"
DRILL_LOG = CLOSURE_DIR / "panic_drill_log.jsonl"
FLAGS_OUT = CLOSURE_DIR / "sprint061_human_closure_flags.json"

WETH = "0x4200000000000000000000000000000000000006"
UNIVERSAL_ROUTER = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"
WETH_PILOT_MAX_WEI = 50_000_000_000_000_000
PANIC_KEY = os.environ.get("SYSTEM_PANIC_KEY", "SYSTEM_PANIC")
REDIS_URL = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def static_protocol_checks() -> dict:
    docs = [
        "allowance_cap_protocol.md",
        "private_key_session_protocol.md",
        "panic_sell_fail_verification.md",
        "README.md",
    ]
    missing = [d for d in docs if not (CLOSURE_DIR / d).exists()]
    preflight_py = (REPO / "services" / "executor" / "src" / "preflight.py").exists()
    return {
        "all_protocol_docs_present": len(missing) == 0,
        "missing_docs": missing,
        "executor_preflight_module": preflight_py,
    }


def redis_health() -> dict:
    try:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        panic = bool(r.get(PANIC_KEY))
        open_n = len(r.smembers("positions:open_pools") or [])
        lag = 0
        for g in r.xinfo_groups("signals"):
            if g.get("name") == "execution_group":
                lag = g.get("lag", 0) or 0
        return {
            "redis_ok": True,
            "panic_active": panic,
            "open_positions": open_n,
            "execution_group_lag": lag,
        }
    except Exception as e:
        return {"redis_ok": False, "error": str(e)}


def panic_drill(dry_run_only: bool = True) -> dict:
    if os.environ.get("LIVE_TRADING_MODE", "").lower() in ("1", "true", "yes"):
        return {"ok": False, "error": "LIVE_TRADING_MODE must be false for drill"}
    try:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        before_panic = bool(r.get(PANIC_KEY))
        r.set(PANIC_KEY, "1")
        after_set = bool(r.get(PANIC_KEY))
        r.delete(PANIC_KEY)
        after_clear = bool(r.get(PANIC_KEY))
        entry = {
            "ts": utc_now(),
            "before_panic": before_panic,
            "after_set": after_set,
            "after_clear": after_clear,
            "dry_run_only": dry_run_only,
        }
        DRILL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DRILL_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return {"ok": after_set and not after_clear, **entry}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_allowance(wallet: str, spender: str) -> dict:
    try:
        from web3 import Web3

        rpc = os.environ.get("EXECUTOR_RPC_URL") or os.environ.get("EVM_HTTP_URL")
        if not rpc:
            return {"ok": False, "error": "no_rpc_url"}
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        abi = [
            {
                "constant": True,
                "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            }
        ]
        token = w3.eth.contract(address=Web3.to_checksum_address(WETH), abi=abi)
        allow = token.functions.allowance(
            Web3.to_checksum_address(wallet), Web3.to_checksum_address(spender)
        ).call()
        return {
            "ok": True,
            "allowance_wei": allow,
            "within_pilot_cap": 0 < allow <= WETH_PILOT_MAX_WEI,
            "is_unlimited": allow > WETH_PILOT_MAX_WEI,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True)
    ap.add_argument("--panic-drill", action="store_true")
    ap.add_argument("--check-allowance", action="store_true")
    ap.add_argument("--wallet", default="")
    ap.add_argument("--spender", default=UNIVERSAL_ROUTER)
    args = ap.parse_args()

    static = static_protocol_checks()
    redis_m = redis_health()
    drill = panic_drill() if args.panic_drill else {"skipped": True}
    allowance = (
        check_allowance(args.wallet, args.spender)
        if args.check_allowance and args.wallet
        else {"skipped": True}
    )

    checklist = json.loads(CHECKLIST.read_text()) if CHECKLIST.exists() else {}
    flags = {
        "generated_at_utc": utc_now(),
        "static": static,
        "redis": redis_m,
        "panic_drill": drill,
        "allowance_check": allowance,
        "protocol_closure": {
            "h1_ready": static.get("all_protocol_docs_present"),
            "h4_ready": static.get("all_protocol_docs_present"),
            "h3_drill_passed": drill.get("ok") is True,
            "h5_ready": static.get("all_protocol_docs_present"),
            "aggregate": "PROTOCOLS_READY_AWAITING_HUMAN"
            if static.get("all_protocol_docs_present")
            else "INCOMPLETE",
        },
        "safety": {
            "live_trading_mode": os.environ.get("LIVE_TRADING_MODE", "false"),
            "private_key_in_env": bool(os.environ.get("EXECUTOR_PRIVATE_KEY")),
            "note": "private_key_in_env should be false outside authorized sessions",
        },
    }
    FLAGS_OUT.write_text(json.dumps(flags, indent=2))
    print(json.dumps(flags, indent=2))


if __name__ == "__main__":
    main()
