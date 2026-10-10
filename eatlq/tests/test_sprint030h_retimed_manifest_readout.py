"""Static checks for Sprint030H retimed manifest readout artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_JSON = ROOT / "mvp" / "diagnostics" / "sprint030h_retimed_manifest_candidates.json"
SUMMARY_JSON = ROOT / "mvp" / "diagnostics" / "sprint030h_retimed_subset_readout_summary.json"
SUMMARY_CSV = ROOT / "mvp" / "diagnostics" / "sprint030h_retimed_subset_summary.csv"
AUDIT_MD = ROOT / "mvp" / "audits" / "sprint030h_retimed_manifest_readout.md"
OPTIONAL_INCREMENTAL = ROOT / "mvp" / "diagnostics" / "sprint030h_incremental_retimed_labels.csv"
THIS_FILE = Path(__file__).resolve()

FINAL_TRIAGE_ALLOWED = frozenset(
    {
        "BEST_RETIMED_SUBSET_FOUND",
        "NO_RETIMED_EDGE_FOUND",
        "NEEDS_INCREMENTAL_LABEL",
        "TOO_SMALL_FOR_SUBSET",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
    }
)


class Sprint030HRetimedManifestReadoutTests(unittest.TestCase):
    def test_core_artifacts_exist(self) -> None:
        for path in (MANIFEST_JSON, SUMMARY_JSON, SUMMARY_CSV, AUDIT_MD):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_incremental_csv_only_if_present_is_optional(self) -> None:
        # Sprint030H may omit incremental labels entirely.
        if OPTIONAL_INCREMENTAL.exists():
            self.assertGreater(OPTIONAL_INCREMENTAL.stat().st_size, 0, msg="incremental CSV empty")

    def test_manifest_json_contract(self) -> None:
        data = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "retimed_scoped_manifest_candidates")
        candidates = data.get("candidates") or {}
        for key in ("A_global_early_Q1_Q2", "B_pool_internal_first_half", "C_filtered_exclude_degenerate_and_late_decay_pools"):
            self.assertIn(key, candidates, msg=f"missing manifest candidate {key}")
            c = candidates[key]
            self.assertTrue(c.get("no_peek"), msg=f"{key} no_peek")
            self.assertTrue(c.get("no_training"), msg=f"{key} no_training")
            self.assertEqual(c.get("missing_label_rows_count"), 0)
            self.assertGreaterEqual(c.get("selected_rows_count", 0), 100)
            self.assertIn("manifest_rows", c)
            self.assertEqual(len(c["manifest_rows"]), c["selected_rows_count"])

    def test_summary_json_contract(self) -> None:
        data = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "retimed_subset_coherent_readout")
        decision = data.get("final_triage_decision")
        self.assertIn(decision, FINAL_TRIAGE_ALLOWED, msg=f"unexpected final_triage_decision: {decision}")
        self.assertFalse(data.get("incremental_label_executed"), msg="incremental labeling must stay false for reuse-only sprint")
        flags = data.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))
        readouts = data.get("candidate_readouts") or {}
        self.assertEqual(len(readouts), 3)
        for key, ro in readouts.items():
            self.assertIn("subset_triage", ro, msg=f"missing subset_triage for {key}")
            self.assertIn(ro["subset_triage"], FINAL_TRIAGE_ALLOWED)

    def test_summary_csv_matches_three_candidates(self) -> None:
        with SUMMARY_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        keys = {r["manifest_key"] for r in rows}
        self.assertEqual(
            keys,
            {
                "A_global_early_Q1_Q2",
                "B_pool_internal_first_half",
                "C_filtered_exclude_degenerate_and_late_decay_pools",
            },
        )

    def test_audit_markdown_contract(self) -> None:
        text = AUDIT_MD.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "`NO_RETIMED_EDGE_FOUND`",
            "## Candidate manifests",
            "## Incremental label",
            "## Why this is not alpha",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
            "missing_label_rows_count",
            "ROUND_TRIP_COST=0.006",
        ):
            self.assertIn(needle, text, msg=f"audit missing: {needle}")

    def test_this_static_test_does_not_import_forbidden_runtime_modules(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {
            "redis",
            "clickhouse_driver",
            "web3",
            "requests",
        }
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        hit = sorted(forbidden & seen)
        self.assertEqual(hit, [], msg=f"forbidden imports present: {hit}")


if __name__ == "__main__":
    unittest.main()
