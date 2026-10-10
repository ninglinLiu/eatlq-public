#!/usr/bin/env python3
"""
Secrets-hygiene lint (Sprint013 S1-T6).

Scans tracked files for shapes that, if committed, would compromise the repo:

- QuickNode / Alchemy / similar RPC tokens embedded in URL paths.
- URLs with embedded ``scheme://`` credentials (outside template
  files that explicitly model that shape with placeholder values).
- ``PRIVATE_KEY=0x<64 hex>`` assignments.
- ``Bearer <long token>`` literals.
- Secret-shaped keys in ``.env.example`` that have non-empty values (private
  key, telegram bot token, dashboard basic auth / action token).

The scan reads ``git ls-files`` so untracked and gitignored files are never
opened. Binary-ish files (by extension) are skipped.

Allowlist policy: a file is allowlisted only when the file's purpose is to
*model* one of these patterns (the lint itself, its tests, and a narrow
.env.example exemption for dev passwords inside a postgres URL template).
Every allowlist entry is documented inline.

Usage:
    python3 scripts/check_no_secrets.py --root .
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple

# Paths allowed to contain the regex shapes (relative to repo root).
# Each entry MUST have a comment explaining why the exemption is safe.
ALLOWLIST_FILES: Set[str] = frozenset(
    {
        # This script defines the patterns themselves.
        "scripts/check_no_secrets.py",
        # These tests exercise the redaction / sentinel functions and must
        # contain the literal shapes to prove detection.
        "tests/test_check_no_secrets.py",
        "tests/test_paper_observation_window.py",
        "tests/test_inspect_dead_letter.py",
    }
)

# .env.example-style template files may use scheme://host
# for local-only compose wiring. We still enforce the per-key empty-value
# rule below (`SECRET_ENV_KEYS_REQUIRED_EMPTY`).
ENV_EXAMPLE_TEMPLATES: Set[str] = frozenset(
    {
        "infra/compose/.env.example",
    }
)

# In any .env.example template, these keys MUST declare an empty right-hand
# side (so a clone never ships with a real secret).
SECRET_ENV_KEYS_REQUIRED_EMPTY: Tuple[str, ...] = (
    "EXECUTOR_PRIVATE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DASHBOARD_BASIC_PASS",
    "DASHBOARD_ACTION_TOKEN",
    "EXECUTOR_RPC_URL",
)

# Skip binary / generated assets.
_SKIP_SUFFIX = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".tar", ".tgz", ".gz", ".bz2",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".parquet", ".pyc", ".so", ".dylib",
)

# Files larger than this are assumed to be generated data (diagnostics JSON,
# labeled CSV, ClickHouse dumps, etc.); secrets do not live in them. Keeping
# the cap at 1 MiB lets a real leaky env file still be scanned.
_SIZE_CAP_BYTES = 1 * 1024 * 1024

# Directories whose contents are generated data artifacts and should not be
# scanned even when individual files are below the size cap.
_SKIP_PREFIXES = (
    ".venv/",
    "upstream_original_wiki/",
    "mvp/diagnostics/",
    "mvp/runs/",
    "models/",
    "cache/",
    "evidence/",
)

# QuickNode-style path token: /<16+ hex chars>/ appearing inside a *.quiknode.pro,
# *.alchemy.com, *.infura.io, or similar provider URL.
_PROVIDER_TOKEN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9\-]*\.(?:quiknode\.pro|alchemy\.com|infura\.io|drpc\.org)/[A-Fa-f0-9]{16,}",
    re.IGNORECASE,
)

# URL with embedded credentials before the host (host must look real).
_URL_CRED_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[A-Za-z][A-Za-z0-9.\-]*",
    re.IGNORECASE,
)

# PRIVATE_KEY assignment to a 64-hex value.
_PRIVKEY_ASSIGN_RE = re.compile(
    r"""(?i)(?:PRIVATE_KEY|PRIVKEY)\s*[:=]\s*["']?0x[a-f0-9]{64}\b""",
)

# Bearer token literal.
_BEARER_RE = re.compile(r"\bBearer [A-Za-z0-9+/=_\-]{20,}\b")

# SECRET_KEY= style assignment with long high-entropy value.
_SECRET_ASSIGN_RE = re.compile(
    r"""(?i)(?:SECRET_KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN)\s*[:=]\s*["']?[A-Za-z0-9+/=_\-]{24,}["']?\s*$""",
)


def _skip_path(rel: str) -> bool:
    if rel.endswith(_SKIP_SUFFIX):
        return True
    if rel.startswith(_SKIP_PREFIXES):
        return True
    return False


def tracked_files(repo: Path) -> List[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


_ALL_PATTERNS = (_PROVIDER_TOKEN_RE, _URL_CRED_RE, _PRIVKEY_ASSIGN_RE, _BEARER_RE, _SECRET_ASSIGN_RE)


def scan_file(repo: Path, rel: str) -> List[Tuple[int, str, str]]:
    """Return [(lineno, rule, matched_excerpt)] for one file; empty list is clean.

    Fast path: read the whole file once; if *none* of the patterns hit the
    concatenated text, return early without doing the per-line sweep. This
    keeps a clean repo scan at O(files) regex-matches instead of O(files *
    lines * patterns).
    """

    if rel in ALLOWLIST_FILES:
        return []
    if _skip_path(rel):
        return []
    path = repo / rel
    try:
        if path.is_file() and path.stat().st_size > _SIZE_CAP_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not any(p.search(text) for p in _ALL_PATTERNS):
        return []

    findings: List[Tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if _PROVIDER_TOKEN_RE.search(line):
            findings.append((i, "provider_rpc_token", line.strip()[:200]))
        if _URL_CRED_RE.search(line) and rel not in ENV_EXAMPLE_TEMPLATES:
            findings.append((i, "url_embedded_credential", line.strip()[:200]))
        if _PRIVKEY_ASSIGN_RE.search(line):
            findings.append((i, "privkey_assignment", line.strip()[:200]))
        if _BEARER_RE.search(line):
            findings.append((i, "bearer_token_literal", line.strip()[:200]))
        if _SECRET_ASSIGN_RE.search(line):
            findings.append((i, "high_entropy_assignment", line.strip()[:200]))
    return findings


def scan_env_example_values(repo: Path, rel: str) -> List[Tuple[int, str, str]]:
    """In an .env.example template, each SECRET_ENV_KEY must declare empty rhs."""

    findings: List[Tuple[int, str, str]] = []
    path = repo / rel
    if not path.is_file():
        return findings
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _sep, val = line.partition("=")
        if key.strip() in SECRET_ENV_KEYS_REQUIRED_EMPTY:
            v = val.strip().strip('"').strip("'")
            if v:
                findings.append((i, "env_example_secret_has_value", f"{key.strip()}={v[:60]}"))
    return findings


def scan_repo(repo: Path) -> List[Tuple[str, int, str, str]]:
    out: List[Tuple[str, int, str, str]] = []
    for rel in tracked_files(repo):
        for lineno, rule, excerpt in scan_file(repo, rel):
            out.append((rel, lineno, rule, excerpt))
    for tmpl in ENV_EXAMPLE_TEMPLATES:
        for lineno, rule, excerpt in scan_env_example_values(repo, tmpl):
            out.append((tmpl, lineno, rule, excerpt))
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo = args.root.resolve()
    findings = scan_repo(repo)
    if not findings:
        print("check_no_secrets: OK — 0 findings across tracked files")
        return 0
    print("check_no_secrets: FAIL", file=sys.stderr)
    for rel, lineno, rule, excerpt in findings:
        print(f"  - {rel}:{lineno} [{rule}] {excerpt}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
