"""Sprint034A diagnostics — few-pool scoped temporal paper validation artifacts."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]

MANIFEST = REPO / "mvp/diagnostics/sprint034a_few_pool_candidate_manifest.json"
SUMMARY_JSON = REPO / "mvp/diagnostics/sprint034a_few_pool_temporal_readout_summary.json"

ALLOWED_STATUS = {
    "SCOPED_TEMPORAL_PAPER_PROMISING_REVIEW_REQUIRED",
    "SCOPED_TEMPORAL_PIPELINE_OK_ECON_WEAK",
    "NO_SCOPED_TEMPORAL_EDGE_FOUND",
    "NEEDS_LABEL_SEMANTICS_REPAIR",
    "BLOCKED",
}


class TestSprint034aFewPoolTemporalReadout(unittest.TestCase):
    def test_manifest_schema(self) -> None:
        self.assertTrue(MANIFEST.exists(), f"missing {MANIFEST}")
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(m.get("kind"), "few_pool_scoped_temporal_manifest")
        self.assertTrue(m.get("created_before_new_label"))
        self.assertTrue(m.get("no_peek"))
        self.assertTrue(m.get("no_training"))
        self.assertTrue(m.get("no_live"))
        self.assertIn("source_artifacts", m)
        self.assertIn("selected_rows_count", m)
        self.assertIn("reused_label_rows_count", m)
        self.assertIn("missing_label_rows_count", m)
        self.assertEqual(m.get("missing_label_rows_count"), 0)
        pools = m.get("pools_ordered") or []
        self.assertGreaterEqual(len(pools), 1)
        self.assertLessEqual(len(pools), 3)

    def test_summary_decision_and_gates(self) -> None:
        self.assertTrue(SUMMARY_JSON.exists(), f"missing {SUMMARY_JSON}")
        s = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.assertEqual(s.get("kind"), "sprint034a_few_pool_temporal_readout_summary")
        st = s.get("decision_status")
        self.assertIn(st, ALLOWED_STATUS)
        gates = s.get("gates") or {}
        self.assertTrue(gates.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertTrue(gates.get("FEW_POOL_SCOPED_TEMPORAL_VALIDATION_ALLOWED"))
        self.assertTrue(gates.get("BOUNDED_LABEL_ALLOWED"))
        self.assertFalse(gates.get("TRAINING_ALLOWED"))
        self.assertFalse(gates.get("LIVE_ALLOWED"))
        self.assertFalse(gates.get("MODEL_PROMOTION_ALLOWED"))
        agg = (s.get("few_pool_aggregate_second_half_bundle") or {})
        self.assertTrue(agg.get("explicit_no_alpha_claim"))
        self.assertTrue(agg.get("explicit_not_live_signal"))

    def test_summary_rows_match_manifest_pools(self) -> None:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        s = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        wanted = {(p["pool_address_lower"]).lower() for p in (m.get("pools_ordered") or [])}
        detail = s.get("pools_detail") or {}
        self.assertEqual(set(detail.keys()), wanted)
        reused = sum(int(v.get("reused_rows", 0)) for v in detail.values())
        self.assertEqual(reused, m.get("reused_label_rows_count"))

    def test_small_csv_readable(self) -> None:
        p = REPO / "mvp/diagnostics/sprint034a_few_pool_temporal_summary.csv"
        self.assertTrue(p.exists(), f"missing {p}")
        with p.open(newline="", encoding="utf-8") as fp:
            rows = list(csv.DictReader(fp))
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 3)

    def test_incremental_optional_cap(self) -> None:
        inc = REPO / "mvp/diagnostics/sprint034a_incremental_few_pool_labels.csv"
        if inc.exists():
            with inc.open(newline="", encoding="utf-8") as fp:
                n = sum(1 for _ in csv.reader(fp)) - 1
            self.assertGreaterEqual(n, 0)
            self.assertLessEqual(n, 300)


if __name__ == "__main__":
    unittest.main()
