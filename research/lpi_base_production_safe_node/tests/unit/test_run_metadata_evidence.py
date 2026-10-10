from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.evidence.run_metadata import write_run_metadata


ROOT = Path("/root/lpi_base_production_safe_node")


class RunMetadataEvidenceTests(unittest.TestCase):
    def test_new_dir_classified_without_generator_code_change(self) -> None:
        """A fresh timestamped dir with run_metadata is classified via metadata, not hardcoded stamps."""
        import importlib.util
        import sys

        path = ROOT / "scripts" / "rebuild_evidence_register.py"
        spec = importlib.util.spec_from_file_location("rebuild_evidence_register", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        raw = ROOT / "reports" / "raw"
        # Use a unique name under var/tmp then we only unit-test classify() helper with a fake path
        # by writing into a temp dir that mimics RAW layout via direct classify call.
        with tempfile.TemporaryDirectory(dir=str(ROOT / "var" / "tmp")) as td:
            d = Path(td) / "offline_tests_synthetic_metadata_only"
            d.mkdir()
            write_run_metadata(
                d,
                {
                    "round_id": "synthetic_round_for_test",
                    "artifact_class": "SYNTHETIC_TEST_ARTIFACT",
                    "source_commit": "0" * 40,
                    "created_at": "2026-07-17T00:00:00Z",
                },
            )
            f = d / "combined.txt"
            f.write_text("ok\n", encoding="utf-8")
            # Monkeypatch: classify uses _read_run_metadata_for which walks parents — works for any path
            classification, retention, source = mod.classify(str(f), f)
            self.assertEqual(source, "synthetic_round_for_test")
            self.assertEqual(classification, "SYNTHETIC_TEST_ARTIFACT")
            self.assertEqual(retention, "seal_in_git_permanent")

    def test_legacy_final_closeout_mapping(self) -> None:
        import importlib.util
        import sys

        path = ROOT / "scripts" / "rebuild_evidence_register.py"
        spec = importlib.util.spec_from_file_location("rebuild_evidence_register2", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        sample = ROOT / "reports/raw/offline_tests_20260717T135359Z/combined.txt"
        if sample.is_file():
            _c, _r, source = mod.classify(str(sample.relative_to(ROOT)), sample)
            self.assertEqual(source, "final_offline_closeout_test")


if __name__ == "__main__":
    unittest.main()
