"""Static scaffold for F09_EXE_NOTIONAL_CURVE.

database_access_enabled = false
rpc_access_enabled = false
large_file_access_enabled = false
backtest_enabled = false
"""
from __future__ import annotations

import json
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

STRATEGY_ID = "F09_EXE_NOTIONAL_CURVE"
CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def signal_fn(bars: Sequence[Bar], params: Mapping[str, float]) -> list[Signal]:
    """Deterministic placeholder: emits FLAT unless synthetic fixture sets feature trigger=1."""
    assert_offline_safe()
    out: list[Signal] = []
    for b in bars:
        trig = float(b.features.get("trigger", 0.0))
        if trig >= 1.0:
            out.append(
                Signal(
                    strategy_id=STRATEGY_ID,
                    pool_id=b.pool_id,
                    side="BUY",
                    strength=float(b.features.get("strength", 1.0)),
                    event_time=b.event_time,
                    feature_available_at=b.feature_available_at,
                    meta={"params_used": dict(params)},
                )
            )
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
        reason="scaffold_entry",
    )


def exit_fn(position: Mapping[str, Any], bars: Sequence[Bar], params: Mapping[str, float]) -> OrderIntent | None:
    assert_offline_safe()
    if not bars:
        return None
    max_hold = int(params.get("maximum_hold_bars", 5))
    opened = int(position.get("entry_time", bars[0].event_time))
    if bars[-1].event_time - opened >= max_hold:
        return OrderIntent(
            strategy_id=STRATEGY_ID,
            pool_id=str(position.get("pool_id", bars[-1].pool_id)),
            side="SELL",
            notional_usd=float(position.get("notional_usd", 50)),
            decision_time=bars[-1].event_time,
            earliest_exec_time=bars[-1].event_time + 1,
            reason="scaffold_max_hold",
        )
    return None


def cost_fn(intent: OrderIntent, params: Mapping[str, float]) -> CostEstimate:
    assert_offline_safe()
    # UNKNOWN fields remain listed; numeric placeholders are explicit scaffold defaults only.
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
    """Empty runner: wires interfaces on optional synthetic bars; never loads production data."""
    assert_offline_safe()
    assert ACCESS_FLAGS["backtest_enabled"] is False
    cfg = load_config()
    params = {"notional_usd": 50, "execution_delay_bars": 1, "maximum_hold_bars": 5}
    bars = list(bars or [])
    signals = signal_fn(bars, params)
    intents = [entry_fn(s, params) for s in signals]
    intents = [i for i in intents if i is not None]
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
