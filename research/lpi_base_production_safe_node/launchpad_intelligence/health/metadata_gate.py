"""Strict one-shot health gate for metadata inventory (stricter than generic HealthGuard)."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guard import GuardDecision


@dataclass(frozen=True)
class MetadataHealthSnapshot:
    timestamp_utc: str
    load_1m: float | None
    nproc: int | None
    mem_available_kb: int | None
    swap_used_kb: int | None
    disk_free_bytes: int | None
    containers: dict[str, dict[str, Any]]
    executor_pending: int | None
    clickhouse_active_queries_excl_self: int | None
    clickhouse_active_merges: int | None
    notes: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MetadataHealthDecision:
    status: str
    triggers: list[str] = field(default_factory=list)
    snapshot: MetadataHealthSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "triggers": self.triggers,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_host_basics() -> dict[str, Any]:
    load_1m = None
    nproc = None
    mem_available_kb = None
    swap_used_kb = None
    disk_free_bytes = None
    notes: list[str] = []
    unknown: list[str] = []
    try:
        load_1m = float(Path("/proc/loadavg").read_text().split()[0])
    except OSError:
        unknown.append("load_1m")
    try:
        nproc = os.cpu_count()
    except Exception:
        unknown.append("nproc")
    try:
        meminfo = Path("/proc/meminfo").read_text()
        vals = {}
        for line in meminfo.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            nums = re.findall(r"\d+", v)
            if nums:
                vals[k] = int(nums[0])
        mem_available_kb = vals.get("MemAvailable")
        st = vals.get("SwapTotal")
        sf = vals.get("SwapFree")
        if st is None or sf is None:
            unknown.append("swap_used_kb")
        else:
            swap_used_kb = st - sf
        if mem_available_kb is None:
            unknown.append("mem_available_kb")
    except OSError:
        unknown.extend(["mem_available_kb", "swap_used_kb"])
    try:
        st = os.statvfs("/")
        disk_free_bytes = int(st.f_bavail * st.f_frsize)
    except OSError:
        unknown.append("disk_free_bytes")
        notes.append("statvfs_failed")
    return {
        "load_1m": load_1m,
        "nproc": nproc,
        "mem_available_kb": mem_available_kb,
        "swap_used_kb": swap_used_kb,
        "disk_free_bytes": disk_free_bytes,
        "notes": notes,
        "unknown_fields": unknown,
    }


def evaluate_metadata_health(snap: MetadataHealthSnapshot) -> MetadataHealthDecision:
    triggers: list[str] = []
    if snap.unknown_fields:
        triggers.append("unknown_fields:" + ",".join(snap.unknown_fields))
    if snap.nproc is None or snap.load_1m is None:
        triggers.append("load_or_nproc_unknown")
    else:
        limit = max(4.0, 0.75 * float(snap.nproc))
        if snap.load_1m > limit:
            triggers.append(f"load1={snap.load_1m}>limit={limit}")
    if snap.mem_available_kb is None or snap.mem_available_kb < 8 * 1024 * 1024:
        triggers.append(f"mem_available_kb={snap.mem_available_kb}")
    if snap.disk_free_bytes is None or snap.disk_free_bytes < 20 * 1024**3:
        triggers.append(f"disk_free_bytes={snap.disk_free_bytes}")
    if not snap.containers:
        triggers.append("containers_unknown")
    for name, meta in snap.containers.items():
        state = meta.get("state")
        if state != "running":
            triggers.append(f"container_not_running:{name}:{state}")
        if meta.get("restart_count") is None:
            triggers.append(f"restart_count_unknown:{name}")
        if meta.get("restart_growth"):
            triggers.append(f"restart_growth:{name}")
    if snap.executor_pending is None:
        triggers.append("executor_pending_unknown")
    elif snap.executor_pending != 0:
        triggers.append(f"executor_pending={snap.executor_pending}")
    if snap.clickhouse_active_queries_excl_self is None:
        triggers.append("clickhouse_active_queries_unknown")
    elif snap.clickhouse_active_queries_excl_self > 2:
        triggers.append(f"clickhouse_active_queries={snap.clickhouse_active_queries_excl_self}")
    if snap.clickhouse_active_merges is None:
        triggers.append("clickhouse_active_merges_unknown")
    elif snap.clickhouse_active_merges > 0:
        triggers.append(f"clickhouse_active_merges={snap.clickhouse_active_merges}")

    if triggers:
        # Map to Codex wording; also compatible with GuardDecision names where useful.
        status = "BLOCKED_LIVE_PRIORITY"
        if any("unknown" in t for t in triggers):
            status = "BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY"
        if any(t.startswith("mem_") or t.startswith("disk_") for t in triggers):
            status = "BLOCKED_RESOURCE_RISK"
        return MetadataHealthDecision(status=status, triggers=triggers, snapshot=snap)
    return MetadataHealthDecision(status=GuardDecision.SAFE_TO_PROCEED.value, triggers=[], snapshot=snap)
