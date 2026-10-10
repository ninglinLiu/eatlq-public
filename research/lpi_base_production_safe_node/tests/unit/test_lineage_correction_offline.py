from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path("/root/lpi_base_production_safe_node")
EV = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z"


class LineageCorrectionOfflineTests(unittest.TestCase):
    def test_original_raw_hashes_stable_baseline(self) -> None:
        # Pin expected hashes for sealed query results (must not change in correction round).
        expected = {
            "q_app_tables.tsv": "70b921369c674f54fd06cfeb83f0ec37fd961303580296e82b4e687e6829c03c",
            "q_app_columns.tsv": "860a94d7cc04587bdff2d7d71cf7f389a56287324ca436eea76007160e9d1b4d",
            "health_merges.tsv": "17d20e6c642ea03efe0bef2d66c63fcba4f99996911293f2466d9a956dbfe640",
            "health_processes.tsv": "e36f5153dd6f55f35276b9d12cd5cb33d8fd77cffa7c8e5dba1da16f3c7b0cf4",
        }
        for name, digest in expected.items():
            path = EV / "query_results" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_superseding_provenance_if_present(self) -> None:
        path = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.superseding_provenance.json"
        if not path.is_file():
            self.skipTest("provenance not built yet")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["execution_source_state"], "UNSEALED_WORKTREE")
        self.assertEqual(data["execution_source_commit"], "UNKNOWN")
        self.assertEqual(data["code_equivalence_to_post_execution_commit"], "UNPROVEN")
        self.assertEqual(data["post_execution_sealed_commit"], "bbc1a8c8f540f8911312f6879c23c788a07c501b")
        self.assertFalse(data["business_table_access"])
        self.assertEqual(len(data["source_query_hashes"]), 9)
        self.assertEqual(
            set(data["source_tables"]),
            {"system.processes", "system.merges", "system.columns", "system.databases", "system.tables"},
        )
        # Must reference original bundle hash
        self.assertEqual(
            data["supersedes_bundle_sha256"],
            "77366faf4955f53adb85c21b81169d100a38529552a31540428be37d23cf232d",
        )


if __name__ == "__main__":
    unittest.main()
