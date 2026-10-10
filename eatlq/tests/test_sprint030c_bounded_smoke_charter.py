"""Static checks for Sprint030C bounded smoke authorization charter artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_FORBIDDEN_IMPORTS = ("redis", "web3", "requests", "clickhouse_driver")


class TestSprint030cBoundedSmokeCharter(unittest.TestCase):
    def test_forbidden_imports_not_present(self) -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {mod}", src)
            self.assertNotIn(f"from {mod}", src)

    def test_charter_md_and_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        md = root / "mvp" / "audits" / "sprint030c_bounded_smoke_authorization_charter.md"
        js = root / "mvp" / "diagnostics" / "sprint030c_bounded_smoke_authorization_charter_summary.json"
        self.assertTrue(md.is_file(), f"missing {md}")
        self.assertTrue(js.is_file(), f"missing {js}")

        body = md.read_text(encoding="utf-8")
        self.assertIn("charter only", body.lower())
        self.assertIn("does not execute labeling", body)
        self.assertIn("SMOKE_EXECUTION_ALLOWED=false", body)
        self.assertIn("LABELING_ALLOWED=false", body)
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE=true", body)
        self.assertIn("SMOKE_LABEL_LIMIT = 250", body)
        self.assertIn("SMOKE_MAX_POOLS = 5", body)
        self.assertIn("SMOKE_MAX_FAILURE_RATE = 0.05", body)
        self.assertIn("SMOKE_MIN_CLEAN_RATE = 0.95", body)
        self.assertIn("ROUND_TRIP_COST=0.006", body)

        data = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "bounded_smoke_authorization_charter")


if __name__ == "__main__":
    unittest.main()
