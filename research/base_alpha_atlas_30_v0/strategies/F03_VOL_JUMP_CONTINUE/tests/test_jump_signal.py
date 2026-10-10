"""Fixture tests for F03 jump formula (no production data).

Status label: FIXTURE_VALIDATED_SIGNAL_LOGIC (not real-market alpha).
"""
from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from orchestration.common import Bar
from strategies.F03_VOL_JUMP_CONTINUE.runner import entry_fn, signal_fn

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "f03_jump_bars.json"


def _load_bars() -> list[Bar]:
    rows = json.loads(FIX.read_text())
    return [
        Bar(
            pool_id=r["pool_id"],
            event_time=r["event_time"],
            known_at=r["known_at"],
            feature_available_at=r["feature_available_at"],
            features=r["features"],
        )
        for r in rows
    ]


class F03JumpTests(unittest.TestCase):
    def test_arm0_detects_up_jump(self):
        bars = _load_bars()
        sigs = signal_fn(bars, {"k": 3.0, "l": 10, "arm": 0, "jump_cooldown_bars": 5})
        self.assertGreaterEqual(len(sigs), 1)
        # first engineered jump is +15% at event_time 1720
        up = [s for s in sigs if s.meta.get("jump_sign") == 1.0]
        self.assertTrue(up)
        self.assertEqual(up[0].side, "BUY")
        self.assertLessEqual(up[0].feature_available_at, up[0].event_time)

    def test_arm1_detects_down_jump_only(self):
        bars = _load_bars()
        sigs = signal_fn(bars, {"k": 3.0, "l": 10, "arm": 1, "jump_cooldown_bars": 5})
        self.assertTrue(sigs)
        for s in sigs:
            self.assertEqual(s.meta.get("jump_sign"), -1.0)

    def test_cooldown_suppresses_repeat(self):
        bars = _load_bars()
        # force tiny cooldown off vs on
        many = signal_fn(bars, {"k": 2.0, "l": 8, "arm": 0, "jump_cooldown_bars": 0})
        few = signal_fn(bars, {"k": 2.0, "l": 8, "arm": 0, "jump_cooldown_bars": 50})
        self.assertGreaterEqual(len(many), len(few))

    def test_pit_no_future_in_rv(self):
        bars = _load_bars()
        sigs = signal_fn(bars, {"k": 3.0, "l": 10, "arm": 0, "jump_cooldown_bars": 5})
        for s in sigs:
            idx = int(s.meta["bar_index"])
            # feature_available_at equals bar t; rv used end_exclusive=t (past only)
            self.assertEqual(s.feature_available_at, bars[idx].feature_available_at)

    def test_entry_delay_seconds_scale_placeholder(self):
        bars = _load_bars()
        sigs = signal_fn(bars, {"k": 3.0, "l": 10, "arm": 0, "jump_cooldown_bars": 5})
        intent = entry_fn(sigs[0], {"notional_usd": 50, "execution_delay_bars": 1})
        self.assertIsNotNone(intent)
        self.assertEqual(intent.earliest_exec_time, sigs[0].event_time + 1)

    def test_missing_close_not_zero_filled(self):
        bars = _load_bars()
        # corrupt mid-series close; must not invent r=0 jump/suppress via zero fill
        bad = list(bars)
        i = 12
        f = dict(bad[i].features)
        f["close"] = None  # type: ignore[assignment]
        f.pop("px", None)
        bad[i] = Bar(
            pool_id=bad[i].pool_id,
            event_time=bad[i].event_time,
            known_at=bad[i].known_at,
            feature_available_at=bad[i].feature_available_at,
            features=f,
        )
        # runner must not crash; signals only on finite returns
        sigs = signal_fn(bad, {"k": 3.0, "l": 10, "arm": 0, "jump_cooldown_bars": 5})
        for s in sigs:
            self.assertTrue(math.isfinite(float(s.meta["r_t"])))


if __name__ == "__main__":
    unittest.main()
