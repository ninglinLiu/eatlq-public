from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from base_rh1_smartmoney_core import (  # noqa: E402
    Episode,
    Trade,
    WalletState,
    build_snapshots,
    is_launch_era_smart,
    metrics,
    process_trade,
    simulate_copy,
)


def trade(ts, side, qty, quote, wallet="0xabc", token="0xt", pool="0xp", idx=0):
    return Trade(
        ts=ts,
        block=int(ts),
        tx_hash=f"0x{int(ts):064x}",
        log_index=idx,
        pool=pool,
        token=token,
        wallet=wallet,
        side=side,
        target_qty=qty,
        quote_weth=quote,
        price_weth=quote / qty,
    )


class EpisodeTests(unittest.TestCase):
    def test_closed_episode_uses_real_buy_and_sell_amounts(self):
        state = WalletState()
        self.assertIsNone(process_trade(state, trade(1, "buy", 10, 1)))
        closed = process_trade(state, trade(2, "sell", 10, 1.5))
        self.assertEqual(closed.cost_weth, 1)
        self.assertEqual(closed.proceeds_weth, 1.5)
        self.assertEqual(closed.pnl_weth, 0.5)
        self.assertEqual(len(state.closed), 1)

    def test_snapshot_is_strictly_before_current_buy(self):
        trades = [
            trade(1, "buy", 10, 1, idx=1),
            trade(2, "sell", 10, 1.5, idx=2),
            trade(3, "buy", 10, 1, idx=3),
        ]
        snapshots, _ = build_snapshots(trades)
        self.assertEqual(snapshots[trades[0].key]["n_closed"], 0)
        self.assertEqual(snapshots[trades[2].key]["n_closed"], 1)

    def test_selector_is_deconcentrated_and_repeated(self):
        state = WalletState(
            closed=[
                Episode("a", 0, 7200, 1, 1.2),
                Episode("b", 100, 7300, 1, 1.2),
                Episode("c", 200, 7400, 1, 1.2),
            ],
            buys=3,
            sells=3,
        )
        self.assertEqual(is_launch_era_smart(metrics(state)), True)


class FillTests(unittest.TestCase):
    def test_entry_is_first_real_trade_after_delay_and_capacity(self):
        signal = trade(0, "buy", 100, 1, idx=0)
        path = [
            trade(9, "buy", 100, 1, idx=1),
            trade(10, "buy", 10, 0.0001, idx=2),
            trade(11, "buy", 100, 2, idx=3),
            trade(20, "sell", 100, 3, idx=4),
            trade(30, "sell", 100, 4, idx=5),
            trade(40, "sell", 100, 5, idx=6),
        ]
        result = simulate_copy(signal, path, 10, 0.001, 0)
        self.assertEqual(result["entry_ts"], 11)

    def test_unfilled_timeout_is_adverse_not_dropped(self):
        signal = trade(0, "buy", 100, 1, idx=0)
        path = [trade(10, "buy", 100, 1, idx=1)]
        result = simulate_copy(signal, path, 10, 0.001, 0)
        self.assertEqual(result["return_frac"], -1)
        self.assertEqual(result["unfilled_frac"], 1)

    def test_tp_requires_real_sell_capacity(self):
        signal = trade(0, "buy", 100, 1, idx=0)
        path = [
            trade(10, "buy", 100, 1, idx=1),
            trade(20, "buy", 100, 2, idx=2),
            trade(30, "sell", 0.01, 0.0002, idx=3),
        ]
        result = simulate_copy(signal, path, 10, 0.001, 0)
        self.assertEqual(result["return_frac"], -1)


if __name__ == "__main__":
    unittest.main()
