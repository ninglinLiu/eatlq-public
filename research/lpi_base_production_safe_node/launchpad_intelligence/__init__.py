"""Launchpad Intelligence — Base Production-Safe Node local baseline."""

from .versions import (
    ADAPTER_API_VERSION,
    MANIFEST_VERSION,
    REMOTE_BUNDLE_VERSION,
    SCHEMA_VERSION,
)

__all__ = [
    "ADAPTER_API_VERSION",
    "MANIFEST_VERSION",
    "REMOTE_BUNDLE_VERSION",
    "SCHEMA_VERSION",
]

__version__ = "0.1.0-local-baseline"
