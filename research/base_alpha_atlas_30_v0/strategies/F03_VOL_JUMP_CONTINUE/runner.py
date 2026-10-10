"""F03_VOL_JUMP_CONTINUE — jump continuation / fade arms.

database_access_enabled = false
rpc_access_enabled = false
large_file_access_enabled = false
backtest_enabled = false

Formulas (preregistration):
  r_t = log(px_t) - log(px_{t-1})
  rv_l_{t-1} = sqrt(sum r^2 over l bars ending at t-1)
  jump_flag = |r_t| >= k * rv_l_{t-1}
  jump_sign = sign(r_t)
  arm=0: long if jump_sign=+1 (continuation)
  arm=1: long if jump_sign=-1 (buy dip / fade)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestration.common import (
    ACCESS_FLAGS,
    Bar,
    CostEstimate,
    OrderIntent,
    Signal,
    assert_offline_safe,
)

STRATEGY_ID = "F03_VOL_JUMP_CONTINUE"
CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def _close_or_none(b: Bar) -> float | None:
    px = b.features.get("close")
    if px is None:
        px = b.features.get("px")
    if px is None:
        return None
    v = float(px)
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _closes(bars: Sequence[Bar]) -> list[float | None]:
    return [_close_or_none(b) for b in bars]


def _log_returns(closes: Sequence[float | None]) -> list[float]:
    """Close-to-close log returns. Invalid/missing closes → NaN (never coerced to 0)."""
    r: list[float] = [float("nan")]
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a is None or b is None or a <= 0 or b <= 0:
            r.append(float("nan"))
        else:
            r.append(math.log(b) - math.log(a))
    return r


def realized_vol(returns: Sequence[float], end_exclusive: int, l: int) -> float:
    """Causal rv using finite returns with indices in [end_exclusive-l, end_exclusive)."""
    if end_exclusive <= 0 or l <= 0:
        return float("nan")
    start = max(1, end_exclusive - l)  # skip r[0] placeholder
    window = [x for x in returns[start:end_exclusive] if math.isfinite(x)]
    if len(window) < max(2, l // 4):
        return float("nan")
    s2 = sum(x * x for x in window)
    return math.sqrt(s2)


def signal_fn(bars: Sequence[Bar], params: Mapping[str, float]) -> list[Signal]:
    """Closed-bar jump signals only. status: FIXTURE_VALIDATED_SIGNAL_LOGIC on fixtures."""
    assert_offline_safe()
    k = float(params.get("k", 3.0))
    l = int(params.get("l", 60))
    arm = int(params.get("arm", 0))
    cooldown = int(params.get("jump_cooldown_bars", 5))
    # Sort by close time; assume each bar is already closed (feature_available_at==event_time)
    bars = sorted(bars, key=lambda b: (b.pool_id, b.event_time, b.feature_available_at))
    out: list[Signal] = []

    # Per-pool causal walk
    by_pool: dict[str, list[Bar]] = {}
    for b in bars:
        by_pool.setdefault(b.pool_id, []).append(b)

    for pool, plist in by_pool.items():
        if len(plist) < l + 2:
            continue
        closes = _closes(plist)
        rets = _log_returns(closes)
        last_sig_t = -10**9
        for t in range(1, len(plist)):
            b = plist[t]
            # PIT: bar must be closed; feature_available_at marks visibility
            if b.feature_available_at < b.event_time:
                continue
            rv = realized_vol(rets, t, l)
            if not math.isfinite(rv) or rv <= 0:
                continue
            r_t = rets[t]
            if not math.isfinite(r_t):
                continue
            jump_ratio = abs(r_t) / rv
            if jump_ratio < k:
                continue
            jump_sign = 1.0 if r_t > 0 else (-1.0 if r_t < 0 else 0.0)
            if jump_sign == 0.0:
                continue
            if arm == 0 and jump_sign < 0:
                continue
            if arm == 1 and jump_sign > 0:
                continue
            if t - last_sig_t <= cooldown:
                continue
            out.append(
                Signal(
                    strategy_id=STRATEGY_ID,
                    pool_id=pool,
                    side="BUY",
                    strength=float(jump_ratio),
                    event_time=b.event_time,
                    feature_available_at=b.feature_available_at,
                    meta={
                        "jump_sign": jump_sign,
                        "r_t": r_t,
                        "rv_l_prev": rv,
                        "jump_ratio": jump_ratio,
                        "k": k,
                        "l": l,
                        "arm": arm,
                        "bar_index": t,
                        "bar_close_time": b.event_time,
                        "status_label": "FIXTURE_VALIDATED_SIGNAL_LOGIC",
                    },
                )
            )
            last_sig_t = t
    out.sort(key=lambda s: (s.pool_id, s.event_time))
    return out


def entry_fn(signal: Signal, params: Mapping[str, float]) -> OrderIntent | None:
    assert_offline_safe()
    if signal.side != "BUY":
        return None
    delay = int(params.get("execution_delay_bars", 1))
    notional = float(params.get("notional_usd", 50))
    return OrderIntent(
        strategy_id=STRATEGY_ID,
        pool_id=signal.pool_id,
        side="BUY",
        notional_usd=notional,
        decision_time=signal.event_time,
        earliest_exec_time=signal.event_time + delay,
        reason="jump_entry",
    )


def exit_fn(
    position: Mapping[str, Any], bars: Sequence[Bar], params: Mapping[str, float]
) -> OrderIntent | None:
    assert_offline_safe()
    if not bars:
        return None
    max_hold = int(params.get("hold", params.get("maximum_hold_bars", 5)))
    opened = int(position.get("entry_time", bars[0].event_time))
    if bars[-1].event_time - opened >= max_hold:
        return OrderIntent(
            strategy_id=STRATEGY_ID,
            pool_id=str(position.get("pool_id", bars[-1].pool_id)),
            side="SELL",
            notional_usd=float(position.get("notional_usd", 50)),
            decision_time=bars[-1].event_time,
            earliest_exec_time=bars[-1].event_time + 1,
            reason="max_hold",
        )
    return None


def cost_fn(intent: OrderIntent, params: Mapping[str, float]) -> CostEstimate:
    assert_offline_safe()
    return CostEstimate(
        gas_usd=float(params.get("gas_usd_placeholder", 0.15)),
        protocol_fee_usd=float(params.get("protocol_fee_usd_placeholder", 0.0)),
        lp_fee_usd=float(params.get("lp_fee_usd_placeholder", 0.0)),
        slippage_usd=float(params.get("slippage_usd_placeholder", 0.0)),
        impact_usd=float(params.get("impact_usd_placeholder", 0.0)),
        mev_usd=float(params.get("mev_usd_placeholder", 0.0)),
        revert_prob=float(params.get("revert_prob_placeholder", 0.05)),
        failed_sell_prob=float(params.get("failed_sell_prob_placeholder", 0.10)),
        unknown_fields=("broadcast_latency_ms", "calibrated_slippage_distribution"),
    )


def run_scaffold(bars: Sequence[Bar] | None = None) -> dict[str, Any]:
    assert_offline_safe()
    assert ACCESS_FLAGS["backtest_enabled"] is False
    cfg = load_config()
    params = {
        "k": 3.0,
        "l": 10,
        "arm": 0,
        "jump_cooldown_bars": 5,
        "notional_usd": 50,
        "execution_delay_bars": 1,
        "hold": 5,
    }
    bars = list(bars or [])
    signals = signal_fn(bars, params)
    intents = [i for i in (entry_fn(s, params) for s in signals) if i is not None]
    costs = [cost_fn(i, params) for i in intents]
    return {
        "strategy_id": STRATEGY_ID,
        "n_bars": len(bars),
        "n_signals": len(signals),
        "n_intents": len(intents),
        "n_costs": len(costs),
        "param_search_space_keys": list(cfg.get("parameter_search_space", {}).keys()),
        "backtest_enabled": False,
    }


if __name__ == "__main__":
    print(json.dumps(run_scaffold(), indent=2))
