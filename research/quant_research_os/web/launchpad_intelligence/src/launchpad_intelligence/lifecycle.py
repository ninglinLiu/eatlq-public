from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import CanonicalEvent, ValidationError


STATES = {
    "DISCOVERED", "CREATED", "PRIMARY_MARKET_ACTIVE", "CURVE_STALLED",
    "GRADUATION_PENDING", "GRADUATED", "MIGRATION_PENDING", "MIGRATION_FAILED",
    "AMM_ACTIVE", "DORMANT", "REVIVED", "DEAD", "LP_RUGGED", "CREATOR_DUMPED",
    "BUNDLED_EXIT", "SELL_BLOCKED", "FEE_EXTRACTION_DEATH", "ABANDONED",
    "UNKNOWN_TERMINAL",
}

TRANSITIONS = {
    ("DISCOVERED", "TOKEN_CREATED"): "CREATED",
    ("CREATED", "PRIMARY_BUY"): "PRIMARY_MARKET_ACTIVE",
    ("CREATED", "PRIMARY_SELL"): "PRIMARY_MARKET_ACTIVE",
    ("PRIMARY_MARKET_ACTIVE", "CURVE_STALLED"): "CURVE_STALLED",
    ("PRIMARY_MARKET_ACTIVE", "GRADUATION_PENDING"): "GRADUATION_PENDING",
    ("GRADUATION_PENDING", "GRADUATED"): "GRADUATED",
    ("PRIMARY_MARKET_ACTIVE", "GRADUATED"): "GRADUATED",
    ("GRADUATED", "MIGRATION_PENDING"): "MIGRATION_PENDING",
    ("MIGRATION_PENDING", "MIGRATION_FAILED"): "MIGRATION_FAILED",
    ("MIGRATION_PENDING", "AMM_TRADE"): "AMM_ACTIVE",
    ("GRADUATED", "AMM_TRADE"): "AMM_ACTIVE",
    ("AMM_ACTIVE", "DORMANT"): "DORMANT",
    ("PRIMARY_MARKET_ACTIVE", "DORMANT"): "DORMANT",
    ("DORMANT", "REVIVED"): "REVIVED",
    ("REVIVED", "AMM_TRADE"): "AMM_ACTIVE",
}

TERMINAL_EVENT_STATES = {
    "LP_RUG": "LP_RUGGED", "CREATOR_DUMP": "CREATOR_DUMPED",
    "BUNDLE_EXIT": "BUNDLED_EXIT", "SELL_BLOCK": "SELL_BLOCKED",
    "MINT_OR_SUPPLY_ATTACK": "DEAD", "MIGRATION_FAILURE": "MIGRATION_FAILED",
    "ADMIN_OR_UPGRADE_ATTACK": "DEAD", "FEE_EXTRACTION_DEATH": "FEE_EXTRACTION_DEATH",
    "ATTENTION_DEATH": "DEAD", "ABANDONED": "ABANDONED",
    "UNKNOWN_TERMINAL": "UNKNOWN_TERMINAL",
}


@dataclass(frozen=True)
class StateChange:
    previous: str
    current: str
    trigger: str
    transaction_id: str
    event_index: int


class LifecycleMachine:
    def __init__(self) -> None:
        self.state = "DISCOVERED"
        self.history: list[StateChange] = []

    def apply(self, event: CanonicalEvent) -> str:
        event.validate()
        next_state = TERMINAL_EVENT_STATES.get(event.event_type) or TRANSITIONS.get((self.state, event.event_type))
        if next_state is None:
            raise ValidationError(f"invalid lifecycle transition {self.state} + {event.event_type}")
        self.history.append(StateChange(
            previous=self.state,
            current=next_state,
            trigger=event.event_type,
            transaction_id=event.context.transaction_id,
            event_index=event.context.event_index,
        ))
        self.state = next_state
        return self.state

    def replay(self, events: Iterable[CanonicalEvent]) -> str:
        ordered = sorted(events, key=lambda item: (
            item.context.block_number_or_slot, item.context.event_index,
        ))
        seen: set[tuple[str, int]] = set()
        for event in ordered:
            identity = (event.context.transaction_id, event.context.event_index)
            if identity in seen:
                continue
            seen.add(identity)
            self.apply(event)
        return self.state

