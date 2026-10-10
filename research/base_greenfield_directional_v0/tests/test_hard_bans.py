#!/usr/bin/env python3
import json
import sys
import unittest
from pathlib import Path

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import assert_no_banned_features, load_bans  # noqa: E402


class TestHardBans(unittest.TestCase):
    def test_ban_list_loads(self):
        bans = load_bans()
        self.assertIn("ks_", bans["banned_feature_column_prefixes"])

    def test_rejects_ks_columns(self):
        with self.assertRaises(ValueError):
            assert_no_banned_features(["early_ret", "ks_live_pass"])

    def test_rejects_f10_prefix(self):
        with self.assertRaises(ValueError):
            assert_no_banned_features(["f10_score"])

    def test_allows_greenfield_cols(self):
        assert_no_banned_features(["early_ret", "net_flow", "liq_log", "base_breadth_1h"])


if __name__ == "__main__":
    unittest.main()
