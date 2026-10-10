import math
import unittest

from src.lp_simulator import (
    FrictionBreakdown,
    SwapObservation,
    implied_share,
    liquidity_for_value_quote,
    position_amounts,
    position_value_quote,
    raw_liquidity_scale,
    raw_position_amounts,
    simulate_static_position,
    sqrt_price_x96_to_price,
    tick_to_price,
)


class PositionMathTests(unittest.TestCase):
    def test_raw_liquidity_scale_18_6(self):
        self.assertEqual(raw_liquidity_scale(18, 6), 1e12)

    def test_raw_liquidity_scale_18_18(self):
        self.assertEqual(raw_liquidity_scale(18, 18), 1e18)

    def test_tick_and_sqrt_price_agree(self):
        tick = -198000
        q96 = int((1.0001 ** (tick / 2)) * 2**96)
        self.assertAlmostEqual(
            tick_to_price(tick, 18, 6),
            sqrt_price_x96_to_price(q96, 18, 6),
            places=9,
        )

    def test_position_amount_regions(self):
        below = position_amounts(10, 0.5, 1, 4)
        inside = position_amounts(10, 2, 1, 4)
        above = position_amounts(10, 5, 1, 4)
        self.assertGreater(below[0], 0)
        self.assertEqual(below[1], 0)
        self.assertGreater(inside[0], 0)
        self.assertGreater(inside[1], 0)
        self.assertEqual(above[0], 0)
        self.assertGreater(above[1], 0)

    def test_liquidity_value_round_trip(self):
        liquidity = liquidity_for_value_quote(500, 2000, 1500, 2500)
        self.assertAlmostEqual(
            position_value_quote(liquidity, 2000, 1500, 2500), 500, places=10
        )

    def test_raw_burn_principal_matches_human_formula(self):
        tick_lower, tick_upper, tick = -200000, -190000, -195000
        sqrt_x96 = int(1.0001 ** (tick / 2) * 2**96)
        raw0, raw1 = raw_position_amounts(
            10**18, sqrt_x96, tick_lower, tick_upper
        )
        price = sqrt_price_x96_to_price(sqrt_x96, 18, 6)
        # L_raw=1e18 corresponds to L_human=1e6 for an 18/6 pair.
        human0, human1 = position_amounts(
            1e6,
            price,
            tick_to_price(tick_lower, 18, 6),
            tick_to_price(tick_upper, 18, 6),
        )
        self.assertLess(abs(raw0 / 1e18 - human0), 1e-9)
        self.assertLess(abs(raw1 / 1e6 - human1), 1e-6)


class CapacityAndFeeTests(unittest.TestCase):
    def test_share_is_not_capped(self):
        self.assertAlmostEqual(implied_share(50, 50), 0.5)

    def test_capacity_violation_marks_whole_sample(self):
        swaps = [
            SwapObservation("a", 0, 99_000, 1_000_000, -999_000),
            SwapObservation("b", 0, 49_000, 1_000_000, -999_000),
        ]
        # d0=d1=0 means my raw L is exactly 1,000.
        result = simulate_static_position(
            swaps,
            liquidity_human=1_000,
            price_lower=0.9,
            price_upper=1.1,
            decimals0=0,
            decimals1=0,
            fee_rate=0.003,
            initial_price=1.0,
        )
        self.assertEqual(result.status, "CAPACITY_VIOLATION")
        self.assertAlmostEqual(result.implied_share_p50, 0.015)
        self.assertAlmostEqual(result.implied_share_p99, 0.0199)
        self.assertEqual(result.capacity_violation_pct, 50.0)
        # The second 2% share remains 2%; it was not clipped to 1%.
        self.assertAlmostEqual(result.fee0, 90.0)

    def test_friction_components_are_individually_deducted(self):
        friction = FrictionBreakdown(1, 2, 3, 4, 5)
        result = simulate_static_position(
            [],
            liquidity_human=10,
            price_lower=0.5,
            price_upper=2,
            decimals0=0,
            decimals1=0,
            fee_rate=0.003,
            initial_price=1,
            frictions=friction,
        )
        self.assertEqual(friction.total_quote, 15)
        self.assertAlmostEqual(result.net_value_quote, result.position_value_quote_at_end - 15)


if __name__ == "__main__":
    unittest.main()
