"""Static checks for Sprint032D residual sanity-clip artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint032d_residual_sanity_clip_and_robustness_report.md"
SANITY = ROOT / "mvp" / "diagnostics" / "sprint032d_sanity_clip_audit_summary.json"
READOUT = ROOT / "mvp" / "diagnostics" / "sprint032d_robust_candidate_readout_summary.json"
CSV_SUM = ROOT / "mvp" / "diagnostics" / "sprint032d_robust_candidate_summary.csv"
THIS_FILE = Path(__file__).resolve()


class Sprint032DResidualSanityClipTests(unittest.TestCase):
    def test_paths_exist(self) -> None:
        for p in (REPORT, SANITY, READOUT, CSV_SUM):
            self.assertTrue(p.is_file(), msg=f"Missing: {p}")

    def test_sanity_json_contract(self) -> None:
        j = json.loads(SANITY.read_text(encoding="utf-8"))
        self.assertEqual(j.get("kind"), "sprint032d_residual_sanity_clip_audit")
        self.assertIn("fail_sanity_clip_count", j)
        self.assertIn(j.get("sanity_clip_assessment"), ("SANITY_CLIP_NOT_BLOCKING", "SANITY_CLIP_STRUCTURAL_RISK"))
        self.assertFalse(bool(j.get("needs_label_semantics_repair")))

    def test_candidate_readout_contract(self) -> None:
        j = json.loads(READOUT.read_text(encoding="utf-8"))
        self.assertEqual(j.get("kind"), "sprint032d_robust_candidate_readout")
        candidates = j.get("candidates") or []
        self.assertGreaterEqual(len(candidates), 5)
        kinds = {c.get("kind") for c in candidates}
        self.assertTrue({"A_full_032c_executable_set", "B_exclude_sanity_clip_structural_pool", "C_balanced_per_pool_cap35", "D_late_window_half", "E_conservative_robust"}.issubset(kinds))
        for c in candidates:
            self.assertTrue(c.get("no_peek") is True)
            self.assertTrue(c.get("no_training") is True)
            self.assertTrue(c.get("no_live") is True)
        self.assertIn(
            j.get("final_status"),
            (
                "ROBUST_SUBSET_REVIEW_REQUIRED",
                "SANITY_CLIP_REPAIR_REQUIRED",
                "LINEAGE_STILL_WEAK_RETURN_TO_MINING",
                "TOO_SMALL_FOR_ROBUST_SUBSET",
                "INCREMENTAL_LABEL_EXECUTED_REVIEW_REQUIRED",
            ),
        )

    def test_candidate_csv_contract(self) -> None:
        with CSV_SUM.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreaterEqual(len(rows), 5)
        for r in rows:
            self.assertIn(r["robust_gate_pass"], ("true", "false"))

    def test_report_contains_required_sections(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "## Residual sanity-clip diagnosis",
            "## Candidate subset summary",
            "## Best candidate readout",
            "## Decision flags",
            "TRAINING_ALLOWED=false",
        ):
            self.assertIn(needle, body, msg=f"report missing: {needle}")

    def test_forbidden_imports(self) -> None:
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
