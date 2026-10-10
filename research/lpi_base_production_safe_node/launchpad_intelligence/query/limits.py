"""Shared fail-closed query bound constants and validators."""

from __future__ import annotations

from pathlib import Path

from ..errors import PreflightError
from ..security.paths import assert_under_workspace
from .identifiers import assert_safe_column_tuple

MAX_BLOCK_SPAN = 1000
MAX_LIMIT = 1000
MIN_LIMIT = 1
MAX_TIMEOUT_SEC = 10
MIN_TIMEOUT_SEC = 1
MAX_BYTES = 8_388_608
MIN_BYTES = 1
REQUIRED_MAX_THREADS = 1

WORKSPACE_ROOT = Path("/root/lpi_base_production_safe_node")
ALLOWED_CHECKPOINT_DIRS = (
    WORKSPACE_ROOT / "checkpoints",
    WORKSPACE_ROOT / "var" / "tmp",
)


def validate_block_range(start_block: int, end_block: int) -> None:
    if start_block < 0 or end_block < start_block:
        raise PreflightError("invalid block range")
    span = end_block - start_block + 1
    if span > MAX_BLOCK_SPAN:
        raise PreflightError(f"block span {span} exceeds hard max {MAX_BLOCK_SPAN}")


def validate_limit(limit: int) -> None:
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise PreflightError(f"limit must be {MIN_LIMIT}..{MAX_LIMIT}")


def validate_threads(max_threads: int) -> None:
    if max_threads != REQUIRED_MAX_THREADS:
        raise PreflightError("max_threads must be 1")


def validate_timeout(statement_timeout_sec: int) -> None:
    if not (MIN_TIMEOUT_SEC <= statement_timeout_sec <= MAX_TIMEOUT_SEC):
        raise PreflightError(
            f"statement_timeout_sec must be {MIN_TIMEOUT_SEC}..{MAX_TIMEOUT_SEC}"
        )


def validate_max_bytes(max_bytes_to_read: int) -> None:
    if not (MIN_BYTES <= max_bytes_to_read <= MAX_BYTES):
        raise PreflightError(f"max_bytes_to_read must be {MIN_BYTES}..{MAX_BYTES}")


def validate_dry_run_explain(*, dry_run: bool, explain_only: bool) -> None:
    if not dry_run or not explain_only:
        raise PreflightError("this stage requires dry_run=true and explain_only=true")


def validate_explicit_columns(explicit_columns: tuple[str, ...]) -> None:
    assert_safe_column_tuple(explicit_columns)


def assert_safe_checkpoint(path: str | None) -> None:
    if path is None:
        return
    p = Path(path)
    if p.exists() and p.is_symlink():
        raise PreflightError("checkpoint_path must not be a symlink")
    try:
        resolved = p.resolve(strict=False)
    except OSError as exc:
        raise PreflightError(f"checkpoint_path unresolvable: {exc}") from exc
    assert_under_workspace(resolved, WORKSPACE_ROOT)
    ok = False
    for d in ALLOWED_CHECKPOINT_DIRS:
        try:
            resolved.relative_to(d.resolve())
            ok = True
            break
        except ValueError:
            continue
    if not ok:
        raise PreflightError("checkpoint_path must be under checkpoints/ or var/tmp/")


def validate_query_bounds_core(
    *,
    start_block: int,
    end_block: int,
    limit: int,
    explicit_columns: tuple[str, ...],
    max_threads: int,
    statement_timeout_sec: int,
    max_bytes_to_read: int,
    dry_run: bool,
    explain_only: bool,
    checkpoint_path: str | None,
) -> None:
    validate_block_range(start_block, end_block)
    validate_limit(limit)
    validate_threads(max_threads)
    validate_timeout(statement_timeout_sec)
    validate_max_bytes(max_bytes_to_read)
    validate_dry_run_explain(dry_run=dry_run, explain_only=explain_only)
    validate_explicit_columns(explicit_columns)
    assert_safe_checkpoint(checkpoint_path)
