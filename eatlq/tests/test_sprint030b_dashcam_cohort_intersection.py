"""Static checks for Sprint030B dashcam ∩ cohort intersection feasibility artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_FORBIDDEN_IMPORTS = ("redis", "web3", "requests", "clickhouse_driver")


class TestSprint030bDashcamCohortIntersection(unittest.TestCase):
    def test_forbidden_imports_not_present(self) -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {mod}", src)
            self.assertNotIn(f"from {mod}", src)

    def test_feasibility_md_and_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        md = root / "mvp" / "audits" / "sprint030b_dashcam_cohort_intersection_feasibility.md"
        js = root / "mvp" / "diagnostics" / "sprint030b_dashcam_cohort_intersection_summary.json"
        self.assertTrue(md.is_file(), f"missing {md}")
        self.assertTrue(js.is_file(), f"missing {js}")

        body = md.read_text(encoding="utf-8")
        self.assertIn("not labels", body)
        self.assertIn("no RPC", body)
        self.assertIn("no alpha", body.lower())
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE=true", body)
        self.assertIn("LABELING_ALLOWED=false", body)

        data = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "dashcam_cohort_intersection_feasibility")


if __name__ == "__main__":
    unittest.main()
