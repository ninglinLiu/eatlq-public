import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path("/opt/EATLQ")
RESULT = ROOT / "mvp/diagnostics/base_gl3_gl12_alpha_beta_20260715.json"
INVENTORY = ROOT / "mvp/diagnostics/base_gl3_event_inventory_20260715.json"
POOLS = ROOT / "mvp/diagnostics/base_gl3_pool_interval_evidence_20260715.json"
PREREG = ROOT / "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md"
PREREG_SHA = "fdbeccb0a4a1b17ccda0bb3e8779608804d051a8e61f89d4021cd5fc5b88a0bf"


class GL3GL12ClosureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = json.loads(RESULT.read_text())
        cls.inventory = json.loads(INVENTORY.read_text())
        cls.pools = json.loads(POOLS.read_text())

    def test_preregistration_and_estimand_are_exact(self):
        self.assertEqual(hashlib.sha256(PREREG.read_bytes()).hexdigest(), PREREG_SHA)
        self.assertEqual(self.result["preregistration"]["sha256"], PREREG_SHA)
        self.assertFalse(self.result["preregistration"]["modified"])
        self.assertEqual(
            self.result["authoritative_estimand"],
            {"entry": "entry_block+1", "exit": "trigger_block+1/+5/+15", "notional_weth_raw": "1000000000000000", "pricing": "WETH"},
        )

    def test_full_population_and_frozen_residual_counts(self):
        self.assertEqual(len(self.result["rows"]), 431)
        self.assertEqual(sum(row["cohort"]["keystone_selected"] for row in self.result["rows"]), 116)
        self.assertEqual(self.result["population"]["frozen_residual_cells"], {"1": 149, "5": 152, "15": 153})
        self.assertEqual(self.result["adjudication_counts"], {"CONFIRMED_CONTINUOUSLY_UNSELLABLE": 411, "UNRESOLVED": 43})

    def test_event_and_true_reserve_coverage_is_exact(self):
        coverage = self.inventory["coverage"]
        self.assertTrue(coverage["all_chunks_ok"])
        self.assertEqual(len(coverage["chunks"]), 3336)
        self.assertEqual(coverage["post_exit_per_pool_equal_n"], 144)
        self.assertEqual((coverage["ch_event_keys_n"], coverage["rpc_event_keys_n"]), (11287, 12770))
        self.assertEqual(
            (self.pools["summary"]["reserve_checks_n"], self.pools["summary"]["reserve_integer_equal_n"], self.pools["summary"]["data_gap_pools"]),
            (625, 625, 0),
        )

    def test_minus_100_requires_full_interval_green_evidence(self):
        count = 0
        for row in self.result["rows"]:
            for cell in row["windows"].values():
                if cell["evidence"] != "CONFIRMED_CONTINUOUSLY_UNSELLABLE":
                    continue
                count += 1
                proof = cell["gl3_full_interval"]
                self.assertEqual(cell["net_return"], -1.0)
                self.assertEqual(cell["net_weth_raw"], "-1000000000000000")
                self.assertTrue(proof["post_exit_ch_rpc_event_keys_equal"])
                self.assertTrue(proof["reserve_transfer_integer_equal"])
                self.assertEqual((proof["true_sell_n"], proof["qualified_mint_n"], proof["candidate_retests"]), (0, 0, []))
                self.assertEqual(proof["scan_through_block"], 46865124)
        self.assertEqual(count, 411)

    def test_remaining_unknowns_are_not_imputed(self):
        expected = {"1": (416, 15, 134), "5": (417, 14, 138), "15": (417, 14, 139)}
        for window, values in expected.items():
            full = self.result["summaries"][window]["full_universe"]
            self.assertEqual((full["n_resolved"], full["n_unresolved"], full["n_confirmed_continuously_unsellable"]), values)
        for row in self.result["rows"]:
            for cell in row["windows"].values():
                if not cell["resolved"]:
                    self.assertIsNone(cell["net_return"])
                    self.assertIsNone(cell["exit_weth_raw"])
                    self.assertTrue(cell["gl3_full_interval"]["data_gaps"])

    def test_bounded_alpha_is_exact_and_strictly_positive(self):
        expected = {
            "1": (0.8440592243945477, 0.8893676900591266),
            "5": (0.8382705225775717, 0.869136556795074),
            "15": (0.8336491590218975, 0.8643623499655508),
        }
        for window, interval in expected.items():
            actual = self.result["summaries"][window]["alpha_beta"]["selection_alpha_interval"]
            self.assertEqual(tuple(actual), interval)
            self.assertGreaterEqual(actual[0], 0.10)
        self.assertEqual(self.result["review_candidate"], {"grade": "IRON", "is_final_grade": False, "rule": "all three alpha_lower >= +0.10 plus independent anchor review", "all_three_alpha_lower_ge_10pp": True})

    def test_rpc_ledger_is_read_only(self):
        methods = set(self.result["source_hashes"])
        self.assertIn("mvp/diagnostics/base_gl3_rpc_full_ledger_20260715.jsonl", methods)
        ledger = (ROOT / "mvp/diagnostics/base_gl3_rpc_full_ledger_20260715.jsonl").read_text().splitlines()
        used = {json.loads(line)["method"] for line in ledger}
        self.assertLessEqual(used, {"eth_call", "eth_getLogs", "eth_getTransactionReceipt"})
        self.assertFalse(any("send" in method.lower() or "sign" in method.lower() for method in used))


if __name__ == "__main__":
    unittest.main()
