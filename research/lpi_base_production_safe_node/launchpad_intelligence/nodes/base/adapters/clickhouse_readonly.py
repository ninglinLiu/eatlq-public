"""ClickHouse readonly adapter scaffold — no transport in offline baseline."""

from __future__ import annotations

from typing import Any, Iterator

from ....envelopes.standard import EventEnvelope, LogEnvelope, TransactionEnvelope
from ....errors import AccessDeniedError
from ....sources.base_source import BaseSource, QueryBounds, SourceCapabilities


class ClickHouseReadonlyAdapter(BaseSource):
    """Scaffold only. No host, credential, database, or table is hardcoded."""

    source_name = "base.clickhouse_readonly"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.endpoint = None
        self.database = None
        self.table = None

    def capabilities(self) -> SourceCapabilities:
        # Contract targets describe the intended API; runtime remains unavailable.
        return SourceCapabilities(
            contract_supports_logs=True,
            contract_supports_transactions=True,
            contract_supports_events=True,
            contract_supports_checkpoint=True,
            contract_supports_dry_run=True,
            contract_supports_explain_only=True,
            runtime_available_logs=False,
            runtime_available_transactions=False,
            runtime_available_events=False,
            runtime_transport_implemented=False,
            requires_database=True,
            requires_rpc=False,
            requires_http=False,
        )

    def iter_logs(self, bounds: QueryBounds) -> Iterator[LogEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError("unreachable")

    def iter_transactions(self, bounds: QueryBounds) -> Iterator[TransactionEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError("unreachable")

    def iter_events(self, bounds: QueryBounds) -> Iterator[EventEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError("unreachable")

    def explain(self, bounds: QueryBounds) -> dict[str, Any]:
        base = super().explain(bounds)
        base.update(
            {
                "adapter": self.source_name,
                "endpoint_configured": self.endpoint is not None,
                "database_configured": self.database is not None,
                "table_configured": self.table is not None,
                "note": (
                    "Scaffold explain only. Transport unimplemented; "
                    "runtime-available reads are false; no socket opened."
                ),
            }
        )
        return base
