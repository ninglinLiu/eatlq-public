from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path

ROOT = Path("/root/lpi_base_production_safe_node")


def _load_rebuild():
    import sys

    path = ROOT / "scripts" / "rebuild_evidence_register.py"
    spec = importlib.util.spec_from_file_location("rebuild_evidence_register", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class EvidenceRegisterTests(unittest.TestCase):
    def test_register_matches_tsv_and_hashes(self) -> None:
        mod = _load_rebuild()
        entries = mod.collect()
        mod.write_inventory(entries)
        mod.write_register(entries)

        inv_lines = (ROOT / "evidence" / "raw_evidence_inventory.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(inv_lines[0].split("\t"), list(mod.FIELDS))
        rows = [ln.split("\t") for ln in inv_lines[1:] if ln.strip()]
        self.assertEqual(len(rows), len(entries))

        md = (ROOT / "evidence" / "RAW_EVIDENCE_REGISTER.md").read_text(encoding="utf-8")
        self.assertIn(f"entries = {len(entries)}", md)
        table_rows = [ln for ln in md.splitlines() if ln.startswith("| `")]
        self.assertEqual(len(table_rows), len(entries))

        sources = {r[5] for r in rows}
        for required in (
            "bootstrap_health_probe",
            "prior_live_capacity_observe",
            "prior_offline_test",
            "correction_round_offline_test",
            "clanker_offline_preflight",
            "final_offline_closeout_test",
        ):
            self.assertIn(required, sources)

        for rel, size, digest, *_ in rows:
            path = ROOT / rel
            self.assertTrue(path.is_file(), rel)
            self.assertFalse(path.is_symlink(), rel)
            self.assertEqual(str(path.stat().st_size), size)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertNotIn("correction_round", sources)


if __name__ == "__main__":
    unittest.main()
