from __future__ import annotations

import unittest

from launchpad_intelligence.health.metadata_gate import MetadataHealthSnapshot, evaluate_metadata_health


def _safe(**kwargs) -> MetadataHealthSnapshot:
    base = dict(
        timestamp_utc="2026-07-17T00:00:00Z",
        load_1m=1.0,
        nproc=8,
        mem_available_kb=16_000_000,
        swap_used_kb=1_000_000,
        disk_free_bytes=50 * 1024**3,
        containers={"eatlq-clickhouse-1": {"state": "running", "restart_count": 0, "restart_growth": False}},
        executor_pending=0,
        clickhouse_active_queries_excl_self=0,
        clickhouse_active_merges=0,
        notes=(),
        unknown_fields=(),
    )
    base.update(kwargs)
    return MetadataHealthSnapshot(**base)  # type: ignore[arg-type]


class MetadataHealthGateTests(unittest.TestCase):
    def test_safe(self) -> None:
        d = evaluate_metadata_health(_safe())
        self.assertEqual(d.status, "SAFE_TO_PROCEED")
        self.assertEqual(d.triggers, [])

    def test_unknown_blocks(self) -> None:
        d = evaluate_metadata_health(_safe(executor_pending=None, unknown_fields=("executor_pending",)))
        self.assertIn("UNKNOWN", d.status)
        self.assertTrue(d.triggers)

    def test_merges_block(self) -> None:
        d = evaluate_metadata_health(_safe(clickhouse_active_merges=1))
        self.assertNotEqual(d.status, "SAFE_TO_PROCEED")

    def test_pending_nonzero_blocks(self) -> None:
        d = evaluate_metadata_health(_safe(executor_pending=2))
        self.assertNotEqual(d.status, "SAFE_TO_PROCEED")


if __name__ == "__main__":
    unittest.main()
