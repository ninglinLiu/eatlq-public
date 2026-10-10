"""Arm helpers stay honest about permute and first_seen."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
sys.path.insert(0, str(ROOT))

from src.arm_a_crowd_fade import _famous_set
from src.arm_b_new_cohort import _is_new


class ArmTests(unittest.TestCase):
    def test_famous_requires_positive_pnl(self):
        fame = _famous_set({"0xa": 3.0, "0xb": -1.0, "0xc": 0.1}, top_n=2)
        self.assertIn("0xa", fame)
        self.assertNotIn("0xb", fame)

    def test_new_wallet_gate(self):
        self.assertTrue(_is_new({"wallet_age_days": 0.5}, 86400))
        self.assertFalse(_is_new({"wallet_age_days": 3.0}, 86400))
        self.assertFalse(_is_new({"wallet_age_days": None}, 86400))


if __name__ == "__main__":
    unittest.main()
