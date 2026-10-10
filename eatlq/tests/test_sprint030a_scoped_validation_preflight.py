"""Static checks for Sprint030A scoped validation prototype preflight artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_FORBIDDEN_IMPORTS = ("redis", "web3", "requests", "clickhouse_driver")


class TestSprint030aScopedValidationPreflight(unittest.TestCase):
    def test_forbidden_imports_not_present(self) -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {mod}", src)
            self.assertNotIn(f"from {mod}", src)

    def test_preflight_md_and_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        md = root / "mvp" / "audits" / "sprint030a_scoped_validation_prototype_preflight.md"
        js = root / "mvp" / "diagnostics" / "sprint030a_scoped_validation_prototype_preflight_summary.json"
        self.assertTrue(md.is_file(), f"missing {md}")
        self.assertTrue(js.is_file(), f"missing {js}")

        body = md.read_text(encoding="utf-8")
        self.assertIn("preflight only", body.lower())
        self.assertIn("does not authorize labeling", body)
        self.assertIn("028B PASS ≠ alpha", body)
        self.assertIn("028C ranking ≠ predictive", body)
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE=true", body)
        self.assertIn("SCOPED_VALIDATION_EXECUTION_ALLOWED=false", body)
        self.assertIn("LABELING_ALLOWED=false", body)
        self.assertIn("ROUND_TRIP_COST=0.006", body)
        self.assertIn("LABEL_NOTIONAL_WEI=0.0005 ETH", body)
        for n in ("300", "1000", "5000", "20000"):
            self.assertIn(n, body)

        data = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "scoped_validation_prototype_preflight")


if __name__ == "__main__":
    unittest.main()
