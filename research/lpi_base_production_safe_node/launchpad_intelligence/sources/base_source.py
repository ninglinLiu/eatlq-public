"""Base Source interface — all adapters implement this contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator

from ..config import AccessDefaults, DEFAULT_ACCESS
from ..envelopes.standard import EventEnvelope, LogEnvelope, TransactionEnvelope
from ..errors import AccessDeniedError, PreflightError
from ..query.limits import validate_query_bounds_core


@dataclass(frozen=True)
class QueryBounds:
    start_block: int
    end_block: int
    limit: int
    explicit_columns: tuple[str, ...]
    max_threads: int = 1
    statement_timeout_sec: int = 10
    max_bytes_to_read: int = 8_388_608
    dry_run: bool = True
    explain_only: bool = True
    checkpoint_path: str | None = None

    def validate(self) -> None:
        try:
            validate_query_bounds_core(
                start_block=self.start_block,
                end_block=self.end_block,
                limit=self.limit,
                explicit_columns=self.explicit_columns,
                max_threads=self.max_threads,
                statement_timeout_sec=self.statement_timeout_sec,
                max_bytes_to_read=self.max_bytes_to_read,
                dry_run=self.dry_run,
                explain_only=self.explain_only,
                checkpoint_path=self.checkpoint_path,
            )
        except PreflightError as exc:
            raise ValueError(str(exc)) from exc


@dataclass(frozen=True)
class SourceCapabilities:
    """Split contract targets from runtime availability."""

    contract_supports_logs: bool = True
    contract_supports_transactions: bool = True
    contract_supports_events: bool = True
    contract_supports_checkpoint: bool = True
    contract_supports_dry_run: bool = True
    contract_supports_explain_only: bool = True
    runtime_available_logs: bool = False
    runtime_available_transactions: bool = False
    runtime_available_events: bool = False
    runtime_transport_implemented: bool = False
    requires_database: bool = False
    requires_rpc: bool = False
    requires_http: bool = False

    # Backward-compatible aliases used by older tests/docs — mean contract targets.
    @property
    def supports_logs(self) -> bool:
        return self.contract_supports_logs

    @property
    def supports_transactions(self) -> bool:
        return self.contract_supports_transactions

    @property
    def supports_events(self) -> bool:
        return self.contract_supports_events

    @property
    def supports_checkpoint(self) -> bool:
        return self.contract_supports_checkpoint

    @property
    def supports_dry_run(self) -> bool:
        return self.contract_supports_dry_run

    @property
    def supports_explain_only(self) -> bool:
        return self.contract_supports_explain_only


class BaseSource(ABC):
    """Read-only Base chain source. Defaults fail closed."""

    source_name: str = "unnamed"
    chain_id: int = 8453

    def __init__(self, access: AccessDefaults | None = None) -> None:
        self.access = access or DEFAULT_ACCESS

    @abstractmethod
    def capabilities(self) -> SourceCapabilities: ...

    def _gate(self) -> None:
        caps = self.capabilities()
        if caps.requires_database:
            self.access.require_database_access()
        if caps.requires_rpc:
            self.access.require_rpc_access()
        if caps.requires_http:
            self.access.require_http_access()
        if not caps.runtime_transport_implemented:
            raise AccessDeniedError(
                f"{self.source_name}: runtime transport not implemented "
                "(contract-supported != runtime-available)"
            )

    def iter_logs(self, bounds: QueryBounds) -> Iterator[LogEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError(f"{self.source_name}: iter_logs blocked")

    def iter_transactions(self, bounds: QueryBounds) -> Iterator[TransactionEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError(f"{self.source_name}: iter_transactions blocked")

    def iter_events(self, bounds: QueryBounds) -> Iterator[EventEnvelope]:
        bounds.validate()
        self._gate()
        raise AccessDeniedError(f"{self.source_name}: iter_events blocked")

    def explain(self, bounds: QueryBounds) -> dict[str, Any]:
        bounds.validate()
        caps = self.capabilities()
        return {
            "source_name": self.source_name,
            "dry_run": True,
            "explain_only": True,
            "start_block": bounds.start_block,
            "end_block": bounds.end_block,
            "limit": bounds.limit,
            "explicit_columns": list(bounds.explicit_columns),
            "max_threads": bounds.max_threads,
            "statement_timeout_sec": bounds.statement_timeout_sec,
            "max_bytes_to_read": bounds.max_bytes_to_read,
            "execution_authorized": False,
            "contract_supported": {
                "logs": caps.contract_supports_logs,
                "transactions": caps.contract_supports_transactions,
                "events": caps.contract_supports_events,
            },
            "runtime_available": {
                "logs": caps.runtime_available_logs,
                "transactions": caps.runtime_available_transactions,
                "events": caps.runtime_available_events,
                "transport_implemented": caps.runtime_transport_implemented,
            },
        }
