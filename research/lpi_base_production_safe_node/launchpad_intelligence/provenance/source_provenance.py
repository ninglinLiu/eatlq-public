"""Source provenance records for every normalized envelope."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SourceProvenance:
    source_name: str
    source_table: str | None
    source_file: str | None
    query_hash: str | None
    observed_at: str
    ingested_at: str
    point_in_time_available_at: str
    collector: str
    notes: str = ""

    def validate(self) -> None:
        if not self.source_name:
            raise ValueError("source_name required")
        if not self.observed_at or not self.ingested_at or not self.point_in_time_available_at:
            raise ValueError("provenance timestamps required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provenance_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
