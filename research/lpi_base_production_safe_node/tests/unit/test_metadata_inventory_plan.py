from __future__ import annotations

import unittest

from launchpad_intelligence.errors import PreflightError
from launchpad_intelligence.query.metadata_inventory import (
    MetadataInventoryPlan,
    assert_sql_matches_plan,
    build_default_schema_inventory_plans,
    require_execution_authorized,
    validate_metadata_sql,
)


class MetadataInventoryPlanTests(unittest.TestCase):
    def _ok(self, **kwargs) -> MetadataInventoryPlan:
        base = dict(
            purpose="test",
            system_table="system.tables",
            explicit_columns=("database", "name", "engine"),
            database_filter="eatlq",
            limit=10,
            execution_authorized=False,
            query_id="q_test",
        )
        base.update(kwargs)
        return MetadataInventoryPlan(**base)  # type: ignore[arg-type]

    def test_default_plans_validate_and_hash_stable(self) -> None:
        plans = build_default_schema_inventory_plans(execution_authorized=False)
        hashes = [p.query_hash() for p in plans]
        plans2 = build_default_schema_inventory_plans(execution_authorized=False)
        self.assertEqual(hashes, [p.query_hash() for p in plans2])
        for p in plans:
            self.assertFalse(p.execution_authorized)
            validate_metadata_sql(p.render_sql(), expected_table=p.system_table)

    def test_reject_non_system_table(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(system_table="eatlq.logs").validate()

    def test_reject_select_star(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(explicit_columns=("*",)).validate()

    def test_reject_missing_database_filter(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(database_filter=None).validate()

    def test_reject_limit_over(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(limit=5001).validate()

    def test_reject_timeout_over(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(max_execution_time_sec=4).validate()

    def test_reject_max_threads(self) -> None:
        with self.assertRaises(PreflightError):
            self._ok(max_threads=2).validate()

    def test_reject_query_log_and_parts(self) -> None:
        with self.assertRaises(PreflightError):
            MetadataInventoryPlan(
                purpose="x",
                system_table="system.query_log",
                explicit_columns=("query",),
                database_filter=None,
            ).validate()
        with self.assertRaises(PreflightError):
            MetadataInventoryPlan(
                purpose="x",
                system_table="system.parts",
                explicit_columns=("name",),
                database_filter=None,
            ).validate()

    def test_reject_join_union_semicolon_comment_in_sql(self) -> None:
        with self.assertRaises(PreflightError):
            validate_metadata_sql(
                "SELECT database FROM system.tables WHERE database = 'eatlq' JOIN x LIMIT 1",
                expected_table="system.tables",
            )
        with self.assertRaises(PreflightError):
            validate_metadata_sql(
                "SELECT database FROM system.tables WHERE database = 'eatlq' LIMIT 1; DROP TABLE t",
                expected_table="system.tables",
            )
        with self.assertRaises(PreflightError):
            validate_metadata_sql(
                "SELECT database FROM system.tables WHERE database = 'eatlq' --x LIMIT 1",
                expected_table="system.tables",
            )

    def test_reject_business_table_from(self) -> None:
        with self.assertRaises(PreflightError):
            validate_metadata_sql(
                "SELECT id FROM eatlq.swaps LIMIT 1",
                expected_table="system.tables",
            )

    def test_execute_unauthorized(self) -> None:
        p = self._ok(execution_authorized=False)
        with self.assertRaises(PreflightError):
            require_execution_authorized(p)

    def test_hash_match_helper(self) -> None:
        p = self._ok()
        p.validate()
        assert_sql_matches_plan(p, p.render_sql())
        with self.assertRaises(PreflightError):
            assert_sql_matches_plan(p, p.render_sql() + " ")

    def test_health_plans_forbid_database_filter(self) -> None:
        with self.assertRaises(PreflightError):
            MetadataInventoryPlan(
                purpose="h",
                system_table="system.processes",
                explicit_columns=("query_id", "elapsed"),
                database_filter="eatlq",
                limit=10,
            ).validate()


if __name__ == "__main__":
    unittest.main()
