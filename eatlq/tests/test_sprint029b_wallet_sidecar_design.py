"""Static checks for Sprint029B wallet sidecar design + schema JSON."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "mvp" / "audits" / "sprint029b_wallet_sidecar_design.md"
SCHEMA = ROOT / "mvp" / "audits" / "sprint029b_wallet_sidecar_schema.json"
THIS_FILE = Path(__file__).resolve()

FORBIDDEN_IMPORTS = frozenset({"redis", "web3", "requests", "clickhouse_driver"})
ETH_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


class Sprint029BWalletSidecarDesignTests(unittest.TestCase):
    def test_design_md_exists(self) -> None:
        self.assertTrue(DESIGN.is_file(), msg=f"Missing {DESIGN}")

    def test_design_contains_flags_and_canon(self) -> None:
        text = DESIGN.read_text(encoding="utf-8")
        self.assertIn("CANON_PARITY=false", text)
        self.assertIn("SMART_MONEY_GENERAL_LINE_OPEN=false", text)
        self.assertIn("WALLET_FEATURE_SIDECAR_RUNTIME_ALLOWED=false", text)

    def test_schema_json_valid_and_kind(self) -> None:
        self.assertTrue(SCHEMA.is_file(), msg=f"Missing {SCHEMA}")
        raw = SCHEMA.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data.get("kind"), "sprint029b_wallet_sidecar_schema")
        for sf in data.get("selected_features", []):
            self.assertEqual(sf.get("status"), "FUTURE/CANDIDATE", msg=f"bad status: {sf}")

    def test_schema_has_no_eth_address_samples(self) -> None:
        raw = SCHEMA.read_text(encoding="utf-8")
        self.assertIsNone(
            ETH_ADDR_RE.search(raw),
            msg="schema JSON must not contain sample Ethereum addresses",
        )

    def test_this_file_forbidden_imports(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        bad = seen & FORBIDDEN_IMPORTS
        self.assertEqual(bad, set(), msg=f"Forbidden imports: {bad}")


if __name__ == "__main__":
    unittest.main()
