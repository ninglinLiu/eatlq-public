from __future__ import annotations

import unittest

from launchpad_intelligence.errors import PreflightError
from launchpad_intelligence.query.preflight import BoundedQueryPlan, build_preflight


class PreflightHardeningTests(unittest.TestCase):
    def _ok(self, **kwargs):
        base = dict(
            candidate_table="logical.base_logs_candidate",
            explicit_columns=("block_number", "transaction_hash"),
            partition_key="block_number",
            start_block=1,
            end_block=10,
            limit=10,
            dry_run=True,
            explain_only=True,
        )
        base.update(kwargs)
        return BoundedQueryPlan(**base)

    def test_rejects_malicious_candidate_table(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(candidate_table="logical.t WHERE 1=1 --").validate()

    def test_rejects_malicious_column(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(explicit_columns=("block_number; DROP TABLE x",)).validate()

    def test_rejects_span_over_1000(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(start_block=1, end_block=1001).validate()

    def test_rejects_limit_over_1000(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(limit=1001).validate()

    def test_rejects_zero_and_negative_timeout(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(statement_timeout_sec=0).validate()
        with self.assertRaises(PreflightError):
            self._ok(statement_timeout_sec=-1).validate()

    def test_rejects_zero_bytes(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(max_bytes_to_read=0).validate()

    def test_rejects_bytes_over_cap(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(max_bytes_to_read=8_388_609).validate()

    def test_estimate_marked_heuristic(self) -> None:
        report = build_preflight(self._ok())
        self.assertEqual(report.estimated_read_bytes_kind, "offline_unverified_heuristic")
        self.assertFalse(report.execution_authorized)
        self.assertIn("identifier_whitelist_syntax", report.offline_enforced_constraints)


if __name__ == "__main__":
    unittest.main()
