"""Sprint037A-BF — backfill inventory script contract tests (CI-safe)."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sprint037a_bf_backfill_inventory.py"

FORBIDDEN = frozenset({"redis", "web3", "eth_account", "torch"})


class TestSprint037aBfInventory(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file())

    def test_no_forbidden_imports(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        mods: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        self.assertEqual(sorted(mods & FORBIDDEN), [])

    def test_decision_flags(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_bf", SCRIPT)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        f = m.DECISION_FLAGS
        self.assertTrue(f.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertTrue(f.get("HISTORICAL_BACKFILL_PLANNING_ALLOWED"))
        self.assertFalse(f.get("FULL_BACKFILL_ALLOWED"))
        self.assertFalse(f.get("SPRINT037B_REPLAY_ALLOWED"))
        self.assertFalse(f.get("TRAINING_ALLOWED"))

    def test_offline_fixture(self) -> None:
        td = ROOT / ".tmp_sprint037a_bf"
        td.mkdir(exist_ok=True)
        j = td / "inv.json"
        md = td / "plan.md"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--offline-fixture"],
            env={**os.environ, "SPRINT037A_BF_OUT_JSON": str(j), "SPRINT037A_BF_OUT_MD": str(md)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(j.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint037a_bf_backfill_inventory")
        self.assertTrue(md.read_text(encoding="utf-8").startswith("# Sprint037A-BF"))


if __name__ == "__main__":
    unittest.main()
