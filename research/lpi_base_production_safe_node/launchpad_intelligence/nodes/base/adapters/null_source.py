"""Null Base source — always fail-closed; used for offline interface tests."""

from __future__ import annotations

from ....sources.base_source import BaseSource, SourceCapabilities


class NullBaseSource(BaseSource):
    source_name = "base.null"

    def capabilities(self) -> SourceCapabilities:
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
            requires_database=False,
            requires_rpc=False,
            requires_http=False,
        )
