"""Path safety helpers for evidence bundle I/O."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..errors import ContractError
from ..security.paths import assert_under_workspace


def reject_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink() or os.path.islink(path):
        raise ContractError(f"{label} must not be a symlink: {path}")


def require_writable_regular_destination(path: Path, *, workspace_root: Path, label: str) -> Path:
    """Reject symlink/special destinations before write; return resolved path under workspace."""
    reject_symlink(path, label=label)
    parent = path.parent
    reject_symlink(parent, label=f"{label}.parent")
    if path.exists():
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ContractError(f"{label} exists and is not a regular file: {path}")
        reject_symlink(path, label=label)
    resolved = path.resolve(strict=False)
    assert_under_workspace(resolved, workspace_root.resolve())
    # Parent must also stay in workspace after resolve.
    assert_under_workspace(resolved.parent, workspace_root.resolve())
    return resolved


def require_regular_non_symlink_file(path: Path, *, workspace_root: Path, label: str) -> Path:
    reject_symlink(path, label=label)
    if not path.exists():
        raise ContractError(f"{label} missing: {path}")
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ContractError(f"{label} is not a regular file: {path}")
    resolved = path.resolve(strict=True)
    assert_under_workspace(resolved, workspace_root.resolve())
    return resolved
