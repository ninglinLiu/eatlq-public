import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
BASE = D / "base_gl1_golden_truth_ledger_gl3_20260715.json"
LEDGER = D / "base_gl1_golden_truth_ledger_gl4_20260716.json"
TRUTH = D / "base_gl4_live_truth_ledger_20260716.json"
REVIEW = D / "base_gl4_independent_review_20260716.json"
HASHES = D / "base_gl4_artifact_hashes_20260716.json"
TEST_EVIDENCE = D / "base_gl4_test_evidence_20260716.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GL4IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(BASE.read_text())
        cls.ledger = json.loads(LEDGER.read_text())
        cls.truth = json.loads(TRUTH.read_text())
        cls.review = json.loads(REVIEW.read_text())
        cls.old = {x["id"]: x for x in cls.base["claims"]}
        cls.new = {x["id"]: x for x in cls.ledger["claims"]}

    def test_independent_review_is_authoritative_and_no_go(self):
        final = self.review["final_adjudication"]
        self.assertFalse(final["substantive_blocker"])
        self.assertEqual(final["go_decision"], "NOT_MADE")
        self.assertEqual(final["GL1_02"]["final_grade"], "PINNED")
        self.assertEqual(final["GL1_02"]["outcome"], "FAIL_AT_OBSERVED_TINY_LIVE_SCALE")
        self.assertEqual(self.new["GL1-02"]["grade"], final["GL1_02"]["final_grade"])
        self.assertEqual(self.new["GL1-02"]["answer"]["verdict"], final["GL1_02"]["outcome"])
        self.assertEqual(self.ledger["go_decision"], "NOT_MADE")

    def test_gl1_24_is_added_and_strict_realized_is_separate_from_mark(self):
        self.assertNotIn("GL1-24", self.old)
        c = self.new["GL1-24"]
        self.assertEqual(c["grade"], "PINNED")
        a = c["answer"]
        self.assertEqual(a["strict_receipt_closed_realized_pnl_weth_exact_decimal"], "-0.000011451891750166")
        self.assertIsNone(a["unsold_positions_cash_realized_pnl"])
        self.assertEqual(a["supplemental_end_mark_economic_pnl_weth_exact_decimal"], "-0.160235862229391189")
        self.assertNotEqual(a["strict_receipt_closed_realized_pnl_weth"], a["supplemental_end_mark_economic_pnl_weth"])

    def test_only_reviewed_claims_changed_and_grade_counts_are_exact(self):
        self.assertEqual(set(self.new) - set(self.old), {"GL1-24"})
        for claim_id in self.old:
            if claim_id not in {"GL1-01", "GL1-02", "GL1-12"}:
                self.assertEqual(self.old[claim_id], self.new[claim_id])
        self.assertEqual(self.new["GL1-01"]["grade"], "PINNED")
        self.assertEqual(self.new["GL1-12"]["grade"], "IRON")
        self.assertIn("not realized live", self.new["GL1-01"]["gl4_scope_enforcement"])
        self.assertIn("not a live", self.new["GL1-12"]["gl4_scope_enforcement"])
        self.assertEqual(Counter(x["grade"] for x in self.ledger["claims"]), Counter({"PINNED": 12, "IRON": 7, "OPEN": 5}))

    def test_integration_hashes_and_all_claim_anchors_match(self):
        integ = self.ledger["gl4_integration"]
        self.assertEqual(integ["base_ledger_sha256"], sha(BASE))
        self.assertEqual(integ["truth_ledger_sha256"], sha(TRUTH))
        self.assertEqual(integ["independent_review_sha256"], sha(REVIEW))
        self.assertEqual(integ["regraded_claims"], {"GL1-02": "PINNED"})
        self.assertEqual(integ["added_claims"], {"GL1-24": "PINNED"})
        for claim_id in ("GL1-02", "GL1-24"):
            for anchor in self.new[claim_id]["anchors"]:
                self.assertEqual(sha(ROOT / anchor["path"]), anchor["sha256"])

    def test_registry_and_human_ledgers_preserve_safety(self):
        registry = (ROOT / "mvp/audits/alpha_registry.md").read_text()
        self.assertIn("INDEPENDENT_REVIEW_COMPLETE_GL1_02_PINNED_FAIL_GL1_24_PINNED_NO_GO", registry)
        self.assertIn("strict realized and unsold end-mark economics must remain separate", registry)
        for path in (ROOT / "reports/GL1_golden_truth_ledger_GL4_20260716.md", ROOT / "reports/GL4_independent_line_review_20260716.md"):
            text = path.read_text()
            self.assertIn("training_allowed: false", text)
            self.assertIn("live_allowed: false", text)
            self.assertIn("self_go_allowed: false", text)

    def test_artifact_hash_ledger_matches_exactly(self):
        hashes = json.loads(HASHES.read_text())
        self.assertEqual(hashes["go_decision"], "NOT_MADE")
        self.assertEqual(hashes["reproduction_result"], "BYTE_IDENTICAL_CORE_RPC_RERUN")
        for rel, expected in hashes["artifacts"].items():
            self.assertEqual(sha(ROOT / rel), expected)

    def test_combined_regression_evidence_is_exact(self):
        evidence = json.loads(TEST_EVIDENCE.read_text())
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["tests_run"], 25)
        self.assertEqual(evidence["failures"], 0)
        self.assertEqual(evidence["errors"], 0)


if __name__ == "__main__":
    unittest.main()
