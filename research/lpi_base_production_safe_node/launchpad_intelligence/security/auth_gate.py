"""Explicit authorization gates for scripts that could open network/DB sockets.

Ordinary environment variables alone must NOT open capability. Authorization
requires both an explicit CLI flag AND a matching capability token file under
the workspace (never a bare env toggle like ENABLE_*=1).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import AccessDeniedError

WORKSPACE_ROOT = Path("/root/lpi_base_production_safe_node")
TOKEN_DIR = WORKSPACE_ROOT / "var" / "auth_tokens"

CAP_REDIS_HEALTH = "redis_health_observe"
CAP_DATABASE_PROBE = "database_probe_execute"
CAP_HTTP = "http_access"
CAP_RPC = "rpc_access"
CAP_METADATA_INVENTORY = "metadata_schema_inventory"


def capability_token_path(capability: str) -> Path:
    return TOKEN_DIR / f"{capability}.authorized"


def _has_symlink_ancestor(path: Path) -> bool:
    """True if path or any workspace-relative ancestor is a symlink."""
    cur = path
    workspace = WORKSPACE_ROOT.resolve()
    # Walk from path up to workspace root (inclusive of path itself).
    for _ in range(64):
        if cur.is_symlink():
            return True
        if cur == workspace or cur == Path("/"):
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return False


def _is_under_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def is_capability_authorized(capability: str, *, explicit_cli_flag: bool) -> bool:
    """Return True only when CLI flag is set AND a safe workspace token file exists."""
    if not explicit_cli_flag:
        return False
    token = capability_token_path(capability)
    try:
        if _has_symlink_ancestor(TOKEN_DIR) or _has_symlink_ancestor(token):
            return False
        if not token.exists() or token.is_symlink() or not token.is_file():
            return False
        # Must be a regular file (not dir/special).
        if not token.is_file() or token.is_symlink():
            return False
        resolved = token.resolve(strict=True)
        if not _is_under_workspace(resolved, WORKSPACE_ROOT):
            return False
        # Also ensure resolved path has no symlink escape via relative_to check only.
        body = resolved.read_text(encoding="utf-8").strip()
        return body == f"AUTHORIZED:{capability}"
    except OSError:
        return False


def require_capability(capability: str, *, explicit_cli_flag: bool) -> None:
    if not is_capability_authorized(capability, explicit_cli_flag=explicit_cli_flag):
        raise AccessDeniedError(
            f"capability={capability} denied: requires --authorize-{capability.replace('_', '-')} "
            f"AND regular non-symlink token file {capability_token_path(capability)} "
            f"(ambient env vars are ignored; symlink ancestors rejected)"
        )


def env_cannot_authorize(capability: str) -> bool:
    os.environ[f"ENABLE_{capability.upper()}"] = "1"
    os.environ[f"ALLOW_{capability.upper()}"] = "1"
    try:
        return not is_capability_authorized(capability, explicit_cli_flag=False)
    finally:
        os.environ.pop(f"ENABLE_{capability.upper()}", None)
        os.environ.pop(f"ALLOW_{capability.upper()}", None)
