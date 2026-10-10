"""Static contract + unit tests for scripts/inspect_dead_letter.py (Sprint013 S1-T4)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Set

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "inspect_dead_letter.py"


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
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _import_script():
    import importlib.util
    import sys as _sys

    name = "_inspect_dead_letter_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class InspectDeadLetterTests(unittest.TestCase):
    def test_script_exists_and_is_read_only_by_ast(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file())
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/usr/bin/env python3"))
        self.assertIn("READ-ONLY", source)

        tree = _load_module_ast()
        called = _attr_call_names(tree)
        offenders = sorted(called & FORBIDDEN_REDIS_METHODS)
        self.assertEqual(
            offenders,
            [],
            msg=(
                "inspect_dead_letter.py must not use Redis write primitives; "
                f"offenders: {offenders!r}"
            ),
        )

    def test_no_forbidden_method_name_as_text(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for banned in ("xadd(", "xdel(", "xtrim(", "xclaim(", "xautoclaim(", "xreadgroup("):
            self.assertNotIn(banned, source, msg=f"forbidden primitive in source: {banned!r}")

    def test_bucket_key_collapses_handler_exception_details(self) -> None:
        mod = _import_script()
        # Executor taxonomy (docs/executor-language-contract.md):
        self.assertEqual(mod.bucket_key("parse_failed"), "parse_failed")
        self.assertEqual(mod.bucket_key("parse_failed: JSON decode at col 12"), "parse_failed")
        self.assertEqual(mod.bucket_key("buy_invalid_pool_or_token_address"), "buy_invalid_pool_or_token_address")
        self.assertEqual(
            mod.bucket_key("handle_failed: ConnectionError at 2026-05-03T10:00Z"),
            "handle_failed",
        )
        self.assertEqual(mod.bucket_key(""), "<empty>")

    def test_normalize_entry_handles_xrange_tuple_and_decoded_dict(self) -> None:
        mod = _import_script()
        xrange_style = (
            b"1714700000000-0",
            {b"entry_id": b"signals-42", b"error": b"parse_failed: bad bytes", b"payload": b"{}"},
        )
        norm = mod.normalize_entry(xrange_style)
        self.assertEqual(norm["stream_message_id"], "1714700000000-0")
        self.assertEqual(norm["signal_entry_id"], "signals-42")
        self.assertEqual(norm["error"], "parse_failed: bad bytes")
        self.assertEqual(norm["payload"], "{}")

        decoded_dict = {
            "stream_message_id": "1714700000001-0",
            "fields": {"entry_id": "signals-43", "error": "buy_invalid_pool_or_token_address", "payload": ""},
        }
        norm2 = mod.normalize_entry(decoded_dict)
        self.assertEqual(norm2["stream_message_id"], "1714700000001-0")
        self.assertEqual(norm2["signal_entry_id"], "signals-43")
        self.assertEqual(norm2["error"], "buy_invalid_pool_or_token_address")

    def test_group_entries_summary_counts_and_sorts(self) -> None:
        mod = _import_script()
        entries = [
            (b"1714700000001-0", {b"entry_id": b"a", b"error": b"parse_failed: bad bytes"}),
            (b"1714700000002-0", {b"entry_id": b"b", b"error": b"parse_failed: other reason"}),
            (b"1714700000003-0", {b"entry_id": b"c", b"error": b"buy_invalid_pool_or_token_address"}),
            (b"1714700000004-0", {b"entry_id": b"d", b"error": b"handle_failed: boom"}),
            (b"1714700000005-0", {b"entry_id": b"e", b"error": b"parse_failed: yet another"}),
        ]
        summary = mod.group_entries(entries)
        self.assertEqual(summary["total"], 5)
        by_reason = {row["reason"]: row for row in summary["buckets_sorted"]}
        self.assertEqual(by_reason["parse_failed"]["count"], 3)
        self.assertEqual(by_reason["buy_invalid_pool_or_token_address"]["count"], 1)
        self.assertEqual(by_reason["handle_failed"]["count"], 1)
        # latest tracks the max outer Redis stream id; for the parse_failed
        # bucket that is the 5th row (signal id "e").
        self.assertEqual(by_reason["parse_failed"]["latest_stream_message_id"], "1714700000005-0")
        self.assertEqual(by_reason["parse_failed"]["latest_signal_entry_id"], "e")
        self.assertEqual(summary["buckets_sorted"][0]["reason"], "parse_failed")

    def test_redact_strips_privkey_and_rpc_token(self) -> None:
        mod = _import_script()
        leaky = (
            "wss://orbital-purple-sea.base-mainnet.quiknode.pro/"
            "0123456789abcdef0123456789abcdef01234567/ "
            "privkey=0x" + "b" * 64
        )
        redacted = mod.redact(leaky)
        self.assertNotIn("0123456789abcdef0123456789abcdef01234567", redacted)
        self.assertNotIn("b" * 64, redacted)

    def test_cli_from_json_stdin_prints_summary(self) -> None:
        synthetic = [
            {"stream_message_id": "1-0", "fields": {"entry_id": "signals-1", "error": "parse_failed: x", "payload": ""}},
            {"stream_message_id": "2-0", "fields": {"entry_id": "signals-2", "error": "parse_failed: y", "payload": ""}},
            {"stream_message_id": "3-0", "fields": {"entry_id": "signals-3", "error": "buy_invalid_pool_or_token_address", "payload": ""}},
        ]
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--from-json", "-", "--format", "json"],
            input=json.dumps(synthetic),
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["stream"], "signals_dead_letter")
        self.assertEqual(out["summary"]["total"], 3)
        self.assertEqual(out["guard"]["controlling_verdict"], "FAILED_NO_7D_ALPHA")
        for flag, val in out["guard"]["decision_flags"].items():
            self.assertFalse(val, msg=f"{flag} must stay false in inspect_dead_letter output")

    def test_show_payloads_zero_by_default(self) -> None:
        """Default CLI path must not print raw payloads even if asked to parse
        entries that contain one — they stay summarized."""

        synthetic = [
            {
                "stream_message_id": "99-0",
                "fields": {
                    "entry_id": "signals-secret",
                    "error": "parse_failed: x",
                    "payload": "privkey=0x" + "c" * 64,
                },
            }
        ]
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--from-json", "-"],
            input=json.dumps(synthetic),
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("c" * 64, proc.stdout, "default text output must not leak payloads")


if __name__ == "__main__":
    unittest.main()
