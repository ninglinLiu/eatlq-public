from __future__ import annotations

import unittest

from launchpad_intelligence.sources.base_source import QueryBounds


class QueryBoundsIdentifierTests(unittest.TestCase):
    def test_rejects_malicious_columns(self) -> None:
        cases = (
            ("block_number; DROP TABLE x",),
            ("x --",),
            ("count(*)",),
            ("a,b",),
            ("",),
        )
        for cols in cases:
            with self.subTest(cols=cols):
                with self.assertRaises(ValueError):
                    QueryBounds(1, 2, 1, cols).validate()

    def test_accepts_safe_column(self) -> None:
        QueryBounds(1, 2, 1, ("block_number", "transaction_hash")).validate()


if __name__ == "__main__":
    unittest.main()
