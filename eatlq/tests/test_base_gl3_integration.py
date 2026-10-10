import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
BASE = D / "base_gl1_golden_truth_ledger_gl2_20260715.json"
LEDGER = D / "base_gl1_golden_truth_ledger_gl3_20260715.json"
REVIEW = D / "base_gl3_independent_review_20260715.json"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GL3IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(BASE.read_text())
        cls.ledger = json.loads(LEDGER.read_text())
        cls.review = json.loads(REVIEW.read_text())

    def test_independent_grade_is_authoritative_and_no_go(self):
        self.assertEqual(self.review["final_grade"], "IRON")
        self.assertFalse(self.review["substantive_blocker"])
        self.assertEqual(self.review["go_decision"], "NOT_MADE")
        claim = next(x for x in self.ledger["claims"] if x["id"] == "GL1-12")
        self.assertEqual(claim["grade"], self.review["final_grade"])
        self.assertIsNone(claim["open_resolution"])
        self.assertEqual(self.ledger["go_decision"], "NOT_MADE")

    def test_only_gl1_12_claim_changed(self):
        old = {x["id"]: x for x in self.base["claims"]}
        new = {x["id"]: x for x in self.ledger["claims"]}
        self.assertEqual(old.keys(), new.keys())
        for claim_id in old:
            if claim_id != "GL1-12":
                self.assertEqual(old[claim_id], new[claim_id])

    def test_grade_counts_are_exact(self):
        self.assertEqual(Counter(x["grade"] for x in self.ledger["claims"]), Counter({"PINNED": 10, "IRON": 7, "OPEN": 6}))

    def test_ledger_anchors_are_current(self):
        claim = next(x for x in self.ledger["claims"] if x["id"] == "GL1-12")
        for anchor in claim["anchors"]:
            self.assertEqual(sha(ROOT / anchor["path"]), anchor["sha256"])
        integ = self.ledger["gl3_integration"]
        self.assertEqual(integ["base_ledger_sha256"], sha(BASE))
        self.assertEqual(integ["independent_review_sha256"], sha(REVIEW))
        self.assertEqual(integ["regraded_claims"], {"GL1-12": "IRON"})

    def test_registry_and_reports_preserve_safety(self):
        registry = (ROOT / "mvp/audits/alpha_registry.md").read_text()
        self.assertIn("INDEPENDENT_REVIEW_COMPLETE_GL1_12_IRON_NO_GO", registry)
        self.assertIn("historical bounded fact only", registry)
        for path in (
            ROOT / "reports/GL1_golden_truth_ledger_GL3_20260715.md",
            ROOT / "reports/GL3_incremental_plain_language_summary_20260715.md",
        ):
            text = path.read_text()
            self.assertIn("training_allowed: false", text)
            self.assertIn("live_allowed: false", text)
            self.assertIn("self_go_allowed: false", text)


if __name__ == "__main__":
    unittest.main()
