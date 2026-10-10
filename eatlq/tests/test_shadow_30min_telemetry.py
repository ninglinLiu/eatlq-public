"""Offline regression test for the 30min SHADOW telemetry (no chain, no money).

2026-06-25 aggressive go-live: the executor records, ONCE per position at +30min
from entry, the would-be mark value (`shadow_30min_*`) into a separate `exec:shadow`
stream — purely so we can compute offline whether a 30min hard-cap would have beaten
the no-cap convex-tail policy, WITHOUT running a live A/B or changing any exit logic.

These tests assert:
  1. `_record_shadow_30min` writes the expected fields/PnL into `exec:shadow`.
  2. It writes ONLY to `exec:shadow` — never pollutes `exec:quality` (exit/fill telemetry).
  3. `PositionStore.mark_shadow_once` is atomic-idempotent: True exactly once, then False
     (so the monitor loop can never double-record across cycles).
  4. The shadow path is observation-only: it never raises and carries no exit decision.

All inputs are fakes; nothing touches the network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services" / "executor" / "src"


def _load_main():
    for _dep in ("web3", "web3.types", "eth_abi", "eth_account"):
        try:
            __import__(_dep)
        except Exception:
            pass
    spec = importlib.util.spec_from_file_location(
        "executor_main_dryrun_test_shadow", ROOT / "tests" / "test_executor_main_dryrun.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    main = mod.load_executor_main()
    main._dryrun_test_mod = mod
    return main


class _StubPoolInfo:
    def __init__(self, pool_address="0xpool", symbol="TKN"):
        self.pool_address = pool_address
        self.non_weth_symbol = symbol
        self.non_weth_token = "0xtoken"


class _StubPos:
    def __init__(self, opened_at_ms=1_700_000_000_000, size_usd=50.0):
        self.opened_at_ms = opened_at_ms
        self.size_usd = size_usd


class _HsetnxRedis:
    """Minimal redis fake with atomic HSETNX semantics for mark_shadow_once."""
    def __init__(self):
        self.h: dict = {}

    def hsetnx(self, key, field, value):
        d = self.h.setdefault(key, {})
        if field in d:
            return 0
        d[field] = value
        return 1


class ShadowTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()
        cls.FakeRedis = cls.main._dryrun_test_mod.FakeRedis
        # import PositionStore from the same executor src
        if str(EXECUTOR_SRC) not in sys.path:
            sys.path.insert(0, str(EXECUTOR_SRC))
        import position as position_mod  # noqa
        cls.PositionStore = position_mod.PositionStore

    def _last_shadow(self, r):
        recs = r.streams.get("exec:shadow", [])
        self.assertTrue(recs, "no exec:shadow record written")
        return json.loads(recs[-1]["json"])

    # 1) fields + PnL computed correctly
    def test_shadow_fields_computed(self):
        m = self.main
        r = self.FakeRedis()
        entry = 10**16          # 0.01 ETH
        mark = 15 * 10**15      # 0.015 ETH = +50%
        m._record_shadow_30min(
            r, pool_info=_StubPoolInfo("0xP", "TKN"), pos=_StubPos(size_usd=50.0),
            mark_wei=mark, entry_wei=entry, peak_wei=2 * 10**16,
            age_sec=1805.0, now_ms=1_700_000_000_000 + 1_805_000,
            block=31_000_123, eth_price=2000.0,
        )
        rec = self._last_shadow(r)
        self.assertEqual(rec["event"], "shadow_30min")
        self.assertEqual(rec["pool"], "0xP")
        self.assertEqual(rec["entry_eth_wei"], entry)
        self.assertEqual(rec["shadow_30min_value_eth_wei"], mark)
        self.assertAlmostEqual(rec["shadow_30min_pnl_pct"], 50.0, places=3)
        # value_usd = 0.015 ETH * $2000 = $30
        self.assertAlmostEqual(rec["shadow_30min_value_usd"], 30.0, places=4)
        self.assertEqual(rec["shadow_30min_block"], 31_000_123)
        self.assertEqual(rec["size_usd"], 50.0)

    # 2) writes ONLY to exec:shadow, never exec:quality
    def test_shadow_does_not_touch_quality(self):
        m = self.main
        r = self.FakeRedis()
        m._record_shadow_30min(
            r, pool_info=_StubPoolInfo(), pos=_StubPos(),
            mark_wei=10**16, entry_wei=10**16, peak_wei=10**16,
            age_sec=1800.0, now_ms=1_700_000_000_000, block=1, eth_price=2000.0,
        )
        self.assertIn("exec:shadow", r.streams)
        self.assertNotIn("exec:quality", r.streams)  # exit/fill stream untouched

    # 3) mark_shadow_once is idempotent (True once, then False)
    def test_mark_shadow_once_idempotent(self):
        store = self.PositionStore(_HsetnxRedis())
        self.assertTrue(store.mark_shadow_once("0xpool"))   # first crossing
        self.assertFalse(store.mark_shadow_once("0xpool"))  # never double-record
        self.assertFalse(store.mark_shadow_once("0xpool"))
        # a different pool still records once
        self.assertTrue(store.mark_shadow_once("0xother"))

    # 4) zero / loss marks compute a sane negative PnL and never raise
    def test_shadow_loss_value(self):
        m = self.main
        r = self.FakeRedis()
        m._record_shadow_30min(
            r, pool_info=_StubPoolInfo(), pos=_StubPos(),
            mark_wei=10**15, entry_wei=10**16, peak_wei=10**16,  # 0.001 vs 0.01 = -90%
            age_sec=1800.0, now_ms=1_700_000_000_000, block=None, eth_price=2000.0,
        )
        rec = self._last_shadow(r)
        self.assertAlmostEqual(rec["shadow_30min_pnl_pct"], -90.0, places=3)
        self.assertIsNone(rec["shadow_30min_block"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
