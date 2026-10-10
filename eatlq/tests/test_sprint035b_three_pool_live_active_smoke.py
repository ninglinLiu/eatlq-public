"""Sprint035B — three-pool live-active smoke manifest contract (file-based)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_manifest.json"
SPRINT034_DEAD = {
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
    "0x8aed442e9c17746c0eb85dd22981ce8974182892",
    "0x1e98a167082332e5e89146662e34c3c99481e5e6",
}


class TestSprint035bThreePoolLiveActiveSmoke(unittest.TestCase):
    def test_manifest_contract(self) -> None:
        self.assertTrue(MANIFEST.is_file(), f"missing {MANIFEST}")
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(d.get("kind"), "three_pool_live_active_scoped_smoke_manifest")
        self.assertIs(d.get("created_before_quote"), True)
        self.assertIs(d.get("no_training"), True)
        self.assertIs(d.get("no_live"), True)
        for flag in (
            "live_active_viability_pass_required",
            "historical_entry_viability_pass_required",
            "current_quote_viability_pass_required",
        ):
            self.assertIs(d.get(flag), True)
        st = d.get("status")
        self.assertIn(st, (None, "TOO_SMALL_FOR_THREE_POOL_SMOKE"))
        rows = d.get("manifest_rows") or []
        self.assertEqual(isinstance(rows, list), True)
        self.assertEqual(int(d.get("selected_pool_count", -1)), 3)
        if st == "TOO_SMALL_FOR_THREE_POOL_SMOKE":
            self.assertEqual(len(rows), 0)
            self.assertIn("reason", d)
            self.assertLess(int(d.get("selected_rows_count", 999)), 120)
            bf = d.get("balance_feasibility_audit_only") or {}
            if "max_feasible_rows_under_top1_top2_share_caps_given_availability" in bf:
                self.assertGreaterEqual(
                    int(bf["max_feasible_rows_under_top1_top2_share_caps_given_availability"]), 0
                )
        else:
            n = int(d.get("selected_rows_count", len(rows)))
            self.assertGreaterEqual(n, 120)
            self.assertLessEqual(n, 250)
            self.assertEqual(len({r.get("pool_audit_only") for r in rows}), 3)
            ctr: dict[str, int] = {}
            for r in rows:
                pool = str(r.get("pool_audit_only") or "").lower()
                self.assertNotIn(pool, SPRINT034_DEAD)
                self.assertIn("signal_ts_ms_audit_only", r)
                ctr[pool] = ctr.get(pool, 0) + 1
            self.assertLessEqual(max(ctr.values()), 90)
            counts = sorted(ctr.values(), reverse=True)
            tot = float(n)
            self.assertLessEqual(counts[0] / tot, 0.45 + 1e-9)
            self.assertLessEqual((counts[0] + counts[1]) / tot, 0.75 + 1e-9)
            for pool in ctr:
                self.assertNotIn(pool, SPRINT034_DEAD)


if __name__ == "__main__":
    unittest.main()
