"""Static checks for Sprint032C historical viability repair artifacts."""

from __future__ import annotations

import ast
import csv
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "mvp" / "audits" / "sprint032c_historical_viability_repair_report.md"
AUTOPSY = ROOT / "mvp" / "diagnostics" / "sprint032c_historical_failure_autopsy.json"
VIABILITY_CSV = ROOT / "mvp" / "diagnostics" / "sprint032c_historical_viability_audit.csv"
MANIFEST = ROOT / "mvp" / "diagnostics" / "sprint032c_viability_verified_smoke_manifest.json"
SMOKE_SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint032c_viability_verified_smoke_label_summary.json"
SMOKE_LABELS = ROOT / "mvp" / "diagnostics" / "sprint032c_viability_verified_smoke_labels.csv"
THIS_FILE = Path(__file__).resolve()


class Sprint032CHistoricalViabilityRepairTests(unittest.TestCase):
    def test_core_paths_exist(self) -> None:
        for p in (REPORT, AUTOPSY, VIABILITY_CSV, MANIFEST, SMOKE_SUMMARY, SMOKE_LABELS):
            self.assertTrue(p.is_file(), msg=f"Missing: {p}")

    def test_manifest_contract(self) -> None:
        mj = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(mj.get("kind"), "viability_verified_hard_quota_smoke_manifest")
        self.assertTrue(mj.get("created_before_quote") is True)
        self.assertTrue(mj.get("no_training") is True)
        self.assertTrue(mj.get("no_live") is True)
        rows_n = int(mj.get("selected_rows_count", 0))
        pools_n = int(mj.get("selected_pool_count", 0))
        self.assertGreaterEqual(rows_n, 120)
        self.assertLessEqual(rows_n, 250)
        self.assertGreaterEqual(pools_n, 4)
        self.assertLessEqual(pools_n, 6)
        mr = mj.get("manifest_rows") or []
        self.assertEqual(len(mr), rows_n)
        c = Counter(str(r["pool_audit_only"]).lower() for r in mr)
        self.assertLessEqual(max(c.values()), 60)
        total = sum(c.values())
        shares = sorted(c.values(), reverse=True)
        self.assertLessEqual(shares[0] / total, 0.30 + 1e-9)
        self.assertLessEqual(sum(shares[:3]) / total, 0.70 + 1e-9)

    def test_autopsy_contract(self) -> None:
        aj = json.loads(AUTOPSY.read_text(encoding="utf-8"))
        self.assertEqual(aj.get("kind"), "sprint032c_historical_failure_autopsy")
        self.assertIn("quote_failed_count", aj)
        self.assertIn("failure_reason_counts", aj)
        self.assertIn("pool_viability_status", aj)
        self.assertLessEqual(int(aj.get("historical_metadata_rpc_calls_estimate", 10**9)), 8000)
        self.assertLessEqual(int(aj.get("viability_probe_quote_budget_used", 10**9)), 1200)

    def test_viability_csv_contract(self) -> None:
        with VIABILITY_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0)
        required = {
            "pool_address_audit_key",
            "bucket_ts_ms_audit_only",
            "block_number_audit_only",
            "token0_ok",
            "token1_ok",
            "fee_ok",
            "liquidity_status",
            "slot0_status",
            "weth_leg_verified",
            "entry_quote_status",
            "entry_quote_zero",
            "exit_quote_status_if_checked",
            "viability_status",
            "failure_reason",
        }
        self.assertTrue(required.issubset(set(rows[0].keys())))

    def test_smoke_summary_contract(self) -> None:
        sj = json.loads(SMOKE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(sj.get("kind"), "sprint032c_viability_verified_bounded_smoke_label_execution")
        self.assertIn(sj.get("status"), ("PASS", "NEEDS_LABEL_SEMANTICS_REPAIR", "BLOCKED_CAP_EXCEEDED"))
        self.assertLessEqual(int(sj.get("attempted_rows", 999)), 250)
        self.assertLessEqual(int(sj.get("conceptual_quote_pairs", 999)), 700)
        self.assertLessEqual(int(sj.get("approx_eth_rpc_round_trips_total", 10**9)), 70000)
        with SMOKE_LABELS.open(encoding="utf-8", newline="") as f:
            out_rows = list(csv.DictReader(f))
        self.assertLessEqual(len(out_rows), 250)
        for r in out_rows:
            self.assertIn(r["label"], ("0", "1"))

    def test_report_contract(self) -> None:
        body = REPORT.read_text(encoding="utf-8")
        for needle in (
            "## Executive conclusion",
            "## Historical failure autopsy",
            "## Historical viability audit",
            "## Repaired manifest summary",
            "## Repaired smoke metrics",
            "## Decision flags",
            "ROUND_TRIP_COST=0.006",
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
