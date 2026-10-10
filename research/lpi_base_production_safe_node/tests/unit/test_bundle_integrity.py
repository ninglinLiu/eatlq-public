from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.verify import verify_bundle_archive


class BundleIntegrityTests(unittest.TestCase):
    def test_export_verify_payload_hashes_and_detached_archive(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        with tempfile.TemporaryDirectory(dir=root / "var" / "tmp") as tmp:
            staging = Path(tmp) / "stage"
            out = Path(tmp) / "bundle.tar.gz"
            staging.mkdir()
            (staging / "reports").mkdir()
            (staging / "reports" / "note.txt").write_text("ok\n", encoding="utf-8")
            exporter = RemoteEvidenceBundleExporter(root)
            manifest = exporter.export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="base-test-bundle",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
                unknown_count=1,
                golden_results={"kind": "synthetic_contract_case"},
            )
            self.assertNotIn("manifest.json", manifest.sha256_per_file)
            result = verify_bundle_archive(
                archive_path=out, workspace_root=root, expected_bundle_id="base-test-bundle"
            )
            self.assertEqual(result["status"], "VERIFIED")

    def test_rejects_symlink_escape(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        with tempfile.TemporaryDirectory(dir=root / "var" / "tmp") as tmp:
            staging = Path(tmp) / "stage"
            out = Path(tmp) / "bundle.tar.gz"
            staging.mkdir()
            target = Path(tmp) / "outside.txt"
            target.write_text("secret\n", encoding="utf-8")
            link = staging / "escape.txt"
            link.symlink_to(target)
            exporter = RemoteEvidenceBundleExporter(root)
            with self.assertRaises(ContractError):
                exporter.export(
                    staging_dir=staging,
                    output_tar=out,
                    bundle_id="bad",
                    source_code_commit="0" * 40,
                    source_tree_hash="1" * 40,
                )


if __name__ == "__main__":
    unittest.main()
