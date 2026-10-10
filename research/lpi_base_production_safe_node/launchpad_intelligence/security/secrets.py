"""Lightweight secret-pattern scanner for offline bundle/export gates."""

from __future__ import annotations

import re
from pathlib import Path

_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),  # raw private-key shaped hex
    re.compile(r"\bmnemonic\s*[:=]", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.I),
]


def scan_text_for_secrets(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in _PATTERNS:
        if pattern.search(text):
            hits.append(pattern.pattern)
    return hits


def scan_path_for_secrets(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"unreadable:{path}"]
    return scan_text_for_secrets(text)
