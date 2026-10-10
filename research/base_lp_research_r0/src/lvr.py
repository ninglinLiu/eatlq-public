"""Loss-versus-rebalancing for a Uniswap-v3 range position.

Milionis et al. (arXiv:2208.06046), Example 4, give instantaneous
LVR = L * sigma**2 * sqrt(P) / 4 while the position is in range.  For
an observed path, squared log-price increments estimate quadratic variation.
The result is denominated in token1 when P is token1 per token0 and L is in
human liquidity units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, log, sqrt
from typing import Sequence


@dataclass(frozen=True)
class LVRPoint:
    timestamp: datetime
    increment_quote: float
    cumulative_quote: float


def realized_lvr_path(
    timestamps: Sequence[datetime],
    prices: Sequence[float],
    *,
    liquidity_human: float,
    price_lower: float,
    price_upper: float,
) -> list[LVRPoint]:
    """Compute non-negative, cumulative ex-post LVR along a price path."""

    if len(timestamps) != len(prices):
        raise ValueError("timestamps and prices must have equal length")
    if liquidity_human < 0 or not 0 < price_lower < price_upper:
        raise ValueError("invalid liquidity or range")
    if any(price <= 0 for price in prices):
        raise ValueError("prices must be positive")
    if any(b < a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError("timestamps must be monotonic")
    if not prices:
        return []

    output = [LVRPoint(timestamps[0], 0.0, 0.0)]
    cumulative = 0.0
    for index in range(1, len(prices)):
        p0, p1 = prices[index - 1], prices[index]
        midpoint = exp((log(p0) + log(p1)) / 2.0)
        dlog = log(p1 / p0)
        increment = 0.0
        if price_lower <= midpoint < price_upper:
            increment = liquidity_human * sqrt(midpoint) * dlog * dlog / 4.0
        cumulative += increment
        output.append(LVRPoint(timestamps[index], increment, cumulative))
    return output


def assert_lvr_invariants(points: Sequence[LVRPoint], tolerance: float = 1e-15) -> None:
    """Enforce the non-negativity and monotonicity required by the paper."""

    previous = 0.0
    for point in points:
        if point.increment_quote < -tolerance:
            raise AssertionError("LVR increment is negative")
        if point.cumulative_quote + tolerance < previous:
            raise AssertionError("cumulative LVR decreased")
        previous = point.cumulative_quote


def daily_lvr(points: Sequence[LVRPoint]) -> dict[str, float]:
    """Aggregate LVR increments by UTC calendar date."""

    totals: dict[str, float] = {}
    for point in points:
        key = point.timestamp.date().isoformat()
        totals[key] = totals.get(key, 0.0) + point.increment_quote
    return totals

