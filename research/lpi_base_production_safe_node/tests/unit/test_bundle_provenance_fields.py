from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.verify import verify_bundle_archive

ROOT = Path("/root/lpi_base_production_safe_node")


class BundleProvenanceFieldsTests(unittest.TestCase):
    def test_export_accepts_source_tables_and_query_hashes(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(ROOT / "var" / "tmp")) as td:
            staging = Path(td) / "stage"
            staging.mkdir()
            (staging / "note.txt").write_text("ok\n", encoding="utf-8")
            out = Path(td) / "prov_test_bundle.tar.gz"
            manifest = RemoteEvidenceBundleExporter(ROOT).export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="prov_test_bundle",
                source_code_commit="a" * 40,
                source_tree_hash="b" * 40,
                source_tables=["system.tables", "system.columns"],
                source_query_hashes=["deadbeef" * 8],
                quality_results={"recommended_business_probe": "NONE", "supersedes": {"x": "y"}},
                golden_results={"on_chain_golden": "NONE"},
            )
            self.assertEqual(manifest.source_tables, ["system.tables", "system.columns"])
            self.assertEqual(len(manifest.source_query_hashes), 1)
            self.assertEqual(manifest.quality_results["recommended_business_probe"], "NONE")
            verify_bundle_archive(archive_path=out, workspace_root=ROOT)
            with tarfile.open(out, "r:gz") as tar:
                raw = tar.extractfile("prov_test_bundle/manifest.json")
                assert raw is not None
                data = json.loads(raw.read().decode())
            self.assertEqual(data["source_tables"], ["system.tables", "system.columns"])
            self.assertTrue(data["source_query_hashes"])

    def test_v2_bundle_if_present(self) -> None:
        path = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z_lineage_correction_v2.tar.gz"
        if not path.is_file():
            self.skipTest("v2 bundle not built yet")
        with tarfile.open(path, "r:gz") as tar:
            members = [m.name for m in tar.getmembers() if m.name.endswith("manifest.json")]
            self.assertTrue(members)
            raw = tar.extractfile(members[0])
            assert raw is not None
            data = json.loads(raw.read().decode())
        self.assertEqual(len(data["source_query_hashes"]), 9)
        self.assertEqual(
            set(data["source_tables"]),
            {"system.processes", "system.merges", "system.columns", "system.databases", "system.tables"},
        )
        q = data["quality_results"]
        self.assertEqual(q["upstream_execution_source_state"], "UNSEALED_WORKTREE")
        self.assertEqual(q["upstream_execution_source_commit"], "UNKNOWN")
        self.assertEqual(q["recommended_business_probe"], "NONE")
        self.assertFalse(q["business_table_access"])
        self.assertIn("supersedes", q)


if __name__ == "__main__":
    unittest.main()
