"""Offline regression test for the EXIT-latency telemetry (no chain, no money).

Task A: the executor must record, on every sell, how long it took from the
trailing/hard-stop/time-exit/blind-panic TRIGGER until the sell tx landed
on-chain (`exit_delay_sec` / `exit_delay_blocks`), without changing any existing
exit decision or any existing telemetry field.

These tests assert:
  1. New fields are computed correctly when the trigger stamps are passed.
  2. OLD behavior is byte-for-byte preserved when the new args are omitted
     (backward compat / "留对照": buy-side delay_sec, deviation_pct unchanged).
  3. The wiring through `_finalize_close(kind="sell")` actually writes the new
     fields into the `exec:quality` Redis stream (mock dry-run sell).

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
    """Load executor main.py via the established dry-run loader (real sibling
    modules, stubbed chain deps)."""
    # Prefer REAL web3/eth_* (installed in venv) so tx_sender can subclass
    # Web3.HTTPProvider; mirrors tests/test_executor_honeypot_defense.py.
    for _dep in ("web3", "web3.types", "eth_abi", "eth_account"):
        try:
            __import__(_dep)
        except Exception:
            pass
    spec = importlib.util.spec_from_file_location(
        "executor_main_dryrun_test", ROOT / "tests" / "test_executor_main_dryrun.py")
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


class _StubStore:
    """Only the methods _finalize_close touches."""
    def __init__(self):
        self.realized = []
        self.closed = []

    def add_realized_pnl_wei(self, day, delta):
        self.realized.append((day, delta))

    def close_position(self, pool_addr):
        self.closed.append(pool_addr)


class ExitLatencyTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()
        cls.FakeRedis = cls.main._dryrun_test_mod.FakeRedis

    def _last_quality(self, r):
        recs = r.streams.get("exec:quality", [])
        self.assertTrue(recs, "no exec:quality record written")
        return json.loads(recs[-1]["json"])

    # 1) new fields computed correctly
    def test_exit_delay_fields_computed(self):
        m = self.main
        r = self.FakeRedis()
        t0 = 1_700_000_000_000
        b0 = 30_000_000
        m._record_exec_quality(
            r, kind="sell", pool_info=_StubPoolInfo(),
            expected_out=1000, actual_out=950,
            signal_ms=None, fill_ms=t0 + 90_000,   # +90s
            sig_block=None, fill_block=b0 + 45,     # +45 blocks
            reverted=False, gas_used=None, gas_cost_wei=None,
            eth_price=2000.0, tx_hash="0xabc", size_usd=50.0,
            exit_trigger_ms=t0, exit_trigger_block=b0,
        )
        rec = self._last_quality(r)
        self.assertEqual(rec["exit_trigger_ms"], t0)
        self.assertEqual(rec["exit_trigger_block"], b0)
        self.assertAlmostEqual(rec["exit_delay_sec"], 90.0, places=2)
        self.assertEqual(rec["exit_delay_blocks"], 45)
        # OLD sell semantics untouched: signal_ms None -> delay_sec None;
        # deviation_pct still computed from expected/actual.
        self.assertIsNone(rec["delay_sec"])
        self.assertIsNone(rec["delay_blocks"])
        self.assertAlmostEqual(rec["deviation_pct"], -5.0, places=3)
        self.assertEqual(rec["kind"], "sell")

    # 2) backward compat: omit new args -> new fields null, old fields intact
    def test_backward_compat_no_trigger(self):
        m = self.main
        r = self.FakeRedis()
        # buy-style call (the path that already records entry delay) must be
        # completely unaffected by the new optional params.
        m._record_exec_quality(
            r, kind="buy", pool_info=_StubPoolInfo(),
            expected_out=1000, actual_out=990,
            signal_ms=1_700_000_000_000, fill_ms=1_700_000_002_000,  # +2s entry delay
            sig_block=30_000_000, fill_block=30_000_001,
            reverted=False, gas_used=None, gas_cost_wei=None,
            eth_price=2000.0, tx_hash="0xbuy", size_usd=50.0,
        )
        rec = self._last_quality(r)
        # new exit fields present but null (additive, no behavior change)
        self.assertIsNone(rec["exit_trigger_ms"])
        self.assertIsNone(rec["exit_trigger_block"])
        self.assertIsNone(rec["exit_delay_sec"])
        self.assertIsNone(rec["exit_delay_blocks"])
        # OLD buy delay still computed exactly as before
        self.assertAlmostEqual(rec["delay_sec"], 2.0, places=2)
        self.assertEqual(rec["delay_blocks"], 1)
        self.assertAlmostEqual(rec["deviation_pct"], -1.0, places=3)

    # 3) wiring: _finalize_close(kind="sell") writes the new fields
    def test_finalize_close_wires_trigger(self):
        m = self.main
        r = self.FakeRedis()
        store = _StubStore()
        # neutralize telegram (no network) without touching exit logic
        orig_tg = m.send_telegram
        m.send_telegram = lambda *a, **k: None
        try:
            t0 = 1_700_000_500_000
            b0 = 31_000_000
            m._finalize_close(
                store, "0xpoolX", entry_wei=10**16, exit_wei=2 * 10**16,
                exit_reason="trailing", dry_run=True, symbol="TKN",
                r=r, pool_info=_StubPoolInfo("0xpoolX", "TKN"),
                expected_out=2 * 10**16, eth_price=2000.0,
                fill_block=b0 + 30,
                exit_trigger_ms=t0, exit_trigger_block=b0,
            )
        finally:
            m.send_telegram = orig_tg
        rec = self._last_quality(r)
        self.assertEqual(rec["kind"], "sell")
        self.assertEqual(rec["exit_trigger_ms"], t0)
        self.assertEqual(rec["exit_trigger_block"], b0)
        self.assertEqual(rec["exit_delay_blocks"], 30)
        self.assertIsNotNone(rec["exit_delay_sec"])  # fill_ms = now() > trigger
        # store side effects happened (position closed) — old behavior intact
        self.assertEqual(store.closed, ["0xpoolX"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
