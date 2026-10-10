"""Static checks for Sprint028C text-only policy and diagnostic ranking artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

POLICY = ROOT / "mvp" / "audits" / "sprint028c_scoped_micro_alpha_policy.md"
RANKING = ROOT / "mvp" / "audits" / "sprint028c_scoped_candidate_ranking.md"
CHARTER = ROOT / "mvp" / "audits" / "sprint028c_micro_live_calibration_charter.md"
SUMMARY_JSON = ROOT / "mvp" / "diagnostics" / "sprint028c_scoped_candidate_ranking_summary.json"


class Sprint028CPolicyArtifactTests(unittest.TestCase):

    def test_files_exist(self) -> None:
        for p in (POLICY, RANKING, CHARTER, SUMMARY_JSON):
            self.assertTrue(p.is_file(), msg=f"Missing: {p}")

    def test_policy_contains_guards_and_scope(self) -> None:
        t = POLICY.read_text(encoding="utf-8")
        for needle in (
            "EATLQ research guards",
            "Author",
            "top1",
            "LOO",
            "within-pool temporal",
            "cost-adjusted",
            "Sprint028C",
            "proof of edge or alpha",
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "SPRINT028C_TEXT_ONLY_RANKING_ALLOWED=true",
            "SPRINT028C_LABELING_ALLOWED=false",
        ):
            self.assertIn(needle, t, msg=f"policy missing: {needle}")

    def test_ranking_disclaims_alpha(self) -> None:
        t = RANKING.read_text(encoding="utf-8")
        for needle in (
            "Diagnostic candidate ranking only",
            "**not** an alpha score",
            "**not** a predictive score",
            "trade signal",
            "mvp/diagnostics/sprint028b_v3_universe_sidecar_sample.csv",
            "3524f285ec967d1b0671d9b22f37db5a8b769aea",
        ):
            self.assertIn(needle, t, msg=f"ranking md missing: {needle}")

    def test_summary_json_shape(self) -> None:
        data = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "scoped_candidate_ranking_diagnostic_summary")
        self.assertIn("candidates", data)
        cands = data["candidates"]
        self.assertGreaterEqual(len(cands), 1)
        for c in cands:
            self.assertIn("rank", c)
            self.assertIn("pool_address_audit_key", c)
            self.assertIn("row_count", c)
            self.assertEqual(c.get("disclaimer"), "diagnostic_only_not_alpha_not_predictive")

    def test_charter_forbids_execution(self) -> None:
        t = CHARTER.read_text(encoding="utf-8")
        self.assertIn("LIVE_ALLOWED", t)
        self.assertIn("private key", t.lower())


if __name__ == "__main__":
    unittest.main()
