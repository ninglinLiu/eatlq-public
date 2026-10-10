"""Offline normalization from synthetic/raw dicts to envelopes."""

from __future__ import annotations

from typing import Any

from ....envelopes.standard import EventEnvelope
from ....provenance.source_provenance import SourceProvenance
from ....schema.unknown import UNKNOWN, is_unknown


def _field(raw: dict[str, Any], key: str, default: Any = UNKNOWN) -> Any:
    if key not in raw:
        return default if not is_unknown(default) else UNKNOWN.__class__(f"MISSING_{key.upper()}")
    value = raw[key]
    if value is None or value == "":
        return UNKNOWN.__class__(f"EMPTY_{key.upper()}")
    return value


def normalize_log_dict(raw: dict[str, Any], provenance: SourceProvenance) -> EventEnvelope:
    common = {
        "chain_id": _field(raw, "chain_id", 8453),
        "block_number": _field(raw, "block_number"),
        "block_hash": _field(raw, "block_hash"),
        "block_timestamp": _field(raw, "block_timestamp"),
        "transaction_hash": _field(raw, "transaction_hash"),
        "transaction_index": _field(raw, "transaction_index"),
        "log_index": _field(raw, "log_index"),
        "contract_address": _field(raw, "contract_address"),
        "topic0": _field(raw, "topic0"),
        "raw_data": _field(raw, "raw_data"),
    }
    return EventEnvelope.from_parts(
        common=common,
        provenance=provenance,
        event_name=str(raw.get("event_name", "UNKNOWN_EVENT")),
        payload=dict(raw.get("payload") or {}),
    )
