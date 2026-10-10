#!/usr/bin/env python3
"""Sprint083 canary harness preflight and dry-run drill.

This prepares a future real-money canary gate while keeping defaults off.
It never reads, prints, or stores private keys and never sends transactions.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs/canary_config.json"
N_ACCOUNTING = REPO / "mvp/audits/execution_readiness/n_accounting.json"
SOP_AUDIT = REPO / "mvp/audits/sprint083_fee500_paper_shadow/sop_reality_audit.json"
TRACK_A_CSV = REPO / "logs/sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp/audits/sprint083_fee500_paper_shadow"
FLAGS_OUT = OUT_DIR / "canary_harness_flags.json"
READINESS_MD = OUT_DIR / "canary_harness_readiness.md"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _latest_signal_age_sec(path: Path) -> tuple[str, int | None]:
    if not path.exists():
        return "", None
    with path.open() as f:
        rows = list(csv.DictReader(f))
    last = max([r.get("signal_time", "") for r in rows if r.get("signal_time")], default="")
    if not last:
        return "", None
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return last, int(datetime.now(timezone.utc).timestamp() - dt.timestamp())
    except Exception:
        return last, None


def _redis_runtime() -> dict[str, Any]:
    result = {
        "executor_lag": None,
        "dead_letters": None,
        "stuck_positions": None,
        "redis_error": None,
    }
    try:
        import redis

        r = redis.from_url(os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0"), decode_responses=True)
        result["dead_letters"] = int(r.xlen("signals_dead_letter") or 0)
        for group in r.xinfo_groups("signals"):
            if group.get("name") == "execution_group":
                result["executor_lag"] = int(group.get("lag", 0) or 0)
                break
        stuck = 0
        now = int(time.time())
        for pool in r.smembers("positions:open_pools"):
            pos = r.hgetall(f"positions:{pool}")
            entry_ms = int(pos.get("entry_time_ms", 0) or 0)
            if entry_ms > 0 and (now * 1000 - entry_ms) > 6 * 3600 * 1000:
                stuck += 1
        result["stuck_positions"] = stuck
    except Exception as exc:
        result["redis_error"] = str(exc)
    return result


@dataclass
class ApprovalRequest:
    strategy_id: str
    pool: str
    max_trade_usd: float


def validate_manual_approval(config: dict[str, Any], req: ApprovalRequest, approval_path: str | None) -> dict[str, Any]:
    """Validate a scoped manual approval token.

    The token is modeled as a local JSON approval document. This function does
    not print token contents and never reads private keys.
    """
    canary = config["canary"]
    if canary.get("require_manual_approval", True) and not approval_path:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_token_missing"}
    if not approval_path:
        return {"ok": True, "hard_abort": False, "reason": "manual_approval_not_required"}

    try:
        approval = _load_json(Path(approval_path))
    except Exception:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_token_unreadable"}

    now = datetime.now(timezone.utc)
    expires_raw = str(approval.get("expires_at_utc", ""))
    try:
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except Exception:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_expiry_invalid"}

    ttl = int((expires - now).total_seconds())
    if ttl <= 0:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_expired"}
    if ttl > int(canary.get("approval_token_ttl_seconds_max", 600)):
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_ttl_gt_10m"}
    if approval.get("strategy_id") != req.strategy_id:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_strategy_mismatch"}
    if str(approval.get("pool", "")).lower() != req.pool.lower():
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_pool_mismatch"}
    if float(approval.get("max_trade_usd", 0)) != float(req.max_trade_usd):
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_amount_mismatch"}
    if not approval.get("nonce"):
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_nonce_missing"}
    return {"ok": True, "hard_abort": False, "reason": "manual_approval_valid_single_scope"}


def evaluate_preflight(approval_path: str | None = None) -> dict[str, Any]:
    config = _load_json(CONFIG)
    n = _load_json(N_ACCOUNTING)
    sop = _load_json(SOP_AUDIT)
    runtime = _redis_runtime()
    last_signal, signal_age = _latest_signal_age_sec(TRACK_A_CSV)
    signal_stale = True if signal_age is None else signal_age > int(os.environ.get("SIGNAL_STALE_SEC", "7200"))

    gates = config["preflight_gates"]
    canary = config["canary"]
    live_env_on = _parse_bool(os.environ.get("LIVE_TRADING_MODE"))
    canary_env_on = _parse_bool(os.environ.get("CANARY_ENABLED"))
    toxic = (((sop.get("rolling_toxic_7d") or {}).get("overall") or {}).get("toxic_rate"))
    true_n = int(n.get("true_live_forward_executor_n", 0) or 0)

    simulated_signal = ApprovalRequest(
        strategy_id=canary["allowed_strategy_ids"][0],
        pool="0x0000000000000000000000000000000000000000",
        max_trade_usd=float(canary["max_trade_usd"]),
    )
    approval = validate_manual_approval(config, simulated_signal, approval_path)

    checks = [
        ("CANARY_ENABLED_default_false", canary.get("CANARY_ENABLED") is False and not canary_env_on),
        ("LIVE_TRADING_MODE_default_false", not live_env_on),
        ("true_live_forward_executor_n_gte_25", true_n >= int(gates["min_true_live_forward_executor_n"])),
        ("signal_stale_false", signal_stale is False),
        ("toxic_rate_7d_lt_0_15", toxic is not None and float(toxic) < float(gates["max_toxic_rate_7d"])),
        ("dead_letters_zero", runtime.get("dead_letters") == 0),
        ("stuck_positions_zero", runtime.get("stuck_positions") == 0),
        ("executor_lag_zero", runtime.get("executor_lag") == 0),
        ("manual_approval_valid", approval["ok"]),
        ("max_trade_usd_lte_5", float(canary["max_trade_usd"]) <= 5.0),
        ("max_daily_trades_lte_1", int(canary["max_daily_trades"]) <= 1),
        ("max_total_loss_usd_lte_20", float(canary["max_total_loss_usd"]) <= 20.0),
    ]
    failed = [name for name, ok in checks if not ok]
    decision = "CANARY_REVIEW_READY" if not failed else "CANARY_BLOCKED"

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "CANARY_ENABLED": False,
        "LIVE_TRADING_MODE": live_env_on,
        "NORMAL_LIVE_ALLOWED": False,
        "REAL_TX_SENT": False,
        "PRIVATE_KEY_TOUCHED": False,
        "PRIVATE_KEY_PRINTED": False,
        "config": canary,
        "preflight_inputs": {
            "true_live_forward_executor_n": true_n,
            "last_signal_time": last_signal,
            "signal_age_sec": signal_age,
            "signal_stale": signal_stale,
            "toxic_rate_7d": toxic,
            **runtime,
        },
        "manual_approval": approval,
        "checks": [{"name": name, "pass": ok} for name, ok in checks],
        "blocked_reasons": failed,
        "dry_run_drill": {
            "simulated_signal": {
                "strategy_id": simulated_signal.strategy_id,
                "pool": simulated_signal.pool,
                "max_trade_usd": simulated_signal.max_trade_usd,
            },
            "result": "BLOCKED" if failed else "WOULD_PASS_PREFLIGHT_ONLY",
            "no_transaction_attempted": True,
        },
        "unlock_conditions": [
            "Set CANARY_ENABLED=true only after explicit manual review; default must remain false until ignition window.",
            "LIVE_TRADING_MODE remains false until separate human ignition step.",
            "true_live_forward_executor_n >= 25.",
            "signal_stale=false.",
            "toxic_rate_7d < 0.15.",
            "dead_letters=0, stuck_positions=0, executor_lag=0.",
            "valid single-use manual approval token scoped to one strategy, one pool, one amount, expiring within 10 minutes.",
            "honeypot pass and sell simulation pass for the exact candidate.",
        ],
    }


def write_artifacts(flags: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FLAGS_OUT.write_text(json.dumps(flags, indent=2, ensure_ascii=False))
    cfg = flags["config"]
    inp = flags["preflight_inputs"]
    lines = [
        "# Canary Harness Readiness",
        "",
        f"Generated: `{flags['generated_at_utc']}`",
        "",
        "## Executive Decision",
        "",
        f"- Decision: `{flags['decision']}`",
        f"- CANARY_ENABLED: `{flags['CANARY_ENABLED']}`",
        f"- LIVE_TRADING_MODE: `{flags['LIVE_TRADING_MODE']}`",
        f"- NORMAL_LIVE_ALLOWED: `{flags['NORMAL_LIVE_ALLOWED']}`",
        f"- REAL_TX_SENT: `{flags['REAL_TX_SENT']}`",
        f"- PRIVATE_KEY_TOUCHED: `{flags['PRIVATE_KEY_TOUCHED']}`",
        "",
        "## Canary Config",
        "",
        f"- max_trade_usd: `{cfg['max_trade_usd']}`",
        f"- max_daily_trades: `{cfg['max_daily_trades']}`",
        f"- max_total_loss_usd: `{cfg['max_total_loss_usd']}`",
        f"- require_manual_approval: `{cfg['require_manual_approval']}`",
        f"- allowed_strategy_ids: `{cfg['allowed_strategy_ids']}`",
        f"- require_honeypot_pass: `{cfg['require_honeypot_pass']}`",
        f"- require_sell_sim_pass: `{cfg['require_sell_sim_pass']}`",
        f"- require_executor_lag_zero: `{cfg['require_executor_lag_zero']}`",
        "",
        "## Preflight Gates",
        "",
        "| gate | pass |",
        "|---|---|",
    ]
    for check in flags["checks"]:
        lines.append(f"| `{check['name']}` | `{check['pass']}` |")
    lines += [
        "",
        "## Dry-Run Drill Result",
        "",
        f"- Result: `{flags['dry_run_drill']['result']}`",
        f"- Simulated strategy: `{flags['dry_run_drill']['simulated_signal']['strategy_id']}`",
        f"- Simulated max_trade_usd: `{flags['dry_run_drill']['simulated_signal']['max_trade_usd']}`",
        "- No transaction attempted.",
        "",
        "## Current Inputs",
        "",
        f"- true_live_forward_executor_n: `{inp['true_live_forward_executor_n']}`",
        f"- signal_stale: `{inp['signal_stale']}`",
        f"- toxic_rate_7d: `{inp['toxic_rate_7d']}`",
        f"- dead_letters: `{inp['dead_letters']}`",
        f"- stuck_positions: `{inp['stuck_positions']}`",
        f"- executor_lag: `{inp['executor_lag']}`",
        "",
        "## Blocked Reasons",
        "",
    ]
    lines.extend([f"- `{x}`" for x in flags["blocked_reasons"]] or ["- none"])
    lines += [
        "",
        "## Unlock Conditions For First $5 Canary",
        "",
    ]
    lines.extend([f"- {x}" for x in flags["unlock_conditions"]])
    READINESS_MD.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approval-token-json", default=None)
    parser.add_argument("--write-artifacts", action="store_true")
    args = parser.parse_args()
    flags = evaluate_preflight(args.approval_token_json)
    if args.write_artifacts:
        write_artifacts(flags)
    print(json.dumps(flags, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
