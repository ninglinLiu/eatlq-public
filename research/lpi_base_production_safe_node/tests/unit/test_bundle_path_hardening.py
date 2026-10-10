from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.verify import verify_bundle_archive


ROOT = Path("/root/lpi_base_production_safe_node")


class BundlePathHardeningTests(unittest.TestCase):
    def _stage(self, tmp: Path) -> tuple[Path, Path]:
        staging = tmp / "stage"
        staging.mkdir()
        (staging / "reports").mkdir()
        (staging / "reports" / "note.txt").write_text("ok\n", encoding="utf-8")
        out = tmp / "bundle.tar.gz"
        return staging, out

    def test_rejects_symlink_archive_input_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            staging, out = self._stage(tmp_p)
            exporter = RemoteEvidenceBundleExporter(ROOT)
            exporter.export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="b1",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
            )
            outside = tmp_p / "outside_real.tar.gz"
            outside.write_bytes(out.read_bytes())
            marker = tmp_p / "outside_marker.txt"
            marker.write_text("untouched\n", encoding="utf-8")
            link = tmp_p / "archive_link.tar.gz"
            link.symlink_to(outside)
            with self.assertRaises(ContractError):
                verify_bundle_archive(archive_path=link, workspace_root=ROOT, expected_bundle_id="b1")
            self.assertEqual(marker.read_text(encoding="utf-8"), "untouched\n")
            self.assertEqual(outside.read_bytes(), out.read_bytes())

    def test_rejects_preexisting_sidecar_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            staging, out = self._stage(tmp_p)
            outside = tmp_p / "escape_target.txt"
            outside.write_text("secret-outside\n", encoding="utf-8")
            sidecar = Path(str(out) + ".sha256")
            sidecar.symlink_to(outside)
            exporter = RemoteEvidenceBundleExporter(ROOT)
            with self.assertRaises(ContractError):
                exporter.export(
                    staging_dir=staging,
                    output_tar=out,
                    bundle_id="b2",
                    source_code_commit="0" * 40,
                    source_tree_hash="1" * 40,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret-outside\n")

    def test_requires_both_sidecars_and_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            staging, out = self._stage(tmp_p)
            exporter = RemoteEvidenceBundleExporter(ROOT)
            exporter.export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="b3",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
            )
            sha = Path(str(out) + ".sha256")
            msha = Path(str(out) + ".manifest.sha256")
            msha.unlink()
            with self.assertRaises(ContractError):
                verify_bundle_archive(archive_path=out, workspace_root=ROOT, expected_bundle_id="b3")
            # Fresh export path for tamper case (overwrite refused by design).
            staging2 = tmp_p / "stage2"
            staging2.mkdir()
            (staging2 / "note.txt").write_text("ok\n", encoding="utf-8")
            out2 = tmp_p / "b3b.tar.gz"
            exporter.export(
                staging_dir=staging2,
                output_tar=out2,
                bundle_id="b3b",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
            )
            Path(str(out2) + ".sha256").write_text(f"{'0'*64}  {out2.name}\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                verify_bundle_archive(archive_path=out2, workspace_root=ROOT, expected_bundle_id="b3b")

    def test_rejects_missing_archive_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            staging, out = self._stage(tmp_p)
            exporter = RemoteEvidenceBundleExporter(ROOT)
            exporter.export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="b4",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
            )
            Path(str(out) + ".sha256").unlink()
            with self.assertRaises(ContractError):
                verify_bundle_archive(archive_path=out, workspace_root=ROOT, expected_bundle_id="b4")


if __name__ == "__main__":
    unittest.main()
