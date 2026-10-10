"""Static checks for Sprint031B new-lineage bounded dry-run artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint031b_new_lineage_dryrun_report.md"
MANIFEST = ROOT / "mvp" / "diagnostics" / "sprint031b_new_lineage_dryrun_manifest.json"
LABEL_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint031b_new_lineage_dryrun_label_summary.json"
LABEL_CSV = ROOT / "mvp" / "diagnostics" / "sprint031b_new_lineage_dryrun_labels.csv"
ECON_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint031b_new_lineage_economic_readout_summary.json"
POOL_CSV = ROOT / "mvp" / "diagnostics" / "sprint031b_new_lineage_per_pool_summary.csv"
THIS_FILE = Path(__file__).resolve()

ALLOWED_DECISION = frozenset(
    {
        "NEW_LINEAGE_DRYRUN_PASS_REVIEW_REQUIRED",
        "PIPELINE_OK_ECONOMICALLY_WEAK",
        "NEEDS_MORE_ROWS_SAME_LINEAGE",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
        "RETURN_TO_UNIVERSE_MINING",
    }
)


class Sprint031BNewLineageDryrunTests(unittest.TestCase):
    def test_paths_exist(self) -> None:
        for path in (REPORT, MANIFEST, LABEL_SUMMARY, LABEL_CSV, ECON_SUMMARY, POOL_CSV):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_manifest_contract(self) -> None:
        mj = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "new_lineage_bounded_dryrun_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertTrue(mj.get("old_closed_cohort_excluded") is True)
        n = int(mj.get("selected_rows_count", 0))
        self.assertGreaterEqual(n, 300)
        self.assertLessEqual(n, 1000)
        self.assertLessEqual(int(mj.get("selected_pool_count", 999)), 8)

    def test_label_summary_contract(self) -> None:
        sj = json.loads(LABEL_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint031b_new_lineage_bounded_dryrun_label_execution")
        self.assertGreaterEqual(int(sj.get("attempted_rows", 0)), 300)
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 1000)
        self.assertGreaterEqual(float(sj.get("clean_rate", 0)), 0.95)
        self.assertLessEqual(float(sj.get("failure_rate", 1)), 0.05)
        self.assertLessEqual(int(sj.get("conceptual_quote_pairs", 9999)), 2300)
        self.assertLessEqual(int(sj.get("approx_eth_rpc_round_trips_total", 10**9)), 120000)

    def test_labels_csv_contract(self) -> None:
        with LABEL_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreaterEqual(len(rows), 300)
        self.assertLessEqual(len(rows), 1000)
        for row in rows:
            self.assertIn(row["label"], ("0", "1"))

    def test_economic_summary_contract(self) -> None:
        ej = json.loads(ECON_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(ej.get("kind"), "sprint031b_new_lineage_economic_readout")
        status = ej.get("final_decision_status")
        self.assertIn(status, ALLOWED_DECISION, msg=f"unexpected decision: {status}")
        flags = ej.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))

    def test_audit_report_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "`PIPELINE_OK_ECONOMICALLY_WEAK`",
            "## Old cohort closeout boundary",
            "## Temporal diagnostics",
            "## Per-pool concentration",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
            "ROUND_TRIP_COST=0.006",
        ):
            self.assertIn(needle, body, msg=f"audit missing: {needle}")

    def test_this_file_static_import_policy(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {"redis", "clickhouse_driver", "web3", "requests"}
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        self.assertFalse(seen & forbidden, msg=f"forbidden imports: {sorted(seen & forbidden)}")


if __name__ == "__main__":
    unittest.main()
