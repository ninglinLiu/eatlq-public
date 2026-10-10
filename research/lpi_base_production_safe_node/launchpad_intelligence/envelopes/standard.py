"""Standard Event/Transaction/Log envelopes for Base chain normalization.

Construction policy: dataclasses call validate() in __post_init__, so direct
construction cannot silently bypass required-field / UNKNOWN rules.
Use EventEnvelope.from_parts for provenance-assisted construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ContractError
from ..provenance.source_provenance import SourceProvenance, provenance_hash
from ..schema.unknown import UNKNOWN, Unknown, is_unknown


REQUIRED_COMMON = (
    "chain_id",
    "block_number",
    "block_hash",
    "block_timestamp",
    "transaction_hash",
    "transaction_index",
    "log_index",
    "contract_address",
    "topic0",
    "raw_data",
    "source_name",
    "source_table",
    "observed_at",
    "ingested_at",
    "point_in_time_available_at",
    "provenance_hash",
)

TX_REQUIRED = (
    "chain_id",
    "block_number",
    "block_hash",
    "block_timestamp",
    "transaction_hash",
    "transaction_index",
    "from_address",
    "to_address",
    "status",
    "source_name",
    "source_table",
    "observed_at",
    "ingested_at",
    "point_in_time_available_at",
    "provenance_hash",
)

LOG_REQUIRED = REQUIRED_COMMON  # plus topics tuple checked separately


def _reject_blank(value: Any, key: str) -> Any:
    if value is None or value == "":
        raise ContractError(f"{key} must not be None/empty; use UNKNOWN sentinel")
    return value


def _validate_keys(data: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise ContractError(f"missing envelope fields: {missing}")
    if data.get("chain_id") != 8453 and not is_unknown(data.get("chain_id")):
        raise ContractError("this node only accepts chain_id=8453 (Base) or UNKNOWN")
    if isinstance(data.get("block_number"), int) and data["block_number"] < 0:
        raise ContractError("block_number must be non-negative")
    for key in keys:
        _reject_blank(data[key], key)


def validate_common_fields(data: dict[str, Any]) -> None:
    _validate_keys(data, REQUIRED_COMMON)


@dataclass(frozen=True)
class EventEnvelope:
    chain_id: int | Unknown
    block_number: int | Unknown
    block_hash: str | Unknown
    block_timestamp: str | Unknown
    transaction_hash: str | Unknown
    transaction_index: int | Unknown
    log_index: int | Unknown
    contract_address: str | Unknown
    topic0: str | Unknown
    raw_data: str | Unknown
    source_name: str
    source_table: str | Unknown
    observed_at: str
    ingested_at: str
    point_in_time_available_at: str
    provenance_hash: str
    event_name: str = "UNKNOWN_EVENT"
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        validate_common_fields(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "transaction_hash": self.transaction_hash,
            "transaction_index": self.transaction_index,
            "log_index": self.log_index,
            "contract_address": self.contract_address,
            "topic0": self.topic0,
            "raw_data": self.raw_data,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "observed_at": self.observed_at,
            "ingested_at": self.ingested_at,
            "point_in_time_available_at": self.point_in_time_available_at,
            "provenance_hash": self.provenance_hash,
            "event_name": self.event_name,
            "payload": self.payload,
        }

    @classmethod
    def from_parts(
        cls,
        *,
        common: dict[str, Any],
        provenance: SourceProvenance,
        event_name: str = "UNKNOWN_EVENT",
        payload: dict[str, Any] | None = None,
    ) -> "EventEnvelope":
        provenance.validate()
        body = dict(common)
        source_table = provenance.source_table
        if source_table is None or source_table == "":
            source_table = Unknown("SOURCE_TABLE_UNAVAILABLE")
        body.update(
            {
                "source_name": provenance.source_name,
                "source_table": source_table,
                "observed_at": provenance.observed_at,
                "ingested_at": provenance.ingested_at,
                "point_in_time_available_at": provenance.point_in_time_available_at,
            }
        )
        body["provenance_hash"] = provenance_hash(
            {k: (v.to_json() if is_unknown(v) else v) for k, v in body.items()}
        )
        return cls(
            chain_id=body["chain_id"],
            block_number=body["block_number"],
            block_hash=body["block_hash"],
            block_timestamp=body["block_timestamp"],
            transaction_hash=body["transaction_hash"],
            transaction_index=body["transaction_index"],
            log_index=body["log_index"],
            contract_address=body["contract_address"],
            topic0=body["topic0"],
            raw_data=body["raw_data"],
            source_name=body["source_name"],
            source_table=body["source_table"],
            observed_at=body["observed_at"],
            ingested_at=body["ingested_at"],
            point_in_time_available_at=body["point_in_time_available_at"],
            provenance_hash=body["provenance_hash"],
            event_name=event_name,
            payload=payload or {},
        )


@dataclass(frozen=True)
class TransactionEnvelope:
    chain_id: int | Unknown
    block_number: int | Unknown
    block_hash: str | Unknown
    block_timestamp: str | Unknown
    transaction_hash: str | Unknown
    transaction_index: int | Unknown
    from_address: str | Unknown
    to_address: str | Unknown
    status: str | Unknown
    source_name: str
    source_table: str | Unknown
    observed_at: str
    ingested_at: str
    point_in_time_available_at: str
    provenance_hash: str
    keep_failed: bool = True  # optional policy flag, not a chain field

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        data = {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "transaction_hash": self.transaction_hash,
            "transaction_index": self.transaction_index,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "status": self.status,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "observed_at": self.observed_at,
            "ingested_at": self.ingested_at,
            "point_in_time_available_at": self.point_in_time_available_at,
            "provenance_hash": self.provenance_hash,
        }
        _validate_keys(data, TX_REQUIRED)


@dataclass(frozen=True)
class LogEnvelope:
    chain_id: int | Unknown
    block_number: int | Unknown
    block_hash: str | Unknown
    block_timestamp: str | Unknown
    transaction_hash: str | Unknown
    transaction_index: int | Unknown
    log_index: int | Unknown
    contract_address: str | Unknown
    topic0: str | Unknown
    topics: tuple[str, ...]
    raw_data: str | Unknown
    source_name: str
    source_table: str | Unknown
    observed_at: str
    ingested_at: str
    point_in_time_available_at: str
    provenance_hash: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        data = {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "transaction_hash": self.transaction_hash,
            "transaction_index": self.transaction_index,
            "log_index": self.log_index,
            "contract_address": self.contract_address,
            "topic0": self.topic0,
            "raw_data": self.raw_data,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "observed_at": self.observed_at,
            "ingested_at": self.ingested_at,
            "point_in_time_available_at": self.point_in_time_available_at,
            "provenance_hash": self.provenance_hash,
        }
        _validate_keys(data, LOG_REQUIRED)
        if self.topics is None:
            raise ContractError("topics must not be None")


def _reject_blank_field(value: Any, key: str) -> None:
    """Reject None/empty string; Unknown and real zero are allowed."""
    if value is None or value == "":
        raise ContractError(f"{key} must not be None/empty; use UNKNOWN sentinel")


def _require_event(event: Any) -> None:
    if not isinstance(event, EventEnvelope):
        raise ContractError("event must be a valid EventEnvelope instance")
    event.validate()


@dataclass(frozen=True)
class TransferEnvelope:
    event: EventEnvelope
    token: str | Unknown
    from_address: str | Unknown
    to_address: str | Unknown
    amount_raw: str | Unknown

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_event(self.event)
        for key in ("token", "from_address", "to_address", "amount_raw"):
            _reject_blank_field(getattr(self, key), key)


@dataclass(frozen=True)
class SwapEnvelope:
    event: EventEnvelope
    pool: str | Unknown
    token_in: str | Unknown
    token_out: str | Unknown
    amount_in_raw: str | Unknown
    amount_out_raw: str | Unknown
    trader: str | Unknown

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_event(self.event)
        for key in ("pool", "token_in", "token_out", "amount_in_raw", "amount_out_raw", "trader"):
            _reject_blank_field(getattr(self, key), key)


@dataclass(frozen=True)
class PoolEnvelope:
    event: EventEnvelope
    pool: str | Unknown
    token0: str | Unknown
    token1: str | Unknown
    fee_or_hook: str | Unknown

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_event(self.event)
        for key in ("pool", "token0", "token1", "fee_or_hook"):
            _reject_blank_field(getattr(self, key), key)


@dataclass(frozen=True)
class LiquidityEnvelope:
    event: EventEnvelope
    pool: str | Unknown
    owner: str | Unknown
    action: str | Unknown
    amount0_raw: str | Unknown
    amount1_raw: str | Unknown

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_event(self.event)
        for key in ("pool", "owner", "action", "amount0_raw", "amount1_raw"):
            _reject_blank_field(getattr(self, key), key)


_ = UNKNOWN
