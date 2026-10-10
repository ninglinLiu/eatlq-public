"""Static checks for Sprint031C rebalance reuse-only readout artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "mvp" / "diagnostics" / "sprint031c_rebalanced_candidate_manifests.json"
SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint031c_rebalanced_lineage_readout_summary.json"
SUMMARY_CSV = ROOT / "mvp" / "diagnostics" / "sprint031c_rebalanced_candidate_summary.csv"
REPORT = ROOT / "mvp" / "audits" / "sprint031c_rebalanced_lineage_readout.md"
OPTIONAL_INC = ROOT / "mvp" / "diagnostics" / "sprint031c_incremental_rebalanced_labels.csv"
THIS_FILE = Path(__file__).resolve()

ALLOWED_FINAL = frozenset(
    {
        "REBALANCED_LINEAGE_REVIEW_REQUIRED",
        "REBALANCE_STILL_WEAK_RETURN_TO_MINING",
        "NEEDS_INCREMENTAL_LABEL",
        "TOO_CONCENTRATED_TO_TRUST",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
    }
)

EXPECTED_KEYS = (
    "A_drop_worst_pool_reuse_only",
    "B_balanced_per_pool_cap100_chronological_prefix_reuse_only",
    "C_late_window_global_second_half_reuse_only",
    "D_conservative_drop_worst_cap100_late_window_reuse_only",
)


class Sprint031CRebalancedLineageTests(unittest.TestCase):
    def test_core_paths_exist(self) -> None:
        for path in (BUNDLE, SUMMARY, SUMMARY_CSV, REPORT):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_incremental_csv_optional(self) -> None:
        if OPTIONAL_INC.exists():
            self.assertGreater(OPTIONAL_INC.stat().st_size, 0)

    def test_bundle_manifest_contract(self) -> None:
        data = json.loads(BUNDLE.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint031c_rebalanced_candidate_manifests_bundle")
        cands = data.get("candidates") or {}
        self.assertEqual(set(cands.keys()), set(EXPECTED_KEYS))
        for key in EXPECTED_KEYS:
            m = cands[key]
            self.assertEqual(m.get("kind"), "rebalanced_lineage_reuse_manifest_slice")
            self.assertTrue(m.get("no_peek"))
            self.assertTrue(m.get("no_training"))
            self.assertTrue(m.get("no_live"))
            self.assertEqual(m.get("missing_label_rows_count"), 0)
            self.assertEqual(m.get("reused_label_rows_count"), m.get("selected_rows_count"))
            rows = m.get("manifest_rows") or []
            self.assertEqual(len(rows), int(m.get("selected_rows_count", -1)))
            self.assertLessEqual(int(m.get("selected_pool_count", 999)), 8)

    def test_summary_contract(self) -> None:
        sj = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint031c_rebalanced_lineage_readout")
        self.assertIn(sj.get("final_decision_status"), ALLOWED_FINAL)
        self.assertFalse(sj.get("incremental_label_executed"))
        flags = sj.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))
        cro = sj.get("candidate_readouts") or {}
        self.assertEqual(set(cro.keys()), set(EXPECTED_KEYS))

    def test_summary_csv_contract(self) -> None:
        with SUMMARY_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        keys = {r["candidate_key"] for r in rows}
        self.assertEqual(keys, set(EXPECTED_KEYS))

    def test_audit_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "`TOO_CONCENTRATED_TO_TRUST`",
            "## Candidate manifests",
            "## Incremental labeling",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
            "ROUND_TRIP_COST",
            "FAILED_NO_7D_ALPHA",
        ):
            self.assertIn(needle, body, msg=f"audit missing: {needle}")

    def test_this_file_import_policy(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {"redis", "clickhouse_driver", "web3", "requests"}
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        self.assertFalse(seen & forbidden)


if __name__ == "__main__":
    unittest.main()
