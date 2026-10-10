from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "mvp/diagnostics/base_gl2_b_stage1_distribution_20260715.json"
COPY = ROOT / "mvp/diagnostics/base_gl2_b_copy_forward_20260715.json"
FINAL = ROOT / "mvp/diagnostics/base_gl2_b_final_20260715.json"
CASH = ROOT / "mvp/diagnostics/base_gl2_b_fifo_cashleg_golden_20260715.json"


def load(path: Path):
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GL2BSmartMoneyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage1 = load(STAGE1)
        cls.copy = load(COPY)
        cls.final = load(FINAL)
        cls.cash = load(CASH)

    def test_frozen_chronology_and_hashes_are_exact(self):
        self.assertEqual(sha(STAGE1), "b80d466a71ea4ed1b9b4e4f59a56485e3b0e426fc0d503483e33bf6f315b8f7b")
        prereg = ROOT / "reports/GL2_B_prereg_copy_forward_20260715.md"
        self.assertEqual(sha(prereg), "a5e6ef0a46245348eb96b50de7243716dce3c746b3074c8a4716e2dc686aa4e2")
        self.assertLess(STAGE1.stat().st_mtime, prereg.stat().st_mtime)
        self.assertLess(prereg.stat().st_mtime, COPY.stat().st_mtime)

    def test_attribution_gate_is_exact(self):
        a = self.stage1["attribution"]
        self.assertEqual((a["eoa_subset_n"], a["eoa_mismatch_n"], a["eoa_mismatch_pct"]), (859, 0, 0.0))
        self.assertEqual((a["trace_eoa_golden_n"], a["trace_eoa_mismatch_n"]), (73, 0))
        self.assertIs(a["gate_le_2pct"], True)

    def test_fifo_and_cash_leg_golden_are_integer_exact(self):
        f = self.stage1["fifo"]
        self.assertEqual((f["golden_n"], f["golden_integer_equal_n"]), (5, 5))
        self.assertTrue(all(g["integer_equal"] and g["all_balance_of_integer_equal"] for g in f["golden_wallets"]))
        self.assertEqual((self.cash["weth_cash_leg_checks"], self.cash["weth_cash_leg_integer_equal_n"]), (20, 20))
        self.assertTrue(all(row["integer_equal"] for row in self.cash["checks"]))

    def test_forward_population_and_contamination_are_exact(self):
        self.assertEqual(self.copy["population"], {"baseline_sample": 4, "pools": 17, "smart_buy_signals": 4, "true_swaps": 4355})
        self.assertEqual(self.final["qualification"]["eligible_n"], 5)
        self.assertEqual(self.final["qualification"]["left_truncated_n"], 2)
        self.assertEqual(self.final["qualification"]["complete_inventory_n"], 3)
        self.assertEqual(self.final["forward"]["complete_inventory_signal_n"], 0)
        self.assertIs(self.final["forward"]["all_signal_wallets_left_truncated"], True)
        contaminated = set(self.final["qualification"]["left_truncated_oversell_wallets"])
        smart_rows = [r for r in self.copy["rows"] if r["kind"] == "smart"]
        self.assertEqual(len(smart_rows), 12)
        self.assertTrue(all(r["source_wallet"] in contaminated for r in smart_rows))

    def test_forward_descriptive_numbers_are_exact(self):
        expected = {
            "1": (-0.053695117184317825, -0.09992434086007895),
            "5": (-0.053695117184317825, -0.09992434086007895),
            "15": (-0.05604115089013783, -0.10124102911169455),
        }
        for delay, (smart_mean, baseline_mean) in expected.items():
            self.assertEqual(self.copy["by_delay"][delay]["smart"]["mean"], smart_mean)
            self.assertEqual(self.copy["by_delay"][delay]["baseline"]["mean"], baseline_mean)
            self.assertEqual(self.copy["by_delay"][delay]["smart"]["resolved"], 4)
            self.assertEqual(self.copy["by_delay"][delay]["baseline"]["resolved"], 2)

    def test_merit_external_gap_and_safety_are_exact(self):
        self.assertEqual(self.final["merit"], "UNTESTED_ELIGIBILITY_CONTAMINATED")
        self.assertEqual(self.final["external_cross_validation"]["nansen"], "NOT_RUN_MISSING_AUTHORITY_AND_API_KEY")
        self.assertIs(self.final["no_go"], True)
        self.assertEqual(self.final["decision_flags"], {"live_allowed": False, "self_go_allowed": False, "training_allowed": False})


if __name__ == "__main__":
    unittest.main()
