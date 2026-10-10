"""Calibration statistics for simulated versus independently observed fees."""

from __future__ import annotations

from dataclasses import dataclass
from math import comb
from statistics import median
from typing import Sequence


@dataclass(frozen=True)
class CalibrationObservation:
    simulated_fee: float
    true_fee: float


@dataclass(frozen=True)
class CalibrationSummary:
    n: int
    median_relative_error: float
    positive_errors: int
    negative_errors: int
    sign_test_p_value: float
    passed: bool


def exact_two_sided_sign_test(positive: int, negative: int) -> float:
    """Exact two-sided binomial sign test, discarding tied observations."""

    if positive < 0 or negative < 0:
        raise ValueError("counts cannot be negative")
    n = positive + negative
    if n == 0:
        return 1.0
    tail = min(positive, negative)
    # The direct integer expression is useful and dependency-free for ordinary
    # unit-test/sample sizes.  R0b can produce hundreds of thousands of Collect
    # intervals, where constructing every huge binomial coefficient is needlessly
    # expensive.  SciPy evaluates the same exact binomial tail stably.
    if n <= 10_000:
        lower_probability = sum(comb(n, k) for k in range(tail + 1)) / 2**n
        return min(1.0, 2.0 * lower_probability)
    from scipy.stats import binomtest

    return float(binomtest(positive, n=n, p=0.5, alternative="two-sided").pvalue)


def summarize_calibration(
    observations: Sequence[CalibrationObservation],
    *,
    relative_error_threshold: float = 0.15,
    bias_p_threshold: float = 0.05,
) -> CalibrationSummary:
    """Apply the GOAL's median-error and no-systematic-bias gates."""

    usable = [observation for observation in observations if observation.true_fee > 0]
    if not usable:
        raise ValueError("independent positive fee truth is required for calibration")
    relative_errors = [
        abs(item.simulated_fee - item.true_fee) / item.true_fee for item in usable
    ]
    positive = sum(item.simulated_fee > item.true_fee for item in usable)
    negative = sum(item.simulated_fee < item.true_fee for item in usable)
    p_value = exact_two_sided_sign_test(positive, negative)
    median_error = median(relative_errors)
    return CalibrationSummary(
        n=len(usable),
        median_relative_error=median_error,
        positive_errors=positive,
        negative_errors=negative,
        sign_test_p_value=p_value,
        passed=median_error < relative_error_threshold and p_value > bias_p_threshold,
    )
