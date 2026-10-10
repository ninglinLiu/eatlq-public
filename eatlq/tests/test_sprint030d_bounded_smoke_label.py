"""Static acceptance checks for Sprint030D bounded smoke label artifacts."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path


_FORBIDDEN_IMPORTS = ("redis", "web3", "requests", "clickhouse_driver")


class TestSprint030dBoundedSmokeLabel(unittest.TestCase):
    def test_forbidden_imports_not_present(self) -> None:
        src = Path(__file__).read_text(encoding="utf-8")
        for mod in _FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {mod}", src)
            self.assertNotIn(f"from {mod}", src)

    def test_manifest_summary_csv_md(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = root / "mvp" / "diagnostics" / "sprint030d_smoke_manifest.json"
        summary_p = root / "mvp" / "diagnostics" / "sprint030d_bounded_smoke_label_summary.json"
        csv_p = root / "mvp" / "diagnostics" / "sprint030d_bounded_smoke_labels.csv"
        md = root / "mvp" / "audits" / "sprint030d_bounded_smoke_label_execution_report.md"

        self.assertTrue(manifest.is_file())
        self.assertTrue(summary_p.is_file())
        self.assertTrue(csv_p.is_file())
        self.assertTrue(md.is_file())

        mj = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "bounded_smoke_label_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertLessEqual(int(mj.get("selected_rows_count", 999)), 250)
        self.assertLessEqual(int(mj.get("selected_pool_count", 999)), 5)

        sj = json.loads(summary_p.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "bounded_smoke_label_execution")
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("labeled_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("conceptual_entry_exit_pairs_budget", 999)), 600)
        self.assertLessEqual(float(sj.get("failure_rate", 1.0)), 0.05)
        self.assertGreaterEqual(float(sj.get("clean_rate", 0.0)), 0.95)

        with csv_p.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertLessEqual(len(rows), 250)
        for row in rows:
            self.assertIn(row["label"], ("0", "1"))

        body = md.read_text(encoding="utf-8")
        self.assertIn("Bounded smoke label execution", body)
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE=true", body)
        self.assertIn("LABELING_ALLOWED=smoke_only", body)


if __name__ == "__main__":
    unittest.main()
