from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_DIR = REPO / "scripts" / "wallet_profile_v0"
sys.path.insert(0, str(MODULE_DIR))

from base_b3_entity_attribution import (  # noqa: E402
    ENTRYPOINTS,
    even_sample,
    map_swap_to_userop,
    route_family,
)
from base_b3_inventory_golden import transfer_net_for  # noqa: E402
from base_b3_finalize import assemble  # noqa: E402


class BaseB3EntityAttributionTests(unittest.TestCase):
    def test_userop_log_boundaries_map_without_first_recipient_fallback(self) -> None:
        events = [
            {"log_index": 20, "sender": "0x" + "11" * 20},
            {"log_index": 40, "sender": "0x" + "22" * 20},
        ]
        self.assertEqual(map_swap_to_userop(10, events), events[0])
        self.assertEqual(map_swap_to_userop(30, events), events[1])
        self.assertEqual(map_swap_to_userop(50, events), None)

    def test_entrypoint_route_has_priority(self) -> None:
        entrypoint = sorted(ENTRYPOINTS)[0]
        self.assertEqual(route_family(entrypoint, set(), True), "erc4337_entrypoint")

    def test_even_sample_is_deterministic_and_unique(self) -> None:
        rows = [
            {
                "tx_hash": f"0x{index:064x}",
                "block_number": index,
                "protocol": "uniswap_v3",
                "venue_address": "0x" + "11" * 20,
                "side": "buy" if index % 2 else "sell",
            }
            for index in range(20)
        ]
        first = even_sample(rows, 10)
        second = even_sample(rows, 10)
        self.assertEqual(first, second)
        self.assertEqual(len({row["tx_hash"] for row in first}), 10)

    def test_transfer_net_for_account_is_exact_integer(self) -> None:
        token = "0x" + "aa" * 20
        account = "0x" + "bb" * 20
        other = "0x" + "cc" * 20
        topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        logs = [{
            "address": token,
            "topics": [topic, "0x" + other[2:].rjust(64, "0"), "0x" + account[2:].rjust(64, "0")],
            "data": "0x" + hex(123)[2:].rjust(64, "0"),
        }]
        self.assertEqual(transfer_net_for(logs, token, account), 123)

    def test_final_artifact_keeps_external_crosscheck_and_no_go_hard(self) -> None:
        def load(name: str) -> dict:
            return json.loads((REPO / "mvp" / "diagnostics" / name).read_text(encoding="utf-8"))

        result = assemble(
            load("base_b3_entity_attribution_sample_v2_20260714.json"),
            load("base_b3_trace_golden_20260714.json"),
            load("base_b3_inventory_golden_20260714.json"),
            load("base_b3_latency_probe_20260714.json"),
            json.loads(
                (REPO / "configs" / "research" / "base_holder_suspected_shared_infra_20260714.json")
                .read_text(encoding="utf-8")
            ),
        )
        self.assertTrue(result["local_numeric_gates_pass"])
        self.assertEqual(result["verdict"], "LOCAL_FEASIBILITY_PASS_EXTERNAL_CROSSCHECK_PENDING")
        self.assertFalse(result["external_crosscheck_complete"])
        self.assertTrue(result["no_go"])
        self.assertEqual(result["rh1_merit"], "UNTESTED")


if __name__ == "__main__":
    unittest.main()
