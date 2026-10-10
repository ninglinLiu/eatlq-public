"""Shared error types."""


class ContractError(ValueError):
    """Raised when a data contract is violated."""


class AccessDeniedError(RuntimeError):
    """Raised when a fail-closed access gate blocks an operation."""


class PreflightError(RuntimeError):
    """Raised when query preflight rejects a plan."""


class UnknownPreservedError(ContractError):
    """Raised when code attempts to coerce UNKNOWN into a concrete value."""
