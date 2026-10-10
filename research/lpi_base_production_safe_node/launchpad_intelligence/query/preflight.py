"""Query preflight — plan and validate bounded reads without executing them."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import PreflightError
from .identifiers import assert_safe_identifier
from .limits import (
    MAX_BLOCK_SPAN,
    MAX_BYTES,
    MAX_LIMIT,
    MAX_TIMEOUT_SEC,
    MIN_BYTES,
    MIN_TIMEOUT_SEC,
    assert_safe_checkpoint,
    validate_query_bounds_core,
)

FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|OPTIMIZE|MUTATION|ATTACH|DETACH|"
    r"CREATE\s+TABLE|CREATE\s+INDEX|SYSTEM\s+|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
SELECT_STAR = re.compile(r"SELECT\s+\*", re.IGNORECASE)


@dataclass(frozen=True)
class BoundedQueryPlan:
    candidate_table: str
    explicit_columns: tuple[str, ...]
    partition_key: str
    start_block: int
    end_block: int
    limit: int
    max_threads: int = 1
    statement_timeout_sec: int = 10
    max_bytes_to_read: int = 8_388_608
    readonly: bool = True
    dry_run: bool = True
    explain_only: bool = True
    checkpoint_path: str | None = None
    estimated_read_bytes: int | None = None
    stop_conditions: tuple[str, ...] = (
        "statement_timeout",
        "max_bytes_to_read",
        "limit_reached",
        "health_guard_trip",
        "access_defaults_closed",
    )

    def validate(self) -> None:
        if not self.readonly:
            raise PreflightError("plan must be readonly")
        assert_safe_identifier(self.candidate_table, field="candidate_table", allow_qualified=True)
        if "." not in self.candidate_table:
            raise PreflightError("candidate_table must be schema.table logical form")
        for col in self.explicit_columns:
            assert_safe_identifier(col, field="explicit_columns")
        assert_safe_identifier(self.partition_key, field="partition_key")
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

    def plan_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class PreflightReport:
    plan: BoundedQueryPlan
    status: str
    required_fields: list[str]
    estimated_read_bytes: int
    estimated_read_bytes_kind: str
    query_timeout_sec: int
    stop_conditions: list[str]
    full_scan_prevention: list[str]
    offline_enforced_constraints: list[str]
    future_db_side_enforced_settings: list[str]
    sql_skeleton: str
    notes: list[str] = field(default_factory=list)
    execution_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "plan_hash": self.plan.plan_hash(),
            "candidate_table": self.plan.candidate_table,
            "required_fields": self.required_fields,
            "partition_key": self.plan.partition_key,
            "start_block": self.plan.start_block,
            "end_block": self.plan.end_block,
            "limit": self.plan.limit,
            "estimated_read_bytes": self.estimated_read_bytes,
            "estimated_read_bytes_kind": self.estimated_read_bytes_kind,
            "query_timeout_sec": self.query_timeout_sec,
            "max_threads": self.plan.max_threads,
            "max_bytes_to_read": self.plan.max_bytes_to_read,
            "stop_conditions": self.stop_conditions,
            "full_scan_prevention": self.full_scan_prevention,
            "offline_enforced_constraints": self.offline_enforced_constraints,
            "future_db_side_enforced_settings": self.future_db_side_enforced_settings,
            "sql_skeleton": self.sql_skeleton,
            "dry_run": self.plan.dry_run,
            "explain_only": self.plan.explain_only,
            "checkpoint_path": self.plan.checkpoint_path,
            "execution_authorized": self.execution_authorized,
            "notes": self.notes,
        }


def validate_plan_sql_shape(sql: str) -> list[str]:
    """Static SQL shape checks — never executes. Operates on builder output only."""
    problems: list[str] = []
    if FORBIDDEN_SQL.search(sql):
        problems.append("contains_mutating_or_admin_keyword")
    if SELECT_STAR.search(sql):
        problems.append("select_star_forbidden")
    if " LIMIT " not in f" {sql.upper()} ":
        problems.append("missing_limit")
    if "WHERE" not in sql.upper():
        problems.append("missing_where")
    return problems


def build_preflight(plan: BoundedQueryPlan, *, bytes_per_block_estimate: int = 4096) -> PreflightReport:
    plan.validate()
    cols = ", ".join(plan.explicit_columns)
    block_span = plan.end_block - plan.start_block + 1
    estimated = plan.estimated_read_bytes or min(
        plan.max_bytes_to_read,
        block_span * bytes_per_block_estimate,
    )
    sql = (
        f"SELECT {cols} FROM {plan.candidate_table} "
        f"WHERE {plan.partition_key} >= {plan.start_block} "
        f"AND {plan.partition_key} <= {plan.end_block} "
        f"LIMIT {plan.limit}"
    )
    problems = validate_plan_sql_shape(sql)
    if problems:
        raise PreflightError(f"sql skeleton rejected: {problems}")

    prevention = [
        "strict_identifier_syntax",
        "explicit_column_projection",
        f"partition_predicate_on_{plan.partition_key}",
        f"bounded_block_range_{plan.start_block}_{plan.end_block}",
        f"LIMIT_{plan.limit}",
        "max_threads=1",
        f"max_bytes_to_read={plan.max_bytes_to_read}",
        f"statement_timeout_sec={plan.statement_timeout_sec}",
        "readonly=true",
        "dry_run_and_explain_only_this_stage",
        "no_select_star",
    ]
    offline = [
        "identifier_whitelist_syntax",
        "block_span_hard_max_1000",
        "limit_hard_max_1000",
        "timeout_range_1_to_10",
        "max_bytes_range_1_to_8388608",
        "max_threads_eq_1",
        "dry_run_explain_only_forced",
        "execution_authorized_false",
    ]
    future_db = [
        "readonly=2_or_equivalent",
        "max_threads_session_setting",
        "max_execution_time_session_setting",
        "max_bytes_to_read_session_setting",
        "server_side_cancel_on_timeout",
    ]
    return PreflightReport(
        plan=plan,
        status="PREFLIGHT_READY_NOT_EXECUTED",
        required_fields=list(plan.explicit_columns),
        estimated_read_bytes=estimated,
        estimated_read_bytes_kind="offline_unverified_heuristic",
        query_timeout_sec=plan.statement_timeout_sec,
        stop_conditions=list(plan.stop_conditions),
        full_scan_prevention=prevention,
        offline_enforced_constraints=offline,
        future_db_side_enforced_settings=future_db,
        sql_skeleton=sql,
        notes=[
            "Candidate table name is logical and unbound to production until owner approval.",
            "estimated_read_bytes is offline_unverified_heuristic, not a ClickHouse measurement.",
            "This report does not authorize business-table queries.",
        ],
        execution_authorized=False,
    )
