from __future__ import annotations

import unittest

from launchpad_intelligence.query.runner_status import (
    RoundStatus,
    RunnerExitCode,
    derive_round_status,
    historical_141325_correction,
)


class RunnerStatusTests(unittest.TestCase):
    def test_single_intro_not_complete(self) -> None:
        status, code, state = derive_round_status(
            pre_health_status="SAFE_TO_PROCEED",
            schema_planned=5,
            schema_succeeded=1,
            schema_complete=False,
            post_collected=False,
        )
        self.assertEqual(status, RoundStatus.PARTIAL_SCHEMA_INVENTORY)
        self.assertEqual(code, RunnerExitCode.SCHEMA_INVENTORY_INCOMPLETE)
        self.assertTrue(state.next_stage_blocked)

    def test_schema_fail(self) -> None:
        status, code, _ = derive_round_status(
            pre_health_status="SAFE_TO_PROCEED",
            schema_planned=5,
            schema_succeeded=0,
            schema_complete=False,
        )
        self.assertEqual(status, RoundStatus.FAILED_SCHEMA_QUERY)
        self.assertEqual(code, RunnerExitCode.SCHEMA_QUERY_FAILED)

    def test_post_merge_blocked_exit_30(self) -> None:
        status, code, state = derive_round_status(
            pre_health_status="SAFE_TO_PROCEED",
            schema_planned=5,
            schema_succeeded=5,
            schema_complete=True,
            post_health_status="BLOCKED_LIVE_PRIORITY",
            post_collected=True,
        )
        self.assertEqual(status, RoundStatus.SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED)
        self.assertEqual(code, RunnerExitCode.BLOCKED_POST_HEALTH)
        self.assertEqual(int(code), 30)
        self.assertTrue(state.next_stage_blocked)

    def test_pre_load_blocked_exit_10(self) -> None:
        status, code, _ = derive_round_status(pre_health_status="BLOCKED_LIVE_PRIORITY")
        self.assertEqual(status, RoundStatus.BLOCKED_PRE_HEALTH)
        self.assertEqual(code, RunnerExitCode.BLOCKED_PRE_HEALTH)

    def test_unknown_not_plain_live_priority(self) -> None:
        status, code, _ = derive_round_status(pre_health_status="BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY")
        self.assertEqual(status, RoundStatus.BLOCKED_PRE_UNKNOWN)
        self.assertEqual(code, RunnerExitCode.BLOCKED_PRE_UNKNOWN)

    def test_historical_correction(self) -> None:
        hist = historical_141325_correction()
        self.assertTrue(hist["schema_inventory_complete"])
        self.assertFalse(hist["post_health_safe"])
        self.assertEqual(hist["historical_expected_exit_semantics"], 30)
        self.assertEqual(hist["actual_historical_exit_semantics"], 0)
        self.assertTrue(hist["historical_exit_code_was_misleading"])


if __name__ == "__main__":
    unittest.main()
