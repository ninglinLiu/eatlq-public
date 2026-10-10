from __future__ import annotations

import unittest

from launchpad_intelligence.config import load_access_defaults
from launchpad_intelligence.errors import AccessDeniedError
from launchpad_intelligence.golden.validator import GoldenApplicability, GoldenValidator
from launchpad_intelligence.lifecycle.machine import LifecycleEvent, LifecycleMachine, LifecycleState
from launchpad_intelligence.nodes.base.adapters.clickhouse_readonly import ClickHouseReadonlyAdapter
from launchpad_intelligence.nodes.base.adapters.null_source import NullBaseSource
from launchpad_intelligence.schema.unknown import UNKNOWN
from launchpad_intelligence.sources.base_source import QueryBounds


class SourceLifecycleGoldenTests(unittest.TestCase):
    def test_access_defaults_fail_closed(self) -> None:
        access = load_access_defaults()
        self.assertFalse(access.database_access_enabled)
        self.assertFalse(access.rpc_access_enabled)
        self.assertFalse(access.http_access_enabled)
        self.assertFalse(access.backfill_enabled)
        with self.assertRaises(AccessDeniedError):
            access.require_database_access()

    def test_null_source_blocks_iteration(self) -> None:
        src = NullBaseSource()
        bounds = QueryBounds(
            start_block=1,
            end_block=10,
            limit=10,
            explicit_columns=("block_number", "transaction_hash"),
        )
        with self.assertRaises(AccessDeniedError):
            list(src.iter_events(bounds))

    def test_clickhouse_adapter_explain_no_endpoint(self) -> None:
        adapter = ClickHouseReadonlyAdapter()
        caps = adapter.capabilities()
        self.assertTrue(caps.contract_supports_logs)
        self.assertFalse(caps.runtime_available_logs)
        self.assertFalse(caps.runtime_transport_implemented)
        bounds = QueryBounds(
            start_block=100,
            end_block=200,
            limit=50,
            explicit_columns=("block_number",),
            dry_run=True,
            explain_only=True,
        )
        plan = adapter.explain(bounds)
        self.assertFalse(plan["execution_authorized"])
        self.assertFalse(plan["runtime_available"]["transport_implemented"])
        self.assertFalse(plan["endpoint_configured"])
        with self.assertRaises(AccessDeniedError):
            list(adapter.iter_logs(bounds))

    def test_golden_is_synthetic_contract_only(self) -> None:
        # Explicit non-claim: these cases are synthetic contract fixtures, not on-chain Golden.
        validator = GoldenValidator()
        self.assertTrue(all(c.case_id.startswith("clanker.") for c in validator.cases))

    def test_lifecycle_preserves_unknown_transition(self) -> None:
        machine = LifecycleMachine()
        machine.apply(LifecycleEvent("TOKEN_CREATED", "0xa", 0, 1))
        self.assertEqual(machine.state, LifecycleState.CREATED)
        machine.apply(LifecycleEvent("WEIRD_EVENT", "0xb", 1, 2))
        self.assertEqual(machine.state, LifecycleState.CREATED)
        self.assertGreaterEqual(machine.unknown_count, 1)

    def test_golden_unknown_and_na(self) -> None:
        validator = GoldenValidator()
        results = validator.validate_all(
            {
                "clanker.creation": {"token": "0xt", "factory": "0xf", "tx": "0x1"},
                "clanker.normal_buy": {"token": "0xt", "trader": UNKNOWN, "pool": "0xp"},
            }
        )
        by_id = {r.case_id: r for r in results}
        self.assertEqual(by_id["clanker.creation"].verdict.value, "PASS")
        self.assertEqual(by_id["clanker.normal_buy"].verdict.value, "UNKNOWN")
        self.assertEqual(by_id["clanker.bonding_curve"].verdict.value, "UNKNOWN")
        self.assertEqual(
            by_id["clanker.bonding_curve"].detail,
            "NOT_APPLICABLE pending verified evidence",
        )
        # Applicability enum exists
        self.assertEqual(GoldenApplicability.REQUIRED.value, "REQUIRED")


if __name__ == "__main__":
    unittest.main()
