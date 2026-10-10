from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_DIR = REPO / "scripts" / "wallet_profile_v0"
sys.path.insert(0, str(MODULE_DIR))

from base_attribution import EXCLUDED_ADDRESSES, EXCLUSION_ROWS, MANIFEST  # noqa: E402
from base_transfer_net_attribution import (  # noqa: E402
    RpcClient,
    resolve_swap_transfer_subgraph,
    resolve_transfer_net,
)
from ch_helper import ch_query  # noqa: E402

FIXTURE_PATH = REPO / "tests" / "fixtures" / "base_transfer_net_b2_golden.json"
SOURCE_PATH = MODULE_DIR / "base_transfer_net_attribution.py"
ENV_PATH = REPO / "infra" / "compose" / ".env"


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_rpc_url() -> str:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("EXECUTOR_READ_RPC_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class TransferNetPureTests(unittest.TestCase):
    def test_golden_integer_deltas_are_exact(self) -> None:
        for case in load_fixture()["golden_accounts"]:
            observed = int(case["balance_end_raw"]) - int(case["balance_start_raw"])
            self.assertEqual(observed, int(case["view_net_delta_raw"]), case["case_id"])

    def test_subgraph_audit_fixture_records_exact_adverse_deltas(self) -> None:
        for case in load_fixture()["transfer_subgraph_golden_audit"]:
            observed = int(case["balance_end_raw"]) - int(case["balance_start_raw"])
            self.assertEqual(observed, int(case["balance_delta_raw"]), case["case_id"])
            self.assertEqual(
                observed == int(case["materialized_net_delta_raw"]),
                case["matches_materialized"],
                case["case_id"],
            )

    def test_multi_buyer_transaction_is_ambiguous(self) -> None:
        case = load_fixture()["ambiguous_case"]
        resolution = resolve_transfer_net(
            side=case["side"],
            transfers=case["transfers"],
            pool_address=case["pool_address"],
        )
        self.assertEqual(resolution.source, case["expected_source"])
        self.assertEqual(resolution.account, "")
        self.assertEqual(resolution.candidate_count, case["expected_candidate_count"])

    def test_exact_ordered_subgraph_resolves_one_swap_without_first_recipient_guess(self) -> None:
        pool = "0x1111111111111111111111111111111111111111"
        relay = "0x2222222222222222222222222222222222222222"
        owner = "0x3333333333333333333333333333333333333333"
        other = "0x4444444444444444444444444444444444444444"
        transfers = [
            {"log_index": 8, "sender": pool, "recipient": relay, "amount": 100},
            {"log_index": 11, "sender": relay, "recipient": owner, "amount": 100},
            {"log_index": 18, "sender": pool, "recipient": relay, "amount": 200},
            {"log_index": 21, "sender": relay, "recipient": other, "amount": 200},
        ]
        resolution = resolve_swap_transfer_subgraph(
            side="buy", transfers=transfers, pool_address=pool, swap_log_index=10,
            next_swap_log_index=20,
        )
        self.assertEqual(resolution.source, "transfer_subgraph")
        self.assertEqual(resolution.account, owner)
        self.assertEqual(resolution.net_delta_raw, 100)

    def test_subgraph_branch_stays_ambiguous(self) -> None:
        pool = "0x1111111111111111111111111111111111111111"
        relay = "0x2222222222222222222222222222222222222222"
        transfers = [
            {"log_index": 8, "sender": pool, "recipient": relay, "amount": 100},
            {"log_index": 11, "sender": relay, "recipient": "0x3333333333333333333333333333333333333333", "amount": 100},
            {"log_index": 11, "sender": relay, "recipient": "0x4444444444444444444444444444444444444444", "amount": 100},
        ]
        resolution = resolve_swap_transfer_subgraph(
            side="buy", transfers=transfers, pool_address=pool, swap_log_index=10,
        )
        self.assertEqual(resolution.source, "ambiguous")
        self.assertEqual(resolution.account, "")

    def test_unique_taxed_token_path_is_allowed_but_not_guessed(self) -> None:
        pool = "0x1111111111111111111111111111111111111111"
        relay = "0x2222222222222222222222222222222222222222"
        owner = "0x3333333333333333333333333333333333333333"
        resolution = resolve_swap_transfer_subgraph(
            side="buy",
            transfers=[
                {"log_index": 8, "sender": pool, "recipient": relay, "amount": 100},
                {"log_index": 11, "sender": relay, "recipient": owner, "amount": 97},
            ],
            pool_address=pool,
            swap_log_index=10,
        )
        self.assertEqual(resolution.source, "transfer_subgraph")
        self.assertEqual(resolution.account, owner)

    def test_window_has_no_arrival_time_fallback(self) -> None:
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("received_at_ms", source)
        self.assertIn("block_number BETWEEN", source)
        self.assertIn("archiver_blockTimestamp", source)

    def test_manifest_contains_required_noise_tokens(self) -> None:
        self.assertIn("0x4200000000000000000000000000000000000006", EXCLUDED_ADDRESSES)
        self.assertIn("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", EXCLUDED_ADDRESSES)
        self.assertIn("0x0000000000000000000000000000000000000000", EXCLUDED_ADDRESSES)


@unittest.skipUnless(os.environ.get("EATLQ_RUN_B2_CH") == "1", "ClickHouse integration disabled")
class TransferNetClickHouseTests(unittest.TestCase):
    def test_exclusion_table_equals_manifest(self) -> None:
        rows = ch_query(
            "SELECT lower(address) AS address,category,label,schema_version "
            "FROM eatlq.base_wallet_attribution_exclusions FINAL "
            "WHERE chain_id=8453"
        )
        observed = {
            (row["address"], row["category"], row["label"], int(row["schema_version"]))
            for row in rows
        }
        expected = {
            (row["address"], row["category"], row["label"], int(MANIFEST["schema_version"]))
            for row in EXCLUSION_ROWS
        }
        self.assertEqual(observed, expected)

    def test_locked_coverage_counts_are_exact(self) -> None:
        rows = ch_query("""
            SELECT count() AS denominator,
                   countIf(resolved_account!='') AS any_count,
                   countIf(attribution_source IN ('transfer_net','transfer_subgraph','tx_from_confirmed')) AS beneficial,
                   countIf(attribution_source IN ('transfer_net','transfer_subgraph','tx_from_confirmed')
                           AND account_code_class='eoa') AS verified_eoa
            FROM eatlq.base_cha_wallet_swaps FINAL
            WHERE block_number BETWEEN 46389458 AND 46417105
              AND protocol IN ('uniswap_v3','aero_cl')
        """)
        observed = tuple(
            int(rows[0][key]) for key in ("denominator", "any_count", "beneficial", "verified_eoa")
        )
        self.assertEqual(observed, (57426, 50164, 47593, 14183))

    def test_no_infrastructure_resolved_account(self) -> None:
        rows = ch_query("""
            SELECT count() AS count FROM
            (SELECT * FROM eatlq.base_cha_wallet_swaps FINAL) AS s
            INNER JOIN (SELECT * FROM eatlq.base_wallet_attribution_exclusions FINAL) AS e
              ON s.chain_id=e.chain_id AND lower(s.resolved_account)=lower(e.address)
            WHERE s.resolved_account!=''
              AND s.block_number BETWEEN 46389458 AND 46417105
        """)
        self.assertEqual(int(rows[0]["count"]), 0)

    def test_tx_from_overlap_counts_are_frozen(self) -> None:
        rows = ch_query("""
            SELECT
              countIf(tx_from!='' AND tx_from NOT IN
                (SELECT address FROM eatlq.base_wallet_attribution_exclusions FINAL WHERE chain_id=8453)
                AND attribution_source IN ('transfer_net','transfer_subgraph','tx_from_confirmed')
                AND account_code_class='eoa') AS eligible,
              countIf(tx_from!='' AND tx_from NOT IN
                (SELECT address FROM eatlq.base_wallet_attribution_exclusions FINAL WHERE chain_id=8453)
                AND attribution_source IN ('transfer_net','transfer_subgraph','tx_from_confirmed')
                AND account_code_class='eoa'
                AND resolved_account=tx_from) AS matches,
              countIf(tx_from!='' AND tx_from NOT IN
                (SELECT address FROM eatlq.base_wallet_attribution_exclusions FINAL WHERE chain_id=8453)
                AND attribution_source IN ('transfer_net','transfer_subgraph','tx_from_confirmed')
                AND account_code_class='eoa'
                AND resolved_account!=tx_from) AS mismatches
            FROM eatlq.base_cha_wallet_swaps FINAL
            WHERE block_number BETWEEN 46389458 AND 46417105
              AND protocol IN ('uniswap_v3','aero_cl')
        """)
        observed = tuple(int(rows[0][key]) for key in ("eligible", "matches", "mismatches"))
        self.assertEqual(observed, (14050, 13302, 748))

    def test_materialized_golden_rows_are_exact(self) -> None:
        for case in load_fixture()["golden_accounts"]:
            rows = ch_query(f"""
                SELECT count() AS swap_rows,uniqExact(venue_address) AS pools,
                       any(net_delta_raw) AS net_delta_raw,
                       uniqExact(resolved_account) AS accounts
                FROM eatlq.base_cha_wallet_swaps FINAL
                WHERE tx_hash='{case['tx_hash']}'
                  AND resolved_account='{case['account']}'
                  AND target_token_address='{case['target_token']}'
                  AND attribution_source IN ('transfer_net','tx_from_confirmed')
            """)
            self.assertEqual(int(rows[0]["swap_rows"]), case["expected_swap_rows"])
            self.assertEqual(int(rows[0]["accounts"]), 1)
            self.assertEqual(int(rows[0]["net_delta_raw"]), int(case["view_net_delta_raw"]))
            if "expected_distinct_pools" in case:
                self.assertEqual(int(rows[0]["pools"]), case["expected_distinct_pools"])

    def test_fixed_ambiguous_row_is_materialized(self) -> None:
        case = load_fixture()["materialized_ambiguous_case"]
        rows = ch_query(f"""
            SELECT attribution_source,same_direction_candidate_count,resolved_account
            FROM eatlq.base_cha_wallet_swaps FINAL
            WHERE tx_hash='{case['tx_hash']}' AND target_token_address='{case['target_token']}'
              AND venue_address='{case['pool_address']}'
              AND side='{case['side']}'
              AND log_index={case['log_index']}
            LIMIT 1
        """)
        self.assertEqual(rows[0]["attribution_source"], "ambiguous")
        self.assertEqual(int(rows[0]["same_direction_candidate_count"]), 2)
        self.assertEqual(rows[0]["resolved_account"], "")

    def test_failed_subgraph_golden_batch_is_rolled_back_exactly(self) -> None:
        for case in load_fixture()["transfer_subgraph_golden_audit"]:
            rows = ch_query(f"""
                SELECT count() AS rows,
                       countIf(attribution_source='ambiguous') AS ambiguous_rows,
                       countIf(attribution_source='transfer_subgraph') AS subgraph_rows
                FROM eatlq.base_cha_wallet_swaps FINAL
                WHERE tx_hash='{case['tx_hash']}'
                  AND target_token_address='{case['target_token']}'
            """)
            self.assertEqual(int(rows[0]["rows"]), case["expected_post_rollback_rows"])
            self.assertEqual(
                int(rows[0]["ambiguous_rows"]),
                case["expected_post_rollback_ambiguous_rows"],
            )
            self.assertEqual(int(rows[0]["subgraph_rows"]), 0)


@unittest.skipUnless(os.environ.get("EATLQ_RUN_B2_RPC") == "1", "archive RPC integration disabled")
class TransferNetArchiveRpcTests(unittest.TestCase):
    def test_archive_balance_boundaries_equal_view_raw_delta(self) -> None:
        fixture = load_fixture()
        rpc = RpcClient(load_rpc_url())
        start = hex(fixture["window"]["balance_start_block"])
        end = hex(fixture["window"]["end_block"])
        for case in fixture["golden_accounts"]:
            calldata = "0x70a08231" + case["account"][2:].rjust(64, "0")
            request = {"to": case["target_token"], "data": calldata}
            balance_start = int(rpc.call("eth_call", [request, start]), 16)
            balance_end = int(rpc.call("eth_call", [request, end]), 16)
            self.assertEqual(balance_start, int(case["balance_start_raw"]), case["case_id"])
            self.assertEqual(balance_end, int(case["balance_end_raw"]), case["case_id"])
            self.assertEqual(balance_end - balance_start, int(case["view_net_delta_raw"]), case["case_id"])

    def test_smart_account_code_is_historical(self) -> None:
        fixture = load_fixture()
        case = next(row for row in fixture["golden_accounts"] if row["case_id"] == "eip7702_smart_account")
        code = RpcClient(load_rpc_url()).call(
            "eth_getCode", [case["account"], hex(fixture["window"]["end_block"])]
        )
        self.assertEqual(str(code).lower().startswith(case["code_prefix"]), True)

    def test_subgraph_uniform_archive_boundaries_are_exact(self) -> None:
        fixture = load_fixture()
        rpc = RpcClient(load_rpc_url())
        start = hex(fixture["window"]["balance_start_block"])
        end = hex(fixture["window"]["end_block"])
        for case in fixture["transfer_subgraph_golden_audit"]:
            calldata = "0x70a08231" + case["account"][2:].rjust(64, "0")
            request = {"to": case["target_token"], "data": calldata}
            balance_start = int(rpc.call("eth_call", [request, start]), 16)
            balance_end = int(rpc.call("eth_call", [request, end]), 16)
            self.assertEqual(balance_start, int(case["balance_start_raw"]), case["case_id"])
            self.assertEqual(balance_end, int(case["balance_end_raw"]), case["case_id"])
            self.assertEqual(
                balance_end - balance_start,
                int(case["balance_delta_raw"]),
                case["case_id"],
            )


if __name__ == "__main__":
    unittest.main()
