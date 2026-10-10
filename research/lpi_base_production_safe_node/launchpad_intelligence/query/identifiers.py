"""Strict SQL identifier validation shared by BoundedQueryPlan and QueryBounds."""

from __future__ import annotations

import re

from ..errors import PreflightError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
_BANNED_TOKENS = ("'", '"', "`", ";", "--", "/*", "*/", "\\", "(", ")", "=", ",", "<", ">", "*")
_BANNED_KEYWORDS = ("WHERE", "SELECT", "FROM", "LIMIT", "UNION", "JOIN", "OR", "AND", "DROP", "TABLE")


def assert_safe_identifier(value: str, *, field: str, allow_qualified: bool = False) -> None:
    if not isinstance(value, str) or not value:
        raise PreflightError(f"{field}: empty identifier forbidden")
    if any(ch.isspace() for ch in value):
        raise PreflightError(f"{field}: whitespace forbidden")
    for token in _BANNED_TOKENS:
        if token in value:
            raise PreflightError(f"{field}: forbidden token {token!r}")
    upper = value.upper()
    for kw in _BANNED_KEYWORDS:
        if upper == kw or f".{kw}" in upper or upper.startswith(f"{kw}."):
            raise PreflightError(f"{field}: SQL keyword injection rejected")
    if allow_qualified:
        if not _QUALIFIED_TABLE.match(value) and not _IDENT.match(value):
            raise PreflightError(f"{field}: invalid identifier syntax")
    elif not _IDENT.match(value):
        raise PreflightError(f"{field}: invalid identifier syntax")


def assert_safe_column_tuple(columns: tuple[str, ...]) -> None:
    if not columns:
        raise PreflightError("explicit_columns required")
    for col in columns:
        if col == "*":
            raise PreflightError("SELECT * forbidden")
        assert_safe_identifier(col, field="explicit_columns", allow_qualified=False)
