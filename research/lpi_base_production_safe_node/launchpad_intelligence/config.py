"""Fail-closed runtime configuration loader."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AccessDeniedError, ContractError

_DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "config" / "defaults.json"


@dataclass(frozen=True)
class AccessDefaults:
    database_access_enabled: bool = False
    rpc_access_enabled: bool = False
    http_access_enabled: bool = False
    backfill_enabled: bool = False
    business_table_query_enabled: bool = False
    worker_concurrency: int = 1
    database_query_concurrency: int = 1
    rpc_concurrency: int = 1
    max_threads: int = 1
    memory_hard_ceiling_mib: int = 384
    database_statement_timeout_sec: int = 10
    request_timeout_sec: int = 10
    retry_limit: int = 1
    batch_size: int = 100
    chain_id: int = 8453
    chain_name: str = "base"
    source_host_role: str = "base_production_safe_data_node"
    workspace_root_required_prefix: str = "/root/lpi_base_production_safe_node"

    def require_database_access(self) -> None:
        if not self.database_access_enabled:
            raise AccessDeniedError("database_access_enabled=false (fail-closed)")

    def require_rpc_access(self) -> None:
        if not self.rpc_access_enabled:
            raise AccessDeniedError("rpc_access_enabled=false (fail-closed)")

    def require_http_access(self) -> None:
        if not self.http_access_enabled:
            raise AccessDeniedError("http_access_enabled=false (fail-closed)")

    def require_backfill(self) -> None:
        if not self.backfill_enabled:
            raise AccessDeniedError("backfill_enabled=false (fail-closed)")

    def require_business_table_query(self) -> None:
        if not self.business_table_query_enabled:
            raise AccessDeniedError("business_table_query_enabled=false (fail-closed)")


def _as_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be bool")
    return value


def load_access_defaults(path: Path | None = None) -> AccessDefaults:
    cfg_path = path or _DEFAULTS_PATH
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    return AccessDefaults(
        database_access_enabled=_as_bool(raw.get("database_access_enabled", False), "database_access_enabled"),
        rpc_access_enabled=_as_bool(raw.get("rpc_access_enabled", False), "rpc_access_enabled"),
        http_access_enabled=_as_bool(raw.get("http_access_enabled", False), "http_access_enabled"),
        backfill_enabled=_as_bool(raw.get("backfill_enabled", False), "backfill_enabled"),
        business_table_query_enabled=_as_bool(
            raw.get("business_table_query_enabled", False), "business_table_query_enabled"
        ),
        worker_concurrency=int(raw.get("worker_concurrency", 1)),
        database_query_concurrency=int(raw.get("database_query_concurrency", 1)),
        rpc_concurrency=int(raw.get("rpc_concurrency", 1)),
        max_threads=int(raw.get("max_threads", 1)),
        memory_hard_ceiling_mib=int(raw.get("memory_hard_ceiling_mib", 384)),
        database_statement_timeout_sec=int(raw.get("database_statement_timeout_sec", 10)),
        request_timeout_sec=int(raw.get("request_timeout_sec", 10)),
        retry_limit=int(raw.get("retry_limit", 1)),
        batch_size=int(raw.get("batch_size", 100)),
        chain_id=int(raw.get("chain_id", 8453)),
        chain_name=str(raw.get("chain_name", "base")),
        source_host_role=str(raw.get("source_host_role", "base_production_safe_data_node")),
        workspace_root_required_prefix=str(
            raw.get("workspace_root_required_prefix", "/root/lpi_base_production_safe_node")
        ),
    )


DEFAULT_ACCESS = load_access_defaults()
