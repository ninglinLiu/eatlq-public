"""Sprint035A — live-active mining summary + viability contract (non-flaky, file-based)."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "mvp/diagnostics/sprint035a_live_active_mining_summary.json"
VIABILITY = REPO / "mvp/diagnostics/sprint035a_live_active_viability_audit.csv"

SPRINT034_DEAD = {
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
    "0x8aed442e9c17746c0eb85dd22981ce8974182892",
    "0x1e98a167082332e5e89146662e34c3c99481e5e6",
}


class TestSprint035aLiveMining(unittest.TestCase):
    def test_summary_schema(self) -> None:
        self.assertTrue(SUMMARY.is_file(), f"missing {SUMMARY}")
        d = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(d.get("kind"), "sprint035a_live_active_mining_summary")
        gates = d.get("live_activity_gates") or {}
        for k in (
            "min_swaps_24h_ch_no_zero_activity",
            "min_recent_5m_buckets_24h_ch",
            "min_swaps_60d_ch_historical_viability_anchor",
            "volume_usd_floor_policy",
            "liquidity_usd_floor_policy",
        ):
            self.assertIn(k, gates)
        self.assertEqual(set(d.get("sprint034_dead_pools_excluded") or []), SPRINT034_DEAD)

        finals = (
            "NO_POOLS_PASSED_LIVE_GATES",
            "LIVE_COHORT_FOUND_NO_RPC_VIABILITY_SKIPPED",
            "NO_VIABILITY_PASS_POOLS",
            "VIABILITY_PASS_BUT_HARD_QUOTA_MANIFEST_NOT_FOUND",
            "PENDING_SMOKE_EXECUTION",
            "BLOCKED_CLICKHOUSE_UNAVAILABLE",
        )
        self.assertIn(d.get("final_decision_status"), finals)

        for p in d.get("viability_pass_pool_addresses") or []:
            self.assertNotIn(str(p).lower(), SPRINT034_DEAD)

        if d.get("hard_quota_manifest_built"):
            self.assertEqual(d.get("final_decision_status"), "PENDING_SMOKE_EXECUTION")

    def test_viability_csv_pass_rows_exclude_sprint034(self) -> None:
        self.assertTrue(VIABILITY.is_file())
        with VIABILITY.open(newline="", encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                if (row.get("viability_status") or "").strip().upper() != "PASS":
                    continue
                pool = (row.get("pool_address_audit_key") or "").strip().lower()
                self.assertTrue(pool.startswith("0x"))
                self.assertNotIn(pool, SPRINT034_DEAD)


if __name__ == "__main__":
    unittest.main()
