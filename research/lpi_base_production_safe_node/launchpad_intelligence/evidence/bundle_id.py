"""Safe bundle_id syntax."""

from __future__ import annotations

import re

from ..errors import ContractError

_SAFE_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def assert_safe_bundle_id(bundle_id: str) -> None:
    if not isinstance(bundle_id, str) or not bundle_id:
        raise ContractError("bundle_id required")
    if any(ch.isspace() for ch in bundle_id) or any(ord(ch) < 32 for ch in bundle_id):
        raise ContractError("bundle_id must not contain whitespace/control characters")
    if "/" in bundle_id or "\\" in bundle_id or ".." in bundle_id:
        raise ContractError("bundle_id must not contain path separators or '..'")
    if not _SAFE_BUNDLE_ID.match(bundle_id):
        raise ContractError("bundle_id has unsafe syntax")
