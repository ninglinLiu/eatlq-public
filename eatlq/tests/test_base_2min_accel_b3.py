from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts/research"))
from base_2min_accel_b3_core import (  # noqa: E402
    classify_verdict, execute_path, newly_positive, reserve_net,
)


class B3CoreTest(unittest.TestCase):
    def test_transfer_net_reserve_exact_integer(self):
        events = [
            {"block_number": 10, "sender": "0x1", "recipient": "0xpool", "amount": "100"},
            {"block_number": 11, "sender": "0xpool", "recipient": "0x2", "amount": "7"},
            {"block_number": 13, "sender": "0x1", "recipient": "0xpool", "amount": "999"},
        ]
        self.assertEqual(reserve_net(events, "0xpool", 12), 93)

    def test_holder_is_positive_balance_not_distinct_recipient(self):
        events = [
            {"block_number": 10, "sender": "0x0", "recipient": "0xa", "amount": "5"},
            {"block_number": 11, "sender": "0xa", "recipient": "0xb", "amount": "5"},
            {"block_number": 12, "sender": "0x0", "recipient": "0xc", "amount": "2"},
        ]
        h1, h2 = newly_positive(events, 9, 11, 12, {"0x0"})
        self.assertEqual(h1, {"0xb"})
        self.assertEqual(h2, {"0xc"})

    def test_no_forward_event(self):
        events = [
            {"block_number": 12, "sender": "0x1", "recipient": "0xpool", "amount": "8"},
            {"block_number": 13, "sender": "0x1", "recipient": "0xpool", "amount": "999"},
        ]
        self.assertEqual(reserve_net(events, "0xpool", 12), 8)

    def test_unfilled_itt_is_exact_zero(self):
        row = execute_path([], 1000, 2000.0)
        self.assertEqual(row["filled"], False)
        self.assertEqual(row["net_return"], 0.0)

    def test_unsellable_is_minus_one_before_gas(self):
        swaps = [{"ts": 1001, "side": "buy", "weth": .01, "price": 1e-8}]
        row = execute_path(swaps, 1000, 2000.0)
        self.assertEqual(row["gross_return"], -1.0)
        self.assertEqual(row["gas_legs"], 2)
        self.assertLess(row["net_return"], -1.0)

    def test_small_n_negative_never_fail(self):
        rows = {
            "W1": {"n": 5, "mean": -1, "alpha_delta": -1},
            "W2": {"n": 0, "mean": 0, "alpha_delta": 0},
            "W3": {"n": 25, "mean": .1, "alpha_delta": .05,
                   "top1_removed_mean": .05, "top1_removed_alpha": .01},
        }
        self.assertEqual(classify_verdict(rows), "NEEDS_REVIEW/INCONCLUSIVE")

    def test_powered_w3_collapse_can_fail(self):
        rows = {"W3": {"n": 25, "mean": 0, "alpha_delta": .1,
                       "top1_removed_mean": .1, "top1_removed_alpha": .1}}
        self.assertEqual(classify_verdict(rows), "FAIL:W3_COLLAPSE")

    def test_windows_are_pairwise_disjoint(self):
        windows = [("2026-02-27 05:41:15", "2026-04-01 00:00:00"),
                   ("2026-04-01 00:00:00", "2026-05-01 00:00:00"),
                   ("2026-05-01 00:00:00", "2026-06-08 18:29:40")]
        intersections = [max(a[0], b[0]) < min(a[1], b[1])
                         for i, a in enumerate(windows) for b in windows[i+1:]]
        self.assertEqual(intersections, [False, False, False])

    def test_real_script_has_no_forbidden_pricing_call(self):
        source = (REPO / "scripts/research/base_2min_accel_b3.py").read_text()
        self.assertEqual("received_at_ms" in source, False)
        self.assertEqual("3d4e44eb1374240ce5f1b871ab261cd16335b76a" in source, False)

    def test_archive_golden_exact_equalities(self):
        fixture = json.loads((REPO / "tests/fixtures/base_2min_accel_b3_golden.json").read_text())
        locked = [x for x in fixture["cases"] if x["kind"] == "locked_standard"]
        self.assertEqual(len(locked), 3)
        for case in locked:
            reserve = case["reserve"]
            self.assertEqual(int(reserve["weth_reconstructed"]), int(reserve["weth_balance_of"]))
            self.assertEqual(int(reserve["target_reconstructed"]), int(reserve["target_balance_of"]))
            self.assertEqual(case["holder_reconstructed_count"], case["holder_balance_of_positive_count"])
            for holder in case["holder_checks"]:
                self.assertEqual(int(holder["reconstructed"]), int(holder["balance_of"]))

    def test_fee_on_sell_golden_is_present_and_not_in_universe(self):
        fixture = json.loads((REPO / "tests/fixtures/base_2min_accel_b3_golden.json").read_text())
        sentinels = [x for x in fixture["cases"] if x["kind"] == "fee_on_sell_sentinel_outside_locked_universe"]
        self.assertEqual(len(sentinels), 1)
        self.assertEqual(sentinels[0]["cached_honeypot_sell_tax_pct"], 100)


if __name__ == "__main__":
    unittest.main()
