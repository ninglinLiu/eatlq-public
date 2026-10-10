"""
EATLQ Executor — consumes Redis `signals` stream, manages positions (dry-run default).

Default: EXECUTOR_DRY_RUN=1 — no real transactions.
LIVE_TRADING_MODE=true requires LIVE_AUTHORIZED_BY=HUMAN and conflicts with EXECUTOR_DRY_RUN=1
(ambiguous live vs simulation — hard abort).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis

import cluster_blacklist
import preflight
from notify import send_telegram, validate_telegram_or_raise
from position import OpenPosition, PositionStore, PENDING_SELL_TTL_MS
from preflight_trade import friction_gate, run_pretrade_gates
from registry import PoolInfo, ROUTER_FOR_POOL_TYPE, WETH_BASE, get_redis, lookup, set_redis
from swap import (
    _SLIPPAGE_TOLERANCE,
    SwapResult,
    _get_token_balance,
    check_honeypot,
    execute_buy,
    execute_sell,
    get_eth_usd_price,
    get_slot0_price_eth,
    quote_sell_eth_wei,
    recover_buy_amount_out,
    simulate_real_sell,
)
from tx_sender import init_send_lane

log = logging.getLogger("executor")

_SIGNAL_PAYLOAD_FIELDS = ("payload", "signal_json")
_EVM_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Exit policy — aligned with keystone OOS research (hard -50%, hold 4h, no tight trail).
# Reads compose env names first; keystone defaults override legacy tight stops.
def _pct(name: str, fallback: str) -> float:
    return float(os.environ.get(name, fallback))


TAKE_PROFIT_PCT = _pct("EXECUTOR_TAKE_PROFIT_PCT", "10.0")       # 1000% = effectively off
HARD_STOP_PCT = _pct("EXECUTOR_STOP_LOSS_PCT", _pct("EXECUTOR_HARD_STOP_PCT", "0.50"))
TRAILING_PULL_PCT = _pct("EXECUTOR_TRAILING_STOP_PCT", _pct("EXECUTOR_TRAILING_PULL_PCT", "1.0"))
_time_min = os.environ.get("EXECUTOR_TIME_DECAY_MIN")
HOLD_SECONDS = int(_time_min) * 60 if _time_min else int(os.environ.get("EXECUTOR_HOLD_SECONDS", "14400"))
MONITOR_INTERVAL_SEC = float(os.environ.get("EXECUTOR_MONITOR_INTERVAL_SEC", "5"))
SIZING_CHECK_INTERVAL_SEC = float(os.environ.get("EXECUTOR_SIZING_CHECK_INTERVAL_SEC", "600"))
HEARTBEAT_SEC = float(os.environ.get("EXECUTOR_HEARTBEAT_SEC", "86400"))  # daily liveness ping
# Signal staleness gate: the `signals` stream can hold a backlog (e.g. the executor
# was stopped while signal-bridge kept producing). Buying on a stale signal enters a
# pool whose fresh-launch momentum is long gone — exactly the "burst of buys right
# after restart" failure. Drop any signal older than this (age = now − stream-id ms).
# 0 disables. 90s is generous vs normal sub-second pipeline latency.
SIGNAL_MAX_AGE_SEC = float(os.environ.get("EXECUTOR_SIGNAL_MAX_AGE_SEC", "90") or 0)
# Consecutive monitor-cycle failures before alerting that positions may be
# unmanaged, plus a rate limit so a persistent fault does not spam Telegram.
MONITOR_FAIL_ALERT_THRESHOLD = int(os.environ.get("EXECUTOR_MONITOR_FAIL_ALERT_THRESHOLD", "3"))
MONITOR_FAIL_ALERT_INTERVAL_SEC = float(os.environ.get("EXECUTOR_MONITOR_FAIL_ALERT_INTERVAL_SEC", "300"))

# E — post-buy delayed recheck (escape). During the first
# EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC after entry, probe "can I still sell?" every
# EXECUTOR_POSTBUY_RECHECK_BLOCKS blocks (Base ≈ 2s/block) instead of the 60s
# steady-state cadence, so a minute-level LP pull is caught and the position is
# ABANDONed fast + the cluster is blacklisted + a CRITICAL alert fires.
# HONEST: a sub-60s pull can still drain before the first probe — E limits
# damage + blocks repeats + books the loss; it is NOT an escape guarantee.
POSTBUY_RECHECK_BLOCKS = int(os.environ.get("EXECUTOR_POSTBUY_RECHECK_BLOCKS", "3"))
POSTBUY_RECHECK_WINDOW_SEC = float(os.environ.get("EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC", "300"))
_BASE_BLOCK_MS = float(os.environ.get("EXECUTOR_BLOCK_TIME_MS", "2000"))
POSTBUY_RECHECK_MS = max(2000.0, POSTBUY_RECHECK_BLOCKS * _BASE_BLOCK_MS)
# A WETH-depth collapse below this floor (ETH) inside the post-buy window is the
# LP-pull signature; force an immediate authoritative real-gas sell sim.
POSTBUY_DEPTH_COLLAPSE_ETH = float(os.environ.get("EXECUTOR_POSTBUY_DEPTH_COLLAPSE_ETH", "0.5"))
POSTBUY_DEPTH_COLLAPSE_WEI = int(POSTBUY_DEPTH_COLLAPSE_ETH * 1e18)

# Shadow telemetry (2026-06-25): record the would-be position value at +30min from
# entry ONCE, purely observational. Lets us learn offline whether a 30min time-cap
# would beat the current no-cap convex-tail policy — WITHOUT running a live A/B.
# It NEVER triggers a sell and NEVER alters any exit decision.
SHADOW_30MIN_SEC = float(os.environ.get("EXECUTOR_SHADOW_30MIN_SEC", "1800"))
SHADOW_30MIN_ENABLED = os.environ.get("EXECUTOR_SHADOW_30MIN", "1").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ExecutorConfig:
    redis_url: str
    signals_stream: str
    signals_dead_letter_stream: str
    consumer_group: str
    consumer_name: str
    chain_id: int
    dry_run: bool
    live_trading_mode: bool
    eth_price_usd: float
    max_position_usd: float
    max_balance_fraction: float
    max_concurrent: int
    min_confidence: float
    panic_key: str
    rpc_url: str
    daily_loss_pct: float
    dynamic_sizing: bool
    size_ladder_usd: tuple
    friction_min: float
    size_min_usd: float
    max_pool_share: float
    max_volume_share: float
    size_bisect_iters: int
    # ETH-native sizing (preferred): when > 0 these override the USD knobs so the
    # critical sizing path never depends on an ETH/USD oracle. USD values are then
    # display-only. Wallet, gas, pool depth and PnL are already ETH-native.
    max_position_eth: float
    size_min_eth: float
    canary_eth: float
    # Portfolio-level exposure cap: sum of open-position entry stake must stay
    # below this fraction of the live wallet balance. Guards correlated -50%
    # tail risk that per-position caps alone do not bound. 0 disables the gate.
    max_portfolio_fraction: float


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _set_panic(r: redis.Redis, key: str, reason: str) -> None:
    r.set(key, reason)
    log.error("SYSTEM_PANIC set: %s", reason)
    send_telegram(f"SYSTEM_PANIC: {reason}")


def _check_daily_loss(
    r: redis.Redis,
    store: PositionStore,
    cfg: ExecutorConfig,
    w3,
    account,
) -> bool:
    """Return True if daily loss limit breached (block new BUY)."""
    if cfg.daily_loss_pct <= 0:
        return False
    day_key = _day_key()
    baseline = store.get_day_start_balance_wei(day_key)
    if baseline is None and w3 is not None and account is not None:
        try:
            baseline = int(w3.eth.get_balance(account.address))
            store.set_day_start_balance_wei(day_key, baseline)
        except Exception as e:
            log.warning("day_start_balance fetch failed: %s", e)
            return False
    if baseline is None or baseline <= 0:
        return False
    realized = store.get_realized_pnl_wei(day_key)
    if realized >= 0:
        return False
    loss_pct = abs(realized) / baseline * 100.0
    if loss_pct >= cfg.daily_loss_pct:
        _set_panic(r, cfg.panic_key, f"daily_loss_{loss_pct:.1f}pct")
        return True
    return False


def _update_monitor_failure_state(
    failed: bool, streak: int, last_alert: float, now: float
) -> tuple[int, float, bool]:
    """Pure state machine for monitor-cycle failure alerting (P1-③).

    Returns (new_streak, new_last_alert, should_alert). A clean cycle resets the
    streak. On failure the streak increments; an alert is due only once the
    streak crosses MONITOR_FAIL_ALERT_THRESHOLD and the rate-limit window has
    elapsed (so a persistent fault does not spam Telegram).
    """
    if not failed:
        return 0, last_alert, False
    streak += 1
    should = streak >= MONITOR_FAIL_ALERT_THRESHOLD and (
        now - last_alert >= MONITOR_FAIL_ALERT_INTERVAL_SEC
    )
    if should:
        last_alert = now
    return streak, last_alert, should


def _open_exposure_wei(store: PositionStore) -> int:
    """Sum of entry stake (wei) across all open positions."""
    total = 0
    for pool in store.list_open_pools():
        pos = store.get(pool)
        if pos is not None:
            total += int(pos.entry_eth_wei or 0)
    return total


def _portfolio_exposure_exceeded(store: PositionStore, cfg: ExecutorConfig, w3, account) -> bool:
    """Return True if opening another position would breach the portfolio cap.

    Sums open-position entry stake and compares to wallet_balance * fraction.
    Fail-open only when the gate cannot be evaluated (no wallet / balance read
    error) so paper/dry-run is unaffected; live with a readable balance enforces.
    """
    if cfg.max_portfolio_fraction <= 0:
        return False
    if account is None or w3 is None or not (cfg.live_trading_mode and not cfg.dry_run):
        return False
    try:
        balance = int(w3.eth.get_balance(account.address))
    except Exception as e:
        log.warning("portfolio exposure gate: balance read failed, skipping gate: %s", e)
        return False
    if balance <= 0:
        return False
    budget = int(balance * cfg.max_portfolio_fraction)
    exposure = _open_exposure_wei(store)
    if exposure >= budget:
        log.warning(
            "skip BUY: portfolio exposure %.6f ETH >= cap %.6f ETH (%.0f%% of %.6f ETH wallet)",
            exposure / 1e18, budget / 1e18, 100 * cfg.max_portfolio_fraction, balance / 1e18,
        )
        return True
    return False


def _record_realized_pnl(store: PositionStore, delta_wei: int) -> None:
    if delta_wei == 0:
        return
    store.add_realized_pnl_wei(_day_key(), delta_wei)


def _parse_signal_enqueue_ms(msg_id: Any) -> Optional[int]:
    """Redis stream IDs are '<ms>-<seq>'; the ms part ≈ signal enqueue time.
    Used as the signal timestamp when the payload carries no explicit one."""
    try:
        return int(str(msg_id).split("-", 1)[0])
    except (ValueError, AttributeError):
        return None


def _record_exec_quality(
    r: redis.Redis,
    *,
    kind: str,
    pool_info: PoolInfo,
    expected_out: Optional[int],
    actual_out: Optional[int],
    signal_ms: Optional[int],
    fill_ms: Optional[int],
    sig_block: Optional[int],
    fill_block: Optional[int],
    reverted: bool,
    gas_used: Optional[int],
    gas_cost_wei: Optional[int],
    eth_price: float,
    tx_hash: str,
    size_usd: float,
    pool_created_block: Optional[int] = None,
    exit_trigger_ms: Optional[int] = None,
    exit_trigger_block: Optional[int] = None,
) -> str:
    """P0.5 execution-quality telemetry. Quantifies the gap between the backtest's
    'zero execution loss' assumption and live reality:
      • deviation_pct  = (actual fill − QuoterV2 quote) / quote  (slippage + latency
                          adverse selection; negative = we got less than quoted)
      • delay_sec/blocks = signal enqueue → tx mined  (OUR PIPELINE latency:
                          bridge→executor→chain; NOT "how far behind snipers" we are)
      • entry_pool_age_blocks = fill_block − pool_created_block  (competition/freshness
                          proxy; only the buy side, when the signal carries created_block)
      • reverted, gas_used, gas_usd
    Pure observability — does NOT change strategy/sizing. Fail-soft (never raises
    into the trade path). Returns a short text block to append to the TG message.
    """
    dev_pct = None
    if expected_out and expected_out > 0 and actual_out is not None:
        dev_pct = 100.0 * (actual_out - expected_out) / expected_out
    delay_sec = None
    if signal_ms and fill_ms:
        delay_sec = max(0.0, (fill_ms - signal_ms) / 1000.0)
    delay_blocks = None
    if sig_block and fill_block:
        delay_blocks = int(fill_block) - int(sig_block)
    pool_age_blocks = None
    if pool_created_block and fill_block:
        pool_age_blocks = int(fill_block) - int(pool_created_block)
    # EXIT latency telemetry (additive; does NOT alter any exit decision).
    # For sells: trigger = moment the monitor decided "sell now"; fill = tx mined.
    # delay_sec is the live answer to "how long from trailing/maxhold/E-window
    # trigger until our sell actually lands on-chain".
    exit_delay_sec = None
    if exit_trigger_ms and fill_ms:
        exit_delay_sec = max(0.0, (fill_ms - exit_trigger_ms) / 1000.0)
    exit_delay_blocks = None
    if exit_trigger_block and fill_block:
        exit_delay_blocks = int(fill_block) - int(exit_trigger_block)
    gas_eth = (gas_cost_wei or 0) / 1e18
    gas_usd = gas_eth * (eth_price or 0)
    rec = {
        "event": "exec_quality",
        "kind": kind,
        "pool": getattr(pool_info, "pool_address", ""),
        "token": getattr(pool_info, "non_weth_symbol", "?"),
        "size_usd": round(size_usd or 0, 2),
        "tx": tx_hash or "",
        "expected_out": expected_out,
        "actual_out": actual_out,
        "deviation_pct": round(dev_pct, 3) if dev_pct is not None else None,
        "delay_sec": round(delay_sec, 2) if delay_sec is not None else None,
        "delay_blocks": delay_blocks,
        "entry_pool_age_blocks": pool_age_blocks,
        "exit_trigger_ms": exit_trigger_ms,
        "exit_trigger_block": exit_trigger_block,
        "exit_delay_sec": round(exit_delay_sec, 2) if exit_delay_sec is not None else None,
        "exit_delay_blocks": exit_delay_blocks,
        "reverted": bool(reverted),
        "gas_used": gas_used,
        "gas_eth": round(gas_eth, 8),
        "gas_usd": round(gas_usd, 4),
    }
    log.info(json.dumps(rec))
    try:
        if r is not None:
            r.xadd("exec:quality", {"json": json.dumps(rec)}, maxlen=10000, approximate=True)
    except Exception as e:
        log.warning("exec_quality xadd failed: %s", e)
    dev_s = f"{dev_pct:+.2f}%" if dev_pct is not None else "n/a"
    delay_s = f"{delay_sec:.1f}s" if delay_sec is not None else "n/a"
    if delay_blocks is not None:
        delay_s += f"/{delay_blocks}blk"
    age_s = f"  pool_age={pool_age_blocks}blk" if pool_age_blocks is not None else ""
    return (
        f"slip(quote→fill)={dev_s}  delay={delay_s}{age_s}\n"
        f"revert={'Y' if reverted else 'N'}  gas=${gas_usd:.2f}"
    )


def _record_shadow_30min(
    r: redis.Redis,
    *,
    pool_info: PoolInfo,
    pos: "OpenPosition",
    mark_wei: int,
    entry_wei: int,
    peak_wei: int,
    age_sec: float,
    now_ms: int,
    block: Optional[int],
    eth_price: float,
) -> None:
    """Shadow telemetry (2026-06-25): the would-be position value at +30min from
    entry. PURE observability — it NEVER triggers a sell and NEVER alters any exit
    decision (it is called after `mark` is computed and writes only to a separate
    `exec:shadow` stream). One record per position (gated by mark_shadow_once).

    A month of these lets us compute, on the SAME real pools we actually held,
    'what would a 30min hard-cap have captured vs the no-cap convex-tail policy' —
    the reason the principal chose 'no cap' but wants the counterfactual for free.
    Fail-soft: never raises into the monitor loop.
    """
    entry = max(int(entry_wei or 0), 1)
    shadow_pnl_pct = 100.0 * (int(mark_wei or 0) - entry) / entry
    value_eth = (int(mark_wei or 0)) / 1e18
    rec = {
        "event": "shadow_30min",
        "pool": getattr(pool_info, "pool_address", ""),
        "token": getattr(pool_info, "non_weth_symbol", "?"),
        "opened_at_ms": getattr(pos, "opened_at_ms", 0),
        "shadow_30min_ts": now_ms,
        "shadow_30min_block": block,
        "age_sec": round(age_sec, 1),
        "entry_eth_wei": int(entry_wei or 0),
        "shadow_30min_value_eth_wei": int(mark_wei or 0),
        "shadow_30min_value_usd": round(value_eth * (eth_price or 0), 4),
        "shadow_30min_pnl_pct": round(shadow_pnl_pct, 3),
        "peak_eth_wei": int(peak_wei or 0),
        "size_usd": round(getattr(pos, "size_usd", 0.0) or 0.0, 2),
    }
    log.info(json.dumps(rec))
    try:
        if r is not None:
            r.xadd("exec:shadow", {"json": json.dumps(rec)}, maxlen=10000, approximate=True)
    except Exception as e:
        log.warning("shadow_30min xadd failed: %s", e)


def _parse_bool(v: str | None) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> ExecutorConfig:
    live = _parse_bool(os.environ.get("LIVE_TRADING_MODE"))
    dry = _parse_bool(os.environ.get("EXECUTOR_DRY_RUN", "1"))
    if live and dry:
        log.error(
            "HARD ABORT: LIVE_TRADING_MODE=true conflicts with EXECUTOR_DRY_RUN=1 — "
            "ambiguous live vs simulation. Exiting."
        )
        sys.exit(1)
    return ExecutorConfig(
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        signals_stream=os.environ.get("SIGNALS_STREAM", "signals"),
        signals_dead_letter_stream=os.environ.get("SIGNALS_DEAD_LETTER_STREAM", "signals_dead_letter"),
        consumer_group=os.environ.get("SIGNALS_CONSUMER_GROUP", os.environ.get("EXECUTOR_CONSUMER_GROUP", "executor")),
        consumer_name=os.environ.get("SIGNALS_CONSUMER_NAME", os.environ.get("EXECUTOR_CONSUMER_NAME", "executor-1")),
        chain_id=int(os.environ.get("CHAIN_ID", "8453")),
        dry_run=dry if not live else False,
        live_trading_mode=live,
        eth_price_usd=float(os.environ.get("EXECUTOR_ETH_PRICE_USD", "3500")),
        max_position_usd=float(os.environ.get("EXECUTOR_MAX_POSITION_USD", "10")),
        # Default 1.0 = disabled. A 0.10 default silently deadlocks any wallet
        # smaller than 10x the stake; see _balance_fraction_active.
        max_balance_fraction=float(os.environ.get("EXECUTOR_MAX_BALANCE_FRACTION", "1.0")),
        max_concurrent=int(os.environ.get("EXECUTOR_MAX_CONCURRENT_POSITIONS", "3")),
        min_confidence=float(os.environ.get("SIGNAL_MIN_CONFIDENCE", os.environ.get("STRATEGY_MIN_CONFIDENCE", "0.52"))),
        panic_key=os.environ.get("SYSTEM_PANIC_KEY", "SYSTEM_PANIC"),
        rpc_url=os.environ.get("EXECUTOR_READ_RPC_URL", os.environ.get("EXECUTOR_RPC_URL", os.environ.get("EVM_HTTP_URL", ""))),
        daily_loss_pct=float(os.environ.get("EXECUTOR_DAILY_LOSS_PCT", "20")),
        dynamic_sizing=_parse_bool(os.environ.get("EXECUTOR_DYNAMIC_SIZING", "0")),
        size_ladder_usd=tuple(sorted(
            (float(x) for x in os.environ.get("EXECUTOR_SIZE_LADDER_USD", "1000,500,300,100").split(",") if x.strip()),
            reverse=True,
        )),
        friction_min=float(os.environ.get("EXECUTOR_FRICTION_MIN", "-0.05")),
        size_min_usd=float(os.environ.get("EXECUTOR_SIZE_MIN_USD", "25")),
        max_pool_share=float(os.environ.get("EXECUTOR_MAX_POOL_SHARE", "0.05")),
        max_volume_share=float(os.environ.get("EXECUTOR_MAX_VOLUME_SHARE", "0.10")),
        size_bisect_iters=int(os.environ.get("EXECUTOR_SIZE_BISECT_ITERS", "7")),
        max_position_eth=float(os.environ.get("EXECUTOR_MAX_POSITION_ETH", "0") or 0),
        size_min_eth=float(os.environ.get("EXECUTOR_SIZE_MIN_ETH", "0") or 0),
        canary_eth=float(os.environ.get("EXECUTOR_EXTERNAL_RISK_CANARY_ETH", "0") or 0),
        max_portfolio_fraction=float(os.environ.get("EXECUTOR_MAX_PORTFOLIO_FRACTION", "0.30") or 0),
    )


def _panic_reason(r: redis.Redis, key: str) -> str:
    try:
        val = r.get(key)
        if val is None:
            return ""
        return val.decode() if isinstance(val, bytes) else str(val)
    except Exception:
        return ""


_panic_read_fail_alerted = False


def _panic_active(r: redis.Redis, key: str) -> bool:
    """Return True if buys should be paused.

    FAIL-CLOSED: if the panic key cannot be read (Redis error), treat the system
    as paused (return True) and alert once, rather than silently resuming buys.
    Missing a few buys is cheaper than buying blind while the kill switch is
    unreadable.
    """
    global _panic_read_fail_alerted
    try:
        val = r.get(key)
        if val is None:
            _panic_read_fail_alerted = False
            return False
        s = val.decode() if isinstance(val, bytes) else str(val)
        _panic_read_fail_alerted = False
        return s.strip() not in ("", "0", "false", "False")
    except Exception as e:
        if not _panic_read_fail_alerted:
            log.error("panic key read failed — FAIL-CLOSED, treating as paused: %s", e)
            send_telegram(
                "⚠️ Keystone panic key UNREADABLE — fail-closed, buys PAUSED. "
                f"Redis error: {e}"
            )
            _panic_read_fail_alerted = True
        return True


def _parse_signal(raw: Dict[Any, Any]) -> Optional[Dict[str, Any]]:
    payload = None
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if key in _SIGNAL_PAYLOAD_FIELDS:
            payload = v
            break
    if payload is None:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None


def _malformed_buy_signal_reason(sig: Dict[str, Any]) -> Optional[str]:
    if sig.get("action") != "BUY":
        return None
    pool = (sig.get("pool_address") or sig.get("token_address") or "").strip()
    if not pool:
        return "buy_missing_pool_or_token"
    if not _EVM_ADDR_RE.match(pool):
        return "buy_invalid_pool_or_token_address"
    return None


def _dead_letter(r: redis.Redis, cfg: ExecutorConfig, msg_id: str, error: str, sig: Optional[dict]) -> None:
    preview = json.dumps(sig or {})[:500]
    try:
        r.xadd(
            cfg.signals_dead_letter_stream,
            {
                "entry_id": msg_id,
                "error": error[:200],
                "payload": preview,
            },
            maxlen=10000,
            approximate=True,
        )
    except Exception as e:
        log.warning("dead letter xadd failed: %s", e)


BUY_BLOCKED_ALERT_TTL_SEC = int(os.environ.get("EXECUTOR_BUY_BLOCKED_ALERT_TTL_SEC", "3600"))


def _alert_buy_blocked(r: redis.Redis, alert_id: str, message: str) -> None:
    """Page once per TTL on a blocker that stops every BUY.

    A capital/sizing blocker rejects *all* signals, not a bad one. Left silent
    it looks identical to "no signals arrived": the 2026-09-06 deadlock
    (stake 0.02 ETH vs 10%-of-balance cap 0.0159 ETH) dead-lettered 15
    consecutive signals over a week with nothing but a Redis stream entry.
    """
    try:
        fresh = r.set(f"executor:buy_blocked_alert:{alert_id}", str(int(time.time())),
                      nx=True, ex=BUY_BLOCKED_ALERT_TTL_SEC)
    except Exception as e:
        log.warning("buy-blocked alert dedupe failed: %s", e)
        fresh = True
    if fresh:
        send_telegram(message)


def _balance_fraction_active(cfg: ExecutorConfig) -> bool:
    """Is the per-position "stake <= fraction of wallet" rule switched on?

    Off by default. It was written for a wallet large enough that one pool
    could not matter, but it duplicates max_portfolio_fraction (which bounds
    the same correlated-drawdown risk across *all* open positions) while adding
    a second, independent way for the buy path to close. Set
    EXECUTOR_MAX_BALANCE_FRACTION below 1.0 only if you want that second cap.
    """
    return 0.0 < cfg.max_balance_fraction < 1.0


def _check_sizing_deadlock(w3, account, r: redis.Redis, cfg: ExecutorConfig) -> None:
    """Proactive: verify the sizing rules can all be satisfied at once.

    Runs on a timer rather than on signal arrival, because in a deadlocked
    state there is nothing to distinguish "blocked" from "quiet" until a signal
    shows up — and by then a whole entry window is gone. The 2026-09-06 case
    (stake 0.02 ETH vs 10%-of-balance cap 0.0159 ETH) dead-lettered 15
    consecutive signals over a week with nothing but a Redis stream entry, so
    every pairwise combination is checked here, not just the one that broke.
    """
    if not (cfg.live_trading_mode and not cfg.dry_run) or account is None:
        return
    if cfg.max_position_eth <= 0:
        return
    try:
        balance = w3.eth.get_balance(account.address)
    except Exception as e:
        log.warning("sizing deadlock check: balance read failed: %s", e)
        return
    bal_eth = balance / 1e18
    stake = cfg.max_position_eth
    gas_buffer = 0.001
    faults = []

    if _balance_fraction_active(cfg) and stake > bal_eth * cfg.max_balance_fraction:
        faults.append(
            f"• 单笔 {stake:.4f} ETH > 余额比例上限 {bal_eth * cfg.max_balance_fraction:.4f} ETH "
            f"({100 * cfg.max_balance_fraction:.0f}% × {bal_eth:.4f})\n"
            f"  → 余额充到 ≥ {stake / cfg.max_balance_fraction:.4f} ETH，"
            f"或关掉该规则 (EXECUTOR_MAX_BALANCE_FRACTION=1.0)"
        )
    if stake + gas_buffer > bal_eth:
        faults.append(
            f"• 单笔 {stake:.4f} + gas {gas_buffer:.4f} > 余额 {bal_eth:.4f} ETH\n"
            f"  → 余额充到 ≥ {stake + gas_buffer:.4f} ETH"
        )
    if cfg.max_portfolio_fraction > 0:
        room = bal_eth * cfg.max_portfolio_fraction
        if stake > room:
            faults.append(
                f"• 单笔 {stake:.4f} ETH > 组合敞口上限 {room:.4f} ETH "
                f"({100 * cfg.max_portfolio_fraction:.0f}% × {bal_eth:.4f})\n"
                f"  → 连第一笔都开不出来"
            )
        elif cfg.max_concurrent * stake > room:
            slots = int(room / stake)
            log.info(
                "sizing: portfolio cap allows %d of %d configured slots "
                "(%.4f ETH room / %.4f ETH stake); fund to >= %.4f ETH for all %d",
                slots, cfg.max_concurrent, room, stake,
                cfg.max_concurrent * stake / cfg.max_portfolio_fraction, cfg.max_concurrent,
            )

    if not faults:
        return
    _alert_buy_blocked(
        r, "sizing_deadlock",
        "🚨 Keystone 买入通道结构性关闭 — 这会拦掉每一个信号，不是拦掉坏信号\n\n"
        + "\n".join(faults),
    )


def signals_dead_letter_ops_summary(r: redis.Redis, stream: str = "signals_dead_letter") -> Dict[str, Any]:
    """Static ops summary for observability contract."""
    try:
        length = int(r.xlen(stream))
    except Exception:
        length = -1
    return {"stream": stream, "length": length, "checked_at": datetime.now(timezone.utc).isoformat()}


def _resolve_pool(sig: Dict[str, Any]) -> Optional[PoolInfo]:
    addr = (sig.get("pool_address") or sig.get("token_address") or "").strip().lower()
    if not addr:
        return None
    return lookup(addr)


# Routine pre-trade filters that reject often and cost nothing when they do
# (no buy happened, no money at risk). These are kept SILENT on Telegram to
# avoid noise / messages that read like a buy. Everything else — honeypot,
# cluster blacklist, external risk, unsellable/sell-sim — is a defense catch
# worth knowing and is sent with an unambiguous "rejected, no buy" label.
_SILENT_REJECT_PREFIXES = ("shallow_pool", "friction", "depth_read_fail", "buy_quote_fail")


def _notify_gate_rejection(pool_addr: str, reason: str) -> None:
    """Telegram policy for a pre-trade gate rejection. Routine filters are silent;
    security/defense catches are surfaced, never worded as a BUY."""
    if any((reason or "").startswith(p) for p in _SILENT_REJECT_PREFIXES):
        return
    send_telegram(f"🛡 防御拦截·未买入 {pool_addr[:10]}… reason={reason}")


def _dry_buy(cfg: ExecutorConfig, pool: PoolInfo, stake_wei: int) -> SwapResult:
    log.info(
        "DRY_RUN BUY pool=%s stake_wei=%d (no tx sent)",
        pool.pool_address,
        stake_wei,
    )
    return SwapResult(
        success=True,
        tx_hash="0x" + "0" * 64,
        amount_in=stake_wei,
        amount_out=stake_wei,
        gas_used=0,
        gas_cost_wei=0,
    )


_eth_price_cache = {"price": 0.0, "ts": 0.0}
_ETH_PRICE_TTL_SEC = 300.0


def _live_eth_price(w3, r: redis.Redis, cfg: ExecutorConfig) -> float:
    """Live ETH/USD for position sizing.

    Uses an on-chain WETH->USDC quote (cached 5 min) so a $25 target spends a
    truly $25-worth of ETH. Falls back to the static EXECUTOR_ETH_PRICE_USD
    when the chain query fails. Publishes to redis exec:eth_price_usd for the
    Telegram bot / dashboard.
    """
    now = time.time()
    cached = _eth_price_cache["price"]
    if cached > 0 and now - _eth_price_cache["ts"] < _ETH_PRICE_TTL_SEC:
        return cached
    live = None
    try:
        live = get_eth_usd_price(w3)
    except Exception as e:
        log.debug("live eth price fetch failed: %s", e)
    if live and live > 0:
        _eth_price_cache["price"] = live
        _eth_price_cache["ts"] = now
        try:
            r.set("exec:eth_price_usd", f"{live:.2f}", ex=900)
        except Exception:
            pass
        if abs(live - cfg.eth_price_usd) / max(cfg.eth_price_usd, 1) > 0.10:
            log.warning(
                "ETH price: live=$%.2f vs static EXECUTOR_ETH_PRICE_USD=$%.2f "
                "(>10%% drift; using live for sizing)", live, cfg.eth_price_usd,
            )
        return live
    log.warning(
        "live ETH price unavailable, falling back to static $%.2f", cfg.eth_price_usd
    )
    return cfg.eth_price_usd


def _pool_weth_reserve_wei(w3, pool_info: PoolInfo) -> Optional[int]:
    """Live WETH reserve (depth) of the pool via ERC20 balanceOf(WETH, pool).

    Works for v2/v3/aero pools (all physically custody WETH at the pool address)
    and is evaluated on pre-trade chain state, so it is causal (no look-ahead).
    """
    try:
        bal = _get_token_balance(w3, WETH_BASE, pool_info.pool_address)
        return int(bal) if bal and bal > 0 else None
    except Exception as e:
        log.warning("pool WETH reserve fetch failed pool=%s: %s", pool_info.pool_address, e)
        return None


def _signal_pre_volume_wei(sig: Optional[Dict[str, Any]]) -> Optional[int]:
    """Pre-entry WETH volume (wei) carried by the bridge signal, if present."""
    if not sig:
        return None
    for k in ("pre_volume_eth_wei", "pre_volume_wei", "pre_volume_eth"):
        v = sig.get(k)
        if v in (None, "", 0, "0"):
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None


def _dynamic_stake_wei(
    w3,
    pool_info: PoolInfo,
    available_wei: int,
    eth_price: float,
    cfg: ExecutorConfig,
    sig: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[int, float, float]]:
    """Continuous causal position sizing (no look-ahead).

    Instead of a fixed $100/$300/$1000 ladder, this finds the LARGEST notional that
    simultaneously satisfies, all on pre-trade chain state:
      1. cash:        stake + gas buffer <= available_wei
      2. pool share:  stake <= max_pool_share * pool WETH reserve (depth cap)
      3. volume share:stake <= max_volume_share * pre-entry WETH volume (if known)
      4. friction:    QuoterV2 round-trip friction >= friction_min at that exact size
      5. account cap: stake <= max_position_usd

    Friction degrades monotonically with size (bigger size => more price impact =>
    worse round-trip), so we binary-search the friction boundary between the floor
    size and the hard ceiling from caps 1-3-5. Caps 2/3 are the explicit depth and
    butterfly-effect guards: they keep us from becoming an outsized share of the
    pool or of recent flow. Returns None if even the floor size fails any gate.

    QuoterV2 + balanceOf are view calls on pre-trade state, so this is causal.
    """
    gas_buffer = int(0.001 * 1e18)

    # ETH-native sizing (preferred): the ceiling/floor come straight from ETH
    # config, so the critical path needs no ETH/USD oracle. USD is only used for
    # the human-readable log line below. Falls back to USD knobs when unset.
    if cfg.max_position_eth > 0:
        ceil_wei = int(cfg.max_position_eth * 1e18)
    else:
        ceil_wei = int(cfg.max_position_usd / max(eth_price, 1) * 1e18)
    hi = min(ceil_wei, max(0, available_wei - gas_buffer))
    cap_reason = "account_cap" if ceil_wei <= hi else "cash"

    reserve = _pool_weth_reserve_wei(w3, pool_info)
    if reserve and cfg.max_pool_share > 0:
        pool_cap = int(reserve * cfg.max_pool_share)
        if pool_cap < hi:
            hi, cap_reason = pool_cap, "pool_share"

    vol_wei = _signal_pre_volume_wei(sig)
    if vol_wei and cfg.max_volume_share > 0:
        vol_cap = int(vol_wei * cfg.max_volume_share)
        if vol_cap < hi:
            hi, cap_reason = vol_cap, "volume_share"

    if cfg.size_min_eth > 0:
        lo = int(cfg.size_min_eth * 1e18)
    else:
        lo = int(cfg.size_min_usd / max(eth_price, 1) * 1e18)
    if lo <= 0:
        return None
    if hi < lo:
        log.info(
            "dyn-size caps below floor pool=%s lo_wei=%d hi_wei=%d cap=%s reserve=%s",
            pool_info.pool_address, lo, hi, cap_reason, reserve,
        )
        return None

    # Floor must clear friction or the pool is untradeable at any size we'd take.
    g_lo = friction_gate(w3, pool_info, lo, friction_min=cfg.friction_min)
    if not g_lo.ok:
        log.info("dyn-size floor fails friction pool=%s fric=%s reason=%s",
                 pool_info.pool_address, g_lo.friction, g_lo.reason)
        return None

    # If the hard ceiling already clears friction, take it (largest feasible).
    g_hi = friction_gate(w3, pool_info, hi, friction_min=cfg.friction_min)
    if g_hi.ok:
        best, best_fric = hi, g_hi.friction
    else:
        a, b = lo, hi
        best, best_fric = lo, g_lo.friction
        for _ in range(max(1, cfg.size_bisect_iters)):
            mid = (a + b) // 2
            if mid <= a:
                break
            g = friction_gate(w3, pool_info, mid, friction_min=cfg.friction_min)
            if g.ok:
                best, best_fric = mid, g.friction
                a = mid
            else:
                b = mid

    stake_usd = best / 1e18 * eth_price
    share = (best / reserve) if reserve else 0.0
    log.info(
        "dyn-size chosen pool=%s size=$%.0f friction=%.4f cap=%s pool_share=%.3f",
        pool_info.pool_address, stake_usd, float(best_fric or 0.0), cap_reason, share,
    )
    return best, float(stake_usd), float(best_fric or 0.0)


def _pool_info_from_position(pos: OpenPosition) -> Optional[PoolInfo]:
    """Reconstruct sell routing metadata from the open position as a last resort.

    Live positions are opened from dynamic Redis pool metadata. If that Redis
    `pool:discovered:*` hash expires or is deleted while we are holding, `lookup()`
    can fail and the monitor would otherwise skip the exit. Persisting token0/token1
    on open lets the executor still sell the position it already bought.
    """
    router = ROUTER_FOR_POOL_TYPE.get(pos.pool_type)
    if router is None:
        return None
    t0 = pos.token0_address.lower()
    t1 = pos.token1_address.lower()
    if not t0 or not t1:
        return None
    if t0 != WETH_BASE.lower() and t1 != WETH_BASE.lower():
        return None
    return PoolInfo(
        pool_address=pos.pool_address.lower(),
        pool_type=pos.pool_type,
        router=router,
        token0=t0,
        token0_symbol=pos.token0_symbol or "?",
        token0_decimals=int(pos.token0_decimals or 18),
        token1=t1,
        token1_symbol=pos.token1_symbol or "?",
        token1_decimals=int(pos.token1_decimals or 18),
        fee_bps=int(pos.fee_tier or 0),
    )


def _pool_info_from_pending(rec: Dict[str, str]) -> Optional[PoolInfo]:
    """Rebuild sell-routing metadata from a persisted pending-buy intent."""
    router = ROUTER_FOR_POOL_TYPE.get(rec.get("pool_type", ""))
    if router is None:
        return None
    t0 = (rec.get("token0_address") or "").lower()
    t1 = (rec.get("token1_address") or "").lower()
    if not t0 or not t1:
        return None
    if t0 != WETH_BASE.lower() and t1 != WETH_BASE.lower():
        return None
    return PoolInfo(
        pool_address=(rec.get("pool_address") or "").lower(),
        pool_type=rec.get("pool_type", ""),
        router=router,
        token0=t0,
        token0_symbol=rec.get("token0_symbol", "?"),
        token0_decimals=int(rec.get("token0_decimals", "18") or 18),
        token1=t1,
        token1_symbol=rec.get("token1_symbol", "?"),
        token1_decimals=int(rec.get("token1_decimals", "18") or 18),
        fee_bps=int(rec.get("fee_tier", "0") or 0),
    )


def _reconcile_orphan_buys(w3, account, store: PositionStore, cfg: ExecutorConfig) -> int:
    """P0: replay pending-buy intents against real wallet balances on startup.

    Catches the crash window between a successful on-chain buy and open_position():
    any token we hold that is not already an open position is adopted so the
    monitor/exit loop takes it over (never an unmonitored orphan).
    Returns the number of orphans recovered.
    """
    recovered = 0
    try:
        pending = store.list_pending_buys()
    except Exception as e:
        log.warning("orphan reconcile: list_pending_buys failed: %s", e)
        return 0
    if not pending:
        return 0
    log.info("orphan reconcile: %d pending-buy intents to check", len(pending))
    for rec in pending:
        pool = (rec.get("pool_address") or "").lower()
        if not pool:
            continue
        if store.has_open(pool):
            store.clear_pending_buy(pool)
            continue
        if cfg.dry_run or not cfg.live_trading_mode or w3 is None or account is None:
            store.clear_pending_buy(pool)
            continue
        pi = lookup(pool) or _pool_info_from_pending(rec)
        if pi is None:
            log.error("orphan reconcile: cannot rebuild route pool=%s — MANUAL CHECK", pool)
            send_telegram(
                f"⚠️ Keystone 启动对账失败\npool={pool}\n无法重建卖出路由,请手动检查钱包是否持有该 token"
            )
            continue
        try:
            bal = _get_token_balance(w3, pi.non_weth_token, account.address)
        except Exception as e:
            log.warning("orphan reconcile: balance check failed pool=%s: %s", pool, e)
            continue
        if bal and bal > 0:
            stake_wei = int(rec.get("stake_wei", "0") or 0)
            now_ms = int(time.time() * 1000)
            pos = OpenPosition(
                pool_address=pool,
                pool_type=pi.pool_type,
                token_address=pi.non_weth_token.lower(),
                token_decimals=pi.non_weth_decimals,
                token_amount=int(bal),
                entry_eth_wei=stake_wei or int(bal),
                entry_quote_eth_wei=stake_wei or int(bal),
                opened_at_ms=now_ms,
                buy_tx_hash=rec.get("buy_tx_hash", ""),
                peak_eth_value=stake_wei or int(bal),
                fee_tier=pi.fee_bps,
                size_usd=float(rec.get("size_usd", "0") or 0),
                token0_address=pi.token0,
                token0_symbol=pi.token0_symbol,
                token0_decimals=pi.token0_decimals,
                token1_address=pi.token1,
                token1_symbol=pi.token1_symbol,
                token1_decimals=pi.token1_decimals,
            )
            store.open_position(pos)
            store.clear_pending_buy(pool)
            recovered += 1
            log.error("ORPHAN RECOVERED pool=%s token=%s bal=%s — now monitored", pool, pi.non_weth_symbol, bal)
            send_telegram(
                f"⚠️ Keystone 启动对账: 发现孤儿仓 {pi.non_weth_symbol}\n"
                f"pool={pool}\nbalance={bal}\n已接管监控,进入正常退出逻辑"
            )
        else:
            store.clear_pending_buy(pool)
            log.info("orphan reconcile: no balance, clearing stale intent pool=%s", pool)
    return recovered


def _handle_signal(
    w3,
    account,
    r: redis.Redis,
    store: PositionStore,
    cfg: ExecutorConfig,
    flags: Dict[str, Any],
    sig: Dict[str, Any],
    msg_id: str,
) -> None:
    if sig.get("action") != "BUY":
        log.info("skip non-BUY signal action=%s msg_id=%s", sig.get("action"), msg_id)
        return

    # Signal staleness gate (runs before any RPC/registry work): a backlog replay
    # after downtime, or a long-pending recovered message, must never trigger a buy.
    if SIGNAL_MAX_AGE_SEC > 0:
        enq_ms = _parse_signal_enqueue_ms(msg_id)
        if enq_ms is not None:
            age_sec = max(0.0, time.time() - enq_ms / 1000.0)
            if age_sec > SIGNAL_MAX_AGE_SEC:
                log.warning(
                    "skip STALE signal age=%.0fs > %ds pool=%s src=%s msg_id=%s",
                    age_sec, int(SIGNAL_MAX_AGE_SEC), sig.get("pool_address"),
                    sig.get("signal_source"), msg_id,
                )
                _dead_letter(r, cfg, msg_id, f"stale_signal_{int(age_sec)}s", sig)
                return

    reason = _malformed_buy_signal_reason(sig)
    if reason:
        _dead_letter(r, cfg, msg_id, reason, sig)
        return

    if sig.get("signal_source") == "pool_score":
        log.info("skip pool_score shadow signal pool=%s", sig.get("pool_address"))
        return

    chain = sig.get("chain_id")
    if chain is not None and int(chain) != cfg.chain_id:
        log.info("skip chain mismatch got=%s want=%s", chain, cfg.chain_id)
        return

    conf = float(sig.get("confidence", 0) or 0)
    if conf < cfg.min_confidence:
        log.info("skip low confidence %.3f < %.3f", conf, cfg.min_confidence)
        return

    pool_info = _resolve_pool(sig)
    if pool_info is None:
        log.warning("pool not in registry: %s", sig.get("pool_address"))
        return

    pool_addr = pool_info.pool_address.lower()
    if store.has_open(pool_addr):
        log.info("skip duplicate open position pool=%s", pool_addr)
        return
    if store.count_open() >= cfg.max_concurrent:
        log.info("skip max concurrent positions=%d", cfg.max_concurrent)
        return

    # Portfolio-level exposure gate: bound total simultaneous capital at risk so
    # a correlated -50% drawdown across many open positions is capped. Per-position
    # sizing alone does not constrain aggregate exposure.
    if _portfolio_exposure_exceeded(store, cfg, w3, account):
        send_telegram(
            f"⚠️ Keystone BUY skipped — portfolio exposure cap "
            f"({100 * cfg.max_portfolio_fraction:.0f}% of wallet) reached. pool={pool_addr}"
        )
        _dead_letter(r, cfg, msg_id, "portfolio_exposure_cap", sig)
        return

    if _check_daily_loss(r, store, cfg, w3, account):
        log.warning("skip BUY: daily loss limit breached")
        return

    eth_price = _live_eth_price(w3, r, cfg)
    # ETH-native fixed stake (preferred) keeps sizing off the oracle. When
    # max_position_eth is unset, fall back to the USD target / live price.
    if cfg.max_position_eth > 0:
        fixed_wei = int(cfg.max_position_eth * 1e18)
        fixed_usd = fixed_wei / 1e18 * eth_price  # display only
    else:
        fixed_usd = min(
            float(sig.get("position_size_usd") or sig.get("position_size") or cfg.max_position_usd),
            cfg.max_position_usd,
        )
        fixed_wei = int(fixed_usd / max(eth_price, 1) * 1e18)

    # Capital available for cash-constrained sizing. Live: balance * fraction.
    available_wei: Optional[int] = None
    if account is not None and cfg.live_trading_mode and not cfg.dry_run:
        try:
            _bal = w3.eth.get_balance(account.address)
            available_wei = int(_bal * max(0.0, min(cfg.max_balance_fraction, 1.0)))
        except Exception as e:
            log.warning("balance fetch for sizing failed: %s", e)

    if cfg.dynamic_sizing and w3 is not None:
        cap_wei = available_wei if available_wei is not None else (1 << 255)
        dyn = _dynamic_stake_wei(w3, pool_info, cap_wei, eth_price, cfg, sig=sig)
        if dyn is None:
            log.warning("BUY skipped: no size clears friction/depth/volume gates pool=%s", pool_addr)
            _dead_letter(r, cfg, msg_id, "dynamic_no_size_passes_gate", sig)
            return
        stake_wei, stake_usd, _dyn_fric = dyn
    else:
        stake_wei = fixed_wei
        stake_usd = fixed_usd

    if cfg.dry_run or not cfg.live_trading_mode:
        result = _dry_buy(cfg, pool_info, stake_wei)
    else:
        # Balance check: ensure enough ETH for stake + gas buffer (~0.001 ETH)
        gas_buffer = int(0.001 * 1e18)
        try:
            balance = w3.eth.get_balance(account.address)
            max_by_balance = int(balance * max(0.0, min(cfg.max_balance_fraction, 1.0)))
            if _balance_fraction_active(cfg) and stake_wei > max_by_balance:
                log.warning("BUY blocked: stake exceeds balance fraction stake=%d cap=%d fraction=%.3f",
                            stake_wei, max_by_balance, cfg.max_balance_fraction)
                _dead_letter(r, cfg, msg_id, "max_balance_fraction_exceeded", sig)
                _alert_buy_blocked(
                    r, "max_balance_fraction",
                    f"🚨 Keystone BUY 被资金上限拦掉 — 每个信号都会被拦\n"
                    f"单笔 {stake_wei / 1e18:.4f} ETH > 上限 {max_by_balance / 1e18:.4f} ETH "
                    f"({100 * cfg.max_balance_fraction:.0f}% × {balance / 1e18:.4f} ETH)\n"
                    f"pool={pool_addr}",
                )
                return
            if balance < stake_wei + gas_buffer:
                log.warning("BUY blocked: insufficient ETH balance=%d need=%d+%d",
                            balance, stake_wei, gas_buffer)
                _dead_letter(r, cfg, msg_id, "insufficient_balance", sig)
                _alert_buy_blocked(
                    r, "insufficient_balance",
                    f"🚨 Keystone BUY 余额不足 — 每个信号都会被拦\n"
                    f"余额 {balance / 1e18:.4f} ETH < 单笔 {stake_wei / 1e18:.4f} ETH "
                    f"+ gas {gas_buffer / 1e18:.4f} ETH\n"
                    f"pool={pool_addr}",
                )
                return
        except Exception as e:
            log.warning("balance check failed, proceeding cautiously: %s", e)

        gate = run_pretrade_gates(w3, pool_info, stake_wei, wallet=account.address)
        if not gate.ok:
            log.warning("BUY blocked by pretrade gate pool=%s reason=%s", pool_addr, gate.reason)
            _notify_gate_rejection(pool_addr, gate.reason)
            _dead_letter(r, cfg, msg_id, f"pretrade_{gate.reason}", sig)
            return

        # P2-1: external risk data was missing (unknown new token) -> canary-size
        # instead of full size. Smaller stake only improves friction, so it stays
        # within the gate we just cleared. In ETH-native mode the canary floor is
        # the ETH floor (oracle-free); otherwise convert the USD canary at price.
        if gate.canary_usd and gate.canary_usd > 0:
            if cfg.canary_eth > 0:
                canary_wei = int(cfg.canary_eth * 1e18)
            else:
                canary_wei = int(gate.canary_usd / max(eth_price, 1) * 1e18)
            if 0 < canary_wei < stake_wei:
                log.warning("external-risk canary: capping stake %d->%d wei (~$%.0f) pool=%s",
                            stake_wei, canary_wei, gate.canary_usd, pool_addr)
                stake_wei = canary_wei
                stake_usd = stake_wei / 1e18 * eth_price
        # P0: durably record the buy INTENT before broadcasting. If we crash in
        # the window between an on-chain buy and store.open_position(), startup
        # reconciliation replays this against the wallet balance so the token can
        # never become an unmonitored orphan.
        store.record_pending_buy(pool_addr, {
            "pool_address": pool_addr,
            "pool_type": pool_info.pool_type,
            "token0_address": pool_info.token0,
            "token0_symbol": pool_info.token0_symbol,
            "token0_decimals": pool_info.token0_decimals,
            "token1_address": pool_info.token1,
            "token1_symbol": pool_info.token1_symbol,
            "token1_decimals": pool_info.token1_decimals,
            "fee_tier": pool_info.fee_bps,
            "stake_wei": stake_wei,
            "size_usd": f"{stake_usd:.6f}",
            "recorded_at_ms": int(time.time() * 1000),
        })
        flags["REAL_TX_SENT"] = True
        preflight.assert_no_live_tx(flags)
        result = execute_buy(w3, account, pool_info, stake_wei)

    if not result.success:
        log.error("BUY failed pool=%s err=%s", pool_addr, result.error)
        store.clear_pending_buy(pool_addr)
        _dead_letter(r, cfg, msg_id, f"buy_failed: {result.error}", sig)
        if cfg.live_trading_mode:
            send_telegram(f"BUY failed {pool_addr[:10]}… err={result.error}")
        return

    if (result.amount_out or 0) <= 0:
        recovered = 0
        if result.success and result.tx_hash and w3 is not None and account is not None:
            recovered = recover_buy_amount_out(w3, pool_info, account, result.tx_hash)
        if recovered > 0:
            log.warning(
                "BUY amount_out recovered from receipt pool=%s amount=%s tx=%s",
                pool_addr, recovered, result.tx_hash,
            )
            result = SwapResult(
                success=result.success,
                tx_hash=result.tx_hash,
                amount_in=result.amount_in,
                amount_out=recovered,
                gas_used=result.gas_used,
                gas_cost_wei=result.gas_cost_wei,
                error=result.error,
            )
        else:
            # CRITICAL safety: a successful on-chain buy must NEVER become an
            # unmonitored orphan. recover_buy_amount_out already tried receipt
            # logs + one balanceOf; do a final RETRIED balanceOf to defeat
            # Flashblocks/RPC lag before we give up. Only abandon if the wallet
            # genuinely holds zero (i.e. the "success" was a cancel/replace tx).
            final_bal = 0
            if w3 is not None and account is not None:
                for delay_sec in (0.0, 0.5, 1.0, 2.0):
                    if delay_sec:
                        time.sleep(delay_sec)
                    try:
                        final_bal = _get_token_balance(w3, pool_info.non_weth_token, account.address)
                    except Exception as e:
                        log.warning("post-buy balance check failed pool=%s: %s", pool_addr, e)
                        final_bal = 0
                    if final_bal > 0:
                        break
            if final_bal > 0:
                log.warning(
                    "BUY amount_out recovered via retried balanceOf pool=%s amount=%s tx=%s",
                    pool_addr, final_bal, result.tx_hash,
                )
                result = SwapResult(
                    success=result.success,
                    tx_hash=result.tx_hash,
                    amount_in=result.amount_in,
                    amount_out=final_bal,
                    gas_used=result.gas_used,
                    gas_cost_wei=result.gas_cost_wei,
                    error=result.error,
                )
            else:
                log.error(
                    "BUY succeeded on-chain but wallet holds 0 tokens after retries — "
                    "treating as cancelled/replaced tx (no position). pool=%s tx=%s",
                    pool_addr, result.tx_hash,
                )
                if cfg.live_trading_mode:
                    send_telegram(
                        f"⚠️ CRITICAL Keystone BUY anomaly {pool_info.non_weth_symbol}\n"
                        f"pool={pool_addr}\n"
                        f"tx={result.tx_hash}\n"
                        f"buy tx succeeded but 0 tokens in wallet after retries.\n"
                        f"No position opened. MANUAL CHECK REQUIRED."
                    )
                store.clear_pending_buy(pool_addr)
                _dead_letter(r, cfg, msg_id, "buy_success_zero_tokens", sig)
                return

    now_ms = int(time.time() * 1000)
    pos = OpenPosition(
        pool_address=pool_addr,
        pool_type=pool_info.pool_type,
        token_address=pool_info.non_weth_token.lower(),
        token_decimals=pool_info.non_weth_decimals,
        token_amount=int(result.amount_out or 0),
        entry_eth_wei=int(result.amount_in or stake_wei),
        entry_quote_eth_wei=int(result.amount_in or stake_wei),
        opened_at_ms=now_ms,
        buy_tx_hash=result.tx_hash or "",
        peak_eth_value=int(result.amount_in or stake_wei),
        fee_tier=pool_info.fee_bps,
        size_usd=stake_usd,
        token0_address=pool_info.token0,
        token0_symbol=pool_info.token0_symbol,
        token0_decimals=pool_info.token0_decimals,
        token1_address=pool_info.token1,
        token1_symbol=pool_info.token1_symbol,
        token1_decimals=pool_info.token1_decimals,
    )
    store.open_position(pos)
    store.clear_pending_buy(pool_addr)
    log.info(
        "position opened pool=%s dry_run=%s tx=%s",
        pool_addr,
        cfg.dry_run,
        result.tx_hash,
    )
    mode = "DRY_RUN" if cfg.dry_run else "LIVE"
    # P0.5 execution-quality telemetry (observability only; strategy unchanged).
    eq_text = _record_exec_quality(
        r,
        kind="buy",
        pool_info=pool_info,
        expected_out=result.expected_out,
        actual_out=int(result.amount_out or 0),
        signal_ms=_parse_signal_enqueue_ms(msg_id),
        fill_ms=int(time.time() * 1000),
        sig_block=sig.get("block_number") or sig.get("block") or sig.get("entry_block"),
        fill_block=result.block_number,
        reverted=not result.success,
        gas_used=result.gas_used,
        gas_cost_wei=result.gas_cost_wei,
        eth_price=eth_price,
        tx_hash=result.tx_hash or "",
        size_usd=stake_usd,
        pool_created_block=sig.get("created_block") or sig.get("pool_created_block"),
    )
    send_telegram(
        f"Keystone BUY [{mode}] {pool_info.pool_type}\n"
        f"token={pool_info.non_weth_symbol}\n"
        f"pool={pool_addr}\n"
        f"size=${stake_usd:.0f}\n"
        f"tx={result.tx_hash or 'dry-run'}\n"
        f"{eq_text}"
    )


_mark_fail_count: Dict[str, int] = {}
_sell_fail_count: Dict[str, int] = {}


def _should_alert_on_sell_fail(count: int) -> bool:
    """Throttle repeat SELL-failure alerts to the 3rd, 10th and 30th attempt, then
    every 50th. A stuck sell retries every monitor cycle, so alerting on each one
    buries the operator and hides any other alert."""
    if count in (3, 10, 30):
        return True
    return count > 30 and count % 50 == 0


_route_fail_count: Dict[str, int] = {}
_last_realgas_check_ms: Dict[str, int] = {}
_MARK_FAIL_PANIC_THRESHOLD = 10  # ~50s at 5s interval → force exit
_ROUTE_FAIL_PANIC_THRESHOLD = 60  # ~5min at 5s interval of an unsellable, unroutable position
_REALGAS_RECHECK_MS = 60_000  # P1: re-run real-gas sell sim at most once per 60s per pool
_UNSALEABLE_SELL_ERRORS = frozenset({
    "sell_no_eth_received",
    "sell_zero_token_balance",
    "sell_quote_failed_fail_closed",
})


def _blacklist_cluster_on_abandon(w3, pool_info: Optional[PoolInfo], reason: str) -> Optional[str]:
    """F feed: when a position is abandoned (honeypot/rug), record its token's
    bytecode-skeleton cluster so the next repeat from the same source is blocked
    pre-entry. Bytecode skeleton is the reliable clone key (the two PARADAENGI
    rugs shared a skeleton but used different deployer EOAs). Best-effort."""
    if pool_info is None or w3 is None:
        return None
    try:
        rec = cluster_blacklist.record(get_redis(), w3, pool_info, f"abandon:{reason}")
        return rec.get("bytecode")
    except Exception as e:
        log.warning("cluster blacklist on abandon failed pool=%s: %s",
                    getattr(pool_info, "pool_address", "?"), e)
        return None


def _abandon_unsellable_position(
    store: PositionStore,
    pool_addr: str,
    entry_wei: int,
    mark_wei: int,
    symbol: str,
    reason: str,
    w3=None,
    pool_info: Optional[PoolInfo] = None,
) -> None:
    """Drop tracking for tokens that cannot be sold (honeypot/rug). Keeps tokens
    in wallet but stops gas burn, and blacklists the cluster (F) so repeats from
    the same source are blocked pre-entry."""
    exit_wei = max(mark_wei, 0)
    delta = exit_wei - entry_wei
    _record_realized_pnl(store, delta)
    store.close_position(pool_addr)
    store.clear_pending_buy(pool_addr)
    _sell_fail_count.pop(pool_addr, None)
    _mark_fail_count.pop(pool_addr, None)
    _route_fail_count.pop(pool_addr, None)
    _last_realgas_check_ms.pop(pool_addr, None)
    pnl_pct = 100.0 * delta / max(entry_wei, 1)
    bl = _blacklist_cluster_on_abandon(w3, pool_info, reason)
    bl_line = ""
    if bl:
        bl_line = f"\ncluster blacklisted (bytecode {bl[:14]}…) — repeats blocked pre-entry"
    sym = getattr(pool_info, "non_weth_symbol", None) if pool_info else None
    if sym:
        bl_line += f"\nsymbol {sym} blacklisted — all same-name signals rejected"
    log.error("ABANDON unsellable pool=%s reason=%s pnl=%.1f%% blacklist=%s",
              pool_addr, reason, pnl_pct, bl)
    send_telegram(
        f"🚨 CRITICAL Keystone ABANDON {symbol}\n"
        f"pool={pool_addr}\n"
        f"reason={reason}\n"
        f"pnl={pnl_pct:+.1f}% ({delta/1e18:+.6f} ETH)\n"
        f"unsellable — stopped sell retries; tokens remain in wallet{bl_line}"
    )


def _should_abandon_after_sell_fail(w3, account, pool_info: PoolInfo, token_amount: int, error: str) -> bool:
    err = (error or "").lower()
    trigger = (err in _UNSALEABLE_SELL_ERRORS) or ("reverted" in err) or ("gas estimation failed" in err)
    if not trigger:
        return False
    # Authoritative check: replay a real-gas sell of the actual holding. A revert
    # at realistic gas == truly unsellable (gas-bomb/honeypot/rug) -> abandon.
    # This catches gas-bomb honeypots that the high-gas check_honeypot misses.
    try:
        amt = int(token_amount or 0)
        if account is not None and amt > 0:
            ok, reason = simulate_real_sell(w3, pool_info, amt, wallet=account.address)
            if ok:
                return False  # sellable at real gas -> failure was transient, keep retrying
            if not (reason.startswith("sim_unavailable") or reason.startswith("sim_rpc_error")):
                return True   # definitive real-gas revert -> abandon
    except Exception as e:
        log.warning("abandon real-gas sim failed pool=%s: %s", pool_info.pool_address, e)
    # sim could not run -> fall back to the quote-based honeypot check
    return check_honeypot(w3, pool_info.non_weth_token, pool_info)


def _finalize_close(
    store: PositionStore,
    pool_addr: str,
    entry_wei: int,
    exit_wei: int,
    exit_reason: str,
    dry_run: bool,
    symbol: str = "?",
    tx_hash: str = "",
    *,
    r: Optional[redis.Redis] = None,
    pool_info: Optional[PoolInfo] = None,
    expected_out: Optional[int] = None,
    gas_used: Optional[int] = None,
    gas_cost_wei: Optional[int] = None,
    fill_block: Optional[int] = None,
    eth_price: float = 0.0,
    exit_trigger_ms: Optional[int] = None,
    exit_trigger_block: Optional[int] = None,
) -> None:
    delta = exit_wei - entry_wei
    _record_realized_pnl(store, delta)
    store.close_position(pool_addr)
    _sell_fail_count.pop(pool_addr, None)
    _route_fail_count.pop(pool_addr, None)
    _last_realgas_check_ms.pop(pool_addr, None)
    pnl_pct = 100.0 * delta / max(entry_wei, 1)
    pnl_eth = delta / 1e18
    mode = "DRY_RUN" if dry_run else "LIVE"
    # P0.5 sell-side execution quality: realized ETH out vs the QuoterV2 sell
    # quote (expected_out=mark) at decision time — the other half of the
    # round-trip the backtest assumes is frictionless. expected_out is None for
    # blind-mark panics (no reliable quote), so deviation is reported n/a there.
    eq_line = ""
    if r is not None:
        eq_text = _record_exec_quality(
            r, kind="sell",
            pool_info=pool_info or _stub_pool_for_symbol(pool_addr, symbol),
            expected_out=expected_out, actual_out=int(exit_wei),
            signal_ms=None, fill_ms=int(time.time() * 1000),
            sig_block=None, fill_block=fill_block,
            reverted=False, gas_used=gas_used, gas_cost_wei=gas_cost_wei,
            eth_price=eth_price, tx_hash=tx_hash,
            size_usd=(exit_wei / 1e18) * (eth_price or 0.0),
            exit_trigger_ms=exit_trigger_ms, exit_trigger_block=exit_trigger_block,
        )
        eq_line = "\n" + eq_text
    send_telegram(
        f"Keystone SELL [{mode}] {symbol}\n"
        f"pool={pool_addr}\n"
        f"reason={exit_reason}\n"
        f"pnl={pnl_pct:+.1f}% ({pnl_eth:+.6f} ETH)\n"
        f"tx={tx_hash or 'dry-run'}"
        f"{eq_line}"
    )


class _StubPool:
    """Minimal pool stand-in for telemetry when full PoolInfo is unavailable."""
    def __init__(self, pool_address: str, symbol: str):
        self.pool_address = pool_address
        self.non_weth_symbol = symbol


def _stub_pool_for_symbol(pool_addr: str, symbol: str):
    return _StubPool(pool_addr, symbol)


def _mark_eth_wei(w3, pool_info: PoolInfo, pos: OpenPosition) -> int:
    """Live mark: QuoterV2 sell quote (accurate) with slot0 fallback (fast).
    Tracks consecutive failures — if RPC is blind for too long, returns -1 to signal forced exit."""
    pool_addr = pos.pool_address
    if pos.token_amount <= 0:
        return 0
    q = quote_sell_eth_wei(w3, pool_info, pos.token_amount)
    if q > 0:
        _mark_fail_count[pool_addr] = 0
        return q
    s = get_slot0_price_eth(w3, pool_info, pos.token_amount)
    if s > 0:
        _mark_fail_count[pool_addr] = 0
        return s
    fails = _mark_fail_count.get(pool_addr, 0) + 1
    _mark_fail_count[pool_addr] = fails
    if fails >= _MARK_FAIL_PANIC_THRESHOLD:
        log.error("BLIND MARK: %d consecutive failures for %s — forcing exit", fails, pool_addr)
        return -1
    log.warning("mark failed (%d/%d) for %s, using entry as fallback",
                fails, _MARK_FAIL_PANIC_THRESHOLD, pool_addr)
    return pos.entry_eth_wei


def _safe_block_number(w3) -> Optional[int]:
    """Best-effort current block for EXIT-latency telemetry only. Fail-soft to None;
    never raises into the trade path and never affects any exit decision."""
    try:
        return int(w3.eth.block_number) if w3 is not None else None
    except Exception:
        return None


def _monitor_once(w3, account, r: redis.Redis, store: PositionStore, cfg: ExecutorConfig, flags: Dict[str, Any]) -> None:
    now_ms = int(time.time() * 1000)
    for pool_addr in store.list_open_pools():
        pos = store.get(pool_addr)
        if pos is None:
            continue
        age_sec = (now_ms - pos.opened_at_ms) / 1000.0
        entry = max(pos.entry_eth_wei, 1)
        peak = max(pos.peak_eth_value, entry)

        pool_info = lookup(pool_addr) or _pool_info_from_position(pos)
        if pool_info is None:
            fails = _route_fail_count.get(pool_addr, 0) + 1
            _route_fail_count[pool_addr] = fails
            log.error(
                "cannot monitor/sell pool=%s: missing registry and position route metadata (x%d)",
                pool_addr, fails,
            )
            if fails in (1, 5) or fails % 20 == 0:
                send_telegram(
                    f"⚠️ Keystone 监控异常 pool={pool_addr}\n"
                    f"无法重建卖出路由 (第{fails}次),该仓位无法被卖出,请手动检查"
                )
            if fails >= _ROUTE_FAIL_PANIC_THRESHOLD:
                _set_panic(r, cfg.panic_key, f"unroutable_position_{pool_addr[:10]}")
            continue
        _route_fail_count.pop(pool_addr, None)

        if not cfg.dry_run and w3 is not None:
            try:
                if check_honeypot(w3, pool_info.non_weth_token, pool_info):
                    mark = quote_sell_eth_wei(w3, pool_info, pos.token_amount) or 0
                    _abandon_unsellable_position(
                        store, pool_addr, entry, mark, pool_info.non_weth_symbol, "honeypot_detected",
                        w3=w3, pool_info=pool_info,
                    )
                    continue
            except Exception as e:
                log.warning("honeypot pre-check failed pool=%s: %s", pool_addr, e)
            # E (post-buy escape) + P1 real-gas recheck. The quote-based check
            # above cannot see gas-bomb honeypots or LP-pulls (they quote healthy
            # at unlimited gas), so we replay a REAL-gas sell of the actual
            # holding. Inside the post-buy window we probe every
            # POSTBUY_RECHECK_BLOCKS blocks (fast) and force an immediate probe on
            # a WETH-depth collapse; after the window we fall back to the 60s
            # cadence. On a confirmed unsellable -> ABANDON + blacklist + alert.
            in_postbuy = POSTBUY_RECHECK_WINDOW_SEC > 0 and age_sec <= POSTBUY_RECHECK_WINDOW_SEC
            recheck_ms = POSTBUY_RECHECK_MS if in_postbuy else _REALGAS_RECHECK_MS
            depth_collapsed = False
            if in_postbuy and pos.token_amount > 0:
                res = _pool_weth_reserve_wei(w3, pool_info)
                if res is not None and res < POSTBUY_DEPTH_COLLAPSE_WEI:
                    depth_collapsed = True
                    log.error("POSTBUY depth-collapse pool=%s weth=%.4fE < %.2fE — forcing sell probe",
                              pool_addr, res / 1e18, POSTBUY_DEPTH_COLLAPSE_ETH)
            last = _last_realgas_check_ms.get(pool_addr, 0)
            due = (now_ms - last >= recheck_ms) or depth_collapsed
            if due and pos.token_amount > 0:
                _last_realgas_check_ms[pool_addr] = now_ms
                try:
                    ok, why = simulate_real_sell(
                        w3, pool_info, pos.token_amount,
                        wallet=account.address if account is not None else None,
                    )
                    if (not ok) and not (why.startswith("sim_unavailable") or why.startswith("sim_rpc_error")):
                        mark = quote_sell_eth_wei(w3, pool_info, pos.token_amount) or 0
                        tag = "postbuy_escape" if in_postbuy else "realgas_unsellable"
                        why2 = f"depth_collapse:{why}" if depth_collapsed else why
                        _abandon_unsellable_position(
                            store, pool_addr, entry, mark, pool_info.non_weth_symbol,
                            f"{tag}:{why2}", w3=w3, pool_info=pool_info,
                        )
                        _last_realgas_check_ms.pop(pool_addr, None)
                        continue
                except Exception as e:
                    log.warning("realgas honeypot recheck failed pool=%s: %s", pool_addr, e)

        if cfg.dry_run or w3 is None:
            mark = entry
        else:
            mark = _mark_eth_wei(w3, pool_info, pos)
            if mark == -1:
                # Blind mark panic: force exit to prevent unmonitored loss
                should_exit = True
                exit_reason = "blind_mark_panic"
                mark = entry  # use entry for PnL logging only
                pnl_pct = 0.0
                # EXIT-latency telemetry only (does not affect this decision)
                _exit_trigger_ms = now_ms
                _exit_trigger_block = _safe_block_number(w3)
                # skip normal exit logic below
                if not store.try_mark_pending_sell(pool_addr, now_ms, PENDING_SELL_TTL_MS):
                    continue
                try:
                    if cfg.dry_run or not cfg.live_trading_mode:
                        log.error("DRY_RUN BLIND PANIC SELL pool=%s", pool_addr)
                        _finalize_close(store, pool_addr, entry, mark, "blind_mark_panic", True,
                                        pool_info.non_weth_symbol,
                                        r=r, pool_info=pool_info, expected_out=None,
                                        eth_price=cfg.eth_price_usd,
                                        exit_trigger_ms=_exit_trigger_ms,
                                        exit_trigger_block=_exit_trigger_block)
                    else:
                        flags["REAL_TX_SENT"] = True
                        preflight.assert_no_live_tx(flags)
                        result = execute_sell(w3, account, pool_info, pos.token_amount, reason="hard_stop")
                        if result.success:
                            _finalize_close(store, pool_addr, entry, int(result.amount_out or 0),
                                            "blind_mark_panic", False, pool_info.non_weth_symbol,
                                            result.tx_hash or "",
                                            r=r, pool_info=pool_info, expected_out=None,
                                            gas_used=result.gas_used, gas_cost_wei=result.gas_cost_wei,
                                            fill_block=result.block_number,
                                            eth_price=_live_eth_price(w3, r, cfg),
                                            exit_trigger_ms=_exit_trigger_ms,
                                            exit_trigger_block=_exit_trigger_block)
                            _check_daily_loss(r, store, cfg, w3, account)
                            log.error("BLIND PANIC SELL ok pool=%s tx=%s", pool_addr, result.tx_hash)
                        else:
                            log.error("BLIND PANIC SELL failed pool=%s: %s", pool_addr, result.error)
                            if _should_abandon_after_sell_fail(w3, account, pool_info, pos.token_amount, result.error or ""):
                                _abandon_unsellable_position(
                                    store, pool_addr, entry, mark, pool_info.non_weth_symbol,
                                    f"blind_panic:{result.error}", w3=w3, pool_info=pool_info,
                                )
                            else:
                                fails = _sell_fail_count.get(pool_addr, 0) + 1
                                _sell_fail_count[pool_addr] = fails
                                if _should_alert_on_sell_fail(fails):
                                    send_telegram(f"SELL stuck x{fails} pool={pool_addr[:10]}… err={result.error}")
                finally:
                    store.clear_pending_sell(pool_addr)
                continue
            if mark <= 0:
                mark = entry

        pnl_pct = (mark - entry) / entry

        if mark > peak:
            store.update_peak(pool_addr, mark)
            peak = mark

        # Shadow telemetry (2026-06-25): once per position, at/after +30min from
        # entry, record the would-be mark. PURELY observational — placed BEFORE the
        # exit decision but it only reads `mark` and writes a separate stream; it
        # does NOT set should_exit or change any trailing/stop/time behavior below.
        if (SHADOW_30MIN_ENABLED and SHADOW_30MIN_SEC > 0 and age_sec >= SHADOW_30MIN_SEC
                and store.mark_shadow_once(pool_addr)):
            try:
                _record_shadow_30min(
                    r, pool_info=pool_info, pos=pos, mark_wei=mark, entry_wei=entry,
                    peak_wei=peak, age_sec=age_sec, now_ms=now_ms,
                    block=_safe_block_number(w3), eth_price=cfg.eth_price_usd,
                )
            except Exception as e:
                log.warning("shadow_30min record failed pool=%s: %s", pool_addr, e)

        should_exit = False
        exit_reason = ""
        if TAKE_PROFIT_PCT < 5.0 and pnl_pct >= TAKE_PROFIT_PCT:
            should_exit = True
            exit_reason = "take_profit"
        elif pnl_pct <= -HARD_STOP_PCT:
            should_exit = True
            exit_reason = "hard_stop"
        elif TRAILING_PULL_PCT < 0.99 and peak > entry and (peak - mark) / peak >= TRAILING_PULL_PCT:
            should_exit = True
            exit_reason = "trailing"
        elif age_sec >= HOLD_SECONDS:
            should_exit = True
            exit_reason = "time_exit"

        if not should_exit:
            continue

        # EXIT-latency telemetry only: stamp the moment we decided to sell.
        # Purely observational — captured AFTER should_exit is already decided,
        # so it cannot change any trailing/hard_stop/time_exit behavior.
        _exit_trigger_ms = now_ms
        _exit_trigger_block = _safe_block_number(w3)

        if not store.try_mark_pending_sell(pool_addr, now_ms, PENDING_SELL_TTL_MS):
            log.info('{"event": "sell_lock_active", "pool": "%s"}', pool_addr)
            continue

        try:
            if cfg.dry_run or not cfg.live_trading_mode:
                log.info("DRY_RUN SELL pool=%s reason=%s mark=%d entry=%d pnl=%.2f%%",
                         pool_addr, exit_reason, mark, entry, 100 * pnl_pct)
                _finalize_close(store, pool_addr, entry, mark, exit_reason, True,
                                pool_info.non_weth_symbol,
                                r=r, pool_info=pool_info, expected_out=mark,
                                eth_price=cfg.eth_price_usd,
                                exit_trigger_ms=_exit_trigger_ms,
                                exit_trigger_block=_exit_trigger_block)
            else:
                flags["REAL_TX_SENT"] = True
                preflight.assert_no_live_tx(flags)
                result = execute_sell(w3, account, pool_info, pos.token_amount, reason=exit_reason)
                if result.success:
                    _finalize_close(store, pool_addr, entry, int(result.amount_out or 0),
                                    exit_reason, False, pool_info.non_weth_symbol,
                                    result.tx_hash or "",
                                    r=r, pool_info=pool_info, expected_out=mark,
                                    gas_used=result.gas_used, gas_cost_wei=result.gas_cost_wei,
                                    fill_block=result.block_number,
                                    eth_price=_live_eth_price(w3, r, cfg),
                                    exit_trigger_ms=_exit_trigger_ms,
                                    exit_trigger_block=_exit_trigger_block)
                    _check_daily_loss(r, store, cfg, w3, account)
                    _sell_fail_count.pop(pool_addr, None)
                    log.info("SELL ok pool=%s reason=%s tx=%s", pool_addr, exit_reason, result.tx_hash)
                else:
                    log.error("SELL failed pool=%s: %s", pool_addr, result.error)
                    if _should_abandon_after_sell_fail(w3, account, pool_info, pos.token_amount, result.error or ""):
                        _abandon_unsellable_position(
                            store, pool_addr, entry, mark, pool_info.non_weth_symbol,
                            f"{exit_reason}:{result.error}", w3=w3, pool_info=pool_info,
                        )
                    else:
                        fails = _sell_fail_count.get(pool_addr, 0) + 1
                        _sell_fail_count[pool_addr] = fails
                        if _should_alert_on_sell_fail(fails):
                            send_telegram(
                                f"SELL stuck x{fails} pool={pool_addr[:10]}… "
                                f"reason={exit_reason} err={result.error}"
                            )
        finally:
            store.clear_pending_sell(pool_addr)


def _ensure_consumer_group(r: redis.Redis, stream: str, group: str) -> None:
    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def _recover_pending_messages(r: redis.Redis, cfg: ExecutorConfig) -> List[Tuple[Any, Dict[Any, Any]]]:
    """P1: reclaim messages that were read but never ACKed (e.g. a crash between
    xreadgroup and xack). Without this they sit in PEL forever because we only
    read with '>'. We claim anything idle >60s back to this consumer; the normal
    loop will then process and ACK them. Returns the count reclaimed.
    """
    reclaimed: List[Tuple[Any, Dict[Any, Any]]] = []
    try:
        cursor = "0-0"
        while True:
            res = r.xautoclaim(
                cfg.signals_stream, cfg.consumer_group, cfg.consumer_name,
                min_idle_time=60_000, start_id=cursor, count=100,
            )
            # redis-py returns (next_cursor, claimed, deleted) or (next_cursor, claimed)
            next_cursor = res[0]
            claimed = res[1] if len(res) > 1 else []
            reclaimed.extend(claimed)
            cursor = next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor)
            if cursor in ("0-0", "0") or not claimed:
                break
    except Exception as e:
        log.warning("xautoclaim recovery failed: %s", e)
    if reclaimed:
        log.warning("recovered %d unacked pending signal(s) via xautoclaim", len(reclaimed))
    return reclaimed


def _process_signal_entry(
    w3,
    account,
    r: redis.Redis,
    store: PositionStore,
    cfg: ExecutorConfig,
    flags: Dict[str, Any],
    msg_id: Any,
    fields: Dict[Any, Any],
) -> None:
    """Shared signal-entry processor for new stream entries and reclaimed PEL entries."""
    msg_id_s = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
    sig = _parse_signal(fields)
    if sig is None:
        _dead_letter(r, cfg, msg_id_s, "parse_failed", None)
        r.xack(cfg.signals_stream, cfg.consumer_group, msg_id)
        return
    try:
        _handle_signal(w3, account, r, store, cfg, flags, sig, msg_id_s)
    except Exception as e:
        log.exception("handle_signal failed: %s", e)
        _dead_letter(r, cfg, msg_id_s, f"handle_failed: {e}", sig)
    r.xack(cfg.signals_stream, cfg.consumer_group, msg_id)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    cfg = load_config()
    r = redis.from_url(cfg.redis_url, decode_responses=False)
    set_redis(redis.from_url(cfg.redis_url, decode_responses=True))

    # F: seed the cluster blacklist with the 2026-06-23 PARADAENGI incident so a
    # repeat from the same source is blocked pre-entry. Idempotent.
    # EXECUTOR_SEED_RUG_BLACKLIST=0 (2026-06-25 aggressive experiment) disables the
    # historical LP-pull seed (247 deployers + 33 skeletons) to test "sell before
    # pull" alpha, while ALWAYS keeping the 2 PARADAENGI honeypot deployer seeds and
    # any runtime-learned (abandon) entries. Fully reversible (flip to 1 + rebuild).
    seed_rug = _parse_bool(os.environ.get("EXECUTOR_SEED_RUG_BLACKLIST", "1"))
    try:
        if seed_rug:
            seeded = cluster_blacklist.seed_paradaengi(get_redis())
            skel_n = cluster_blacklist.seed_safe_rug_skeletons(get_redis())
            dep_n = cluster_blacklist.seed_safe_rug_deployers(get_redis())
            log.info("cluster blacklist seeded: bytecode=%s deployers=%s safe_skeletons=%d safe_deployers=%d",
                     seeded.get("bytecode"), seeded.get("deployers"), skel_n, dep_n)
        else:
            skel_n = cluster_blacklist.unseed_safe_rug_skeletons(get_redis())
            dep_n = cluster_blacklist.unseed_safe_rug_deployers(get_redis())
            seeded = cluster_blacklist.seed_paradaengi(get_redis())  # re-assert honeypot seed AFTER unseed
            log.error("F LP-pull blacklist DISABLED (EXECUTOR_SEED_RUG_BLACKLIST=0): "
                      "removed skeletons=%d deployers=%d; PARADAENGI honeypot seed KEPT deployers=%s",
                      skel_n, dep_n, seeded.get("deployers"))
    except Exception as e:
        log.warning("cluster blacklist seed/unseed failed (continuing): %s", e)

    # P0.5: print the explicit slippage caps in effect (minOut = expected×(1−tol)).
    # There is NO infinite-slippage path: buys abort if QuoterV2 returns 0.
    log.info("slippage caps (minOut tolerances): %s", dict(sorted(_SLIPPAGE_TOLERANCE.items())))

    panic = _panic_active(r, cfg.panic_key)
    flags = preflight.run_preflight_checks(panic_active=panic)

    # P1: in live mode, a working alert channel is mandatory — otherwise
    # honeypots / stuck sells / RPC outages would fail silently. Hard-stop start.
    validate_telegram_or_raise(cfg.live_trading_mode and not cfg.dry_run)

    store = PositionStore(redis.from_url(cfg.redis_url, decode_responses=False))
    migrated = store.migrate_legacy_exec_keys()
    if migrated:
        log.info("migrated %d legacy positions", migrated)

    w3 = None
    account = None
    if cfg.live_trading_mode and not cfg.dry_run:
        from eth_account import Account

        pk = os.environ.get("EXECUTOR_PRIVATE_KEY", "").strip()
        w3, _send = init_send_lane(cfg.rpc_url)
        account = Account.from_key(pk)
    elif cfg.rpc_url:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))

    _ensure_consumer_group(r, cfg.signals_stream, cfg.consumer_group)

    # P0: adopt any bought-but-unrecorded tokens before we start trading.
    try:
        recovered = _reconcile_orphan_buys(w3, account, store, cfg)
        if recovered:
            log.error("startup orphan reconcile recovered %d position(s)", recovered)
    except Exception as e:
        log.exception("orphan reconcile crashed (continuing): %s", e)

    # Fail loud at boot if the sizing rules contradict each other: a deadlock
    # found only on the timer costs up to SIZING_CHECK_INTERVAL_SEC of silence.
    try:
        _check_sizing_deadlock(w3, account, r, cfg)
    except Exception as e:
        log.exception("startup sizing check crashed (continuing): %s", e)

    panic_now = _panic_active(r, cfg.panic_key)
    # P1: reclaim and process unacked pending signals from a prior crash only
    # when buys are not paused. If panic is active, keep them pending until the
    # operator explicitly resumes.
    if not panic_now:
        for msg_id, fields in _recover_pending_messages(r, cfg):
            _process_signal_entry(w3, account, r, store, cfg, flags, msg_id, fields)
    log.info(
        "executor started dry_run=%s live=%s stream=%s "
        "exit_policy stop=%.0f%% hold=%ds trail=%.0f%% monitor=%ds panic=%s",
        cfg.dry_run,
        cfg.live_trading_mode,
        cfg.signals_stream,
        100 * HARD_STOP_PCT,
        HOLD_SECONDS,
        100 * TRAILING_PULL_PCT,
        int(MONITOR_INTERVAL_SEC),
        panic_now,
    )
    # P1: startup banner so a silent crash/restart is always visible.
    try:
        open_n = store.count_open()
    except Exception:
        open_n = -1
    if cfg.max_position_eth > 0:
        size_desc = f"size={cfg.max_position_eth:.4f} ETH (floor {cfg.size_min_eth:.4f})"
    else:
        size_desc = f"size=${cfg.max_position_usd:.0f}"
    send_telegram(
        f"Keystone executor 启动\n"
        f"mode={'LIVE' if cfg.live_trading_mode and not cfg.dry_run else 'DRY_RUN'}\n"
        f"open_positions={open_n}  max_concurrent={cfg.max_concurrent}\n"
        f"{size_desc} dynamic={cfg.dynamic_sizing}\n"
        f"panic={'ACTIVE: ' + str(_panic_reason(r, cfg.panic_key)) if panic_now else 'none'}"
    )

    stop = False
    last_monitor = 0.0
    last_heartbeat = time.monotonic()
    last_pending_recover = 0.0
    last_sizing_check = 0.0
    monitor_fail_streak = 0
    last_monitor_alert = 0.0

    def _sig_handler(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    while not stop:
        try:
            now = time.monotonic()
            if now - last_monitor >= MONITOR_INTERVAL_SEC:
                _monitor_once(w3, account, r, store, cfg, flags)
                last_monitor = now
            if now - last_sizing_check >= SIZING_CHECK_INTERVAL_SEC:
                last_sizing_check = now
                _check_sizing_deadlock(w3, account, r, cfg)
            if HEARTBEAT_SEC > 0 and now - last_heartbeat >= HEARTBEAT_SEC:
                last_heartbeat = now
                try:
                    hb_open = store.count_open()
                    hb_pnl = store.get_realized_pnl_wei(_day_key()) / 1e18
                except Exception:
                    hb_open, hb_pnl = -1, 0.0
                send_telegram(
                    f"Keystone 心跳 ✅ open={hb_open} "
                    f"today_pnl={hb_pnl:+.5f} ETH "
                    f"panic={'ACTIVE' if _panic_active(r, cfg.panic_key) else 'none'}"
                )
            # A fully clean cycle resets the failure streak.
            monitor_fail_streak, last_monitor_alert, _ = _update_monitor_failure_state(
                False, monitor_fail_streak, last_monitor_alert, time.monotonic()
            )
        except Exception:
            monitor_fail_streak, last_monitor_alert, should_alert = _update_monitor_failure_state(
                True, monitor_fail_streak, last_monitor_alert, time.monotonic()
            )
            log.exception("monitor cycle failed (consecutive=%d)", monitor_fail_streak)
            # Repeated cycle failures mean open positions may be unmanaged.
            if should_alert:
                send_telegram(
                    f"🚨 Keystone monitor 连续 {monitor_fail_streak} 次异常 — "
                    f"持仓可能无人管理，请检查 executor 日志。"
                )

        if _panic_active(r, cfg.panic_key):
            log.error("SYSTEM_PANIC active — blocking new BUY signals (monitor still runs)")
            time.sleep(2)
            continue

        # If an operator resumes without restarting the executor, reclaim any
        # old PEL entries that accumulated while paused/crashed.
        if now - last_pending_recover >= 60:
            last_pending_recover = now
            for msg_id, fields in _recover_pending_messages(r, cfg):
                _process_signal_entry(w3, account, r, store, cfg, flags, msg_id, fields)

        try:
            messages = r.xreadgroup(
                cfg.consumer_group,
                cfg.consumer_name,
                {cfg.signals_stream: ">"},
                count=10,
                block=2000,
            )
        except Exception as e:
            log.warning("xreadgroup failed: %s", e)
            time.sleep(1)
            continue

        if not messages:
            continue

        for _stream, entries in messages:
            for msg_id, fields in entries:
                _process_signal_entry(w3, account, r, store, cfg, flags, msg_id, fields)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
