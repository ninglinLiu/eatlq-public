from __future__ import annotations

import unittest

from launchpad_intelligence.health.guard import GuardDecision, HealthGuard, HealthSnapshot


class HealthGuardTests(unittest.TestCase):
    def test_safe_when_stable(self) -> None:
        before = HealthSnapshot(
            timestamp_utc="2026-07-17T00:00:00Z",
            executor_pending=0,
            executor_idle_ms=500,
            container_restart_counts={"eatlq-executor-1": 0},
            load_1m=7.0,
            mem_available_kb=10_000_000,
            swap_used_kb=2_000_000,
            disk_free_bytes=50_000_000_000,
            clickhouse_active_queries=0,
        )
        after = HealthSnapshot(
            timestamp_utc="2026-07-17T00:05:00Z",
            executor_pending=0,
            executor_idle_ms=600,
            container_restart_counts={"eatlq-executor-1": 0},
            load_1m=7.2,
            mem_available_kb=9_900_000,
            swap_used_kb=2_000_100,
            disk_free_bytes=50_000_000_000,
            clickhouse_active_queries=0,
        )
        decision = HealthGuard().evaluate(before, after)
        self.assertEqual(decision, GuardDecision.SAFE_TO_PROCEED)

    def test_pause_on_restart_growth(self) -> None:
        before = HealthSnapshot(
            timestamp_utc="t0",
            executor_pending=0,
            executor_idle_ms=1,
            container_restart_counts={"eatlq-executor-1": 0},
            load_1m=1.0,
            mem_available_kb=10_000_000,
            swap_used_kb=1000,
            disk_free_bytes=50_000_000_000,
        )
        after = HealthSnapshot(
            timestamp_utc="t1",
            executor_pending=0,
            executor_idle_ms=1,
            container_restart_counts={"eatlq-executor-1": 1},
            load_1m=1.0,
            mem_available_kb=10_000_000,
            swap_used_kb=1000,
            disk_free_bytes=50_000_000_000,
        )
        self.assertEqual(HealthGuard().evaluate(before, after), GuardDecision.PAUSED_LIVE_PRIORITY)


if __name__ == "__main__":
    unittest.main()
