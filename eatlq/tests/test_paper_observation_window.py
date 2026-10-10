"""Static contract tests for scripts/paper_observation_window.py (Sprint013 S1-T3).

These tests never talk to Redis. They AST-scan the script source for forbidden
write primitives, verify URL/credential redaction on synthetic inputs, confirm
the script exposes the guard artifact schema, and smoke-test the ``--dry``
mode end-to-end via subprocess.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Set

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "paper_observation_window.py"


# Redis-py write primitives that must NEVER appear as method names in the script.
# Read-only operations (xlen, xinfo_groups, xrange, xrevrange, xpending, xread)
# are allowed. `xreadgroup` is NOT allowed because it mutates PEL.
FORBIDDEN_REDIS_METHODS: Set[str] = frozenset(
    {
        "xadd",
        "xdel",
        "xtrim",
        "xautoclaim",
        "xclaim",
        "xgroup_create",
        "xgroup_createconsumer",
        "xgroup_delconsumer",
        "xgroup_destroy",
        "xreadgroup",
        "set",
        "setex",
        "setnx",
        "mset",
        "delete",
        "unlink",
        "expire",
        "pexpire",
        "hset",
        "hmset",
        "hdel",
        "sadd",
        "srem",
        "spop",
        "lpush",
        "rpush",
        "lpop",
        "rpop",
        "lrem",
        "zadd",
        "zrem",
        "publish",
        "flushdb",
        "flushall",
        "rename",
        "renamenx",
    }
)


def _load_module_ast() -> ast.Module:
    return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH))


def _attr_call_names(tree: ast.Module) -> Set[str]:
    """Return every attribute-call method name (e.g. `.xadd(` as `"xadd"`)."""

    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


class PaperObservationWindowStaticContractTests(unittest.TestCase):
    def test_script_exists_and_is_executable_python(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), "paper_observation_window.py must exist")
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/usr/bin/env python3"))
        self.assertIn("READ-ONLY", source)

    def test_no_forbidden_redis_write_primitives(self) -> None:
        tree = _load_module_ast()
        called = _attr_call_names(tree)
        offenders = sorted(called & FORBIDDEN_REDIS_METHODS)
        self.assertEqual(
            offenders,
            [],
            msg=(
                "paper_observation_window.py must be read-only; "
                f"forbidden Redis write method(s) appear as attribute calls: {offenders!r}"
            ),
        )

    def test_no_forbidden_method_name_as_text(self) -> None:
        """Belt-and-suspenders grep pass — catches e.g. getattr(client, 'xadd')."""

        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for banned in ("xadd(", "xdel(", "xtrim(", "xclaim(", "xautoclaim(", "xreadgroup("):
            self.assertNotIn(banned, source, msg=f"banned Redis write primitive appears in source: {banned!r}")
        for banned_getattr in ("'xadd'", '"xadd"', "'xdel'", '"xdel"', "'xtrim'", '"xtrim"'):
            self.assertNotIn(banned_getattr, source, msg=f"string-literal Redis write name leaks: {banned_getattr!r}")

    def test_only_expected_redis_reads_are_used(self) -> None:
        tree = _load_module_ast()
        called = _attr_call_names(tree)
        allow_read = {"xlen", "xinfo_groups", "xrange", "xrevrange", "xpending", "xread", "ping"}
        unknown_redis_methods = {
            name for name in called if name.startswith("x") and name not in allow_read and name not in {"xlen", "xinfo_groups"}
        }
        self.assertEqual(
            unknown_redis_methods,
            set(),
            msg=(
                "paper_observation_window.py introduced a new Redis stream primitive; "
                "extend allow_read + FORBIDDEN_REDIS_METHODS intentionally. "
                f"unexpected methods: {sorted(unknown_redis_methods)!r}"
            ),
        )

    def test_redact_url_scrubs_credentials_and_tokens(self) -> None:
        spec = _import_script()
        redact = spec.redact_url

        cases = [
            (
                "wss://example-endpoint.base-mainnet.quiknode.pro//",
                "0123456789abcdef0123456789abcdef01234567",
            ),
            (
                "https://example.com/path",
                "supersecret",
            ),
            (
                "redis://example.com:6379/0",
                "password",
            ),
            (
                "privkey=0x" + "a" * 64,
                "a" * 64,
            ),
        ]
        for raw, secret in cases:
            redacted = redact(raw)
            self.assertNotIn(secret, redacted, msg=f"redact_url leaked secret material in output: {redacted!r}")

    def test_dry_run_mode_prints_safe_config_without_network(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
            env={**_env_without_redis_url(), "REDIS_URL": "redis://fakehost:6379/7"},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "dry")
        self.assertIn("streams", payload)
        self.assertIn("groups", payload)
        self.assertEqual(payload["guard"], "no_write_no_new_allow_flag_no_economic_claim")
        # Credential must be scrubbed before it hits stdout.
        self.assertNotIn("dummyPW", proc.stdout)

    def test_observations_dir_exists_with_gitkeep(self) -> None:
        obs = ROOT / "mvp" / "observations"
        self.assertTrue(obs.is_dir())
        keep = obs / ".gitkeep"
        self.assertTrue(keep.is_file(), msg="mvp/observations/.gitkeep must exist so the dir is tracked")

    def test_gitignore_drops_generated_observation_artifacts(self) -> None:
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("mvp/observations/obs_*.json", gi)
        self.assertIn("mvp/observations/obs_*.md", gi)

    def test_runbook_references_script_and_read_only_contract(self) -> None:
        doc = (ROOT / "docs" / "observability-stream-contract.md").read_text(encoding="utf-8")
        self.assertIn("scripts/paper_observation_window.py", doc)
        self.assertIn("No writes", doc)
        self.assertIn("Credential redaction", doc)
        self.assertIn("FAILED_NO_7D_ALPHA", doc)
        self.assertIn("mvp/observations/", doc)


def _env_without_redis_url() -> dict:
    import os

    env = {k: v for k, v in os.environ.items() if k != "REDIS_URL"}
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


def _import_script():
    import importlib.util
    import sys

    name = "_paper_observation_window_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main()
