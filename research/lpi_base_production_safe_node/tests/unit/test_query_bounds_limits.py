from __future__ import annotations

import unittest

from launchpad_intelligence.errors import AccessDeniedError
from launchpad_intelligence.nodes.base.adapters.clickhouse_readonly import ClickHouseReadonlyAdapter
from launchpad_intelligence.nodes.base.adapters.null_source import NullBaseSource
from launchpad_intelligence.sources.base_source import QueryBounds


class QueryBoundsLimitsTests(unittest.TestCase):
    def test_rejects_overspan_and_overlimit(self) -> None:
        with self.assertRaises(ValueError):
            QueryBounds(1, 1001, 10, ("block_number",)).validate()
        with self.assertRaises(ValueError):
            QueryBounds(1, 10, 1001, ("block_number",)).validate()

    def test_rejects_bad_timeout_bytes_and_flags(self) -> None:
        with self.assertRaises(ValueError):
            QueryBounds(1, 2, 1, ("block_number",), statement_timeout_sec=0).validate()
        with self.assertRaises(ValueError):
            QueryBounds(1, 2, 1, ("block_number",), max_bytes_to_read=0).validate()
        with self.assertRaises(ValueError):
            QueryBounds(1, 2, 1, ("block_number",), dry_run=False).validate()
        with self.assertRaises(ValueError):
            QueryBounds(1, 2, 1, ("block_number",), explain_only=False).validate()

    def test_adapter_explain_rejects_invalid_bounds(self) -> None:
        adapter = ClickHouseReadonlyAdapter()
        with self.assertRaises(ValueError):
            adapter.explain(QueryBounds(1, 2000, 10, ("block_number",)))

    def test_adapter_iter_rejects_invalid_bounds_before_access(self) -> None:
        src = NullBaseSource()
        with self.assertRaises(ValueError):
            list(src.iter_events(QueryBounds(1, 2, 0, ("block_number",))))
        # valid bounds still fail closed on transport
        with self.assertRaises(AccessDeniedError):
            list(src.iter_events(QueryBounds(1, 2, 1, ("block_number",))))


if __name__ == "__main__":
    unittest.main()
