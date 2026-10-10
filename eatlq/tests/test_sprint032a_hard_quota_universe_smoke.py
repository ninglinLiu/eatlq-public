"""Static checks for Sprint032A hard-quota universe mining + bounded smoke artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint032a_hard_quota_universe_smoke_report.md"
MINING_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint032a_hard_quota_universe_summary.json"
MANIFEST = ROOT / "mvp" / "diagnostics" / "sprint032a_hard_quota_smoke_manifest.json"
SMOKE_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint032a_hard_quota_smoke_label_summary.json"
SMOKE_LABELS = ROOT / "mvp" / "diagnostics" / "sprint032a_hard_quota_smoke_labels.csv"
CANDIDATE_CSV = ROOT / "mvp" / "diagnostics" / "sprint032a_hard_quota_candidate_summary.csv"
THIS_FILE = Path(__file__).resolve()

ALLOWED_FINAL = frozenset(
    {
        "HARD_QUOTA_SMOKE_PASS",
        "HARD_QUOTA_SMOKE_PROMISING_REVIEW_REQUIRED",
        "NO_BALANCED_COHORT_FOUND",
        "PIPELINE_OK_ECONOMICALLY_WEAK",
        "NEEDS_CH_OR_DASHCAM_REPAIR",
        "NEEDS_LABEL_SEMANTICS_REPAIR",
        "BLOCKED_CAP_EXCEEDED",
        "NEW_COHORT_FOUND_NO_RPC",
        "PENDING_SMOKE_EXECUTION",
    }
)


class Sprint032AHardQuotaUniverseSmokeTests(unittest.TestCase):
    def test_core_paths_exist(self) -> None:
        for path in (REPORT, MINING_SUMMARY, MANIFEST, SMOKE_SUMMARY, SMOKE_LABELS, CANDIDATE_CSV):
            self.assertTrue(path.is_file(), msg=f"Missing: {path}")

    def test_manifest_hard_quota_contract(self) -> None:
        mj = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "hard_quota_new_universe_smoke_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertTrue(mj.get("no_training") is True)
        self.assertTrue(mj.get("no_live") is True)
        rows_n = int(mj.get("selected_rows_count", 0))
        self.assertGreaterEqual(rows_n, 120)
        self.assertLessEqual(rows_n, 250)
        pools_n = int(mj.get("selected_pool_count", 0))
        self.assertGreaterEqual(pools_n, 4)
        self.assertLessEqual(pools_n, 6)
        manifest_rows = mj.get("manifest_rows") or []
        self.assertEqual(len(manifest_rows), rows_n)
        ctr = Counter(str(r["pool_audit_only"]).lower() for r in manifest_rows)
        self.assertLessEqual(max(ctr.values()), 60)
        total = sum(ctr.values())
        shares = sorted(ctr.values(), reverse=True)
        top1 = shares[0] / total
        top3 = sum(shares[:3]) / total
        self.assertLessEqual(top1, 0.30 + 1e-9)
        self.assertLessEqual(top3, 0.70 + 1e-9)

    def test_mining_summary_contract(self) -> None:
        data = json.loads(MINING_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint032a_hard_quota_universe_mining")
        status = data.get("final_decision_status")
        self.assertIn(status, ALLOWED_FINAL, msg=f"unexpected final_decision_status: {status}")
        flags = data.get("decision_flags") or {}
        self.assertFalse(flags.get("TRAINING_ALLOWED"))
        self.assertFalse(flags.get("LIVE_ALLOWED"))
        self.assertFalse(flags.get("MODEL_PROMOTION_ALLOWED"))
        self.assertTrue(flags.get("FAILED_NO_7D_ALPHA_ACTIVE"))

    def test_smoke_label_outputs_contract(self) -> None:
        sj = json.loads(SMOKE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint032a_hard_quota_bounded_smoke_label_execution")
        self.assertIn(sj.get("status"), ("PASS", "NEEDS_LABEL_SEMANTICS_REPAIR", "BLOCKED_CAP_EXCEEDED", "FAIL"))
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("conceptual_quote_pairs", 999)), 700)
        self.assertLessEqual(int(sj.get("approx_eth_rpc_round_trips_total", 10**9)), 40000)

        if sj.get("status") == "PASS":
            self.assertGreaterEqual(float(sj.get("clean_rate", 0)), 0.95)
            self.assertLessEqual(float(sj.get("failure_rate", 1)), 0.05)

        with SMOKE_LABELS.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertLessEqual(len(rows), 250)
        for row in rows:
            self.assertIn(row["label"], ("0", "1"))

    def test_audit_markdown_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "## Closed lineage",
            "## Mining sources",
            "## Hard-quota manifest summary",
            "## Smoke label metrics",
            "## Economic readout",
            "## Decision flags",
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
