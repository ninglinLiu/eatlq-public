from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: str):
    return json.loads((ROOT / path).read_text())


def sha(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


class GL2IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = load("mvp/diagnostics/base_gl2_independent_review_20260715.json")
        cls.ledger = load("mvp/diagnostics/base_gl1_golden_truth_ledger_gl2_20260715.json")
        cls.summary = load("mvp/diagnostics/base_gl2_three_opens_20260715.json")

    def test_independent_grades_are_exact(self):
        self.assertEqual({key: value["grade"] for key, value in self.review["claims"].items()}, {
            "GL1-12": "OPEN", "GL1-18": "OPEN", "GL1-19": "OPEN"
        })
        self.assertEqual(self.summary["grades"], {"GL1-12": "OPEN", "GL1-18": "OPEN", "GL1-19": "OPEN"})

    def test_updated_gl1_claims_match_review_exactly(self):
        claims = {row["id"]: row for row in self.ledger["claims"]}
        for key in ("GL1-12", "GL1-18", "GL1-19"):
            self.assertEqual(claims[key]["grade"], self.review["claims"][key]["grade"])
            self.assertEqual(claims[key]["answer"], self.review["claims"][key]["answer"])
            self.assertEqual(claims[key]["anchors"], self.review["claims"][key]["anchors"])
            self.assertEqual(claims[key]["gl2_review_residual"], self.review["claims"][key]["residual"])

    def test_review_anchor_hashes_are_current(self):
        for claim in self.review["claims"].values():
            for anchor in claim["anchors"]:
                self.assertEqual(sha(anchor["path"]), anchor["sha256"])

    def test_no_go_and_safety_are_exact(self):
        expected = {"live_allowed": False, "self_go_allowed": False, "training_allowed": False}
        self.assertEqual(self.review["decision_flags"], expected)
        self.assertEqual(self.summary["decision_flags"], expected)
        self.assertEqual(self.ledger["gl2_integration"]["safety"], expected)
        self.assertEqual(self.review["go_decision"], "NOT_MADE")
        self.assertEqual(self.summary["go_decision"], "NOT_MADE")
        self.assertEqual(self.ledger["go_decision"], "NOT_MADE")

    def test_registry_is_review_complete_but_not_authorized(self):
        registry = (ROOT / "mvp/audits/alpha_registry.md").read_text()
        row = next(line for line in registry.splitlines() if line.startswith("| AR-GL2-THREE-OPENS |"))
        self.assertIn("INDEPENDENT_REVIEW_COMPLETE_ALL_OPEN", row)
        self.assertTrue(row.rstrip().endswith("| false | false |"))


if __name__ == "__main__":
    unittest.main()
