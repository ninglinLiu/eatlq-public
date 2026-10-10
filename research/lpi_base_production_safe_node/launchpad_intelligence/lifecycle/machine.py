"""Clanker-first lifecycle state machine — facts only, no strategy conclusions.

Does not force bonding-curve graduation models onto platforms that lack them.
Unknown or inapplicable transitions remain UNKNOWN / TERMINAL_UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from ..errors import ContractError


class LifecycleState(StrEnum):
    DISCOVERED = "discovered"
    CREATED = "created"
    CREATED_WITH_DEV_BUY = "created_with_dev_buy"
    POOL_INITIALIZED = "pool_initialized"
    FIRST_EXTERNAL_BUY = "first_external_buy"
    FIRST_SELL = "first_sell"
    FEE_GENERATED = "fee_generated"
    FEE_CLAIMED = "fee_claimed"
    ACTIVE_AFTER_1H = "active_after_1h"
    ACTIVE_AFTER_6H = "active_after_6h"
    ACTIVE_AFTER_24H = "active_after_24h"
    DORMANT = "dormant"
    REVIVED = "revived"
    TERMINAL_UNKNOWN = "terminal_unknown"


# Event type -> preferred next state when transition is valid from current.
TRANSITIONS: dict[tuple[LifecycleState, str], LifecycleState] = {
    (LifecycleState.DISCOVERED, "TOKEN_CREATED"): LifecycleState.CREATED,
    (LifecycleState.CREATED, "DEV_BUY"): LifecycleState.CREATED_WITH_DEV_BUY,
    (LifecycleState.CREATED, "POOL_INITIALIZED"): LifecycleState.POOL_INITIALIZED,
    (LifecycleState.CREATED_WITH_DEV_BUY, "POOL_INITIALIZED"): LifecycleState.POOL_INITIALIZED,
    (LifecycleState.POOL_INITIALIZED, "EXTERNAL_BUY"): LifecycleState.FIRST_EXTERNAL_BUY,
    (LifecycleState.CREATED, "EXTERNAL_BUY"): LifecycleState.FIRST_EXTERNAL_BUY,
    (LifecycleState.CREATED_WITH_DEV_BUY, "EXTERNAL_BUY"): LifecycleState.FIRST_EXTERNAL_BUY,
    (LifecycleState.FIRST_EXTERNAL_BUY, "SELL"): LifecycleState.FIRST_SELL,
    (LifecycleState.POOL_INITIALIZED, "SELL"): LifecycleState.FIRST_SELL,
    (LifecycleState.FIRST_SELL, "FEE_ACCRUAL"): LifecycleState.FEE_GENERATED,
    (LifecycleState.FIRST_EXTERNAL_BUY, "FEE_ACCRUAL"): LifecycleState.FEE_GENERATED,
    (LifecycleState.FEE_GENERATED, "FEE_CLAIM"): LifecycleState.FEE_CLAIMED,
    (LifecycleState.FIRST_EXTERNAL_BUY, "ACTIVE_1H"): LifecycleState.ACTIVE_AFTER_1H,
    (LifecycleState.ACTIVE_AFTER_1H, "ACTIVE_6H"): LifecycleState.ACTIVE_AFTER_6H,
    (LifecycleState.ACTIVE_AFTER_6H, "ACTIVE_24H"): LifecycleState.ACTIVE_AFTER_24H,
    (LifecycleState.ACTIVE_AFTER_1H, "DORMANT"): LifecycleState.DORMANT,
    (LifecycleState.ACTIVE_AFTER_6H, "DORMANT"): LifecycleState.DORMANT,
    (LifecycleState.ACTIVE_AFTER_24H, "DORMANT"): LifecycleState.DORMANT,
    (LifecycleState.FIRST_EXTERNAL_BUY, "DORMANT"): LifecycleState.DORMANT,
    (LifecycleState.DORMANT, "REVIVE"): LifecycleState.REVIVED,
    (LifecycleState.REVIVED, "EXTERNAL_BUY"): LifecycleState.FIRST_EXTERNAL_BUY,
}


@dataclass(frozen=True)
class StateChange:
    previous: LifecycleState
    current: LifecycleState
    trigger: str
    transaction_hash: str
    log_index: int
    unknown_reason: str | None = None


@dataclass(frozen=True)
class LifecycleEvent:
    event_type: str
    transaction_hash: str
    log_index: int
    block_number: int


class LifecycleMachine:
    def __init__(self, initial: LifecycleState = LifecycleState.DISCOVERED) -> None:
        self.state = initial
        self.history: list[StateChange] = []
        self.unknown_count = 0

    def apply(self, event: LifecycleEvent) -> LifecycleState:
        if event.event_type in ("UNKNOWN", "UNKNOWN_EVENT", "TERMINAL_UNKNOWN"):
            self.unknown_count += 1
            change = StateChange(
                previous=self.state,
                current=LifecycleState.TERMINAL_UNKNOWN
                if event.event_type == "TERMINAL_UNKNOWN"
                else self.state,
                trigger=event.event_type,
                transaction_hash=event.transaction_hash,
                log_index=event.log_index,
                unknown_reason="event_type_unknown_or_terminal",
            )
            if event.event_type == "TERMINAL_UNKNOWN":
                self.state = LifecycleState.TERMINAL_UNKNOWN
            self.history.append(change)
            return self.state

        nxt = TRANSITIONS.get((self.state, event.event_type))
        if nxt is None:
            # Preserve UNKNOWN rather than inventing a transition.
            self.unknown_count += 1
            self.history.append(
                StateChange(
                    previous=self.state,
                    current=self.state,
                    trigger=event.event_type,
                    transaction_hash=event.transaction_hash,
                    log_index=event.log_index,
                    unknown_reason=f"no_transition_from_{self.state}",
                )
            )
            return self.state

        self.history.append(
            StateChange(
                previous=self.state,
                current=nxt,
                trigger=event.event_type,
                transaction_hash=event.transaction_hash,
                log_index=event.log_index,
            )
        )
        self.state = nxt
        return self.state

    def replay(self, events: Iterable[LifecycleEvent]) -> LifecycleState:
        ordered = sorted(events, key=lambda e: (e.block_number, e.log_index))
        seen: set[tuple[str, int]] = set()
        for event in ordered:
            identity = (event.transaction_hash, event.log_index)
            if identity in seen:
                raise ContractError(f"duplicate lifecycle event {identity}")
            seen.add(identity)
            self.apply(event)
        return self.state
