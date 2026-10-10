"""Sprint034D — dashcam coverage autopsy & CH activity artifact contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

AUTO = REPO / "mvp/diagnostics/sprint034d_dashcam_coverage_autopsy.json"
CH = REPO / "mvp/diagnostics/sprint034d_same_pool_chain_activity_summary.json"
MANIFEST = REPO / "mvp/diagnostics/sprint034d_same_pool_additive_manifest.json"
EXPECTED_POOLS = {
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
    "0x8aed442e9c17746c0eb85dd22981ce8974182892",
    "0x1e98a167082332e5e89146662e34c3c99481e5e6",
}


class TestSprint034dCoverage(unittest.TestCase):
    def test_dashcam_autopsy(self) -> None:
        self.assertTrue(AUTO.exists(), f"missing {AUTO}")
        d = json.loads(AUTO.read_text(encoding="utf-8"))
        self.assertEqual(d.get("kind"), "sprint034d_dashcam_coverage_autopsy")
        self.assertGreater(d.get("dashcam_rows_scanned", 0), 100_000)
        pools = {p["pool_address_lower"] for p in d["pools"]}
        self.assertEqual(pools, EXPECTED_POOLS)
        self.assertFalse(d.get("any_post_sprint031b_dashcam_rows"))

    def test_chain_summary_schema(self) -> None:
        self.assertTrue(CH.exists(), f"missing {CH}")
        c = json.loads(CH.read_text(encoding="utf-8"))
        self.assertEqual(c.get("kind"), "sprint034d_same_pool_chain_activity_summary")
        ds = c.get("decision_status")
        allowed = (
            "DASHCAM_INGESTION_GAP_FOUND",
            "NO_CHAIN_ACTIVITY_AFTER_CUTOFF",
            "READY_FOR_LABEL_BUT_RPC_ENV_MISSING",
            "INSUFFICIENT_NEW_COVERAGE",
            "BLOCKED",
        )
        self.assertIn(ds, allowed)

    def test_additive_manifest_contract(self) -> None:
        self.assertTrue(MANIFEST.exists())
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(m.get("kind"), "sprint034d_same_pool_additive_manifest")
        self.assertTrue(m.get("created_before_label"))
        self.assertTrue(m.get("no_post_label_pool_replacement"))
        self.assertLessEqual(int(m.get("missing_label_rows_cap", 9999)), 300)
        ps = {p["pool_address_lower"] for p in m.get("pools_ordered") or []}
        self.assertEqual(ps, EXPECTED_POOLS)


if __name__ == "__main__":
    unittest.main()
