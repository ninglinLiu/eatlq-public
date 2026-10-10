"""Metadata inventory runner status machine and exit codes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class RoundStatus(StrEnum):
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
    BLOCKED_PRE_HEALTH = "BLOCKED_PRE_HEALTH"
    FAILED_SCHEMA_QUERY = "FAILED_SCHEMA_QUERY"
    PARTIAL_SCHEMA_INVENTORY = "PARTIAL_SCHEMA_INVENTORY"
    SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE = "SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE"
    SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED = "SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    EXECUTION_SOURCE_UNSEALED = "EXECUTION_SOURCE_UNSEALED"
    BLOCKED_PRE_UNKNOWN = "BLOCKED_PRE_UNKNOWN"
    BLOCKED_POST_UNKNOWN = "BLOCKED_POST_UNKNOWN"


class RunnerExitCode(IntEnum):
    SUCCESS = 0
    ACCESS_DENIED = 3
    BLOCKED_PRE_HEALTH = 10
    BLOCKED_PRE_UNKNOWN = 11
    SCHEMA_QUERY_FAILED = 20
    SCHEMA_INVENTORY_INCOMPLETE = 21
    BLOCKED_POST_HEALTH = 30
    BLOCKED_POST_UNKNOWN = 31
    EXECUTION_SOURCE_UNSEALED = 40
    INTERNAL_ERROR = 1


@dataclass
class RunnerState:
    health_preflight_passed: bool = False
    any_health_query_executed: bool = False
    any_schema_query_executed: bool = False
    schema_query_count_planned: int = 0
    schema_query_count_succeeded: int = 0
    schema_inventory_complete: bool = False
    post_health_collected: bool = False
    post_health_safe: bool = False
    round_status: str = RoundStatus.INTERNAL_ERROR.value
    next_stage_blocked: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_round_status(
    *,
    dry_run: bool = False,
    pre_health_status: str | None = None,
    schema_planned: int = 0,
    schema_succeeded: int = 0,
    schema_complete: bool = False,
    post_health_status: str | None = None,
    post_collected: bool = False,
    seal_ok: bool = True,
) -> tuple[RoundStatus, RunnerExitCode, RunnerState]:
    state = RunnerState(
        schema_query_count_planned=schema_planned,
        schema_query_count_succeeded=schema_succeeded,
        schema_inventory_complete=schema_complete,
        post_health_collected=post_collected,
    )
    if dry_run:
        state.round_status = RoundStatus.DRY_RUN_COMPLETE.value
        state.next_stage_blocked = True
        return RoundStatus.DRY_RUN_COMPLETE, RunnerExitCode.SUCCESS, state
    if not seal_ok:
        state.round_status = RoundStatus.EXECUTION_SOURCE_UNSEALED.value
        state.next_stage_blocked = True
        return RoundStatus.EXECUTION_SOURCE_UNSEALED, RunnerExitCode.EXECUTION_SOURCE_UNSEALED, state

    if pre_health_status is None:
        state.round_status = RoundStatus.INTERNAL_ERROR.value
        return RoundStatus.INTERNAL_ERROR, RunnerExitCode.INTERNAL_ERROR, state

    if "UNKNOWN" in pre_health_status:
        state.health_preflight_passed = False
        state.round_status = RoundStatus.BLOCKED_PRE_UNKNOWN.value
        state.next_stage_blocked = True
        return RoundStatus.BLOCKED_PRE_UNKNOWN, RunnerExitCode.BLOCKED_PRE_UNKNOWN, state

    if pre_health_status != "SAFE_TO_PROCEED":
        state.health_preflight_passed = False
        state.round_status = RoundStatus.BLOCKED_PRE_HEALTH.value
        state.next_stage_blocked = True
        return RoundStatus.BLOCKED_PRE_HEALTH, RunnerExitCode.BLOCKED_PRE_HEALTH, state

    state.health_preflight_passed = True

    if schema_succeeded == 0 and schema_planned > 0 and not schema_complete:
        # no schema progress yet beyond planned — treat as incomplete if we intended to run
        pass

    if schema_succeeded > 0:
        state.any_schema_query_executed = True

    if schema_planned > 0 and schema_succeeded < schema_planned and not schema_complete:
        if schema_succeeded == 0:
            state.round_status = RoundStatus.FAILED_SCHEMA_QUERY.value
            state.next_stage_blocked = True
            return RoundStatus.FAILED_SCHEMA_QUERY, RunnerExitCode.SCHEMA_QUERY_FAILED, state
        state.round_status = RoundStatus.PARTIAL_SCHEMA_INVENTORY.value
        state.next_stage_blocked = True
        return RoundStatus.PARTIAL_SCHEMA_INVENTORY, RunnerExitCode.SCHEMA_INVENTORY_INCOMPLETE, state

    if not schema_complete:
        state.round_status = RoundStatus.PARTIAL_SCHEMA_INVENTORY.value
        state.next_stage_blocked = True
        return RoundStatus.PARTIAL_SCHEMA_INVENTORY, RunnerExitCode.SCHEMA_INVENTORY_INCOMPLETE, state

    state.schema_inventory_complete = True
    if not post_collected or post_health_status is None:
        state.round_status = RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED.value
        state.next_stage_blocked = True
        return (
            RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED,
            RunnerExitCode.BLOCKED_POST_HEALTH,
            state,
        )

    if "UNKNOWN" in post_health_status:
        state.post_health_collected = True
        state.post_health_safe = False
        state.round_status = RoundStatus.BLOCKED_POST_UNKNOWN.value
        state.next_stage_blocked = True
        return RoundStatus.BLOCKED_POST_UNKNOWN, RunnerExitCode.BLOCKED_POST_UNKNOWN, state

    state.post_health_collected = True
    if post_health_status == "SAFE_TO_PROCEED":
        state.post_health_safe = True
        state.round_status = RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE.value
        state.next_stage_blocked = True  # business probe still requires separate authorization
        return (
            RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE,
            RunnerExitCode.SUCCESS,
            state,
        )

    state.post_health_safe = False
    state.round_status = RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED.value
    state.next_stage_blocked = True
    return (
        RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED,
        RunnerExitCode.BLOCKED_POST_HEALTH,
        state,
    )


def historical_141325_correction() -> dict[str, Any]:
    """Offline correction record for the 20260717T141325Z round (do not rewrite originals)."""
    status, code, state = derive_round_status(
        pre_health_status="SAFE_TO_PROCEED",
        schema_planned=5,
        schema_succeeded=5,
        schema_complete=True,
        post_health_status="BLOCKED_LIVE_PRIORITY",
        post_collected=True,
        seal_ok=True,
    )
    state.any_health_query_executed = True
    state.any_schema_query_executed = True
    return {
        "schema_inventory_complete": True,
        "post_health_collected": True,
        "post_health_safe": False,
        "round_status": status.value,
        "historical_expected_exit_semantics": int(code),
        "actual_historical_exit_semantics": 0,
        "historical_exit_code_was_misleading": True,
        "state": state.to_dict(),
    }
