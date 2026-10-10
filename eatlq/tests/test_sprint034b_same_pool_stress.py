"""Sprint034B — same-three-pool stress readout artifacts (schema sanity)."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MANIFEST_034A = REPO / "mvp/diagnostics/sprint034a_few_pool_candidate_manifest.json"
SUMMARY = REPO / "mvp/diagnostics/sprint034b_same_pool_stress_summary.json"
CSV_PATH = REPO / "mvp/diagnostics/sprint034b_same_pool_window_stress.csv"

EXPECTED = {
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
    "0x8aed442e9c17746c0eb85dd22981ce8974182892",
    "0x1e98a167082332e5e89146662e34c3c99481e5e6",
}

ALLOWED_DECISION = {
    "SCOPED_STRESS_SURVIVES_REVIEW_REQUIRED",
    "SCOPED_STRESS_WEAK_CLOSE_SCOPE",
    "SCOPED_STRESS_AMBIGUOUS_NEEDS_ADD_LABEL_MANIFEST",
    "NEEDS_LABEL_SEMANTICS_REPAIR",
    "BLOCKED",
}


class TestSprint034bSamePoolStress(unittest.TestCase):
    def test_manifest_scope_unchanged(self) -> None:
        self.assertTrue(MANIFEST_034A.exists(), f"missing {MANIFEST_034A}")
        m = json.loads(MANIFEST_034A.read_text(encoding="utf-8"))
        pools = {(p["pool_address_lower"]).lower() for p in (m.get("pools_ordered") or [])}
        self.assertEqual(pools, EXPECTED)
        self.assertEqual(m.get("missing_label_rows_count"), 0)
        self.assertEqual(m.get("reused_label_rows_count"), 483)
        self.assertTrue(m.get("no_training"))
        self.assertTrue(m.get("no_live"))

    def test_summary_schema(self) -> None:
        self.assertTrue(SUMMARY.exists(), f"missing {SUMMARY}")
        s = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(s.get("kind"), "sprint034b_same_three_pool_scope_stress_readout")
        self.assertFalse(s.get("rpc_used"))
        self.assertEqual(s.get("new_labels_this_sprint"), 0)

        ds = s.get("decision_status")
        self.assertIn(ds, ALLOWED_DECISION)

        gates = s.get("gates") or {}
        self.assertTrue(gates.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertTrue(gates.get("SAME_POOL_STRESS_ALLOWED"))
        self.assertFalse(gates.get("BOUNDED_RPC_ALLOWED_THIS_SPRINT"))
        self.assertFalse(gates.get("BOUNDED_LABEL_ALLOWED_THIS_SPRINT"))
        self.assertFalse(gates.get("TRAINING_ALLOWED"))
        self.assertFalse(gates.get("LIVE_ALLOWED"))

        per = s.get("per_pool_detail") or {}
        self.assertEqual(set(per.keys()), EXPECTED)
        agg = (s.get("aggregate_stress_bundle") or {}).get("drop_best_pool_second_half") or {}
        self.assertGreater(agg.get("pooled_second_half_rows", 0), 50)

    def test_csv_three_rows(self) -> None:
        self.assertTrue(CSV_PATH.exists(), f"missing {CSV_PATH}")
        with CSV_PATH.open(newline="", encoding="utf-8") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(len(rows), 3)
        pools = {r["pool_address_lower"] for r in rows}
        self.assertEqual(pools, EXPECTED)


if __name__ == "__main__":
    unittest.main()
