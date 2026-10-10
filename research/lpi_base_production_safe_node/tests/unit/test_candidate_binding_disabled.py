from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path("/root/lpi_base_production_safe_node")


class CandidateBindingDisabledTests(unittest.TestCase):
    def test_defaults_still_closed(self) -> None:
        defaults = json.loads((ROOT / "config" / "defaults.json").read_text(encoding="utf-8"))
        self.assertFalse(defaults.get("database_access_enabled"))
        self.assertFalse(defaults.get("business_table_query_enabled"))
        self.assertFalse(defaults.get("backfill_enabled"))

    def test_candidate_file_if_present_is_disabled(self) -> None:
        path = ROOT / "config" / "base_table_bindings.candidate.json"
        if not path.is_file():
            self.skipTest("candidate file not generated yet")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(data.get("runtime_enabled"))
        self.assertFalse(data.get("business_query_authorized"))
        self.assertIn(
            data.get("status"),
            {"CANDIDATE_UNVERIFIED_DATA", "WEAK_CANDIDATE_UNVERIFIED_DATA", "NO_SAFE_CANDIDATE"},
        )
        self.assertTrue(data.get("review_required"))


if __name__ == "__main__":
    unittest.main()
