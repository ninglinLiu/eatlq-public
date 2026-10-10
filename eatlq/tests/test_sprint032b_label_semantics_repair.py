"""Static checks for Sprint032B metadata-verified smoke repair artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint032b_label_semantics_repair_report.md"
DIAG = ROOT / "mvp" / "diagnostics" / "sprint032b_failure_diagnosis_summary.json"
AUDIT_CSV = ROOT / "mvp" / "diagnostics" / "sprint032b_pool_metadata_audit.csv"
MANIFEST = ROOT / "mvp" / "diagnostics" / "sprint032b_metadata_verified_smoke_manifest.json"
SMOKE_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint032b_metadata_verified_smoke_label_summary.json"
SMOKE_LABELS = ROOT / "mvp" / "diagnostics" / "sprint032b_metadata_verified_smoke_labels.csv"
THIS_FILE = Path(__file__).resolve()


class Sprint032BLabelSemanticsRepairTests(unittest.TestCase):
    def test_core_paths_exist(self) -> None:
        for path in (REPORT, DIAG, AUDIT_CSV, MANIFEST, SMOKE_SUMMARY, SMOKE_LABELS):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_manifest_contract(self) -> None:
        mj = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "metadata_verified_hard_quota_smoke_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertTrue(mj.get("no_training") is True)
        self.assertTrue(mj.get("no_live") is True)
        rows_n = int(mj.get("selected_rows_count", 0))
        self.assertGreaterEqual(rows_n, 120)
        self.assertLessEqual(rows_n, 250)
        pools_n = int(mj.get("selected_pool_count", 0))
        self.assertGreaterEqual(pools_n, 4)
        self.assertLessEqual(pools_n, 6)
        mr = mj.get("manifest_rows") or []
        self.assertEqual(len(mr), rows_n)
        ctr = Counter(str(r["pool_audit_only"]).lower() for r in mr)
        self.assertLessEqual(max(ctr.values()), 60)
        total = sum(ctr.values())
        shares = sorted(ctr.values(), reverse=True)
        self.assertLessEqual(shares[0] / total, 0.30 + 1e-9)
        self.assertLessEqual(sum(shares[:3]) / total, 0.70 + 1e-9)

    def test_metadata_audit_csv_has_pass_rows(self) -> None:
        passes = 0
        with AUDIT_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("metadata_status") == "PASS":
                    passes += 1
                    self.assertIn(row.get("resolved_pool_type"), ("v2", "v3"))
                    self.assertEqual(row.get("weth_leg_verified"), "true")
        self.assertGreaterEqual(passes, 6)

    def test_failure_diagnosis_contract(self) -> None:
        data = json.loads(DIAG.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint032b_failure_diagnosis")
        self.assertIn("quote_failed_count", data)

    def test_smoke_summary_contract(self) -> None:
        sj = json.loads(SMOKE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint032b_metadata_verified_bounded_smoke_label_execution")
        self.assertIn(sj.get("status"), ("PASS", "METADATA_REPAIR_SMOKE_PASS", "NEEDS_LABEL_SEMANTICS_REPAIR"))
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("conceptual_quote_pairs", 999)), 700)
        self.assertLessEqual(int(sj.get("approx_eth_rpc_round_trips_total", 10**9)), 50000)

        with SMOKE_LABELS.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertLessEqual(len(rows), 250)
        for row in rows:
            self.assertIn(row["label"], ("0", "1"))

    def test_audit_markdown_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "## Failure diagnosis",
            "## Metadata audit summary",
            "## Repaired smoke label metrics",
            "NEEDS_LABEL_SEMANTICS_REPAIR",
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
