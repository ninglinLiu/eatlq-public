"""Path isolation helpers."""

from __future__ import annotations

from pathlib import Path

from ..errors import ContractError


def is_under_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve().relative_to(workspace_root.resolve())
        return True
    except ValueError:
        return False


def assert_under_workspace(path: Path, workspace_root: Path) -> None:
    if not is_under_workspace(path, workspace_root):
        raise ContractError(f"path escapes workspace: {path}")
