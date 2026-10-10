"""Small offline fixture tests for Phase 0.5 (unittest, no pytest required)."""
from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

from orchestration.common import ACCESS_FLAGS, Bar, assert_offline_safe
from orchestration.strategies_catalog import FAMILIES, STRATEGIES, by_id

ROOT = Path(__file__).resolve().parents[1]


class Phase05CatalogTests(unittest.TestCase):
    def test_thirty_strategies_ten_families(self):
        self.assertEqual(len(STRATEGIES), 30)
        self.assertEqual(len(FAMILIES), 10)
        for fam in FAMILIES:
            self.assertEqual(sum(1 for s in STRATEGIES if s["family"] == fam), 3)

    def test_param_caps_and_ids_unique(self):
        ids = [s["strategy_id"] for s in STRATEGIES]
        self.assertEqual(len(ids), len(set(ids)))
        for s in STRATEGIES:
            self.assertLessEqual(s["param_combo_count"], 512)
            self.assertLessEqual(len(s["parameter_search_space"]), 5)

    def test_access_flags_safe(self):
        assert_offline_safe()
        self.assertFalse(ACCESS_FLAGS["database_access_enabled"])
        self.assertFalse(ACCESS_FLAGS["backtest_enabled"])

    def test_wave_file_constraints(self):
        waves = json.loads((ROOT / "orchestration" / "wave_priority.json").read_text())
        self.assertLessEqual(len(waves["wave1"]), 6)
        self.assertLessEqual(len(waves["wave2"]), 9)
        idmap = by_id()
        for sid in waves["wave1"]:
            self.assertNotEqual(idmap[sid]["data_class"], "C_PRODUCTION_DB_HEAVY")
            self.assertNotEqual(idmap[sid]["data_class"], "D_DATA_UNAVAILABLE")
            self.assertLess(idmap[sid]["scores"]["live_conflict_risk"], 5)

    def test_each_strategy_scaffold_importable(self):
        for s in STRATEGIES:
            mod = importlib.import_module(f"strategies.{s['strategy_id']}.runner")
            out = mod.run_scaffold([])
            self.assertTrue(out.get("n_bars", out.get("n_swaps", 0)) == 0)
            # Real formula runners (not synthetic trigger scaffolds)
            if s["strategy_id"] in {"F03_VOL_JUMP_CONTINUE", "F01_OFS_SIGN_RUNLEN"}:
                continue
            bars = [
                Bar(
                    pool_id="0xpool",
                    event_time=100,
                    known_at=100,
                    feature_available_at=100,
                    features={"trigger": 1.0, "strength": 0.8},
                )
            ]
            sigs = mod.signal_fn(bars, {})
            self.assertEqual(len(sigs), 1)


if __name__ == "__main__":
    unittest.main()
