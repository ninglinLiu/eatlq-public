"""Static checks for Sprint031A universe mining + bounded smoke artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint031a_return_to_universe_mining_report.md"
MINING_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint031a_return_to_universe_mining_summary.json"
MANIFEST = ROOT / "mvp" / "diagnostics" / "sprint031a_new_universe_smoke_manifest.json"
SMOKE_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint031a_new_universe_smoke_label_summary.json"
SMOKE_LABELS = ROOT / "mvp" / "diagnostics" / "sprint031a_new_universe_smoke_labels.csv"
CANDIDATE_CSV = ROOT / "mvp" / "diagnostics" / "sprint031a_candidate_pool_summary.csv"
THIS_FILE = Path(__file__).resolve()

ALLOWED_FINAL = frozenset(
    {
        "NEW_COHORT_SMOKE_PASS",
        "NEW_COHORT_FOUND_NO_RPC",
        "NO_NEW_COHORT_FOUND",
        "NEEDS_CH_OR_DASHCAM_REPAIR",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
        "ECONOMICALLY_WEAK_BUT_PIPELINE_OK",
    }
)


class Sprint031AReturnToUniverseMiningTests(unittest.TestCase):
    def test_core_paths_exist(self) -> None:
        for path in (REPORT, MINING_SUMMARY, MANIFEST, SMOKE_SUMMARY, SMOKE_LABELS, CANDIDATE_CSV):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_manifest_contract(self) -> None:
        mj = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "new_universe_bounded_smoke_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertTrue(mj.get("no_training") is True)
        self.assertTrue(mj.get("no_live") is True)
        self.assertGreaterEqual(int(mj.get("selected_rows_count", 0)), 100)
        self.assertLessEqual(int(mj.get("selected_rows_count", 999)), 250)
        self.assertLessEqual(int(mj.get("selected_pool_count", 999)), 5)
        rows = mj.get("manifest_rows") or []
        self.assertEqual(len(rows), int(mj.get("selected_rows_count", -1)))

    def test_mining_summary_contract(self) -> None:
        data = json.loads(MINING_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint031a_return_to_universe_mining")
        status = data.get("final_decision_status")
        self.assertIn(status, ALLOWED_FINAL, msg=f"unexpected final_decision_status: {status}")
        flags = data.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))
        self.assertTrue(flags.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertIn("smoke_execution_summary", data)

    def test_smoke_label_outputs_contract(self) -> None:
        sj = json.loads(SMOKE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint031a_new_universe_bounded_smoke_label_execution")
        self.assertEqual(sj.get("status"), "PASS")
        self.assertGreaterEqual(float(sj.get("clean_rate", 0)), 0.95)
        self.assertLessEqual(float(sj.get("failure_rate", 1)), 0.05)
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("conceptual_quote_pairs", 999)), 600)
        self.assertLessEqual(int(sj.get("approx_eth_rpc_round_trips_total", 10**9)), 30000)

        with SMOKE_LABELS.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertLessEqual(len(rows), 250)
        for row in rows:
            self.assertIn(row["label"], ("0", "1"))

    def test_audit_markdown_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "`NEW_COHORT_SMOKE_PASS`",
            "## Closed cohort",
            "## Mining sources",
            "## Smoke manifest summary",
            "## Smoke label metrics",
            "## Initial economic readout",
            "## What this does NOT prove",
            "TRAINING_ALLOWED=false",
            "ROUND_TRIP_COST=0.006",
        ):
            self.assertIn(needle, body, msg=f"report missing: {needle}")

    def test_this_static_test_avoids_forbidden_runtime_imports(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {"redis", "clickhouse_driver", "web3", "requests"}
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
