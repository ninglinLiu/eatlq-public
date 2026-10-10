"""F01_OFS_SIGN_RUNLEN — causal swap-run signal (Stage 1A).

No PnL, no future swaps, no parameter search.
UNKNOWN never coerced to 0 for signs/notionals.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from orchestration.common import (
    ACCESS_FLAGS,
    CostEstimate,
    OrderIntent,
    Signal,
    assert_offline_safe,
)

STRATEGY_ID = "F01_OFS_SIGN_RUNLEN"
CONFIG_PATH = Path(__file__).with_name("config.json")
EPS = 1e-12


@dataclass(frozen=True)
class Swap:
    pool_id: str
    block_number: int
    log_index: int
    timestamp_ms: Optional[int]
    trade_sign: Optional[int]  # +1 / -1 / None=UNKNOWN
    quote_notional_eth: Optional[float]  # None=UNKNOWN
    transaction_index: Optional[int] = None  # None=UNKNOWN
    tx_hash: Optional[str] = None

    @property
    def sort_key(self) -> tuple:
        # transaction_index UNKNOWN → sort after block, before log via sentinel None
        # Use -1 for missing tx index so key is total-ordered but documented as non-authoritative
        tx = self.transaction_index if self.transaction_index is not None else -1
        return (self.block_number, tx, self.log_index)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def sort_swaps(swaps: Sequence[Swap]) -> list[Swap]:
    return sorted(swaps, key=lambda s: (s.sort_key, s.tx_hash or ""))


def _dedupe(swaps: Sequence[Swap]) -> list[Swap]:
    seen: set[tuple] = set()
    out: list[Swap] = []
    for s in sort_swaps(swaps):
        key = (s.pool_id, s.block_number, s.transaction_index, s.log_index)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def signal_fn(swaps: Sequence[Swap], params: Mapping[str, Any]) -> list[Signal]:
    """Emit BUY signals on +1 runs crossing thresholds (edge-triggered)."""
    assert_offline_safe()
    min_run_len = int(params.get("min_run_len", 5))
    min_notional = float(params.get("min_run_notional_eth", 0.05))
    L = int(params.get("L_completed_runs", 20))
    z_enter = params.get("z_enter", None)
    if z_enter is not None:
        z_enter = float(z_enter)
    cooldown_swaps = int(params.get("cooldown_swaps", 20))
    min_swaps_in_pool = int(params.get("min_swaps_in_pool", 10))

    clean = _dedupe(swaps)
    by_pool: dict[str, list[Swap]] = {}
    for s in clean:
        by_pool.setdefault(s.pool_id, []).append(s)

    out: list[Signal] = []
    for pool, plist in by_pool.items():
        plist = sort_swaps(plist)
        if len(plist) < min_swaps_in_pool:
            continue

        # Walk causally
        completed_run_lengths: list[int] = []
        run_sign: Optional[int] = None
        run_len = 0
        run_notional: Optional[float] = 0.0  # None if UNKNOWN contaminated
        prev_opp_len = 0
        prev_opp_notional: Optional[float] = 0.0
        cooldown_left = 0
        known_sign_count = 0

        for idx, s in enumerate(plist):
            if cooldown_left > 0:
                cooldown_left -= 1

            if s.trade_sign is None:
                # UNKNOWN sign: does not extend or break run with fake 0; skip
                continue

            known_sign_count += 1
            prev_run_len = run_len

            if run_sign is None or s.trade_sign != run_sign:
                # close previous run into completed history
                if run_sign is not None and run_len > 0:
                    completed_run_lengths.append(run_len)
                    if run_sign == -s.trade_sign:
                        prev_opp_len = run_len
                        prev_opp_notional = run_notional
                run_sign = s.trade_sign
                run_len = 1
                run_notional = s.quote_notional_eth
            else:
                run_len += 1
                if run_notional is None or s.quote_notional_eth is None:
                    run_notional = None
                else:
                    run_notional += s.quote_notional_eth

            # z vs completed runs only
            run_len_z: Optional[float] = None
            if len(completed_run_lengths) >= max(5, L // 4):
                window = completed_run_lengths[-L:]
                mean = sum(window) / len(window)
                var = sum((x - mean) ** 2 for x in window) / max(len(window) - 1, 1)
                std = math.sqrt(var)
                run_len_z = (run_len - mean) / (std + EPS)

            # Edge trigger: length increased and crossed threshold
            crossed = run_len >= min_run_len and prev_run_len < min_run_len
            # Also allow re-cross after cooldown when length steps up further at multiples? Spec: edge at crossing min_run_len
            if not crossed:
                continue
            if s.trade_sign != 1:
                continue  # long-only BUY signals
            if cooldown_left > 0:
                continue
            if run_notional is None or run_notional < min_notional:
                continue
            if z_enter is not None:
                if run_len_z is None or run_len_z < z_enter:
                    continue

            obs_ms = s.timestamp_ms if s.timestamp_ms is not None else s.block_number
            out.append(
                Signal(
                    strategy_id=STRATEGY_ID,
                    pool_id=pool,
                    side="BUY",
                    strength=float(run_len),
                    event_time=int(obs_ms),
                    feature_available_at=int(obs_ms),
                    meta={
                        "run_length": run_len,
                        "run_notional_eth": run_notional,
                        "run_len_z": run_len_z,
                        "previous_opposite_run_length": prev_opp_len,
                        "previous_opposite_run_notional_eth": prev_opp_notional,
                        "trigger_block_number": s.block_number,
                        "trigger_log_index": s.log_index,
                        "trigger_transaction_index": s.transaction_index,
                        "trigger_tx_hash": s.tx_hash,
                        "trade_sign": s.trade_sign,
                        "sort_authority": "block_number,transaction_index(UNKNOWN=-1),log_index",
                        "known_sign_count_before": known_sign_count,
                    },
                )
            )
            cooldown_left = cooldown_swaps

    # Deterministic output order
    out.sort(
        key=lambda sig: (
            sig.pool_id,
            sig.meta.get("trigger_block_number", 0),
            sig.meta.get("trigger_log_index", 0),
        )
    )
    return out


def entry_fn(signal: Signal, params: Mapping[str, Any]) -> OrderIntent | None:
    assert_offline_safe()
    if signal.side != "BUY":
        return None
    delay = int(params.get("execution_delay_blocks", 1))
    return OrderIntent(
        strategy_id=STRATEGY_ID,
        pool_id=signal.pool_id,
        side="BUY",
        notional_usd=float(params.get("notional_usd", 50)),
        decision_time=signal.event_time,
        earliest_exec_time=signal.event_time + delay,
        reason="f01_runlen_entry",
    )


def exit_fn(position: Mapping[str, Any], swaps: Sequence[Swap], params: Mapping[str, Any]):
    assert_offline_safe()
    return None  # Stage1A: no exits


def cost_fn(intent: OrderIntent, params: Mapping[str, Any]) -> CostEstimate:
    assert_offline_safe()
    return CostEstimate(
        gas_usd=float("nan"),
        protocol_fee_usd=float("nan"),
        lp_fee_usd=float("nan"),
        slippage_usd=float("nan"),
        impact_usd=float("nan"),
        mev_usd=float("nan"),
        revert_prob=float("nan"),
        failed_sell_prob=float("nan"),
        unknown_fields=(
            "gas_usd",
            "protocol_fee_usd",
            "lp_fee_usd",
            "slippage_usd",
            "impact_usd",
            "mev_usd",
            "revert_prob",
            "failed_sell_prob",
        ),
    )


def run_scaffold(swaps: Sequence[Swap] | None = None) -> dict[str, Any]:
    assert_offline_safe()
    assert ACCESS_FLAGS["backtest_enabled"] is False
    params = {
        "min_run_len": 3,
        "min_run_notional_eth": 0.01,
        "L_completed_runs": 20,
        "z_enter": None,
        "cooldown_swaps": 5,
        "min_swaps_in_pool": 3,
    }
    swaps = list(swaps or [])
    sigs = signal_fn(swaps, params)
    return {
        "strategy_id": STRATEGY_ID,
        "n_swaps": len(swaps),
        "n_signals": len(sigs),
        "backtest_enabled": False,
    }


if __name__ == "__main__":
    print(json.dumps(run_scaffold(), indent=2))
