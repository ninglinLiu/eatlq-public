"""Evidence Manifest — immutable metadata for remote bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..versions import (
    ADAPTER_API_VERSION,
    MANIFEST_VERSION,
    REMOTE_BUNDLE_VERSION,
    SCHEMA_VERSION,
)


@dataclass
class EvidenceManifest:
    bundle_id: str
    chain_id: int
    chain_name: str
    source_host_role: str
    source_code_commit: str
    source_tree_hash: str
    schema_version: str
    adapter_versions: dict[str, str]
    evidence_manifest_version: str
    remote_bundle_version: str
    data_start_block: int | None
    data_end_block: int | None
    database_snapshot_time: str | None
    created_at: str
    source_tables: list[str]
    source_files: list[str]
    source_query_hashes: list[str]
    file_count: int
    total_bytes: int
    sha256_per_file: dict[str, str]
    unknown_count: int
    golden_results: dict[str, Any]
    quality_results: dict[str, Any]
    production_state_mutated: bool = False
    live_execution_health_before: dict[str, Any] = field(default_factory=dict)
    live_execution_health_after: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.chain_id != 8453 or self.chain_name != "base":
            raise ValueError("manifest must declare Base chain")
        if self.production_state_mutated:
            raise ValueError("production_state_mutated must be false")
        if self.evidence_manifest_version != MANIFEST_VERSION:
            raise ValueError("manifest version mismatch")
        if not self.bundle_id or not self.source_code_commit:
            raise ValueError("bundle_id and source_code_commit required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    *,
    bundle_id: str,
    source_code_commit: str,
    source_tree_hash: str,
    files: dict[str, Path],
    unknown_count: int = 0,
    golden_results: dict[str, Any] | None = None,
    quality_results: dict[str, Any] | None = None,
    data_start_block: int | None = None,
    data_end_block: int | None = None,
    source_tables: list[str] | None = None,
    source_query_hashes: list[str] | None = None,
    health_before: dict[str, Any] | None = None,
    health_after: dict[str, Any] | None = None,
) -> EvidenceManifest:
    sha_map: dict[str, str] = {}
    total = 0
    for rel, path in sorted(files.items()):
        digest = file_sha256(path)
        sha_map[rel] = digest
        total += path.stat().st_size
    manifest = EvidenceManifest(
        bundle_id=bundle_id,
        chain_id=8453,
        chain_name="base",
        source_host_role="base_production_safe_data_node",
        source_code_commit=source_code_commit,
        source_tree_hash=source_tree_hash,
        schema_version=SCHEMA_VERSION,
        adapter_versions={"base": ADAPTER_API_VERSION},
        evidence_manifest_version=MANIFEST_VERSION,
        remote_bundle_version=REMOTE_BUNDLE_VERSION,
        data_start_block=data_start_block,
        data_end_block=data_end_block,
        database_snapshot_time=None,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_tables=source_tables or [],
        source_files=sorted(files.keys()),
        source_query_hashes=source_query_hashes or [],
        file_count=len(files),
        total_bytes=total,
        sha256_per_file=sha_map,
        unknown_count=unknown_count,
        golden_results=golden_results or {},
        quality_results=quality_results or {},
        production_state_mutated=False,
        live_execution_health_before=health_before or {},
        live_execution_health_after=health_after or {},
    )
    manifest.validate()
    return manifest
