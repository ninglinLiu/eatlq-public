#!/usr/bin/env python3
"""Offline, read-only preflight for a future authoritative code candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tomllib
from pathlib import Path
from urllib.parse import urlparse

MAX_FILES = 10_000
MAX_TEXT_BYTES = 2 * 1024 * 1024

RISK_PATTERNS = {
    "shell_pipe_install": re.compile(r"(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b", re.I),
    "privileged_or_system_install": re.compile(r"\b(?:sudo|apt(?:-get)?|dnf|yum)\b", re.I),
    "service_or_scheduler_mutation": re.compile(r"\b(?:systemctl|service|crontab)\b", re.I),
    "docker_mutation": re.compile(r"\bdocker(?:\s+compose)?\s+(?:up|run|start|restart|rm|build)\b", re.I),
    "production_path": re.compile(r"/(?:opt/EATLQ|var/lib/docker|var/lib/clickhouse|var/lib/postgresql)(?:/|\b)"),
    "secret_access": re.compile(r"(?:PRIVATE_KEY|MNEMONIC|KEYSTORE|API_SECRET|WALLET_SECRET|AUTHORIZATION)", re.I),
    "database_default": re.compile(r"(?:clickhouse|postgres(?:ql)?|redis)://", re.I),
    "robinhood_fixed_path": re.compile(r"/(?:opt|root)/[^\n\"']*robinhood", re.I),
}

DEPENDENCY_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements.lock",
    "poetry.lock",
    "uv.lock",
    "pdm.lock",
    "pipfile",
    "pipfile.lock",
    "setup.py",
    "setup.cfg",
}

NATIVE_BUILD_HINTS = re.compile(
    r"\b(?:numpy|pandas|polars|pyarrow|cryptography|psycopg2|orjson|uvloop|grpcio|lxml)\b",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _requirement_count(path: Path, text: str) -> int:
    if path.name.lower().startswith("requirements"):
        return sum(
            1
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        )
    if path.name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return 0
        project = data.get("project", {})
        count = len(project.get("dependencies", []))
        count += sum(len(values) for values in project.get("optional-dependencies", {}).values())
        count += len(data.get("build-system", {}).get("requires", []))
        return count
    return 0


def audit(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("candidate root must be a directory")

    findings: list[dict[str, str]] = []
    dependency_files: list[str] = []
    network_hosts: set[str] = set()
    native_build_candidates: set[str] = set()
    dependency_count = 0
    file_count = 0
    total_bytes = 0

    for path in sorted(root.rglob("*")):
        file_count += 1
        if file_count > MAX_FILES:
            findings.append({"severity": "block", "kind": "file_limit", "path": "."})
            break
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = path.resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                findings.append({"severity": "block", "kind": "symlink_escape", "path": rel})
            else:
                findings.append({"severity": "review", "kind": "symlink", "path": rel})
            continue
        if not path.is_file():
            continue

        size = path.stat().st_size
        total_bytes += size
        if stat.S_IMODE(path.stat().st_mode) & 0o111:
            findings.append({"severity": "review", "kind": "executable_file", "path": rel})
        if path.name == ".gitmodules":
            findings.append({"severity": "block", "kind": "git_submodule", "path": rel})
        if path.name.lower() in DEPENDENCY_FILES or path.name.lower().startswith("requirements"):
            dependency_files.append(rel)
        if size > MAX_TEXT_BYTES:
            continue

        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        dependency_count += _requirement_count(path, text)
        if NATIVE_BUILD_HINTS.search(text):
            native_build_candidates.add(rel)
        for kind, pattern in RISK_PATTERNS.items():
            if pattern.search(text):
                findings.append({"severity": "block", "kind": kind, "path": rel})
        for match in URL_RE.finditer(text):
            host = urlparse(match.group(0)).hostname
            if host:
                network_hosts.add(host.lower())

    risk = "BLOCK" if any(item["severity"] == "block" for item in findings) else (
        "REVIEW" if findings else "LOW"
    )
    return {
        "candidate_root": str(root),
        "dependency_count_lower_bound": dependency_count,
        "dependency_files": dependency_files,
        "file_count": file_count,
        "findings": findings,
        "native_build_candidates": sorted(native_build_candidates),
        "network_hosts": sorted(network_hosts),
        "risk_assessment": risk,
        "total_bytes": total_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.candidate)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 2 if result["risk_assessment"] == "BLOCK" else 0


if __name__ == "__main__":
    raise SystemExit(main())
