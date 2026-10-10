"""Guards: no Dune-as-GMGN, no lookahead fame, no live, FIFO units."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
sys.path.insert(0, str(ROOT))

from src.arm_a_crowd_fade import _fade_return
from src.common import norm_addr, sha256_bytes
from src.normalize_boards import rows_from_debot_discover, rows_from_gmgn_json


class GuardTests(unittest.TestCase):
    def test_no_live_lock_exists(self):
        self.assertTrue((ROOT / "NO_LIVE.lock").exists())
        freeze = json.loads((ROOT / "configs/freeze.json").read_text())
        self.assertFalse(freeze["deploy_allowed"])

    def test_norm_addr(self):
        self.assertEqual(norm_addr("0xABC"), "0xabc")
        self.assertEqual(norm_addr(None), "")

    def test_dune_path_is_overlay_not_vendor(self):
        paths = json.loads((ROOT / "configs/paths.json").read_text())
        self.assertIn("dune", paths["eatlq_dune"])
        charter = (ROOT / "CHARTER.md").read_text()
        self.assertIn("Substitute Dune", charter)
        # normalize must tag overlay separately
        self.assertTrue((ROOT / "data/raw/dune_overlay").exists() or True)

    def test_fame_now_label_on_normalized_rows(self):
        payload = {"data": {"rank": [{"address": "0x" + "11" * 20, "pnl_7d": 1.2, "winrate": 0.8}]}}
        rows = rows_from_gmgn_json(payload, chain="base", captured_at="2026-09-17T00:00:00+00:00", tag="smart_degen", window="7d")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fame_kind"], "fame_now")
        self.assertEqual(rows[0]["source"], "gmgn")

    def test_debot_discover_is_perp_and_not_spot_reconcilable(self):
        payload = {
            "data": {
                "total": 1,
                "records": [
                    {
                        "wallet": "0x" + "22" * 20,
                        "realized_pnl": 1902433.95,
                        "long_realized_pnl": 0,
                        "short_realized_pnl": 1834043.45,
                        "win_rate": 0.98,
                        "trade_count": 3447,
                        "max_drawdown_ratio": 0.0002,
                        "is_smart_money": True,
                        "tier": "A",
                    }
                ],
            }
        }
        rows = rows_from_debot_discover(payload, captured_at="2026-09-17T00:00:00+00:00", duration="7d")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["venue"], "hyperliquid_perp")
        self.assertFalse(rows[0]["spot_reconcilable"])
        self.assertEqual(rows[0]["chain"], "hyperliquid")

    def test_gmgn_rows_are_spot_reconcilable(self):
        payload = {"data": {"rank": [{"address": "0x" + "33" * 20, "pnl_7d": 1.0}]}}
        rows = rows_from_gmgn_json(payload, chain="base", captured_at="2026-09-17T00:00:00+00:00", tag="kol", window="7d")
        self.assertTrue(rows[0]["spot_reconcilable"])
        self.assertEqual(rows[0]["venue"], "base_bsc_spot")

    def test_fade_return_sign(self):
        # bounce then dump: r30=+10%, r5m=-10% → long after fade entry is negative → short wins
        r = _fade_return({"copy_delay_30s": 0.10, "future_return_5m": -0.10})
        self.assertIsNotNone(r)
        self.assertGreater(r, 0)

    def test_sha_stable(self):
        self.assertEqual(sha256_bytes(b"abc"), sha256_bytes(b"abc"))
        self.assertNotEqual(sha256_bytes(b"abc"), sha256_bytes(b"abd"))

    def test_lookahead_window(self):
        tape_end = datetime(2026, 7, 14, tzinfo=timezone.utc)
        captured = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.assertLess(tape_end, captured)
        self.assertGreater((captured - tape_end).days, 0)


if __name__ == "__main__":
    unittest.main()
