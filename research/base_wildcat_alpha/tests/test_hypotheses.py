from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hypotheses import FAMILIES, HYPOTHESES


class HypothesisTests(unittest.TestCase):
    def test_minimum_breadth(self):
        self.assertGreaterEqual(len(HYPOTHESES), 40)
        self.assertGreaterEqual(len({h["family"] for h in HYPOTHESES}), 10)

    def test_ids_unique_and_fields_present(self):
        ids = [h["alpha_id"] for h in HYPOTHESES]
        self.assertEqual(len(ids), len(set(ids)))
        required = {"hypothesis", "mechanism", "prediction_object", "horizon", "data_source", "experiment", "falsifier"}
        for h in HYPOTHESES:
            self.assertFalse(required - set(h))

    def test_nontraditional_share(self):
        unusual = {"block_time", "liquidity", "survival", "lp_state", "lifecycle", "anomaly"}
        self.assertGreaterEqual(sum(h["family"] in unusual for h in HYPOTHESES) / len(HYPOTHESES), 0.30)


if __name__ == "__main__":
    unittest.main()
