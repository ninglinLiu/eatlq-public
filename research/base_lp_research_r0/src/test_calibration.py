import unittest

from src.calibration import (
    CalibrationObservation,
    exact_two_sided_sign_test,
    summarize_calibration,
)


class CalibrationTests(unittest.TestCase):
    def test_balanced_signs_are_not_biased(self):
        self.assertEqual(exact_two_sided_sign_test(5, 5), 1.0)

    def test_one_sided_errors_are_biased(self):
        self.assertLess(exact_two_sided_sign_test(10, 0), 0.05)

    def test_gate_passes_small_balanced_errors(self):
        observations = [
            CalibrationObservation(99, 100),
            CalibrationObservation(101, 100),
            CalibrationObservation(98, 100),
            CalibrationObservation(102, 100),
        ]
        result = summarize_calibration(observations)
        self.assertTrue(result.passed)
        self.assertLess(result.median_relative_error, 0.15)
        self.assertGreater(result.sign_test_p_value, 0.05)

    def test_missing_truth_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "independent positive fee truth"):
            summarize_calibration([])


if __name__ == "__main__":
    unittest.main()
