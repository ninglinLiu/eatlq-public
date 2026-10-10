"""FIFO aggregation sanity on synthetic closed trades."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
sys.path.insert(0, str(ROOT))

from src.tape import wallet_fifo


class FifoTests(unittest.TestCase):
    def test_wallet_fifo_sign_and_top1(self):
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        closed = [
            {"wallet": "0xaaa", "token": "0x1", "pnl_weth": 2.0, "exit_time": t0},
            {"wallet": "0xaaa", "token": "0x2", "pnl_weth": -0.5, "exit_time": t0},
            {"wallet": "0xbbb", "token": "0x1", "pnl_weth": -1.0, "exit_time": t0},
        ]
        out = wallet_fifo(closed)
        self.assertEqual(out["0xaaa"]["n_closed"], 2)
        self.assertAlmostEqual(out["0xaaa"]["pnl_weth"], 1.5)
        self.assertEqual(out["0xaaa"]["sign"], 1)
        self.assertEqual(out["0xbbb"]["sign"], -1)
        self.assertAlmostEqual(out["0xaaa"]["top1_pnl_share"], 1.0)


if __name__ == "__main__":
    unittest.main()
