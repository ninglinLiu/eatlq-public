"""Sprint034C — additive label manifest contract tests (no RPC; INCONCLUSIVE run)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MANIFEST = REPO / "mvp/diagnostics/sprint034c_additive_label_manifest.json"
SUMMARY = REPO / "mvp/diagnostics/sprint034c_additive_label_summary.json"

EXPECTED_POOLS = {
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
    "0x8aed442e9c17746c0eb85dd22981ce8974182892",
    "0x1e98a167082332e5e89146662e34c3c99481e5e6",
}

ALLOWED_DECISION = {
    "STRONG_PASS", "PASS", "WEAK_PASS", "FAIL", "INCONCLUSIVE",
    "BLOCKED", "BLOCKED_NO_NEW_ROWS_IN_DASHCAM",
}


class TestSprint034cAdditiveContract(unittest.TestCase):
    def test_manifest_exists_and_schema(self) -> None:
        self.assertTrue(MANIFEST.exists(), f"missing {MANIFEST}")
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(m.get("kind"), "sprint034c_additive_label_manifest")
        self.assertTrue(m.get("created_before_new_label"))
        self.assertTrue(m.get("no_peek"))
        self.assertTrue(m.get("no_post_label_pool_replacement"))
        self.assertTrue(m.get("no_training"))
        self.assertTrue(m.get("no_live"))
        self.assertEqual(m.get("quote_failed_policy"), "omit_not_label_zero")
        self.assertLessEqual(m.get("missing_label_rows_cap", 9999), 300)
        pools = {p["pool_address_lower"] for p in (m.get("pools_ordered") or [])}
        self.assertEqual(pools, EXPECTED_POOLS)

    def test_summary_decision_and_gates(self) -> None:
        self.assertTrue(SUMMARY.exists(), f"missing {SUMMARY}")
        s = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(s.get("kind"), "sprint034c_additive_label_summary")
        self.assertIn(s.get("decision_status"), ALLOWED_DECISION)
        gates = s.get("gates") or {}
        self.assertTrue(gates.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertFalse(gates.get("TRAINING_ALLOWED"))
        self.assertFalse(gates.get("LIVE_ALLOWED"))
        self.assertFalse(gates.get("MODEL_PROMOTION_ALLOWED"))
        self.assertFalse(gates.get("ALPHA_PROOF_ALLOWED"))

    def test_new_rows_cap_respected(self) -> None:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        s = json.loads(SUMMARY.read_text(encoding="utf-8"))
        new_labels = int(s.get("new_labels_produced", 0))
        self.assertLessEqual(new_labels, 300)
        candidate_total = int(m.get("new_candidate_rows_total", 0))
        self.assertGreaterEqual(candidate_total, 0)


if __name__ == "__main__":
    unittest.main()
