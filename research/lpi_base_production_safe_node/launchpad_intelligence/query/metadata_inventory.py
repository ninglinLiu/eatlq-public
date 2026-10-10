"""MetadataInventoryPlan — separate security domain from BoundedQueryPlan.

Builds fixed-template SELECTs against an allowlisted subset of system.* tables.
Never accepts arbitrary user SQL. Does not authorize business-table reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from ..errors import PreflightError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BANNED_CHARS = ("'", '"', "`", ";", "--", "/*", "*/", "\\", "(", ")", "=", ",", "<", ">", "*", " ")


def assert_safe_metadata_ident(value: str, *, field: str) -> None:
    """Identifier check for system-catalog plans (allows names like table/name/type)."""
    if not isinstance(value, str) or not value:
        raise PreflightError(f"{field}: empty identifier forbidden")
    for token in _BANNED_CHARS:
        if token in value:
            raise PreflightError(f"{field}: forbidden token {token!r}")
    if not _IDENT.match(value):
        raise PreflightError(f"{field}: invalid identifier syntax")


ALLOWED_SYSTEM_TABLES = frozenset(
    {
        "system.tables",
        "system.columns",
        "system.databases",
        "system.processes",
        "system.merges",
    }
)

# Schema inventory prefers tables/columns; processes/merges are health-only.
SCHEMA_INVENTORY_TABLES = frozenset({"system.tables", "system.columns"})
HEALTH_ONLY_TABLES = frozenset({"system.processes", "system.merges"})

MAX_LIMIT = 5000
MAX_RESULT_ROWS = 5000
MAX_RESULT_BYTES = 2_097_152
MAX_BYTES_TO_READ = 4_194_304
MAX_EXECUTION_TIME_SEC = 3
REQUIRED_READONLY = 2
REQUIRED_MAX_THREADS = 1

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|OPTIMIZE|MUTATION|ATTACH|DETACH|"
    r"CREATE\s+|GRANT|REVOKE|JOIN|UNION|INTO\s+OUTFILE|FORMAT\s+Native)\b|"
    r";|--|/\*|\*/",
    re.IGNORECASE,
)
_SELECT_STAR = re.compile(r"SELECT\s+\*", re.IGNORECASE)
_MULTI_STATEMENT = re.compile(r";")


@dataclass(frozen=True)
class MetadataInventoryPlan:
    """Fixed-template plan for a single system.* metadata/health query."""

    purpose: str
    system_table: str
    explicit_columns: tuple[str, ...]
    database_filter: str | None
    table_name_filter: str | None = None
    limit: int = 5000
    readonly: int = REQUIRED_READONLY
    max_threads: int = REQUIRED_MAX_THREADS
    max_execution_time_sec: int = MAX_EXECUTION_TIME_SEC
    max_result_rows: int = MAX_RESULT_ROWS
    max_result_bytes: int = MAX_RESULT_BYTES
    max_bytes_to_read: int = MAX_BYTES_TO_READ
    execution_authorized: bool = False
    query_id: str = ""

    def validate(self) -> None:
        if not self.purpose or not isinstance(self.purpose, str):
            raise PreflightError("purpose required")
        if self.system_table not in ALLOWED_SYSTEM_TABLES:
            raise PreflightError(f"system_table not allowlisted: {self.system_table!r}")
        if not self.explicit_columns:
            raise PreflightError("explicit_columns required")
        for col in self.explicit_columns:
            if col == "*":
                raise PreflightError("SELECT * forbidden")
            assert_safe_metadata_ident(col, field="explicit_columns")
        if self.system_table in SCHEMA_INVENTORY_TABLES:
            if not self.database_filter:
                raise PreflightError("database_filter required for schema inventory tables")
            assert_safe_metadata_ident(self.database_filter, field="database_filter")
        elif self.system_table == "system.databases":
            if self.database_filter is not None:
                assert_safe_metadata_ident(self.database_filter, field="database_filter")
        else:
            # health-only tables: database_filter must be absent (no business DB filter semantics)
            if self.database_filter is not None:
                raise PreflightError("database_filter forbidden for health-only system tables")
        if self.table_name_filter is not None:
            if self.system_table not in {"system.columns", "system.tables"}:
                raise PreflightError("table_name_filter only allowed for system.tables/columns")
            assert_safe_metadata_ident(self.table_name_filter, field="table_name_filter")
        if self.limit < 1 or self.limit > MAX_LIMIT:
            raise PreflightError(f"limit out of range 1..{MAX_LIMIT}")
        if self.readonly != REQUIRED_READONLY:
            raise PreflightError(f"readonly must be {REQUIRED_READONLY}")
        if self.max_threads != REQUIRED_MAX_THREADS:
            raise PreflightError("max_threads must be 1")
        if self.max_execution_time_sec < 1 or self.max_execution_time_sec > MAX_EXECUTION_TIME_SEC:
            raise PreflightError(f"max_execution_time_sec must be 1..{MAX_EXECUTION_TIME_SEC}")
        if self.max_result_rows < 1 or self.max_result_rows > MAX_RESULT_ROWS:
            raise PreflightError("max_result_rows out of range")
        if self.max_result_bytes < 1 or self.max_result_bytes > MAX_RESULT_BYTES:
            raise PreflightError("max_result_bytes out of range")
        if self.max_bytes_to_read < 1 or self.max_bytes_to_read > MAX_BYTES_TO_READ:
            raise PreflightError("max_bytes_to_read out of range")
        if self.query_id:
            assert_safe_metadata_ident(self.query_id, field="query_id")
        # Validate rendered SQL shape without trusting external SQL.
        sql = self.render_sql()
        validate_metadata_sql(sql, expected_table=self.system_table)

    def render_sql(self) -> str:
        cols = ", ".join(self.explicit_columns)
        table = self.system_table
        where_parts: list[str] = []
        if self.system_table == "system.databases":
            if self.database_filter is not None:
                where_parts.append(f"name = '{self.database_filter}'")
        elif self.system_table in SCHEMA_INVENTORY_TABLES:
            assert self.database_filter is not None
            where_parts.append(f"database = '{self.database_filter}'")
            if self.table_name_filter is not None:
                col = "name" if self.system_table == "system.tables" else "table"
                where_parts.append(f"{col} = '{self.table_name_filter}'")
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        return f"SELECT {cols} FROM {table}{where_sql} LIMIT {self.limit}"

    def settings_dict(self) -> dict[str, Any]:
        return {
            "readonly": self.readonly,
            "max_threads": self.max_threads,
            "max_execution_time": self.max_execution_time_sec,
            "max_result_rows": self.max_result_rows,
            "max_result_bytes": self.max_result_bytes,
            "max_bytes_to_read": self.max_bytes_to_read,
        }

    def query_hash(self) -> str:
        payload = {
            "sql": self.render_sql(),
            "settings": self.settings_dict(),
            "purpose": self.purpose,
            "system_table": self.system_table,
            "execution_authorized": self.execution_authorized,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "query_id": self.query_id or self.purpose,
            "purpose": self.purpose,
            "system_table": self.system_table,
            "sql": self.render_sql(),
            "settings": self.settings_dict(),
            "expected_columns": list(self.explicit_columns),
            "limit": self.limit,
            "database_filter": self.database_filter,
            "table_name_filter": self.table_name_filter,
            "execution_authorized": self.execution_authorized,
            "query_hash": self.query_hash(),
            "plan_fields": asdict(self),
        }


def validate_metadata_sql(sql: str, *, expected_table: str) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise PreflightError("empty SQL forbidden")
    if _MULTI_STATEMENT.search(sql.rstrip().rstrip(";")) and sql.count(";") > 0:
        # Disallow any semicolon (no multi-statement). Trailing semicolon also banned.
        raise PreflightError("semicolon / multi-statement forbidden")
    if ";" in sql:
        raise PreflightError("semicolon forbidden")
    if _SELECT_STAR.search(sql):
        raise PreflightError("SELECT * forbidden")
    if _FORBIDDEN_SQL.search(sql):
        raise PreflightError("forbidden SQL token")
    if not re.match(r"^SELECT\s+", sql, re.IGNORECASE):
        raise PreflightError("only SELECT allowed")
    if not re.search(rf"\bFROM\s+{re.escape(expected_table)}\b", sql, re.IGNORECASE):
        raise PreflightError("FROM must target expected allowlisted system table")
    # Reject any other FROM targets / non-system schema reads.
    from_match = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE)
    if not from_match:
        raise PreflightError("qualified system table required in FROM")
    if from_match.group(1).lower() not in {t.lower() for t in ALLOWED_SYSTEM_TABLES}:
        raise PreflightError("non-allowlisted FROM target")
    if from_match.group(1).lower().split(".", 1)[0] != "system":
        raise PreflightError("non-system schema forbidden")
    # Reject nested selects / subqueries by forbidding second SELECT.
    if len(re.findall(r"\bSELECT\b", sql, re.IGNORECASE)) != 1:
        raise PreflightError("subquery / nested SELECT forbidden")
    if expected_table not in ALLOWED_SYSTEM_TABLES:
        raise PreflightError("expected_table not allowlisted")


def require_execution_authorized(plan: MetadataInventoryPlan) -> None:
    plan.validate()
    if not plan.execution_authorized:
        raise PreflightError("execution_authorized=false; refuse execute")


def assert_sql_matches_plan(plan: MetadataInventoryPlan, sql: str) -> None:
    plan.validate()
    if sql != plan.render_sql():
        raise PreflightError("executed SQL does not match pre-registered plan SQL")
    if hashlib.sha256(
        json.dumps(
            {"sql": sql, "settings": plan.settings_dict(), "purpose": plan.purpose, "system_table": plan.system_table, "execution_authorized": plan.execution_authorized},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest() != plan.query_hash():
        raise PreflightError("query hash mismatch against plan")


def build_default_schema_inventory_plans(
    *,
    app_database: str = "eatlq",
    execution_authorized: bool = False,
) -> list[MetadataInventoryPlan]:
    """Canonical pre-registered plans for this inventory round."""
    assert_safe_metadata_ident(app_database, field="app_database")
    # total_rows/total_bytes omitted by default (optional; not required for binding).
    tables_cols = (
        "database",
        "name",
        "engine",
        "partition_key",
        "sorting_key",
        "primary_key",
        "sampling_key",
        "storage_policy",
    )
    columns_cols = (
        "database",
        "table",
        "name",
        "type",
        "position",
        "default_kind",
        "default_expression",
        "is_in_partition_key",
        "is_in_sorting_key",
        "is_in_primary_key",
        "is_in_sampling_key",
    )
    plans = [
        MetadataInventoryPlan(
            query_id="q_sys_tables_columns",
            purpose="introspect_system_tables_columns",
            system_table="system.columns",
            explicit_columns=("database", "table", "name", "type", "position"),
            database_filter="system",
            table_name_filter="tables",
            limit=200,
            execution_authorized=execution_authorized,
        ),
        MetadataInventoryPlan(
            query_id="q_sys_columns_columns",
            purpose="introspect_system_columns_columns",
            system_table="system.columns",
            explicit_columns=("database", "table", "name", "type", "position"),
            database_filter="system",
            table_name_filter="columns",
            limit=200,
            execution_authorized=execution_authorized,
        ),
        MetadataInventoryPlan(
            query_id="q_app_databases",
            purpose="confirm_app_database_exists",
            system_table="system.databases",
            explicit_columns=("name", "engine"),
            database_filter=app_database,
            limit=10,
            execution_authorized=execution_authorized,
        ),
        MetadataInventoryPlan(
            query_id="q_app_tables",
            purpose="list_app_table_schema_metadata",
            system_table="system.tables",
            explicit_columns=tables_cols,
            database_filter=app_database,
            limit=5000,
            execution_authorized=execution_authorized,
        ),
        MetadataInventoryPlan(
            query_id="q_app_columns",
            purpose="list_app_column_schema_metadata",
            system_table="system.columns",
            explicit_columns=columns_cols,
            database_filter=app_database,
            limit=5000,
            execution_authorized=execution_authorized,
        ),
    ]
    for p in plans:
        p.validate()
    return plans


def build_health_system_plans(*, execution_authorized: bool = False) -> list[MetadataInventoryPlan]:
    return [
        MetadataInventoryPlan(
            query_id="q_health_processes",
            purpose="health_active_queries",
            system_table="system.processes",
            explicit_columns=("query_id", "elapsed", "read_rows", "memory_usage"),
            database_filter=None,
            limit=100,
            execution_authorized=execution_authorized,
        ),
        MetadataInventoryPlan(
            query_id="q_health_merges",
            purpose="health_active_merges",
            system_table="system.merges",
            explicit_columns=("database", "table", "elapsed", "progress", "num_parts"),
            database_filter=None,
            limit=100,
            execution_authorized=execution_authorized,
        ),
    ]


@dataclass
class MetadataPlanRegistry:
    """Sealed set of plans; execute only by query_id with hash match."""

    plans: dict[str, MetadataInventoryPlan] = field(default_factory=dict)

    @classmethod
    def from_plans(cls, plans: list[MetadataInventoryPlan]) -> MetadataPlanRegistry:
        reg = cls()
        for p in plans:
            p.validate()
            qid = p.query_id or p.purpose
            if qid in reg.plans:
                raise PreflightError(f"duplicate query_id: {qid}")
            reg.plans[qid] = p
        return reg

    def to_list(self) -> list[dict[str, Any]]:
        return [self.plans[k].to_dict() for k in sorted(self.plans)]

    def get(self, query_id: str) -> MetadataInventoryPlan:
        if query_id not in self.plans:
            raise PreflightError(f"unknown query_id: {query_id}")
        return self.plans[query_id]

    def authorize_all(self) -> MetadataPlanRegistry:
        out: list[MetadataInventoryPlan] = []
        for p in self.plans.values():
            out.append(
                MetadataInventoryPlan(
                    **{**asdict(p), "execution_authorized": True}  # type: ignore[arg-type]
                )
            )
        return MetadataPlanRegistry.from_plans(out)
