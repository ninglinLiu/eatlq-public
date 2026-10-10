"""
Execution layer preflight checks.

Gates:
  1. LIVE_TRADING_MODE must be false by default.
  2. If LIVE_TRADING_MODE=true, require LIVE_AUTHORIZED_BY=HUMAN and
     EXECUTOR_PRIVATE_KEY to be set — otherwise hard abort.
  3. PRIVATE_KEY absence must not fail dry-run (EXECUTOR_DRY_RUN=1).
  4. Panic flag must not be set.

Called from executor main.run() before the event loop starts.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Any

log = logging.getLogger("preflight")

_LIVE_TRADING_MODE_ENV = "LIVE_TRADING_MODE"
_LIVE_AUTHORIZED_ENV = "LIVE_AUTHORIZED_BY"
_DRY_RUN_ENV = "EXECUTOR_DRY_RUN"
_PRIVATE_KEY_ENV = "EXECUTOR_PRIVATE_KEY"
_CANARY_ENABLED_ENV = "CANARY_ENABLED"


def _parse_bool(v: str | None) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def run_preflight_checks(panic_active: bool = False) -> Dict[str, Any]:
    """Run all preflight checks and return a summary dict.

    On hard-abort conditions this function calls sys.exit(1).
    """
    flags: Dict[str, Any] = {}

    live_mode = _parse_bool(os.environ.get(_LIVE_TRADING_MODE_ENV))
    dry_run = _parse_bool(os.environ.get(_DRY_RUN_ENV))
    has_pk = bool(os.environ.get(_PRIVATE_KEY_ENV, "").strip())
    authorized = os.environ.get(_LIVE_AUTHORIZED_ENV, "").strip().upper()

    flags["LIVE_TRADING_MODE"] = live_mode
    flags["EXECUTOR_DRY_RUN"] = dry_run
    flags["PRIVATE_KEY_PRESENT"] = has_pk
    flags["PRIVATE_KEY_TOUCHED"] = False
    flags["REAL_TX_SENT"] = False
    flags["PANIC_ACTIVE"] = panic_active

    # Gate 1: LIVE_TRADING_MODE must be false unless explicitly authorized
    if live_mode:
        if authorized != "HUMAN":
            log.error(
                "HARD ABORT: LIVE_TRADING_MODE=true but LIVE_AUTHORIZED_BY is not 'HUMAN'. "
                "Set LIVE_AUTHORIZED_BY=HUMAN only after manual human review. Exiting."
            )
            sys.exit(1)
        if not has_pk:
            log.error(
                "HARD ABORT: LIVE_TRADING_MODE=true requires EXECUTOR_PRIVATE_KEY. Exiting."
            )
            sys.exit(1)
        if dry_run:
            log.error(
                "HARD ABORT: LIVE_TRADING_MODE=true conflicts with EXECUTOR_DRY_RUN=1. Exiting."
            )
            sys.exit(1)
        log.warning(
            "LIVE_TRADING_MODE=true with LIVE_AUTHORIZED_BY=HUMAN — live execution enabled."
        )
        flags["mode"] = "LIVE"
    else:
        flags["mode"] = "DRY_RUN" if dry_run else "PAPER"

    # Gate 2: dry-run must not require private key
    if dry_run and not has_pk:
        log.info("EXECUTOR_DRY_RUN=1, no private key — dry-run mode confirmed, no wallet needed.")

    # Gate 3: panic check. Panic blocks new BUYs in main.py, but startup must
    # still proceed so open positions keep being monitored and exited.
    if panic_active:
        log.error("SYSTEM_PANIC is active — starting in buy-blocked monitoring mode.")

    log.info(
        "Preflight OK: mode=%s live=%s dry_run=%s has_pk=%s",
        flags["mode"], live_mode, dry_run, has_pk,
    )
    return flags


def assert_no_live_tx(flags: Dict[str, Any]) -> None:
    """Raise RuntimeError if a live transaction would be sent in non-live mode.

    Called before any eth_sendRawTransaction to enforce the REAL_TX_SENT=false gate.
    """
    if not flags.get("LIVE_TRADING_MODE") and flags.get("REAL_TX_SENT"):
        raise RuntimeError(
            "BUG: REAL_TX_SENT=true while LIVE_TRADING_MODE=false. "
            "This is a programming error in the executor — abort."
        )


def validate_canary_manual_approval(
    *,
    canary_enabled: bool | None = None,
    approval: Dict[str, Any] | None = None,
    strategy_id: str,
    pool: str,
    max_trade_usd: float,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Validate a one-shot canary approval document without touching keys.

    This is a reusable hard-abort guard for any future real-money canary call
    site. The approval is scoped to exactly one strategy, one pool, and one
    amount, with an expiry no more than 10 minutes in the future.
    """
    enabled = _parse_bool(os.environ.get(_CANARY_ENABLED_ENV)) if canary_enabled is None else bool(canary_enabled)
    if not enabled:
        return {"ok": False, "hard_abort": True, "reason": "CANARY_ENABLED_false"}
    if not approval:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_token_missing"}

    current = now or datetime.now(timezone.utc)
    expires_raw = str(approval.get("expires_at_utc", ""))
    try:
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except Exception:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_expiry_invalid"}

    ttl = int((expires - current).total_seconds())
    if ttl <= 0:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_expired"}
    if ttl > 600:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_ttl_gt_10m"}
    if approval.get("strategy_id") != strategy_id:
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_strategy_mismatch"}
    if str(approval.get("pool", "")).lower() != pool.lower():
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_pool_mismatch"}
    if float(approval.get("max_trade_usd", 0)) != float(max_trade_usd):
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_amount_mismatch"}
    if not approval.get("nonce"):
        return {"ok": False, "hard_abort": True, "reason": "manual_approval_nonce_missing"}
    return {"ok": True, "hard_abort": False, "reason": "manual_approval_valid_single_scope"}
