"""Offline Evidence Bundle verifier."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from ..errors import ContractError
from .bundle_id import assert_safe_bundle_id
from .manifest import file_sha256
from .pathsafety import reject_symlink, require_regular_non_symlink_file


def verify_bundle_archive(
    *,
    archive_path: Path,
    workspace_root: Path,
    expected_bundle_id: str | None = None,
) -> dict[str, str]:
    archive_path = Path(archive_path)
    reject_symlink(archive_path, label="archive_path")
    archive_resolved = require_regular_non_symlink_file(
        archive_path, workspace_root=workspace_root, label="archive_path"
    )

    sha_path = Path(str(archive_path) + ".sha256")
    manifest_sha_path = Path(str(archive_path) + ".manifest.sha256")
    sha_resolved = require_regular_non_symlink_file(
        sha_path, workspace_root=workspace_root, label="archive.sha256"
    )
    manifest_sha_resolved = require_regular_non_symlink_file(
        manifest_sha_path, workspace_root=workspace_root, label="archive.manifest.sha256"
    )

    expected_archive = sha_resolved.read_text(encoding="utf-8").strip().split()[0]
    actual_archive = file_sha256(archive_resolved)
    if expected_archive != actual_archive:
        raise ContractError("archive detached hash mismatch")

    with tarfile.open(archive_resolved, "r:gz") as tar:
        members = tar.getmembers()
        names_all = [m.name for m in members]
        if len(names_all) != len(set(names_all)):
            raise ContractError("archive contains duplicate member names")
        for m in members:
            if m.issym() or m.islnk():
                raise ContractError(f"archive contains link: {m.name}")
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ContractError(f"unsafe archive member path: {m.name}")
        file_members = [m for m in members if m.isfile()]
        names = {m.name for m in file_members}
        manifest_members = [n for n in names if n.endswith("/manifest.json") or n == "manifest.json"]
        if len(manifest_members) != 1:
            raise ContractError("archive must contain exactly one manifest.json")
        manifest_name = manifest_members[0]
        extracted = tar.extractfile(manifest_name)
        if extracted is None:
            raise ContractError("cannot read manifest from archive")
        manifest_bytes = extracted.read()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        bundle_id = manifest.get("bundle_id")
        assert_safe_bundle_id(str(bundle_id) if bundle_id is not None else "")
        if expected_bundle_id and bundle_id != expected_bundle_id:
            raise ContractError("bundle_id mismatch")
        prefix = manifest_name[: -len("manifest.json")]
        sha_map = manifest.get("sha256_per_file") or {}
        source_files = list(manifest.get("source_files") or [])
        if "manifest.json" in sha_map:
            raise ContractError("manifest must not self-hash via sha256_per_file")

        payload_names = {n for n in names if n != manifest_name}
        expected_payload_names = {f"{prefix}{rel}" for rel in sha_map}
        if payload_names != expected_payload_names:
            raise ContractError("archive payload set != manifest sha256_per_file keys")
        if set(source_files) != set(sha_map.keys()):
            raise ContractError("manifest.source_files != sha256_per_file keys")
        if int(manifest.get("file_count", -1)) != len(sha_map):
            raise ContractError("manifest.file_count mismatch")

        total = 0
        for rel, digest in sha_map.items():
            member = f"{prefix}{rel}"
            fh = tar.extractfile(member)
            if fh is None:
                raise ContractError(f"cannot read payload: {rel}")
            data = fh.read()
            total += len(data)
            h = hashlib.sha256(data).hexdigest()
            if h != digest:
                raise ContractError(f"payload hash mismatch: {rel}")
        if int(manifest.get("total_bytes", -1)) != total:
            raise ContractError("manifest.total_bytes mismatch")

    expected_manifest = manifest_sha_resolved.read_text(encoding="utf-8").strip().split()[0]
    actual_manifest = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest != actual_manifest:
        raise ContractError("detached manifest hash mismatch")

    return {"archive_sha256": actual_archive, "manifest_sha256": actual_manifest, "status": "VERIFIED"}
