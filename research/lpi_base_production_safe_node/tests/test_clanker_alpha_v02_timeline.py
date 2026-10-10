#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_intelligence.clanker_alpha.v02_timeline import validate_timeline


class TimelineTests(unittest.TestCase):
    def test_no_quote(self):
        st, _ = validate_timeline(
            intended_entry=100, intended_exit=200, max_entry_wait=5,
            entry_quote_block=None, exit_quote_block=None,
        )
        self.assertEqual(st, "NO_TRADE_NO_QUOTE")

    def test_entry_after_exit_invalid(self):
        # entry within max wait but after intended exit
        st, reason = validate_timeline(
            intended_entry=100, intended_exit=102, max_entry_wait=5,
            entry_quote_block=103, exit_quote_block=130,
        )
        self.assertEqual(st, "INVALID_TIMELINE")
        self.assertIn("entry_after_intended_exit", reason)

    def test_beyond_wait(self):
        st, _ = validate_timeline(
            intended_entry=100, intended_exit=200, max_entry_wait=5,
            entry_quote_block=106, exit_quote_block=200,
        )
        self.assertEqual(st, "NO_TRADE_NO_QUOTE")

    def test_ok(self):
        st, _ = validate_timeline(
            intended_entry=100, intended_exit=200, max_entry_wait=5,
            entry_quote_block=103, exit_quote_block=200,
        )
        self.assertEqual(st, "EXECUTABLE")


if __name__ == "__main__":
    unittest.main()
