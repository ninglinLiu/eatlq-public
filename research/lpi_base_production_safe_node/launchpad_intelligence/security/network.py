"""Network fail-closed helper for offline tests."""

from __future__ import annotations

import socket
from typing import Any


class NetworkFailClosed:
    """Context manager that blocks outbound socket connections."""

    def __init__(self) -> None:
        self._original = socket.socket

    def __enter__(self) -> "NetworkFailClosed":
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("network_fail_closed: socket connections blocked")

        socket.socket = blocked  # type: ignore[assignment]
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        socket.socket = self._original  # type: ignore[assignment]
