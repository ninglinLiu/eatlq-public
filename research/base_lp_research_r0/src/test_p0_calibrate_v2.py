import unittest

from src.p0_calibrate_v2 import Fenwick, PositionState, accrue, settle_collect


class FenwickTests(unittest.TestCase):
    def test_range_is_lower_inclusive_upper_exclusive(self):
        tree = Fenwick()
        tree.add(-10, 1.0)
        tree.add(0, 2.0)
        tree.add(10, 4.0)
        self.assertEqual(tree.range_sum(-10, 10), 3.0)
        self.assertEqual(tree.range_sum(0, 11), 6.0)

    def test_position_accrues_liquidity_times_growth(self):
        growth0, growth1 = Fenwick(), Fenwick()
        state = PositionState(known=True, liquidity=100)
        accrue(state, -10, 10, growth0, growth1)
        growth0.add(0, 0.25)
        growth1.add(0, 0.5)
        accrue(state, -10, 10, growth0, growth1)
        self.assertAlmostEqual(state.simulated_total0, 25)
        self.assertAlmostEqual(state.simulated_total1, 50)

    def test_partial_collect_carries_unrecovered_burn_principal(self):
        self.assertEqual(settle_collect(60, 100), (60, 0, 40))
        self.assertEqual(settle_collect(55, 40), (40, 15, 0))


if __name__ == "__main__":
    unittest.main()
