"""Static checks for Sprint029D label authorization preflight checklist artifact."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "mvp" / "audits" / "sprint029d_label_authorization_preflight.md"
THIS_FILE = Path(__file__).resolve()

FORBIDDEN_IMPORTS = frozenset({"web3", "requests", "redis", "clickhouse_driver"})


class Sprint029DLabelPreflightTests(unittest.TestCase):
    def test_preflight_md_exists(self) -> None:
        self.assertTrue(PREFLIGHT.is_file(), msg=f"Missing {PREFLIGHT}")

    def test_preflight_contains_contract_phrases(self) -> None:
        text = PREFLIGHT.read_text(encoding="utf-8")
        for needle in (
            "checklist only",
            "does not authorize labeling",
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "LABELING_ALLOWED=false",
            "LABEL_EXIT_HORIZON_SEC=1800",
            "ROUND_TRIP_COST=0.006",
            "LABEL_NOTIONAL_WEI=0.0005 ETH",
            "quote_failed",
            "300",
            "1000",
            "5000",
            "20000",
        ):
            self.assertIn(needle, text, msg=f"preflight md missing: {needle}")

    def test_this_file_has_no_forbidden_imports(self) -> None:
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
