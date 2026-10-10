#!/usr/bin/env python3
"""V3 Rescue unit tests: bans, registry, monthly wallet accounting, verdict vocabulary."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import assert_no_banned_features, load_bans  # noqa: E402
from src.v3.dual_head_model import compute_utility  # noqa: E402
from src.v3.hypothesis_registry import load_registry, validate_registry  # noqa: E402
from src.v3.oof_eval import ALLOWED_VERDICTS  # noqa: E402
from src.v3.wallet_monthly import simulate_wallet_monthly  # noqa: E402


class TestV3HardBans(unittest.TestCase):
    def test_bans_include_g2_c3(self):
        bans = load_bans()
        self.assertIn("g2_", bans["banned_feature_column_prefixes"])
        self.assertIn("c3_", bans["banned_feature_column_prefixes"])
        self.assertIn("venue_rejection", bans["banned_actions"])

    def test_rejects_g2_score(self):
        with self.assertRaises(ValueError):
            assert_no_banned_features(["flow_persist_score", "g2_score"])

    def test_rejects_c3_selected(self):
        with self.assertRaises(ValueError):
            assert_no_banned_features(["c3_selected"])


class TestRegistry(unittest.TestCase):
    def test_registry_loads(self):
        reg = load_registry()
        self.assertGreaterEqual(len(reg["hypotheses"]), 6)
        validate_registry(reg)


class TestWalletMonthly(unittest.TestCase):
    def test_no_compound_withdraw(self):
        # two months: earn then earn again; withdrawn profits must not grow capital
        rows = pd.DataFrame(
            {
                "signal_id": ["a", "b", "c"],
                "decision_time": [
                    int(pd.Timestamp("2025-10-05", tz="UTC").timestamp()),
                    int(pd.Timestamp("2025-10-20", tz="UTC").timestamp()),
                    int(pd.Timestamp("2025-11-10", tz="UTC").timestamp()),
                ],
                "label_end_time": [
                    int(pd.Timestamp("2025-10-05", tz="UTC").timestamp()) + 3600,
                    int(pd.Timestamp("2025-10-20", tz="UTC").timestamp()) + 3600,
                    int(pd.Timestamp("2025-11-10", tz="UTC").timestamp()) + 3600,
                ],
                "policy_selected": [True, True, True],
                "wallet_pnl_weth": [0.05, 0.05, 0.05],
            }
        )
        initial = 0.3333333333333333
        curve, monthly, summary = simulate_wallet_monthly(
            rows,
            initial_weth=initial,
            eth_usd=3000.0,
            amount_in_weth=0.016666666666666666,
            max_concurrent=3,
            monthly_target_usd=1000.0,
            compound=False,
        )
        self.assertFalse(summary["compound"])
        # after each month withdraw, wallet returns near initial
        self.assertAlmostEqual(summary["final_wallet_weth"], initial, places=6)
        self.assertGreater(summary["cum_withdrawn_weth"], 0.0)
        # withdrawn equals sum of positive month profits approximately
        self.assertAlmostEqual(summary["cum_withdrawn_weth"], float(rows["wallet_pnl_weth"].sum()), places=5)


class TestUtilityAndVerdicts(unittest.TestCase):
    def test_utility_has_decomposition_inputs(self):
        pred = pd.DataFrame(
            {
                "p_buy_fail": [0.1],
                "p_future_unsellable": [0.2],
                "p_liquidity_collapse": [0.1],
                "p_infrastructure_unknown": [0.0],
                "p_current_unsellable": [0.0],
                "expected_recovery_fraction": [0.1],
                "conditional_q50_return": [0.2],
                "expected_mfe": [0.3],
                "p_right_tail_25": [0.4],
                "entry_slip_proxy": [0.05],
            }
        )
        u = compute_utility(pred, gas_weth=0.00008, amount_in_weth=0.0167)
        self.assertEqual(len(u), 1)
        self.assertTrue(np.isfinite(u.iloc[0]))

    def test_verdict_whitelist(self):
        freeze = json.loads((ROOT / "configs" / "v3_rescue_freeze.json").read_text())
        self.assertEqual(set(freeze["allowed_verdicts"]), ALLOWED_VERDICTS)


if __name__ == "__main__":
    unittest.main()
