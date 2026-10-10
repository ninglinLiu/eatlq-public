"""Shared static interfaces for BASE_ALPHA_ATLAS_30_v0.

No production DB/RPC/large-file clients may be imported here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


ACCESS_FLAGS = {
    "database_access_enabled": False,
    "rpc_access_enabled": False,
    "large_file_access_enabled": False,
    "backtest_enabled": False,
}


@dataclass(frozen=True)
class Bar:
    pool_id: str
    event_time: int  # unix seconds, bar close
    known_at: int
    feature_available_at: int
    features: Mapping[str, float]


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    pool_id: str
    side: str  # BUY | SELL | FLAT
    strength: float
    event_time: int
    feature_available_at: int
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderIntent:
    strategy_id: str
    pool_id: str
    side: str
    notional_usd: float
    decision_time: int
    earliest_exec_time: int
    reason: str


@dataclass(frozen=True)
class CostEstimate:
    gas_usd: float
    protocol_fee_usd: float
    lp_fee_usd: float
    slippage_usd: float
    impact_usd: float
    mev_usd: float
    revert_prob: float
    failed_sell_prob: float
    unknown_fields: Sequence[str] = field(default_factory=tuple)

    @property
    def total_known_usd(self) -> float:
        return (
            self.gas_usd
            + self.protocol_fee_usd
            + self.lp_fee_usd
            + self.slippage_usd
            + self.impact_usd
            + self.mev_usd
        )


class SignalFn(Protocol):
    def __call__(self, bars: Sequence[Bar], params: Mapping[str, float]) -> Sequence[Signal]:
        ...


class EntryFn(Protocol):
    def __call__(self, signal: Signal, params: Mapping[str, float]) -> OrderIntent | None:
        ...


class ExitFn(Protocol):
    def __call__(
        self,
        position: Mapping[str, Any],
        bars: Sequence[Bar],
        params: Mapping[str, float],
    ) -> OrderIntent | None:
        ...


class CostFn(Protocol):
    def __call__(self, intent: OrderIntent, params: Mapping[str, float]) -> CostEstimate:
        ...


def assert_offline_safe() -> None:
    for k, v in ACCESS_FLAGS.items():
        if v:
            raise RuntimeError(f"unsafe flag enabled: {k}")
