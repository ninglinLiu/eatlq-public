"""Remote Evidence Bundle export — offline, path-isolated, no self-hash paradox."""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..security.paths import assert_under_workspace
from ..security.secrets import scan_text_for_secrets
from .bundle_id import assert_safe_bundle_id
from .manifest import EvidenceManifest, build_manifest, file_sha256
from .pathsafety import reject_symlink, require_writable_regular_destination
from .verify import verify_bundle_archive


class RemoteEvidenceBundleExporter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def _reject_unsafe_path(self, path: Path, staging_dir: Path) -> None:
        if path.is_symlink() or os.path.islink(path):
            raise ContractError(f"symlink rejected in staging: {path}")
        if not path.exists():
            return
        mode = path.lstat().st_mode
        if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            raise ContractError(f"special file rejected in staging: {path}")
        resolved = path.resolve()
        assert_under_workspace(resolved, self.workspace_root)
        try:
            resolved.relative_to(staging_dir.resolve())
        except ValueError as exc:
            raise ContractError(f"staging path escapes staging_dir: {path}") from exc

    def export(
        self,
        *,
        staging_dir: Path,
        output_tar: Path,
        bundle_id: str,
        source_code_commit: str,
        source_tree_hash: str,
        unknown_count: int = 0,
        golden_results: dict[str, Any] | None = None,
        quality_results: dict[str, Any] | None = None,
        source_tables: list[str] | None = None,
        source_query_hashes: list[str] | None = None,
        health_before: dict[str, Any] | None = None,
        health_after: dict[str, Any] | None = None,
    ) -> EvidenceManifest:
        staging_dir = Path(staging_dir)
        output_tar = Path(output_tar)
        assert_safe_bundle_id(bundle_id)
        reject_symlink(staging_dir, label="staging_dir")
        reject_symlink(output_tar, label="output_tar")

        staging_resolved = staging_dir.resolve()
        assert_under_workspace(staging_resolved, self.workspace_root)

        # Fail closed on overwrite: refuse if archive or sidecars already exist.
        sha_path = Path(str(output_tar) + ".sha256")
        manifest_sha_path = Path(str(output_tar) + ".manifest.sha256")
        for existing, label in (
            (output_tar, "output_tar"),
            (sha_path, "output_tar.sha256"),
            (manifest_sha_path, "output_tar.manifest.sha256"),
        ):
            if existing.exists() or existing.is_symlink():
                raise ContractError(f"{label} already exists; refuse overwrite: {existing}")

        output_resolved = require_writable_regular_destination(
            output_tar, workspace_root=self.workspace_root, label="output_tar"
        )
        sha_resolved = require_writable_regular_destination(
            sha_path, workspace_root=self.workspace_root, label="output_tar.sha256"
        )
        manifest_sha_resolved = require_writable_regular_destination(
            manifest_sha_path,
            workspace_root=self.workspace_root,
            label="output_tar.manifest.sha256",
        )

        try:
            output_resolved.relative_to(staging_resolved)
            raise ContractError("output_tar must not be inside staging_dir")
        except ValueError:
            pass

        payload_files: dict[str, Path] = {}
        for path in sorted(staging_dir.rglob("*")):
            self._reject_unsafe_path(path, staging_resolved)
            if path.is_dir():
                continue
            if not path.is_file() or path.is_symlink():
                raise ContractError(f"non-regular file rejected: {path}")
            rel = str(path.relative_to(staging_dir))
            if rel == "manifest.json":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = scan_text_for_secrets(text)
            if hits:
                raise ContractError(f"secret-like content blocked in {rel}: {hits[:3]}")
            payload_files[rel] = path

        manifest = build_manifest(
            bundle_id=bundle_id,
            source_code_commit=source_code_commit,
            source_tree_hash=source_tree_hash,
            files=payload_files,
            unknown_count=unknown_count,
            golden_results=golden_results,
            quality_results=quality_results,
            source_tables=source_tables,
            source_query_hashes=source_query_hashes,
            health_before=health_before,
            health_after=health_after,
        )
        manifest_path = staging_dir / "manifest.json"
        reject_symlink(manifest_path, label="manifest.json")
        if manifest_path.exists() and not manifest_path.is_file():
            raise ContractError("manifest.json path is not a regular file")
        manifest_path.write_text(manifest.to_json(), encoding="utf-8")

        with tarfile.open(output_resolved, "w:gz") as tar:
            for rel, path in sorted(payload_files.items()):
                tar.add(path, arcname=f"{bundle_id}/{rel}", recursive=False)
            tar.add(manifest_path, arcname=f"{bundle_id}/manifest.json", recursive=False)

        archive_sha = file_sha256(output_resolved)
        sha_resolved.write_text(f"{archive_sha}  {output_resolved.name}\n", encoding="utf-8")
        manifest_sha = file_sha256(manifest_path)
        manifest_sha_resolved.write_text(f"{manifest_sha}  manifest.json\n", encoding="utf-8")

        verify_bundle_archive(
            archive_path=output_resolved,
            workspace_root=self.workspace_root,
            expected_bundle_id=bundle_id,
        )
        return manifest
