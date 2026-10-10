"""Injectable external command executor for fail-closed offline testing."""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CommandRecord:
    command_id: str
    command_class: str
    argv_sanitized: list[str]
    started_at: str
    ended_at: str
    exit_code: int
    timeout: bool
    output_bytes: int
    stdout_sha256: str
    stderr_sha256: str
    authorized_capability: str | None
    source_commit: str | None
    source_tree_hash: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_class": self.command_class,
            "argv_sanitized": self.argv_sanitized,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "timeout": self.timeout,
            "output_bytes": self.output_bytes,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "authorized_capability": self.authorized_capability,
            "source_commit": self.source_commit,
            "source_tree_hash": self.source_tree_hash,
        }


@dataclass
class ExternalCommandExecutor:
    """Real or stub executor. Offline tests inject a stub that refuses or counts calls."""

    records: list[CommandRecord] = field(default_factory=list)
    call_count: int = 0
    stub: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
    source_commit: str | None = None
    source_tree_hash: str | None = None
    authorized_capability: str | None = None
    allow_calls: bool = True

    def run(
        self,
        argv: list[str],
        *,
        command_id: str,
        command_class: str,
        timeout_sec: float,
        argv_sanitized: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not self.allow_calls:
            raise RuntimeError("external command executor calls disabled")
        self.call_count += 1
        started = _utc()
        timed_out = False
        try:
            if self.stub is not None:
                cp = self.stub(argv)
            else:
                cp = subprocess.run(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else "timeout"
            cp = subprocess.CompletedProcess(argv, 124, stdout, stderr)
        ended = _utc()
        stdout = cp.stdout or ""
        stderr = cp.stderr or ""
        self.records.append(
            CommandRecord(
                command_id=command_id,
                command_class=command_class,
                argv_sanitized=list(argv_sanitized or argv),
                started_at=started,
                ended_at=ended,
                exit_code=int(cp.returncode),
                timeout=timed_out,
                output_bytes=len(stdout.encode("utf-8", errors="replace")),
                stdout_sha256=hashlib.sha256(stdout.encode("utf-8", errors="replace")).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr.encode("utf-8", errors="replace")).hexdigest(),
                authorized_capability=self.authorized_capability,
                source_commit=self.source_commit,
                source_tree_hash=self.source_tree_hash,
            )
        )
        return cp
