"""Commit-before-query hard gate for production-adjacent runners.

Initial seal requires a fully clean worktree (including untracked files).
After seal, only explicitly registered run-owned outputs may appear as
untracked paths; tracked/HEAD/critical-blob drift still fails closed.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..errors import AccessDeniedError, PreflightError

DEFAULT_CRITICAL_PATHS = (
    "scripts/run_metadata_inventory.py",
    "launchpad_intelligence/query/metadata_inventory.py",
    "launchpad_intelligence/query/metadata_ranking.py",
    "launchpad_intelligence/query/runner_status.py",
    "launchpad_intelligence/health/metadata_gate.py",
    "launchpad_intelligence/health/command_executor.py",
    "launchpad_intelligence/security/auth_gate.py",
    "launchpad_intelligence/security/execution_seal.py",
    "launchpad_intelligence/evidence/manifest.py",
    "launchpad_intelligence/evidence/bundle.py",
)


@dataclass(frozen=True)
class ExecutionSeal:
    workspace_root: Path
    head_commit: str
    head_tree: str
    critical_blob_sha256: dict[str, str]
    allowed_rel_prefixes: tuple[str, ...] = ()
    allowed_rel_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_root": str(self.workspace_root),
            "execution_source_commit": self.head_commit,
            "execution_source_tree_hash": self.head_tree,
            "critical_blob_sha256": dict(sorted(self.critical_blob_sha256.items())),
            "allowed_rel_prefixes": list(self.allowed_rel_prefixes),
            "allowed_rel_files": list(self.allowed_rel_files),
        }


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        raise PreflightError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel_under(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(root.resolve())).replace("\\", "/")
    except ValueError as exc:
        raise AccessDeniedError(f"path escapes workspace: {path}") from exc


def _is_allowed_untracked(rel: str, seal: ExecutionSeal) -> bool:
    rel = rel.rstrip("/")
    for exact in seal.allowed_rel_files:
        if rel == exact.rstrip("/"):
            return True
    for prefix in seal.allowed_rel_prefixes:
        p = prefix.rstrip("/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def _parse_porcelain(porcelain: str) -> list[tuple[str, str]]:
    """Return list of (xy_status, path) from git status --porcelain."""
    out: list[tuple[str, str]] = []
    for line in porcelain.splitlines():
        if not line:
            continue
        if len(line) < 4:
            raise AccessDeniedError(f"unparseable git status line: {line!r}")
        xy = line[:2]
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        # Unquoted paths only (our allowlisted names have no spaces).
        path = rest.strip().strip('"')
        out.append((xy, path.replace("\\", "/")))
    return out


def require_clean_sealed_worktree(
    workspace_root: Path,
    *,
    critical_paths: tuple[str, ...] = DEFAULT_CRITICAL_PATHS,
) -> ExecutionSeal:
    """Fail closed unless worktree is clean and critical files match HEAD blobs."""
    root = Path(workspace_root).resolve()
    porcelain = _git(root, "status", "--porcelain")
    if porcelain:
        raise AccessDeniedError(
            "execution seal denied: dirty worktree (git status --porcelain not empty)"
        )

    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if not head or not tree:
        raise AccessDeniedError("execution seal denied: HEAD/tree unknown")

    blobs: dict[str, str] = {}
    for rel in critical_paths:
        path = root / rel
        if path.is_symlink():
            raise AccessDeniedError(f"execution seal denied: critical path is symlink: {rel}")
        if not path.is_file():
            raise AccessDeniedError(f"execution seal denied: critical path missing: {rel}")
        ls = _git(root, "ls-tree", "HEAD", "--", rel)
        if not ls:
            raise AccessDeniedError(f"execution seal denied: critical path not in HEAD tree: {rel}")
        blob = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            raise AccessDeniedError(f"execution seal denied: cannot read HEAD blob for {rel}")
        work = path.read_bytes()
        if work != blob.stdout:
            raise AccessDeniedError(f"execution seal denied: worktree bytes != HEAD blob for {rel}")
        blobs[rel] = hashlib.sha256(work).hexdigest()

    return ExecutionSeal(
        workspace_root=root,
        head_commit=head,
        head_tree=tree,
        critical_blob_sha256=blobs,
    )


def register_run_owned_outputs(
    seal: ExecutionSeal,
    *,
    evidence_dir: Path,
    staging_dir: Path | None = None,
    bundle_tar: Path | None = None,
    token_path: Path | None = None,
) -> ExecutionSeal:
    """Bind exact run-owned outputs allowed after the initial clean seal."""
    root = seal.workspace_root
    prefixes: list[str] = [_rel_under(root, evidence_dir)]
    files: list[str] = []
    if staging_dir is not None:
        prefixes.append(_rel_under(root, staging_dir))
    if bundle_tar is not None:
        rel = _rel_under(root, bundle_tar)
        files.extend([rel, rel + ".sha256", rel + ".manifest.sha256"])
    if token_path is not None:
        files.append(_rel_under(root, token_path))
    return replace(
        seal,
        allowed_rel_prefixes=tuple(dict.fromkeys(prefixes)),
        allowed_rel_files=tuple(dict.fromkeys(files)),
    )


def _reject_symlinks_under(root: Path, rel_prefix: str) -> None:
    base = root / rel_prefix
    if not base.exists():
        return
    if base.is_symlink():
        raise AccessDeniedError(f"execution seal broken: symlink at allowlisted root {rel_prefix}")
    for path in base.rglob("*"):
        if path.is_symlink() or path.is_symlink():
            raise AccessDeniedError(f"execution seal broken: symlink in allowlisted output: {path}")


def assert_seal_unchanged(seal: ExecutionSeal) -> None:
    """Re-check HEAD/tree/critical blobs; allow only registered run-owned untracked outputs."""
    root = seal.workspace_root
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if head != seal.head_commit or tree != seal.head_tree:
        raise AccessDeniedError("execution seal broken: HEAD/tree changed during execution")

    for rel, digest in seal.critical_blob_sha256.items():
        path = root / rel
        if path.is_symlink():
            raise AccessDeniedError(f"execution seal broken: critical path became symlink: {rel}")
        if _file_sha256(path) != digest:
            raise AccessDeniedError(f"execution seal broken: critical file changed: {rel}")

    for prefix in seal.allowed_rel_prefixes:
        _reject_symlinks_under(root, prefix)

    porcelain = _git(root, "status", "--porcelain")
    if not porcelain:
        return
    for xy, path in _parse_porcelain(porcelain):
        # Any tracked/staged/index change is forbidden.
        if xy != "??":
            raise AccessDeniedError(
                f"execution seal broken: tracked/index change during run: {xy} {path}"
            )
        abs_path = root / path
        # Git may report a parent untracked directory; expand and require every
        # concrete file/symlink beneath it to be run-owned.
        if path.endswith("/") or abs_path.is_dir():
            if abs_path.is_symlink():
                raise AccessDeniedError(f"execution seal broken: symlink untracked dir: {path}")
            found_any = False
            for child in abs_path.rglob("*"):
                if child.is_symlink():
                    raise AccessDeniedError(
                        f"execution seal broken: symlink in untracked tree: {child}"
                    )
                if not child.is_file():
                    continue
                found_any = True
                child_rel = _rel_under(root, child)
                if not _is_allowed_untracked(child_rel, seal):
                    raise AccessDeniedError(
                        f"execution seal broken: untracked path outside run-owned allowlist: {child_rel}"
                    )
            if not found_any and not _is_allowed_untracked(path.rstrip("/"), seal):
                raise AccessDeniedError(
                    f"execution seal broken: untracked empty dir outside allowlist: {path}"
                )
            continue
        if not _is_allowed_untracked(path, seal):
            raise AccessDeniedError(
                f"execution seal broken: untracked path outside run-owned allowlist: {path}"
            )
