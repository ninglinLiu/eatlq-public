import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path("/opt/EATLQ")
ARTIFACT = ROOT / "mvp/diagnostics/base_gl2_a_universe_ev_20260715.json"
PREREG = ROOT / "reports/GL2_A_prereg_five_evidence_20260715.md"


class GL2AUniverseEVTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(ARTIFACT.read_text())

    def test_preregistration_is_frozen(self):
        digest = hashlib.sha256(PREREG.read_bytes()).hexdigest()
        self.assertEqual(digest, "331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07")
        self.assertEqual(self.data["preregistration"]["sha256"], digest)
        self.assertFalse(self.data["preregistration"]["modified"])

    def test_population_and_rpc_ledger_are_exact(self):
        self.assertEqual(len(self.data["rows"]), 431)
        self.assertEqual(len({row["stable_key"] for row in self.data["rows"]}), 431)
        self.assertEqual(sum(row["cohort"]["keystone_selected"] for row in self.data["rows"]), 116)
        self.assertEqual(self.data["rpc_ledger"]["rpc"]["ledger_rows"], 7369)
        self.assertEqual(self.data["rpc_ledger"]["rpc"]["full_ledger_sha256"], self.data["source_hashes"]["mvp/diagnostics/base_gl2_a_rpc_full_ledger_20260715.jsonl"])

    def test_three_window_anchor_counts_and_means_are_exact(self):
        expected = {
            "1": (282, 149, 0, 0.033086279408226164, 112, 4, 0.598409267494895, 0.5653229880866688),
            "5": (279, 152, 0, 0.0351133825576947, 110, 6, 0.6117220408521848, 0.5766086582944902),
            "15": (278, 153, 0, 0.03942605061837818, 109, 7, 0.6217078603083527, 0.5822818096899744),
        }
        for window, values in expected.items():
            result = self.data["summaries"][window]["including_confirmed_rugs"]
            full = result["full_universe"]
            selected = result["keystone_subset"]
            alpha = result["alpha_beta_decomposition"]
            self.assertEqual(
                (
                    full["n_resolved"], full["n_unresolved"], full["n_confirmed_rug"], full["mean"],
                    selected["n_resolved"], selected["n_unresolved"], selected["mean"], alpha["selection_alpha_component"],
                ),
                values,
            )

    def test_real_sell_never_impersonates_synthetic_position_fill(self):
        allowed = {
            "ATOMIC_GUARDED_EXACT_QUOTE",
            "ATOMIC_GUARDED_EXACT_QUOTE_REAL_SELL_CONFIRMED",
            "CONFIRMED_NONRECOVERING_RUG",
            "NO_ENTRY_FLAT",
        }
        for row in self.data["rows"]:
            for cell in row["windows"].values():
                if cell["resolved"]:
                    self.assertIn(cell["evidence"], allowed)
                self.assertNotEqual(cell["evidence"], "REAL_SELL")
                if cell["evidence"] == "ATOMIC_GUARDED_EXACT_QUOTE_REAL_SELL_CONFIRMED":
                    self.assertIsNotNone(cell["real_sell"])
                    self.assertEqual(cell["exit_weth_raw"], cell["exact_quote"]["amount_out"])

    def test_minus_100_only_after_all_five_evidence(self):
        rug_count = 0
        for row in self.data["rows"]:
            for cell in row["windows"].values():
                if cell["evidence"] != "CONFIRMED_NONRECOVERING_RUG":
                    continue
                rug_count += 1
                five = cell["five_evidence"]
                self.assertEqual(
                    (
                        five["E1_mechanism"],
                        five["E2_pre_atomic_sellable"],
                        five["E2_post_failure"],
                        five["E3_no_later_real_sell_through_cutoff"],
                        five["E4_no_recovery_through_cutoff"],
                        five["E5_observation_complete"],
                        five["confirmed_nonrecovering_rug"],
                        cell["net_return"],
                    ),
                    (True, True, True, True, True, True, True, -1.0),
                )
        self.assertEqual(rug_count, 0)

    def test_incomplete_e4_prevents_every_minus_100(self):
        self.assertEqual(self.data["rug_review"]["prior_confirmed_rugs"], {"1": 141, "5": 143, "15": 145})
        self.assertEqual(self.data["rug_review"]["confirmed_rugs_after_review"], {"1": 0, "5": 0, "15": 0})
        self.assertEqual(self.data["trigger_evidence_counts"]["final_unresolved_after_review"], {"1": 149, "5": 152, "15": 153})

    def test_immediate_ladder_is_not_gl1_estimand(self):
        self.assertFalse(self.data["prereg_text_estimand_defect"]["valid_for_GL1_12_alpha_beta"])
        self.assertEqual(self.data["prereg_text_estimand_defect"]["immediate_ladder_counts"], {"1": {"resolved": 423, "unresolved": 8}, "5": {"resolved": 313, "unresolved": 118}, "15": {"resolved": 305, "unresolved": 126}})

    def test_unresolved_is_not_imputed(self):
        for row in self.data["rows"]:
            for cell in row["windows"].values():
                if not cell["resolved"]:
                    self.assertIsNone(cell["net_return"])
                    self.assertIsNone(cell["exit_weth_raw"])


if __name__ == "__main__":
    unittest.main()
