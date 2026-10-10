"""UNKNOWN preservation — never coerce to zero, empty string, or None silently."""

from __future__ import annotations

from typing import Any

from ..errors import UnknownPreservedError


class Unknown:
    """Singleton-like sentinel for missing or unverified values."""

    __slots__ = ("reason",)

    def __init__(self, reason: str = "UNSPECIFIED") -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"UNKNOWN({self.reason!r})"

    def __str__(self) -> str:
        return f"UNKNOWN:{self.reason}"

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unknown) and self.reason == other.reason

    def __hash__(self) -> int:
        return hash(("UNKNOWN", self.reason))

    def to_json(self) -> dict[str, str]:
        return {"__unknown__": self.reason}


UNKNOWN = Unknown("UNSPECIFIED")


def is_unknown(value: Any) -> bool:
    return isinstance(value, Unknown)


def preserve_unknown(value: Any, *, forbid_zero_fill: bool = True) -> Any:
    """Return value unchanged; reject attempts to treat UNKNOWN as numeric zero."""
    if is_unknown(value):
        return value
    if forbid_zero_fill and value == 0 and isinstance(value, (int, float)):
        # Zero is allowed as a real observed value; this helper does not rewrite.
        return value
    return value


def reject_unknown_coercion(value: Any, target_type: type) -> Any:
    if is_unknown(value):
        raise UnknownPreservedError(
            f"cannot coerce {value!r} to {target_type.__name__}; preserve UNKNOWN"
        )
    return value
