#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_intelligence.clanker_alpha.v03_route_decode import map_captured_transaction
from launchpad_intelligence.clanker_alpha.v03_semantics import (
    classify_token_swap,
    old_incorrect_buy_predicate,
)


class SemanticsTests(unittest.TestCase):
    def test_token1_buy_signs(self) -> None:
        token = "0x0000000000000000000000000000000000000002"
        paired = "0x0000000000000000000000000000000000000001"  # paired is token0
        label = classify_token_swap(amount0=-100, amount1=500, token=token, paired=paired)
        self.assertEqual(label.side, "BUY_TOKEN")
        self.assertTrue(old_incorrect_buy_predicate(-100, 500, token_is_token0=False) is False)

    def test_token0_buy_rejects_old_predicate(self) -> None:
        # correct buy: amount0>0, amount1<0
        self.assertFalse(old_incorrect_buy_predicate(100, -20, token_is_token0=True))
        label = classify_token_swap(
            amount0=100,
            amount1=-20,
            token="0x0000000000000000000000000000000000000001",
            paired="0x0000000000000000000000000000000000000002",
        )
        self.assertEqual(label.side, "BUY_TOKEN")

    def test_old_predicate_matches_inverted_only(self) -> None:
        # inverted (wrong) buy signs that v0.2 used
        self.assertTrue(old_incorrect_buy_predicate(-100, 20, token_is_token0=True))


class RouteDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "evidence/clanker_sell_route_capture_v021/transactions.json"
        if not path.exists():
            raise unittest.SkipTest("sealed route capture missing")
        cls.rows = json.loads(path.read_text())["rows"]

    def test_entrypoint_maps_single_swap_exact_in(self) -> None:
        ep = [r for r in self.rows if r["top_level"]["selector"] == "0x765e827f"]
        self.assertGreaterEqual(len(ep), 1)
        mapped = map_captured_transaction(ep[0])
        self.assertEqual(mapped.status, "MAPPED")
        self.assertEqual(mapped.action_name, "SWAP_EXACT_IN_SINGLE")
        self.assertEqual(mapped.action, 0x06)
        self.assertIsNotNone(mapped.pool_key)
        self.assertEqual(mapped.hook_data_hex, "0x")

    def test_multicall_rejected_as_ambiguous(self) -> None:
        multi = [r for r in self.rows if r["to_address"] == "0xca11bde05977b3631167028862be2a173976ca11"]
        if not multi:
            self.skipTest("no multicall sample")
        mapped = map_captured_transaction(multi[0])
        self.assertEqual(mapped.status, "AMBIGUOUS_MULTI_LEG")


if __name__ == "__main__":
    unittest.main()
