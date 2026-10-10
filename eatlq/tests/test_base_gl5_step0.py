import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path("/opt/EATLQ")
ANCHOR = ROOT / "mvp/diagnostics/base_gl5_step0_anchor_20260716.json"
PER_LEG = ROOT / "mvp/diagnostics/base_gl5_per_leg_20260716.json"
PREREG = ROOT / "reports/GL5_preregistration_20260716.md"
SCRIPT = ROOT / "scripts/research/base_gl5/gl5_step0_anchor.py"


class TestGL5Step0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.anchor = json.loads(ANCHOR.read_text())
        cls.per_leg = json.loads(PER_LEG.read_text())

    def test_preregistration_is_frozen(self):
        self.assertEqual(
            hashlib.sha256(PREREG.read_bytes()).hexdigest(),
            "909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9",
        )
        self.assertEqual(self.anchor["preregistration_sha256"],
                         "909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9")

    def test_arm0_exact_headline(self):
        x = self.anchor["arm0_reproduction"]
        self.assertEqual(x["status"], "PASS")
        self.assertEqual(x["frontier"]["1"]["n"], 113)
        self.assertEqual(x["frontier"]["1"]["unresolved"], 3)
        self.assertEqual(x["frontier"]["1"]["mean"], 0.5663701436516096)
        self.assertEqual(x["frontier"]["1"]["median"], 0.2597229826746521)
        self.assertEqual(x["frontier"]["1"]["top1_removed_mean"], 0.48288116031956935)
        self.assertTrue(all(v["hardcoded_anchor_exact"] and v["reaudit10_final_exact"]
                            for v in x["frontier"].values()))

    def test_gl1_taxonomy_exact(self):
        x = self.anchor["arm0_reproduction"]["gl1_taxonomy"]
        self.assertEqual(x["GL1-01_grade"], "PINNED")
        self.assertTrue(x["GL1-01_plus56_anchor_exact"])
        self.assertEqual(x["GL1-12_grade"], "IRON")
        self.assertTrue(x["GL1-12_interval_anchor_exact"])

    def test_f_identity_hard_stop(self):
        self.assertFalse(self.anchor["f_identity"]["pass"])
        self.assertIsNone(self.anchor["pinned_estimand"]["F_filter"])
        self.assertEqual(self.anchor["step0"]["status"], "STOPPED")
        self.assertEqual(self.anchor["step0"]["stop_reason"],
                         "STOP_OBJECT_IDENTITY_MISMATCH")
        self.assertFalse(self.anchor["step0"]["object_identity_pass"])
        self.assertTrue(self.anchor["step0"]["arm0_reproduction_pass"])
        self.assertIsNone(self.anchor["pinned_estimand"]["legacy_exit"]["take_profit_multiple"])
        self.assertIsNone(self.anchor["pinned_estimand"]["legacy_exit"]["tp_ladder"])

    def test_no_post_gate_work_or_rpc(self):
        x = self.anchor["post_step0"]
        self.assertEqual(x["arm1"], "NOT_RUN_STOP_GATE")
        self.assertEqual(x["same_age_baseline_repricing"], "NOT_RUN_STOP_GATE")
        self.assertEqual(x["decomposition"], "NOT_RUN_STOP_GATE")
        self.assertEqual(x["golden_rpc"], "NOT_RUN_STOP_GATE")
        self.assertEqual(x["new_rpc_calls"], 0)
        self.assertFalse(x["rpc_url_accessed"])
        source = SCRIPT.read_text()
        self.assertNotIn("urlopen", source)
        self.assertNotIn("eth_send", source)
        self.assertNotIn("sendRawTransaction", source)

    def test_per_leg_is_complete_stop_ledger(self):
        rows = self.per_leg["rows"]
        self.assertEqual(len(rows), 116)
        self.assertTrue(all(r["selected"] is True for r in rows))
        self.assertTrue(all(len(r["arm0"]) == 5 for r in rows))
        self.assertTrue(all(r["F_status"] == "NOT_DEFINED_IN_SOURCE_ESTIMAND" for r in rows))
        self.assertTrue(all(r["arm1"] == "NOT_RUN_STOP_GATE" for r in rows))

    def test_separate_f_estimands_are_not_grafted(self):
        x = self.anchor["f_identity"]["separate_external_contrast_objects"]
        self.assertEqual(x["conservative_644_pool_rebacktest"]["universe_n"], 644)
        self.assertEqual(x["conservative_644_pool_rebacktest"]["deployer_blacklist_n"], 247)
        self.assertEqual(x["conservative_644_pool_rebacktest"]["skeleton_blacklist_n"], 33)
        self.assertEqual(x["aggressive_644_pool_rebacktest"]["reported_rug_entries"], 207)
        self.assertTrue(all(v["same_estimand"] is False for v in x.values()))

    def test_go_is_not_made(self):
        self.assertEqual(self.anchor["go_decision"], "NOT_MADE")
        self.assertFalse(self.anchor["decision_flags"]["training_allowed"])
        self.assertFalse(self.anchor["decision_flags"]["live_allowed"])
        self.assertFalse(self.anchor["decision_flags"]["broadcast_allowed"])


if __name__ == "__main__":
    unittest.main()
