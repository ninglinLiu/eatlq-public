"""Sprint092 v2 — assert null-overlay path is whitepaper-faithful and bug-free.

Covers all 8 review fixes:
  1. tick→return = 1.0001^Δtick
  2. direction sign × per-pool target side
  3. single-buy $100k event gate (not cumulative wallet)
  4. no whale.csv / no PIT fallback in scoring
  5. day-paginated discovery (no LIMIT truncation symptom)
  6. is_buy = target-leg outflow
  7. honeypot pre-signal sell-path check exists
  8. USD-normalized features
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WPV0 = ROOT / "scripts" / "wallet_profile_v0"
sys.path.insert(0, str(WPV0))
sys.path.insert(0, str(ROOT / "services" / "strategy" / "src"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestNoOverlayPath(unittest.TestCase):
    STRATEGY = WPV0 / "sprint092_null_overlay_strategy.py"
    EXPANDER = WPV0 / "sprint092_universe_expander.py"

    FORBIDDEN_IMPORTS = (
        "discover_historical_onsets",
        "pivot_pool_fusion",
        "compute_smart_money",
        "sprint091_sender_gated_paper",
    )
    FORBIDDEN_NAMES = (
        "min_distinct_buyers",
        "ONSET_THRESH",
        "ONSET_MAX",
        "fusion_score",
    )

    # ---- contract tests: no overlays ----

    def test_no_forbidden_imports(self) -> None:
        for path in (self.STRATEGY, self.EXPANDER):
            src = _read(path)
            for name in self.FORBIDDEN_IMPORTS + self.FORBIDDEN_NAMES:
                self.assertNotIn(name, src, f"{path.name} must not reference {name}")

    def test_no_entropy_in_score_formula(self) -> None:
        """Ensure no entropy variable is consumed in scoring (docstrings allowed to mention it)."""
        import ast
        src = _read(self.STRATEGY)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_final_score_null_overlay":
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Name, ast.Attribute, ast.Constant)):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            if sub.value in ("unique_buyer_entropy_5m",):
                                self.fail("entropy key referenced in scoring")
                        elif isinstance(sub, ast.Name) and "entropy" in sub.id.lower():
                            self.fail(f"entropy variable {sub.id} in scoring")
                        elif isinstance(sub, ast.Attribute) and "entropy" in sub.attr.lower():
                            self.fail(f"entropy attribute {sub.attr} in scoring")
                return
        self.fail("compute_final_score_null_overlay not found")

    def test_mode_a_weights_formula(self) -> None:
        src = _read(self.STRATEGY)
        self.assertIn("0.40 * whale_score + 0.30 * momentum + 0.20 * activity - 0.10 * risk", src)

    # ---- whitepaper constants ----

    def test_whitepaper_thresholds(self) -> None:
        from sprint092_universe_expander import (
            BASE_CAPITAL_USD,
            POOL_MAX_AGE_SEC,
            SIGNAL_THRESHOLD,
            WHALE_USD_THRESH,
            FEE_TIERS_BPS,
        )
        self.assertEqual(SIGNAL_THRESHOLD, 0.52)
        self.assertEqual(BASE_CAPITAL_USD, 25.0)
        self.assertEqual(POOL_MAX_AGE_SEC, 72 * 3600)
        self.assertEqual(WHALE_USD_THRESH, 100_000)
        self.assertEqual(set(FEE_TIERS_BPS), {30, 100, 500, 3000, 10000})

    # ---- fix 1: tick→return log formula ----

    def test_tick_delta_to_return_uses_log_price(self) -> None:
        from sprint092_universe_expander import tick_delta_to_return
        # +1000 ticks ≈ +10.5% price (token0 target)
        r = tick_delta_to_return(1000, +1)
        expected = math.pow(1.0001, 1000) - 1
        self.assertAlmostEqual(r, expected, places=6)
        self.assertGreater(r, 0.10)  # not the linear 1000/195000=0.005

    # ---- fix 2: direction sign ----

    def test_tick_delta_to_return_sign_flips_with_direction(self) -> None:
        from sprint092_universe_expander import tick_delta_to_return
        r_target0 = tick_delta_to_return(500, +1)
        r_target1 = tick_delta_to_return(500, -1)
        self.assertAlmostEqual(r_target0, -r_target1, places=8)
        self.assertGreater(r_target0, 0)
        self.assertLess(r_target1, 0)

    # ---- fix 3: single-buy $100k event gate (not cumulative) ----

    def test_whale_score_is_event_based_not_wallet_tag(self) -> None:
        from sprint092_null_overlay_strategy import whale_score_from_swap
        self.assertEqual(whale_score_from_swap(50_000), 0.0)
        self.assertEqual(whale_score_from_swap(99_999.99), 0.0)
        self.assertEqual(whale_score_from_swap(100_000), 1.0)
        self.assertEqual(whale_score_from_swap(5_000_000), 1.0)

    def test_strategy_does_not_import_whale_csv_path(self) -> None:
        """No imports of cumulative whale.csv loaders (WHALE_USD_THRESH constant is OK)."""
        import ast
        src = _read(self.STRATEGY)
        tree = ast.parse(src)
        forbidden = {"load_whale_senders", "load_whale_at", "load_wallet_tags",
                     "WHALE_CSV", "_whale_csv_addrs", "w_score_null_overlay"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden,
                                     f"strategy must not import {alias.name}")
            if isinstance(node, (ast.Name, ast.Attribute)):
                ident = node.id if isinstance(node, ast.Name) else node.attr
                self.assertNotIn(ident, forbidden,
                                 f"strategy must not reference {ident}")

    # ---- fix 4: no PIT fallback / no look-ahead ----

    def test_strategy_does_not_use_pit_labels(self) -> None:
        src = _read(self.STRATEGY)
        self.assertNotIn("all_labels_pit_", src)
        self.assertNotIn("PIT_LABELS_DIR", src)
        self.assertNotIn("load_whale_at", src)
        self.assertNotIn("load_wallet_tags", src)

    # ---- fix 5: day-paginated discovery ----

    def test_discovery_is_day_paginated(self) -> None:
        src = _read(self.EXPANDER)
        self.assertIn("for d in range(days)", src)
        self.assertIn("_discover_day", src)
        # No DESC LIMIT 2000 pattern from v1
        self.assertNotIn("ORDER BY signal_ts DESC", src)

    # ---- fix 6: is_buy quote-aware ----

    def test_is_buy_target_aware_in_sql(self) -> None:
        src = _read(self.EXPANDER)
        # Must branch on quote_side, not blanket amount0<0
        self.assertIn("pm.quote_side = 1 AND s.amount0 < 0", src)
        self.assertIn("pm.quote_side = 0 AND s.amount1 < 0", src)

    # ---- fix 7: honeypot pre-filter ----

    def test_honeypot_prefilter_function_exists(self) -> None:
        from sprint092_universe_expander import (
            HONEYPOT_LOOKBACK_SEC,
            check_pre_signal_sell_path,
        )
        self.assertTrue(callable(check_pre_signal_sell_path))
        self.assertEqual(HONEYPOT_LOOKBACK_SEC, 6 * 3600)

    def test_strategy_invokes_honeypot_check(self) -> None:
        src = _read(self.STRATEGY)
        self.assertIn("check_pre_signal_sell_path", src)
        self.assertIn("honeypot_no_pre_sells", src)

    # ---- fix 8: USD-normalized features ----

    def test_features_use_usd_normalization(self) -> None:
        from sprint092_null_overlay_strategy import compute_features_usd_normalized
        rows = [
            {"abs_usd": 150000.0, "is_buy": True},
            {"abs_usd": 50000.0, "is_buy": False},
            {"abs_usd": 75000.0, "is_buy": True},
            {"abs_usd": 25000.0, "is_buy": False},
        ]
        f = compute_features_usd_normalized(rows)
        # net = 150k+75k - 50k-25k = 150k; total = 300k; velocity = 0.5
        self.assertAlmostEqual(f["price_velocity_5m"], 0.5, places=4)
        # buy_pressure = 2/4 = 0.5
        self.assertAlmostEqual(f["buy_pressure_ratio"], 0.5, places=4)
        self.assertEqual(f["activity_heat"], 4)
        self.assertGreater(f["token_volatility_5m"], 0)
        self.assertLess(f["token_volatility_5m"], 2.0)

    def test_empty_features_safe_defaults(self) -> None:
        from sprint092_null_overlay_strategy import compute_features_usd_normalized
        f = compute_features_usd_normalized([])
        self.assertEqual(f["price_velocity_5m"], 0.0)
        self.assertEqual(f["buy_pressure_ratio"], 0.5)
        self.assertEqual(f["token_volatility_5m"], 0.0)
        self.assertEqual(f["activity_heat"], 0)

    # ---- score integration ----

    def test_final_score_under_event_trigger(self) -> None:
        from sprint092_null_overlay_strategy import compute_final_score_null_overlay
        # Triggering buy at $150k → whale=1.0; features moderate
        out = compute_final_score_null_overlay(
            notional_usd=150_000,
            features={
                "price_velocity_5m": 0.5,
                "activity_heat": 50,
                "token_volatility_5m": 0.8,
                "buy_pressure_ratio": 0.6,
            },
        )
        # 0.40*1.0 + 0.30*0.5 + 0.20*0.5 - 0.10*0 = 0.65
        self.assertAlmostEqual(out["final_score"], 0.65, places=4)
        self.assertEqual(out["whale_score"], 1.0)

    def test_final_score_below_whale_thresh_kills_signal(self) -> None:
        from sprint092_null_overlay_strategy import compute_final_score_null_overlay
        out = compute_final_score_null_overlay(
            notional_usd=50_000,  # below threshold
            features={
                "price_velocity_5m": 0.5,
                "activity_heat": 50,
                "token_volatility_5m": 0.8,
            },
        )
        # 0 + 0.15 + 0.10 - 0 = 0.25 → below SIGNAL_THRESHOLD 0.52
        self.assertAlmostEqual(out["final_score"], 0.25, places=4)
        self.assertEqual(out["whale_score"], 0.0)

    # ---- exit simulator correctness ----

    def test_simulate_exit_hard_stop_uses_log_price(self) -> None:
        from sprint092_null_overlay_strategy import simulate_exit
        # 200 ticks down with direction=+1 → price ratio 1.0001^-200 ≈ 0.9802 → -1.98%
        # Not hit hard_stop (15%) → horizon_end
        path = [{"ts": 100, "tick": -200.0}]
        out = simulate_exit(
            entry_tick=0.0,
            tick_path=path,
            entry_ts=0,
            position_usd=100.0,
            direction=+1,
        )
        self.assertEqual(out["exit_reason"], "horizon_end")
        self.assertAlmostEqual(out["return_pct"], math.pow(1.0001, -200) - 1, places=6)

    def test_simulate_exit_direction_inverts_pnl(self) -> None:
        from sprint092_null_overlay_strategy import simulate_exit
        path = [{"ts": 100, "tick": 500.0}]
        out_target0 = simulate_exit(0.0, path, 0, 100.0, +1)
        out_target1 = simulate_exit(0.0, path, 0, 100.0, -1)
        self.assertGreater(out_target0["return_pct"], 0)
        self.assertLess(out_target1["return_pct"], 0)
        self.assertAlmostEqual(out_target0["return_pct"], -out_target1["return_pct"], places=8)

    def test_simulate_exit_real_hard_stop(self) -> None:
        from sprint092_null_overlay_strategy import simulate_exit
        # -2000 ticks with direction=+1 → 1.0001^-2000 - 1 ≈ -18% → hits hard_stop 15%
        path = [{"ts": 60, "tick": -2000.0}]
        out = simulate_exit(0.0, path, 0, 100.0, +1)
        self.assertEqual(out["exit_reason"], "hard_stop")
        self.assertLess(out["return_pct"], -0.15)


if __name__ == "__main__":
    unittest.main()
