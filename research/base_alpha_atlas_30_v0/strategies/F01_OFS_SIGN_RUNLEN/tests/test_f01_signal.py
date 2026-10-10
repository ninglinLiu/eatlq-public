"""Synthetic fixtures for F01 causal run-length signals."""
from __future__ import annotations

import unittest

from strategies.F01_OFS_SIGN_RUNLEN.runner import Swap, signal_fn


def S(pool, block, log, sign, notional, ts=None, tx_index=None, tx="0x1"):
    return Swap(
        pool_id=pool,
        block_number=block,
        log_index=log,
        timestamp_ms=ts if ts is not None else block * 1000,
        trade_sign=sign,
        quote_notional_eth=notional,
        transaction_index=tx_index,
        tx_hash=tx,
    )


PARAMS = {
    "min_run_len": 3,
    "min_run_notional_eth": 0.1,
    "L_completed_runs": 20,
    "z_enter": None,
    "cooldown_swaps": 1000,  # effectively one signal per test unless reset
    "min_swaps_in_pool": 3,
}


class F01SignalTests(unittest.TestCase):
    def test_buy_run_triggers(self):
        swaps = [
            S("p", 1, 1, 1, 0.05),
            S("p", 2, 1, 1, 0.05),
            S("p", 3, 1, 1, 0.05),  # crosses 3
        ]
        # cooldown large but first cross ok
        params = dict(PARAMS, cooldown_swaps=10)
        sigs = signal_fn(swaps, params)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].meta["run_length"], 3)
        self.assertEqual(sigs[0].meta["trigger_block_number"], 3)

    def test_sell_run_no_long_signal(self):
        swaps = [S("p", i, 1, -1, 0.2) for i in range(1, 6)]
        sigs = signal_fn(swaps, dict(PARAMS, cooldown_swaps=0))
        self.assertEqual(sigs, [])

    def test_alternating_no_run(self):
        swaps = [
            S("p", 1, 1, 1, 0.2),
            S("p", 2, 1, -1, 0.2),
            S("p", 3, 1, 1, 0.2),
            S("p", 4, 1, -1, 0.2),
            S("p", 5, 1, 1, 0.2),
        ]
        sigs = signal_fn(swaps, dict(PARAMS, min_run_len=3, cooldown_swaps=0))
        self.assertEqual(sigs, [])

    def test_same_block_multiple_logs(self):
        swaps = [
            S("p", 10, 1, 1, 0.05),
            S("p", 10, 2, 1, 0.05),
            S("p", 10, 3, 1, 0.05),
        ]
        sigs = signal_fn(swaps, dict(PARAMS, cooldown_swaps=0))
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].meta["trigger_log_index"], 3)

    def test_shuffled_input_deterministic(self):
        swaps = [
            S("p", 3, 1, 1, 0.05, tx="c"),
            S("p", 1, 1, 1, 0.05, tx="a"),
            S("p", 2, 1, 1, 0.05, tx="b"),
        ]
        a = signal_fn(swaps, dict(PARAMS, cooldown_swaps=0))
        b = signal_fn(list(reversed(swaps)), dict(PARAMS, cooldown_swaps=0))
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].meta, b[0].meta)

    def test_duplicate_log_deduped(self):
        swaps = [
            S("p", 1, 1, 1, 0.05, tx="a"),
            S("p", 1, 1, 1, 0.05, tx="a"),  # dup
            S("p", 2, 1, 1, 0.05, tx="b"),
            S("p", 3, 1, 1, 0.05, tx="c"),
        ]
        sigs = signal_fn(swaps, dict(PARAMS, cooldown_swaps=0))
        self.assertEqual(len(sigs), 1)

    def test_unknown_sign_not_zero(self):
        swaps = [
            S("p", 1, 1, 1, 0.2),
            S("p", 2, 1, None, 0.2),  # UNKNOWN — must not break as opposite
            S("p", 3, 1, 1, 0.2),
            S("p", 4, 1, 1, 0.2),
        ]
        # run should be 1,1,1 across known signs → length 3 at swap4
        sigs = signal_fn(swaps, dict(PARAMS, min_run_len=3, cooldown_swaps=0, min_swaps_in_pool=3))
        self.assertEqual(len(sigs), 1)

    def test_unknown_notional_blocks_amount_gate(self):
        swaps = [
            S("p", 1, 1, 1, None),
            S("p", 2, 1, 1, None),
            S("p", 3, 1, 1, None),
        ]
        sigs = signal_fn(swaps, dict(PARAMS, cooldown_swaps=0))
        self.assertEqual(sigs, [])

    def test_future_swaps_do_not_change_past_signal(self):
        base = [
            S("p", 1, 1, 1, 0.05),
            S("p", 2, 1, 1, 0.05),
            S("p", 3, 1, 1, 0.05),
        ]
        sig1 = signal_fn(base, dict(PARAMS, cooldown_swaps=0))
        more = base + [S("p", 4, 1, 1, 0.05), S("p", 5, 1, -1, 1.0)]
        sig2 = signal_fn(more, dict(PARAMS, cooldown_swaps=100))
        # first signal coordinates unchanged
        self.assertEqual(sig1[0].meta["trigger_block_number"], sig2[0].meta["trigger_block_number"])
        self.assertEqual(sig1[0].meta["run_length"], sig2[0].meta["run_length"])


if __name__ == "__main__":
    unittest.main()
