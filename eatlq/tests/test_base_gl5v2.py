import hashlib
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path("/opt/EATLQ")
PREREG = ROOT / "reports/GL5v2_preregistration_20260716.md"
OUT = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_repricing_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
SCRIPT = ROOT / "scripts/research/base_gl5v2/gl5v2_reprice.py"

spec = importlib.util.spec_from_file_location("gl5v2_test_module", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestGL5v2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = json.loads(OUT.read_text())
        cls.per = json.loads(PER.read_text())
        cls.selected = [r for r in cls.per["rows"] if r["selected_Fon"]]

    def test_preregistration_frozen(self):
        self.assertEqual(hashlib.sha256(PREREG.read_bytes()).hexdigest(),
                         "c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e")
        self.assertEqual(self.out["preregistration_sha256"],
                         "c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e")

    def test_arm0_full_tree_is_exact(self):
        x = self.out["arm0_reproduction"]
        self.assertEqual(x["status"], "PASS")
        self.assertEqual(x["assertion_count"], 70)
        self.assertTrue(x["all_python_equal"])
        self.assertTrue(all(a["python_equal"] for a in x["assertions"]))

    def test_own_headline_not_plus56(self):
        for lens in ("qv2", "fork"):
            h = self.out["arm0_reproduction"]["lenses"][lens]["live_full_gates"]
            self.assertEqual(h["pass_n"], 120)
            self.assertEqual(h["monthly"], 0.08574380412768878)
            self.assertEqual(h["mean_ret"], 1.4280241443411608)
            self.assertEqual(h["fatal_n"], 2)
            self.assertEqual(h["dr_traded"], 0)

    def test_original_evidence_composition(self):
        x = self.out["arm0_source_evidence_composition"]
        self.assertEqual(x["qv2_lens"], {"UNGUARDED_QV2_QUOTE": 120})
        self.assertEqual(x["fork_lens"], {"FORK_SIMULATION": 120})
        self.assertEqual(x["true_receipt_sell_used_by_original"], 0)
        self.assertEqual(x["atomic_guarded_quote_used_by_original"], 0)
        self.assertEqual(x["fork_crosstab_n"], 65)

    def test_population_and_branches_close(self):
        self.assertEqual(len(self.per["rows"]), 644)
        self.assertEqual(len(self.selected), 120)
        for offset in ("1", "5", "15"):
            self.assertEqual(sum(self.out["arm1"][offset]["branches"].values()), 120)
            self.assertEqual(sum(self.out["baseline"][offset]["branches"].values()), 644)
            self.assertEqual(self.out["arm1"][offset]["confirmed_collapse_n"], 0)
            self.assertEqual(self.out["arm1"][offset]["status"], "INCONCLUSIVE")

    def test_primary_exact_counts_and_bounds(self):
        x = self.out["arm1"]["1"]
        self.assertEqual(x["branches"], {
            "REAL_SELL": 31, "SURVIVAL_ONLY_UNRESOLVED": 11,
            "UNRESOLVED": 58, "UNRESOLVED_SIZE": 20,
        })
        self.assertEqual(x["unresolved"], 89)
        self.assertEqual(x["point_resolved_only"]["mean"], 1.5407292661261793)
        self.assertEqual(x["population_mean_interval_qv2"],
                         [-0.3606990866834547, 1.4370311344990674])
        self.assertEqual(self.out["decomposition"]["1"]["Fon_rug_rate_interval"],
                         [0.0, 0.48333333333333334])

    def test_evidence_priority(self):
        self.assertEqual(mod.priority_branch(True, True, True, True), "REAL_SELL")
        self.assertEqual(mod.priority_branch(False, True, True, True), "CONFIRMED_COLLAPSE")
        self.assertEqual(mod.priority_branch(False, False, True, True), "SURVIVED_ATOMIC_QUOTE")
        self.assertEqual(mod.priority_branch(False, False, True, False), "UNRESOLVED")
        self.assertEqual(mod.priority_branch(False, False, False, True), "UNRESOLVED")

    def test_collapse_threshold_boundaries(self):
        self.assertTrue(mod.collapse_at(100, 10, 0.90))
        self.assertFalse(mod.collapse_at(100, 11, 0.90))
        self.assertTrue(mod.collapse_at(100, 5, 0.95))
        self.assertFalse(mod.collapse_at(100, 6, 0.95))
        self.assertTrue(mod.collapse_at(100, 1, 0.99))
        self.assertFalse(mod.collapse_at(100, 2, 0.99))
        self.assertIsNone(mod.collapse_at(None, 1, 0.95))

    def test_symmetric_unknown_and_no_rpc(self):
        self.assertLess(mod.PESS, -1.0)
        self.assertEqual(self.out["data_coverage"]["archive_rpc"], "NOT_RUN_ENV_UNSET")
        self.assertEqual(self.out["data_coverage"]["new_rpc_calls"], 0)
        self.assertEqual(self.out["verdict_candidate"], "INCONCLUSIVE")
        source = SCRIPT.read_text()
        self.assertNotIn("urlopen", source)
        self.assertNotIn("eth_send", source)
        self.assertNotIn("sendRawTransaction", source)

    def test_go_not_made(self):
        self.assertEqual(self.out["go_decision"], "NOT_MADE")
        self.assertFalse(self.out["decision_flags"]["training_allowed"])
        self.assertFalse(self.out["decision_flags"]["live_allowed"])
        self.assertFalse(self.out["decision_flags"]["broadcast_allowed"])


if __name__ == "__main__":
    unittest.main()
