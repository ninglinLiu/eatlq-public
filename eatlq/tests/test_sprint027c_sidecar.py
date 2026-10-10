"""Static contract + smoke tests for scripts/sprint027c_enriched_feature_sidecar.py.

Contract (mirrors scripts/sprint027c_enriched_feature_sidecar.py docstring):
- No Redis write imports or attribute calls.
- No production service imports (redis, web3, ...).
- No RPC patterns in executable code paths.
- Dry-run exits 0 and prints valid JSON.
- Decision flags are all false (except diagnostic sidecar flag).

This test does NOT train, label, retrain, call RPC, write Redis, or modify any
runtime service. It is safe to run in CI.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sprint027c_enriched_feature_sidecar.py"

FORBIDDEN_REDIS_ATTRS = frozenset({
    "xadd", "xdel", "xtrim", "xclaim", "xautoclaim", "xreadgroup",
    "set", "setex", "setnx", "mset", "delete", "unlink",
    "expire", "pexpire", "hset", "hmset", "hdel",
    "sadd", "srem", "spop", "zadd", "zrem",
    "publish", "flushdb", "flushall",
})

FORBIDDEN_IMPORTS = frozenset({"redis", "web3", "eth_account"})


def _load_ast() -> ast.Module:
    return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH))


def _attr_call_names(tree: ast.Module) -> frozenset:
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def _imported_modules(tree: ast.Module) -> set:
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


class Sprint027CSidecarContractTests(unittest.TestCase):

    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), msg=f"Missing: {SCRIPT_PATH}")

    def test_no_redis_write_primitives_in_ast(self) -> None:
        tree = _load_ast()
        called = _attr_call_names(tree)
        offenders = sorted(called & FORBIDDEN_REDIS_ATTRS)
        self.assertEqual(offenders, [],
                         msg=f"Forbidden Redis write primitives found: {offenders}")

    def test_no_forbidden_imports(self) -> None:
        tree = _load_ast()
        imported = _imported_modules(tree)
        offenders = sorted(imported & FORBIDDEN_IMPORTS)
        self.assertEqual(offenders, [],
                         msg=f"Forbidden production service imports: {offenders}")

    def test_no_literal_redis_import_in_code(self) -> None:
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        # Only look in non-comment, non-docstring lines
        code_lines = [
            line for line in src.splitlines()
            if not line.strip().startswith("#") and not line.strip().startswith('"""')
        ]
        for line in code_lines:
            self.assertNotIn("import redis", line,
                             msg=f"Unexpected redis import in code line: {line!r}")

    def test_decision_flags_all_false_except_sidecar(self) -> None:
        """DECISION_FLAGS must keep all blocking flags false."""
        # Import the module safely
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sidecar_under_test", SCRIPT_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        flags = mod.DECISION_FLAGS
        must_be_false = [
            "TRAINING_ALLOWED", "LABELING_ALLOWED", "LIVE_ALLOWED",
            "PHASE6_ALLOWED", "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED",
            "redis_write", "rpc_called", "dune_called",
        ]
        for flag in must_be_false:
            self.assertFalse(flags.get(flag), msg=f"Flag must be false: {flag}")
        # Diagnostic sidecar flag must be True
        self.assertTrue(flags.get("IMPLEMENTATION_ALLOWED_sidecar_diagnostic"),
                        msg="IMPLEMENTATION_ALLOWED_sidecar_diagnostic must be true")

    def test_denylist_contains_required_fields(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sidecar_under_test2", SCRIPT_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        required = {"pool_address", "token_address", "tx_hash", "log_index", "block_number", "timestamp"}
        for field in required:
            self.assertIn(field, mod.DENYLIST_COLUMNS,
                          msg=f"Denylist missing required audit-only field: {field}")

    def test_dry_run_exits_zero_and_prints_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry"],
            capture_output=True, text=True, timeout=15
        )
        self.assertEqual(proc.returncode, 0,
                         msg=f"--dry exited non-zero: {proc.stderr}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"--dry output is not valid JSON: {exc}\nOutput: {proc.stdout[:500]}")
        # Check that decision_flags are present in output
        self.assertIn("decision_flags", data)

    def test_windows_sec_covers_required_set(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sidecar_under_test3", SCRIPT_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        required = {60, 180, 300, 900, 1800}
        self.assertTrue(required.issubset(set(mod.WINDOWS_SEC)),
                        msg=f"WINDOWS_SEC missing required values. Got: {mod.WINDOWS_SEC}")


if __name__ == "__main__":
    unittest.main()
