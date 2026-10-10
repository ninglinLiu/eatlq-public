#!/usr/bin/env python3
"""Golden tests for Clanker v4 launch decoder."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from launchpad_intelligence.clanker_alpha.decoder import ClankerLaunchDecoder

GOLDEN = ROOT / "golden" / "clanker_alpha"


class TestClankerAlphaDecoder(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        man = GOLDEN / "MANIFEST.json"
        if not man.exists():
            raise unittest.SkipTest("goldens not fetched yet")
        cls.manifest = json.loads(man.read_text())

    def test_manifest_cases(self) -> None:
        dec = ClankerLaunchDecoder()
        for case in self.manifest["cases"]:
            raw = json.loads((GOLDEN / case["file"]).read_text())
            got = dec.decode_log(raw, allow_duplicate=True)
            self.assertEqual(got.status, case["expected_status"], case["file"])
            if got.status == "OK":
                self.assertIsNotNone(got.record)
                self.assertEqual(got.record.factory_version, "4")
                self.assertTrue(got.record.token_address.startswith("0x"))
                self.assertEqual(len(got.record.pool_id), 66)

    def test_idempotent_dedupe(self) -> None:
        success = [c for c in self.manifest["cases"] if c["expected_status"] == "OK"]
        if not success:
            self.skipTest("no success golden")
        raw = json.loads((GOLDEN / success[0]["file"]).read_text())
        dec = ClankerLaunchDecoder()
        self.assertEqual(dec.decode_log(raw).status, "OK")
        self.assertEqual(dec.decode_log(raw).status, "DUPLICATE")


if __name__ == "__main__":
    unittest.main()
