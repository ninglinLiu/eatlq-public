"""Static checks for Sprint030G time-stratified cohort triage artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "mvp" / "audits" / "sprint030g_time_stratified_cohort_triage.md"
SUMMARY_JSON = ROOT / "mvp" / "diagnostics" / "sprint030g_time_stratified_cohort_triage_summary.json"
POOL_TIME_CSV = ROOT / "mvp" / "diagnostics" / "sprint030g_pool_time_summary.csv"
THIS_FILE = Path(__file__).resolve()

VALID_TRIAGE = frozenset(
    {
        "CLOSE_CURRENT_COHORT",
        "NARROW_TO_SINGLE_POOL_OR_SMALL_SUBSET",
        "RETIME_MANIFEST_BEFORE_EXPAND",
        "RETURN_TO_UNIVERSE_MINING",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
    }
)


class Sprint030GTimeStratifiedTriageTests(unittest.TestCase):
    def test_artifacts_exist(self) -> None:
        for path in (AUDIT, SUMMARY_JSON, POOL_TIME_CSV):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_summary_json_contract(self) -> None:
        data = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "time_stratified_cohort_triage")
        self.assertEqual(data.get("status"), "COMPLETE")
        decision = data.get("triage_decision")
        self.assertIn(decision, VALID_TRIAGE, msg=f"unexpected triage_decision: {decision}")
        flags = data.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"), msg="TRAINING_ALLOWED must be false")
        self.assertFalse(flags.get("LIVE_ALLOWED"), msg="LIVE_ALLOWED must be false")
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"), msg="MODEL_PROMOTION_ALLOWED must be false")
        self.assertTrue(flags.get("FAILED_NO_7D_ALPHA_ACTIVE"), msg="FAILED_NO_7D_ALPHA_ACTIVE must stay true")
        self.assertTrue(flags.get("TIME_STRATIFIED_TRIAGE_ALLOWED"), msg="TIME_STRATIFIED_TRIAGE_ALLOWED expected true")
        self.assertIn("temporal_splits", data)
        self.assertIn("pool_time_diagnostics", data)
        self.assertIn("positive_pool_audit", data)
        self.assertIn("bad_pool_audit", data)
        self.assertIn("robust_return_diagnostics", data)

    def test_pool_time_csv_header_and_bins(self) -> None:
        with POOL_TIME_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows, msg="pool-time CSV empty")
        bins = {r["time_bin"] for r in rows}
        self.assertEqual(bins, {"Q1", "Q2", "Q3", "Q4"}, msg=f"time bins: {bins}")

    def test_audit_markdown_contract(self) -> None:
        text = AUDIT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "`RETIME_MANIFEST_BEFORE_EXPAND`",
            "Why Sprint030F",
            "## Global temporal split diagnostics",
            "## Pool × time diagnostics",
            "## Positive pool audit",
            "## Bad pool",
            "## Robust return diagnostics",
            "## Triage decision",
            "## What this does NOT prove",
            "## Recommended next move",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
            "MODEL_PROMOTION_ALLOWED=false",
            "998",
            "ROUND_TRIP_COST=0.006",
        ):
            self.assertIn(needle, text, msg=f"audit missing: {needle}")

    def test_this_static_test_does_not_import_forbidden_runtime_modules(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {
            "scripts.retrain_model",
            "retrain_model",
            "scripts.label_dashcam_executable",
            "label_dashcam_executable",
            "redis",
            "clickhouse_driver",
            "requests",
            "web3",
        }
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        hit = sorted(forbidden & seen)
        self.assertEqual(hit, [], msg=f"forbidden imports-like refs: {hit}")


if __name__ == "__main__":
    unittest.main()
