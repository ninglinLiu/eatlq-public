"""Sprint033A — artifact contracts for CH-backed mining + viability + optional manifest."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint033a_ch_backed_hard_quota_smoke_report.md"
UNIVERSE = ROOT / "mvp" / "diagnostics" / "sprint033a_ch_backed_universe_summary.json"
class Sprint033AHardQuotaTests(unittest.TestCase):
    """Static checks against committed Sprint033A diagnostic artifacts."""

    def test_core_reports_exist(self) -> None:
        self.assertTrue(REPORT.is_file(), msg=f"Missing: {REPORT}")
        self.assertTrue(UNIVERSE.is_file(), msg=f"Missing: {UNIVERSE}")

    def test_universe_summary_contract(self) -> None:
        data = json.loads(UNIVERSE.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint033a_ch_backed_universe_mining")
        status = data.get("final_decision_status")
        self.assertIn(
            status,
            (
                "NO_VIABILITY_BALANCED_COHORT_FOUND",
                "NEEDS_DATA_SOURCE_REPAIR",
                "NEW_COHORT_FOUND_NO_RPC",
                "PENDING_SMOKE_EXECUTION",
            ),
            msg=f"unexpected final_decision_status: {status}",
        )
        flags = data.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))
        self.assertTrue(flags.get("FAILED_NO_7D_ALPHA_ACTIVE"))

    def test_viability_csv_columns(self) -> None:
        p = ROOT / "mvp" / "diagnostics" / "sprint033a_prelabel_viability_audit.csv"
        self.assertTrue(p.is_file(), msg=f"Missing: {p}")
        with p.open(newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f), None)
        self.assertIsNotNone(row)
        assert row is not None
        for col in (
            "pool_address_audit_key",
            "metadata_status",
            "weth_leg_verified",
            "viability_status",
            "source",
        ):
            self.assertIn(col, row, msg=f"missing column {col}")

    def test_candidate_csv_exists(self) -> None:
        p = ROOT / "mvp" / "diagnostics" / "sprint033a_candidate_pool_summary.csv"
        self.assertTrue(p.is_file(), msg=f"Missing: {p}")

    def test_optional_manifest_strict(self) -> None:
        mj = ROOT / "mvp" / "diagnostics" / "sprint033a_hard_quota_viability_smoke_manifest.json"
        if not mj.is_file():
            self.skipTest("manifest not produced (gate failed)")
        data = json.loads(mj.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "ch_backed_hard_quota_viability_smoke_manifest")
        rows_n = int(data.get("selected_rows_count", 0))
        pools_n = int(data.get("selected_pool_count", 0))
        self.assertGreaterEqual(rows_n, 120)
        self.assertLessEqual(rows_n, 250)
        self.assertGreaterEqual(pools_n, 4)
        self.assertLessEqual(pools_n, 6)

    def test_report_contains_required_sections(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Closed lineage",
            "## Mining sources summary",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
            "ROUND_TRIP_COST",
        ):
            self.assertIn(needle, body, msg=f"report missing: {needle}")

    def test_optional_smoke_outputs(self) -> None:
        labels = ROOT / "mvp" / "diagnostics" / "sprint033a_hard_quota_viability_smoke_labels.csv"
        summary_p = ROOT / "mvp" / "diagnostics" / "sprint033a_hard_quota_viability_smoke_label_summary.json"
        if labels.is_file():
            self.assertTrue(summary_p.is_file(), msg="labels without summary")


THIS_FILE = Path(__file__).resolve()


class Sprint033AImportSafety(unittest.TestCase):
    def test_no_forbidden_heavy_runtime_imports(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {"redis", "web3", "requests", "clickhouse_driver"}
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        self.assertFalse(forbidden & seen, msg=f"forbidden imports: {sorted(forbidden & seen)}")


if __name__ == "__main__":
    unittest.main()
