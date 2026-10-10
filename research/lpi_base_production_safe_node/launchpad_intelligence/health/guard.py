"""Health guard interface — observes production signals; never mutates them."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class GuardDecision(StrEnum):
    SAFE_TO_PROCEED = "SAFE_TO_PROCEED"
    PAUSED_LIVE_PRIORITY = "PAUSED_LIVE_PRIORITY"
    BLOCKED_RESOURCE_RISK = "BLOCKED_RESOURCE_RISK"
    BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY = "BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY"


@dataclass(frozen=True)
class HealthSnapshot:
    timestamp_utc: str
    executor_pending: int | None
    executor_idle_ms: int | None
    container_restart_counts: dict[str, int]
    load_1m: float
    mem_available_kb: int
    swap_used_kb: int
    disk_free_bytes: int
    cpu_idle_bp: int | None = None
    io_wait_bp: int | None = None
    clickhouse_active_queries: int | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HealthGuard:
    """Compares before/after snapshots and applies hard stop rules."""

    max_swap_growth_kb: int = 64_000
    min_mem_available_kb: int = 1_048_576
    min_disk_free_bytes: int = 5_368_709_120
    max_load_1m: float = 24.0
    max_clickhouse_active_queries: int = 8

    def evaluate(self, before: HealthSnapshot, after: HealthSnapshot | None = None) -> GuardDecision:
        snap = after or before
        if snap.mem_available_kb < self.min_mem_available_kb:
            return GuardDecision.BLOCKED_RESOURCE_RISK
        if snap.disk_free_bytes < self.min_disk_free_bytes:
            return GuardDecision.BLOCKED_RESOURCE_RISK
        if snap.load_1m > self.max_load_1m:
            return GuardDecision.PAUSED_LIVE_PRIORITY
        if (
            snap.clickhouse_active_queries is not None
            and snap.clickhouse_active_queries > self.max_clickhouse_active_queries
        ):
            return GuardDecision.PAUSED_LIVE_PRIORITY
        if after is not None:
            growth = after.swap_used_kb - before.swap_used_kb
            if growth > self.max_swap_growth_kb:
                return GuardDecision.BLOCKED_RESOURCE_RISK
            for name, before_rc in before.container_restart_counts.items():
                after_rc = after.container_restart_counts.get(name)
                if after_rc is not None and after_rc > before_rc:
                    return GuardDecision.PAUSED_LIVE_PRIORITY
        if snap.executor_pending is None:
            return GuardDecision.BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY
        return GuardDecision.SAFE_TO_PROCEED
