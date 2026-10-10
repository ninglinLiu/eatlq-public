"""Static checks for Sprint030F scoped economic readout artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_ALLOWED_STATUS = frozenset(
    {
        "PASS_EXPAND_TO_1800",
        "NEEDS_RETURN_SEMANTICS_REVIEW",
        "NEEDS_LABEL_OR_POOL_REPAIR",
        "ECONOMICALLY_WEAK_BUT_PIPELINE_OK",
        "BLOCKED",
        "FAIL",
    }
)
_FORBIDDEN_IMPORTS = ("redis", "web3", "requests", "clickhouse_driver")


class TestSprint030fScopedEconomicReadout(unittest.TestCase):
    def test_forbidden_imports_not_present(self) -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {mod}", src)
            self.assertNotIn(f"from {mod}", src)

    def test_readout_artifacts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        md = root / "mvp" / "audits" / "sprint030f_scoped_economic_readout.md"
        js_ok = root / "mvp" / "diagnostics" / "sprint030f_scoped_economic_readout_summary.json"
        csv_p = root / "mvp" / "diagnostics" / "sprint030f_per_pool_economic_summary.csv"

        self.assertTrue(md.is_file(), f"missing {md}")
        self.assertTrue(js_ok.is_file(), f"missing {js_ok}")
        self.assertTrue(csv_p.is_file())

        data = json.loads(js_ok.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "scoped_economic_readout")
        self.assertIn(data.get("status"), _ALLOWED_STATUS)
        self.assertEqual(data.get("decision_flags", {}).get("TRAINING_ALLOWED"), False)

        body = md.read_text(encoding="utf-8")
        self.assertIn("not alpha", body.lower())
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE=true", body)
        self.assertIn("TRAINING_ALLOWED=false", body)
        self.assertIn("ECONOMICALLY_WEAK_BUT_PIPELINE_OK", body)


if __name__ == "__main__":
    unittest.main()
