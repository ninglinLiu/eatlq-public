from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.verify import verify_bundle_archive

ROOT = Path("/root/lpi_base_production_safe_node")


class BundleSetEqualityTests(unittest.TestCase):
    def _export(self, tmp: Path, bundle_id: str = "safe-bundle-1") -> Path:
        staging = tmp / "stage"
        staging.mkdir()
        (staging / "reports").mkdir()
        (staging / "reports" / "note.txt").write_text("ok\n", encoding="utf-8")
        out = tmp / f"{bundle_id}.tar.gz"
        RemoteEvidenceBundleExporter(ROOT).export(
            staging_dir=staging,
            output_tar=out,
            bundle_id=bundle_id,
            source_code_commit="0" * 40,
            source_tree_hash="1" * 40,
        )
        return out

    def test_rejects_bad_bundle_id(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            staging = tmp_p / "stage"
            staging.mkdir()
            (staging / "a.txt").write_text("x\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                RemoteEvidenceBundleExporter(ROOT).export(
                    staging_dir=staging,
                    output_tar=tmp_p / "out.tar.gz",
                    bundle_id="../evil",
                    source_code_commit="0" * 40,
                    source_tree_hash="1" * 40,
                )

    def test_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            out = self._export(Path(tmp), "once-only")
            staging = Path(tmp) / "stage2"
            staging.mkdir()
            (staging / "b.txt").write_text("y\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                RemoteEvidenceBundleExporter(ROOT).export(
                    staging_dir=staging,
                    output_tar=out,
                    bundle_id="once-only-2",
                    source_code_commit="0" * 40,
                    source_tree_hash="1" * 40,
                )

    def test_rejects_extra_payload_and_bad_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var" / "tmp") as tmp:
            tmp_p = Path(tmp)
            out = self._export(tmp_p, "set-eq")
            # Rebuild a tampered archive with an extra file and rewritten manifest counts.
            bad = tmp_p / "tampered.tar.gz"
            with tarfile.open(out, "r:gz") as src, tarfile.open(bad, "w:gz") as dst:
                for m in src.getmembers():
                    if m.isfile():
                        f = src.extractfile(m)
                        assert f is not None
                        data = f.read()
                        if m.name.endswith("manifest.json"):
                            manifest = json.loads(data.decode())
                            manifest["file_count"] = 999
                            data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
                        import io

                        info = tarfile.TarInfo(name=m.name)
                        info.size = len(data)
                        dst.addfile(info, io.BytesIO(data))
                extra = b"extra\n"
                info = tarfile.TarInfo(name="set-eq/extra_unlisted.txt")
                info.size = len(extra)
                import io

                dst.addfile(info, io.BytesIO(extra))
            # Write sidecars matching tampered archive so hash gate passes into structural checks.
            digest = hashlib.sha256(bad.read_bytes()).hexdigest()
            Path(str(bad) + ".sha256").write_text(f"{digest}  {bad.name}\n", encoding="utf-8")
            with tarfile.open(bad, "r:gz") as tar:
                mf = tar.extractfile("set-eq/manifest.json")
                assert mf is not None
                mdigest = hashlib.sha256(mf.read()).hexdigest()
            Path(str(bad) + ".manifest.sha256").write_text(f"{mdigest}  manifest.json\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                verify_bundle_archive(archive_path=bad, workspace_root=ROOT, expected_bundle_id="set-eq")


if __name__ == "__main__":
    unittest.main()
