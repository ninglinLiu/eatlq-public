from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ValidationError(ValueError):
    """Raised when a record violates the canonical data contract."""


class Confidence(StrEnum):
    HARD = "hard_attribution"
    PROBABILISTIC = "probabilistic_attribution"
    UNRESOLVED = "unresolved"


class SourceTier(StrEnum):
    RAW_CHAIN = "RAW_CHAIN"
    VERIFIED_CONTRACT = "VERIFIED_CONTRACT"
    OFFICIAL_DOCS = "OFFICIAL_DOCS"
    DEX_STATE = "DEX_STATE"
    INDEXER = "INDEXER"
    PROTOCOL_API = "PROTOCOL_API"
    FRONTEND = "FRONTEND"
    OFFICIAL_SOCIAL = "OFFICIAL_SOCIAL"
    THIRD_PARTY = "THIRD_PARTY"
    SYNTHETIC = "SYNTHETIC"


def parse_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValidationError("timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventContext:
    chain: str
    block_number_or_slot: int
    block_timestamp: str
    transaction_id: str
    event_index: int
    launchpad_id: str
    version_id: str
    token: str
    pool: str | None
    observation_time: str
    information_available_at: str
    ingested_at: str
    source: str
    label_time: str | None = None

    def validate(self) -> None:
        if not self.chain or not self.transaction_id or not self.token:
            raise ValidationError("chain, transaction_id and token are required")
        if self.block_number_or_slot < 0 or self.event_index < 0:
            raise ValidationError("block/slot and event index must be non-negative")
        event_time = parse_time(self.block_timestamp)
        observed = parse_time(self.observation_time)
        available = parse_time(self.information_available_at)
        ingested = parse_time(self.ingested_at)
        if event_time > observed:
            raise ValidationError("event_time cannot be after observation_time")
        if available > observed:
            raise ValidationError("information_available_at cannot be after observation_time")
        if observed > ingested:
            raise ValidationError("observation_time cannot be after ingestion_time")
        if self.label_time and parse_time(self.label_time) < observed:
            raise ValidationError("label_time cannot precede observation_time")


@dataclass(frozen=True)
class CanonicalEvent:
    context: EventContext
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.context.validate()
        if not self.event_type:
            raise ValidationError("event_type is required")

    def as_record(self) -> dict[str, Any]:
        self.validate()
        return {**asdict(self.context), "event_type": self.event_type, "payload": self.payload}


@dataclass(frozen=True)
class Provenance:
    source: str
    source_tier: SourceTier
    retrieved_at: str
    effective_at: str
    confidence: float
    raw_payload_hash: str
    conflict_status: str = "NONE"

    def validate(self) -> None:
        parse_time(self.retrieved_at)
        parse_time(self.effective_at)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("confidence must be within [0, 1]")
        if len(self.raw_payload_hash) != 64:
            raise ValidationError("raw_payload_hash must be sha256 hex")

