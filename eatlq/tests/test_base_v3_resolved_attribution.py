from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "wallet_profile_v0"
sys.path.insert(0, str(LIB))

from base_attribution import (  # noqa: E402
    EXCLUDED_ADDRESSES,
    MANIFEST_PATH,
    resolve_eoa,
    target_trade_side,
)


class ResolutionPriorityTests(unittest.TestCase):
    EOA_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    EOA_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    EOA_C = "0xcccccccccccccccccccccccccccccccccccccccc"
    UNIVERSAL_ROUTER = "0x6ff5693b99212da76ad316178a184ab56d299b43"

    def test_tx_from_wins(self) -> None:
        got = resolve_eoa(self.EOA_A, self.EOA_B, self.EOA_C)
        self.assertEqual((got.address, got.source), (self.EOA_A, "tx_from"))

    def test_recipient_fallback_skips_router_tx_from(self) -> None:
        got = resolve_eoa(self.UNIVERSAL_ROUTER, self.EOA_B, self.EOA_C)
        self.assertEqual((got.address, got.source), (self.EOA_B, "recipient"))

    def test_sender_is_last_resort(self) -> None:
        got = resolve_eoa("", self.UNIVERSAL_ROUTER, self.EOA_C)
        self.assertEqual((got.address, got.source), (self.EOA_C, "sender"))

    def test_all_infrastructure_is_unresolved(self) -> None:
        got = resolve_eoa("", self.UNIVERSAL_ROUTER, self.UNIVERSAL_ROUTER)
        self.assertEqual((got.address, got.source), ("", "unresolved"))

    def test_manifest_is_unique_and_contains_required_classes(self) -> None:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        categories = {row["category"] for row in data["addresses"]}
        self.assertTrue({"router", "aggregator", "npm", "quoter", "quote_token"} <= categories)
        self.assertEqual(len(data["addresses"]), len(EXCLUDED_ADDRESSES))


class SideAndSqlContractTests(unittest.TestCase):
    WETH = "0x4200000000000000000000000000000000000006"
    TOKEN = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def test_trade_side_is_quote_leg_aware(self) -> None:
        self.assertEqual(target_trade_side(100, -5, self.WETH, self.TOKEN), "buy")
        self.assertEqual(target_trade_side(-100, 5, self.WETH, self.TOKEN), "sell")
        self.assertEqual(target_trade_side(-5, 100, self.TOKEN, self.WETH), "buy")
        self.assertEqual(target_trade_side(5, -100, self.TOKEN, self.WETH), "sell")

    def test_view_never_uses_received_at_fallback(self) -> None:
        ddl = (REPO / "infra/clickhouse/init/041_base_v3_resolved_wallet_swaps.sql").read_text()
        self.assertNotIn("received_at_ms", ddl.lower())
        self.assertIn("block_ts_map", ddl)
        self.assertIn("archiver_blockTimestamp", ddl)

    def test_analysis_windows_are_block_number_only(self) -> None:
        source = (LIB / "base_v3_resolved_attribution.py").read_text()
        self.assertIn("block_number BETWEEN", source)
        self.assertNotIn("INTERVAL", source)
        self.assertNotIn("toDateTime(", source)


class GoldenRegressionTests(unittest.TestCase):
    FIXTURE = REPO / "tests/fixtures/base_v3_resolved_eoa_golden.json"

    def test_golden_fixture_invariants(self) -> None:
        golden = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertLess(golden["start_block"], golden["end_block"])
        for wallet in golden["wallets"]:
            self.assertEqual(wallet["swaps"], wallet["buys"] + wallet["sells"])
            self.assertEqual(wallet["code_at_end_block"], "0x")
            self.assertIn(wallet["sample_tx_hash"], wallet["basescan_tx"])

    @unittest.skipUnless(
        os.environ.get("EATLQ_RUN_CH_GOLDEN") == "1",
        "set EATLQ_RUN_CH_GOLDEN=1 for ClickHouse golden reconciliation",
    )
    def test_clickhouse_view_matches_golden(self) -> None:
        from ch_helper import ch_query

        golden = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        for expected in golden["wallets"]:
            rows = ch_query(
                f"""
                SELECT
                  count() AS swaps,
                  countIf(target_trade_side = 'buy') AS buys,
                  countIf(target_trade_side = 'sell') AS sells,
                  toString(sum(resolved_wallet_target_delta_raw)) AS net_raw
                FROM eatlq.base_v3_wallet_swaps_resolved
                WHERE block_number BETWEEN {golden['start_block']} AND {golden['end_block']}
                  AND resolved_eoa = '{expected['wallet']}'
                  AND target_token_address = '{golden['target_token']}'
                """,
                timeout=600,
            )
            self.assertEqual(
                rows[0],
                {key: str(expected[key]) for key in ("swaps", "buys", "sells", "net_raw")},
            )


if __name__ == "__main__":
    unittest.main()
