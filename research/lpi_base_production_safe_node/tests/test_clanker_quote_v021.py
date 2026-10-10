from __future__ import annotations

import json
import math
import unittest

from eth_abi import encode
from eth_hash.auto import keccak

from launchpad_intelligence.clanker_alpha.v02_quote import PoolKey
from launchpad_intelligence.clanker_alpha.v021_audit import (
    RpcOutcome,
    adjudicate,
    build_quote_calldata,
    classify_jsonrpc_error,
    compute_pool_id,
    strict_json_dumps,
)
from scripts.run_clanker_quote_calibration_v021 import actual_buy_amounts
from scripts.run_clanker_sell_crosscheck_v021 import actual_sell_amounts, round_robin
from scripts.run_clanker_sell_route_capture_v021 import decode_top_level_input


class ClankerQuoteV021Test(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = PoolKey(
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
            0x800000,
            200,
            "0x3333333333333333333333333333333333333333",
        )

    def test_pool_id_matches_canonical_tuple_encoding(self) -> None:
        expected = "0x" + keccak(
            encode(
                ["(address,address,uint24,int24,address)"],
                [self.pool.as_tuple()],
            )
        ).hex()
        self.assertEqual(compute_pool_id(self.pool), expected)

    def test_quote_calldata_has_expected_selector_and_encoded_arguments(self) -> None:
        data = build_quote_calldata(self.pool, zero_for_one=True, amount_in=250_000_000)
        expected_selector = keccak(
            b"quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
        )[:4].hex()
        self.assertTrue(data.startswith("0x" + expected_selector))
        self.assertGreater(len(data), 10)

    def test_unknown_revert_is_application_failure_not_transport(self) -> None:
        payload = "0x6190b2b0" + "00" * 32 + "00" * 28 + "7a5ed73a" + "00" * 32
        cls, raw, selectors = classify_jsonrpc_error({"code": 3, "data": payload})
        self.assertEqual(cls, "UNKNOWN_REVERT")
        self.assertEqual(raw, payload)
        self.assertIn("0x6190b2b0", selectors)
        self.assertFalse(RpcOutcome(False, error_class=cls).transport_failed)

    def test_rpc_error_without_revert_data_is_not_mislabeled_no_quote(self) -> None:
        cls, raw, selectors = classify_jsonrpc_error({"code": -32000, "message": "execution failed"})
        self.assertEqual(cls, "JSON_RPC_ERROR")
        self.assertIsNone(raw)
        self.assertEqual(selectors, [])

    def test_transport_taxonomy_property(self) -> None:
        for cls in ["HTTP_429", "HTTP_5XX", "TIMEOUT", "CONNECTION_ERROR", "INVALID_JSON"]:
            with self.subTest(cls=cls):
                self.assertTrue(RpcOutcome(False, error_class=cls).transport_failed)
        self.assertFalse(RpcOutcome(False, error_class="UNKNOWN_REVERT").transport_failed)

    def test_strict_json_rejects_nan_and_infinity(self) -> None:
        for value in [math.nan, math.inf, -math.inf]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json_dumps({"value": value})

    def test_strict_json_round_trip(self) -> None:
        value = {"failure": {"error_class": "HTTP_429", "ok": False}, "amount_out": None}
        self.assertEqual(json.loads(strict_json_dumps(value)), value)

    def test_entry_only_cannot_validate_quote_path(self) -> None:
        verdict = adjudicate(
            entry_successes=12,
            immediate_reverse_successes=0,
            crosscheck_passes=0,
            transport_failures=0,
            total_network_attempts=50,
            budget_exhausted=False,
        )
        self.assertEqual(verdict, "QUOTE_PATH_NOT_VALIDATED")

    def test_transport_contamination_has_priority(self) -> None:
        verdict = adjudicate(
            entry_successes=12,
            immediate_reverse_successes=12,
            crosscheck_passes=8,
            transport_failures=21,
            total_network_attempts=100,
            budget_exhausted=False,
        )
        self.assertEqual(verdict, "TRANSPORT_INSUFFICIENT")

    def test_validation_threshold(self) -> None:
        verdict = adjudicate(
            entry_successes=8,
            immediate_reverse_successes=8,
            crosscheck_passes=5,
            transport_failures=2,
            total_network_attempts=80,
            budget_exhausted=False,
        )
        self.assertEqual(verdict, "QUOTE_PATH_VALIDATED")

    def test_crosscheck_direction_filters_sell_before_cap(self) -> None:
        token0 = self.pool.currency0
        buy = {"amount0": 100, "amount1": -20}
        sell = {"amount0": -100, "amount1": 20}
        self.assertEqual(actual_buy_amounts(buy, self.pool, token0), (20, 100, False))
        self.assertIsNone(actual_buy_amounts(sell, self.pool, token0))

    def test_sell_crosscheck_direction(self) -> None:
        token0 = self.pool.currency0
        sell = {"amount0": -100, "amount1": 20}
        buy = {"amount0": 100, "amount1": -20}
        self.assertEqual(actual_sell_amounts(sell, self.pool, token0), (100, 20, True))
        self.assertIsNone(actual_sell_amounts(buy, self.pool, token0))

    def test_v02_fill_proxy_uses_caller_delta_signs(self) -> None:
        from launchpad_intelligence.clanker_alpha.v02_quote import swap_fill_proxy

        result = swap_fill_proxy(
            swap={
                "amount0": 100,
                "amount1": -20,
                "block_number": 1,
                "transaction_hash": "0x1",
                "log_index": 0,
                "fee": 10000,
                "sqrt_price_x96": 1,
                "liquidity": 1,
            },
            token_is_token0=True,
            target_amount_in=20,
            pool_fee=0x800000,
        )
        self.assertTrue(result.ok)
        self.assertEqual((result.amount_in, result.amount_out), (20, 100))

    def test_round_robin_prefers_pool_diversity(self) -> None:
        rows = round_robin({"a": [{"id": "a1"}, {"id": "a2"}], "b": [{"id": "b1"}]}, 3)
        self.assertEqual([row["id"] for row in rows], ["a1", "b1", "a2"])

    def test_universal_router_outer_decode_preserves_raw_command_input(self) -> None:
        selector = keccak(b"execute(bytes,bytes[],uint256)")[:4]
        calldata = "0x" + (
            selector + encode(["bytes", "bytes[]", "uint256"], [b"\x10", [b"\x01\x02"], 123])
        ).hex()
        decoded = decode_top_level_input(calldata)
        self.assertTrue(decoded["universal_router_execute_decoded"])
        self.assertEqual(decoded["command_rows"][0]["command_type_low6"], 16)
        self.assertEqual(decoded["command_rows"][0]["input"], "0x0102")


if __name__ == "__main__":
    unittest.main()
