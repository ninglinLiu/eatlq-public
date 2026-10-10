"""Live FIFO classify + consume, including the CAT WETH mint/burn case."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
sys.path.insert(0, str(ROOT))

from src.fifo_live import Fill, TransferLeg, apply_fifo, classify_tx, legs_from_receipt, native_usd_at

W = "0xfd7e55a555555c2f25053a38ec744de1afea4fa4"
CAT = "0xb200000000000000000000c97fb2116b12161201"
WETH = "0x4200000000000000000000000000000000000006"
ZERO = "0x0000000000000000000000000000000000000000"
ROUTER = "0xd8ba9d1a99fc21f0eca24e9b85737c28a194a4e2"
T0 = datetime(2026, 9, 15, 17, 29, 39, tzinfo=timezone.utc)


class LiveFifoTests(unittest.TestCase):
    def test_cat_sell_uses_weth_mint_burn(self):
        legs = [
            TransferLeg(CAT, "CAT", 2753295.5518534654, W, "0x498581ff718922c3f8e6a244956af099b2652b2b"),
            TransferLeg(WETH, "WETH", 0.012344272076008947, ZERO, ROUTER),
            TransferLeg(WETH, "WETH", 0.012344272076008947, ROUTER, ZERO),
        ]
        fills = classify_tx(
            wallet=W, ts=T0, tx="0xaaa", legs=legs,
            native_out=0.0, native_in=0.0, chain="base", native_usd=2425.0,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].kind, "sell")
        self.assertAlmostEqual(fills[0].proceeds_usd, 0.012344272076008947 * 2425.0, places=4)
        self.assertEqual(fills[0].quote_kind, "weth_mint_burn")

    def test_cat_buy_uses_weth_mint_or_native(self):
        legs = [
            TransferLeg(WETH, "WETH", 0.014142857142857143, ZERO, ROUTER),
            TransferLeg(CAT, "CAT", 5506591.103706931, "0x498581ff718922c3f8e6a244956af099b2652b2b", W),
            TransferLeg(WETH, "WETH", 0.014142857142857143, ROUTER, ZERO),
        ]
        fills = classify_tx(
            wallet=W, ts=T0, tx="0xbbb", legs=legs,
            native_out=0.0, native_in=0.0, chain="base", native_usd=2425.0,
        )
        self.assertEqual(fills[0].kind, "buy")
        self.assertAlmostEqual(fills[0].cost_usd, 0.014142857142857143 * 2425.0, places=4)

    def test_fifo_roundtrip_sign(self):
        fills = [
            Fill("buy", CAT, "CAT", 100.0, T0, "0x1", cost_usd=50.0),
            Fill("sell", CAT, "CAT", 100.0, T0 + timedelta(hours=1), "0x2", proceeds_usd=80.0),
        ]
        out = apply_fifo(fills, now=T0 + timedelta(days=1))
        self.assertAlmostEqual(out.realized_usd_7d, 30.0)
        self.assertEqual(out.n_sell, 1)

    def test_transfer_in_is_contaminated_zero_cost(self):
        fills = [
            Fill("transfer_in", CAT, "CAT", 100.0, T0, "0x1", contaminated=True),
            Fill("sell", CAT, "CAT", 100.0, T0 + timedelta(hours=1), "0x2", proceeds_usd=20.0),
        ]
        out = apply_fifo(fills, now=T0 + timedelta(days=1))
        self.assertAlmostEqual(out.realized_usd_7d, 20.0)
        self.assertEqual(out.n_transfer_in, 1)

    def test_weth_deposit_withdrawal_is_mint_burn(self):
        weth = "0x4200000000000000000000000000000000000006"
        wad = 12344272076008947
        receipt = {
            "logs": [
                {
                    "address": CAT,
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        "0x" + "0" * 24 + W[2:],
                        "0x" + "0" * 24 + "498581ff718922c3f8e6a244956af099b2652b2b",
                    ],
                    "data": hex(2753295551853465471915565),
                },
                {
                    "address": weth,
                    "topics": [
                        "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c",
                        "0x" + "0" * 24 + ROUTER[2:],
                    ],
                    "data": hex(wad),
                },
                {
                    "address": weth,
                    "topics": [
                        "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
                        "0x" + "0" * 24 + ROUTER[2:],
                    ],
                    "data": hex(wad),
                },
            ]
        }
        legs = legs_from_receipt(receipt, {CAT: 18})
        fills = classify_tx(
            wallet=W, ts=T0, tx="0xsell", legs=legs,
            native_out=0.0, native_in=0.0, chain="base", native_usd=2425.0,
        )
        self.assertEqual(fills[0].kind, "sell")
        self.assertAlmostEqual(fills[0].proceeds_usd, (wad / 1e18) * 2425.0, places=4)

    def test_legs_from_receipt_transfer(self):
        weth = "0x4200000000000000000000000000000000000006"
        qty = 123456789
        receipt = {
            "logs": [
                {
                    "address": weth,
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        "0x" + "0" * 24 + ZERO[2:],
                        "0x" + "0" * 24 + W[2:],
                    ],
                    "data": hex(qty),
                }
            ]
        }
        legs = legs_from_receipt(receipt)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].to, W)
        self.assertAlmostEqual(legs[0].qty, qty / 1e18)

    def test_native_usd_nearest_day(self):
        daily = {"2026-09-14": 2000.0, "2026-09-16": 2100.0}
        self.assertEqual(native_usd_at(datetime(2026, 9, 16, tzinfo=timezone.utc), daily, 1.0), 2100.0)
        self.assertEqual(native_usd_at(datetime(2026, 9, 15, tzinfo=timezone.utc), daily, 1.0), 2000.0)


if __name__ == "__main__":
    unittest.main()
