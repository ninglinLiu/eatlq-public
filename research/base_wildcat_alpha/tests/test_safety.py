from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety import LiveTradingForbidden, require_no_live_lock, validate_readonly_query


class SafetyTests(unittest.TestCase):
    def test_lock_is_mandatory_and_valid(self):
        self.assertTrue(require_no_live_lock().is_file())

    def test_select_is_allowed(self):
        self.assertEqual(validate_readonly_query("SELECT 1;"), "SELECT 1")

    def test_mutations_are_rejected(self):
        for sql in (
            "INSERT INTO x VALUES (1)",
            "SELECT 1; DROP TABLE x",
            "WITH 1 AS x DELETE FROM y WHERE x=1",
            "SELECT * FROM url('https://example.com')",
        ):
            with self.subTest(sql=sql), self.assertRaises(LiveTradingForbidden):
                validate_readonly_query(sql)


if __name__ == "__main__":
    unittest.main()
