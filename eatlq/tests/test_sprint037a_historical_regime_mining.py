"""Sprint037A — regime mining contract tests (CI-safe: no CH required)."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sprint037a_historical_regime_mining.py"

FORBIDDEN_IMPORTS = frozenset({"redis", "web3", "eth_account", "torch", "sklearn"})
FORBIDDEN_REDIS = frozenset({"xadd", "set", "setex", "hset", "publish"})


def _load_ast() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _attr_calls(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    )


class TestSprint037aRegimeMining(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), msg=f"missing {SCRIPT}")

    def test_no_forbidden_imports(self) -> None:
        tree = _load_ast()
        offenders = sorted(_imports(tree) & FORBIDDEN_IMPORTS)
        self.assertEqual(offenders, [], msg=f"forbidden imports: {offenders}")

    def test_no_redis_write_primitives(self) -> None:
        tree = _load_ast()
        bad = sorted(_attr_calls(tree) & FORBIDDEN_REDIS)
        self.assertEqual(bad, [], msg=f"redis-like calls: {bad}")

    def test_decision_flags_charter(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_m037a", SCRIPT)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        f = m.DECISION_FLAGS
        self.assertFalse(f.get("TRAINING_ALLOWED"))
        self.assertFalse(f.get("H12_ABLATION_ALLOWED"))
        self.assertFalse(f.get("LIVE_ALLOWED"))
        self.assertFalse(f.get("MODEL_PROMOTION_ALLOWED"))
        self.assertTrue(f.get("NO_ALPHA_CLAIM"))
        self.assertTrue(f.get("REGIME_DISCOVERY_ONLY"))

    def test_dry_run_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("kind"), "sprint037a_regime_mining_plan")

    def test_offline_fixture_writes(self) -> None:
        td = ROOT / ".tmp_sprint037a_test"
        td.mkdir(exist_ok=True)
        j = td / "out.json"
        c = td / "out.csv"
        md = td / "out.md"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--offline-fixture",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                **os.environ,
                "SPRINT037A_OUT_JSON": str(j),
                "SPRINT037A_OUT_CSV": str(c),
                "SPRINT037A_OUT_MD": str(md),
            },
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(j.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("kind"), "sprint037a_regime_mining")
        self.assertTrue(payload.get("offline_fixture"))
        tops = payload.get("top_historical_regimes") or []
        self.assertGreaterEqual(len(tops), 1)
        self.assertIn("sprint037b_replay_candidate", tops[0])
        self.assertTrue(c.is_file())
        self.assertTrue(md.is_file())
        self.assertIn("Leakage", md.read_text(encoding="utf-8"))

    def test_ch_query_guard_blocks_mutations(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_m037b", SCRIPT)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with self.assertRaises(ValueError):
            m._ch_guard_readonly("INSERT INTO t VALUES (1)")


if __name__ == "__main__":
    unittest.main()
