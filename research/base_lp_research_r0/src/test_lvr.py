import unittest
from datetime import datetime, timedelta, timezone

from src.lvr import assert_lvr_invariants, daily_lvr, realized_lvr_path


class LVRTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.times = [start + timedelta(days=i) for i in range(4)]

    def test_lvr_is_nonnegative_and_monotonic(self):
        points = realized_lvr_path(
            self.times,
            [100, 110, 90, 105],
            liquidity_human=50,
            price_lower=80,
            price_upper=120,
        )
        assert_lvr_invariants(points)
        self.assertTrue(all(point.increment_quote >= 0 for point in points))
        cumulative = [point.cumulative_quote for point in points]
        self.assertEqual(cumulative, sorted(cumulative))

    def test_constant_price_has_zero_lvr(self):
        points = realized_lvr_path(
            self.times,
            [100] * 4,
            liquidity_human=50,
            price_lower=80,
            price_upper=120,
        )
        self.assertEqual(points[-1].cumulative_quote, 0)

    def test_out_of_range_has_zero_lvr(self):
        points = realized_lvr_path(
            self.times,
            [200, 210, 190, 205],
            liquidity_human=50,
            price_lower=80,
            price_upper=120,
        )
        self.assertEqual(points[-1].cumulative_quote, 0)

    def test_formula_for_one_increment(self):
        points = realized_lvr_path(
            self.times[:2],
            [100, 100 * 1.01],
            liquidity_human=40,
            price_lower=50,
            price_upper=150,
        )
        self.assertGreater(points[-1].cumulative_quote, 0)

    def test_daily_aggregation(self):
        points = realized_lvr_path(
            self.times,
            [100, 101, 99, 102],
            liquidity_human=40,
            price_lower=50,
            price_upper=150,
        )
        totals = daily_lvr(points)
        self.assertEqual(len(totals), 4)
        self.assertAlmostEqual(sum(totals.values()), points[-1].cumulative_quote)


if __name__ == "__main__":
    unittest.main()
