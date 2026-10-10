"""Auditable Uniswap-v3-style position mathematics.

The module deliberately has no RPC, wallet, or transaction functionality.  It
works on already-decoded swap observations.  Fee allocation is a counterfactual
estimate based on the active-liquidity value emitted by each swap; it must not
be described as calibrated until compared with fee-growth or Collect truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import median
from typing import Iterable, Sequence


Q96 = 2**96
CAPACITY_LIMIT = 0.01


def raw_liquidity_scale(decimals0: int, decimals1: int) -> float:
    """Scale human-unit liquidity into the uint128 liquidity unit.

    For human price token1/token0, L_raw = L_human *
    10**((decimals0 + decimals1) / 2).
    """

    if decimals0 < 0 or decimals1 < 0:
        raise ValueError("token decimals must be non-negative")
    return 10.0 ** ((decimals0 + decimals1) / 2.0)


def tick_to_price(tick: int | float, decimals0: int, decimals1: int) -> float:
    """Return human token1 per token0 from a pool tick."""

    return 1.0001**tick * 10.0 ** (decimals0 - decimals1)


def sqrt_price_x96_to_price(
    sqrt_price_x96: int, decimals0: int, decimals1: int
) -> float:
    """Return human token1 per token0 from the Q64.96 pool price."""

    if sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 must be positive")
    raw_price = (sqrt_price_x96 / Q96) ** 2
    return raw_price * 10.0 ** (decimals0 - decimals1)


def position_amounts(
    liquidity: float, price: float, price_lower: float, price_upper: float
) -> tuple[float, float]:
    """Return (token0, token1) amounts for human-unit liquidity."""

    if not (liquidity >= 0 and 0 < price_lower < price_upper and price > 0):
        raise ValueError("invalid liquidity or price range")
    sp, sa, sb = sqrt(price), sqrt(price_lower), sqrt(price_upper)
    if sp <= sa:
        return liquidity * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, liquidity * (sb - sa)
    return (
        liquidity * (sb - sp) / (sp * sb),
        liquidity * (sp - sa),
    )


def position_value_quote(
    liquidity: float, price: float, price_lower: float, price_upper: float
) -> float:
    """Mark a position in token1 (the quote unit)."""

    amount0, amount1 = position_amounts(liquidity, price, price_lower, price_upper)
    return amount0 * price + amount1


def liquidity_for_value_quote(
    value_quote: float, price: float, price_lower: float, price_upper: float
) -> float:
    """Solve for L at an in-range entry price and a quote-unit value."""

    if value_quote <= 0 or not price_lower < price < price_upper:
        raise ValueError("entry price must be inside the range and value positive")
    one_l_value = position_value_quote(1.0, price, price_lower, price_upper)
    return value_quote / one_l_value


def raw_position_amounts(
    liquidity_raw: int,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
) -> tuple[float, float]:
    """Approximate raw principal amounts represented by a Mint/Burn event.

    This mirrors the v3 whitepaper formulas in raw token units.  It is useful
    for auditing that Burn amounts are principal, not fee observations.
    """

    if liquidity_raw < 0 or sqrt_price_x96 <= 0 or tick_lower >= tick_upper:
        raise ValueError("invalid raw position arguments")
    sp = sqrt_price_x96 / Q96
    sa = 1.0001 ** (tick_lower / 2.0)
    sb = 1.0001 ** (tick_upper / 2.0)
    if sp <= sa:
        return liquidity_raw * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, liquidity_raw * (sb - sa)
    return (
        liquidity_raw * (sb - sp) / (sp * sb),
        liquidity_raw * (sp - sa),
    )


def implied_share(my_liquidity_raw: float, active_liquidity_raw: float) -> float:
    """Counterfactual pool share, with no clipping or silent cap."""

    if my_liquidity_raw < 0 or active_liquidity_raw < 0:
        raise ValueError("liquidity cannot be negative")
    total = my_liquidity_raw + active_liquidity_raw
    return my_liquidity_raw / total if total else 0.0


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    location = (len(ordered) - 1) * probability
    lo = int(location)
    hi = min(lo + 1, len(ordered) - 1)
    weight = location - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


@dataclass(frozen=True)
class SwapObservation:
    timestamp: str
    tick: int
    active_liquidity_raw: int
    amount0_raw: int
    amount1_raw: int


@dataclass(frozen=True)
class FrictionBreakdown:
    gas_quote: float = 0.0
    entry_swap_fee_quote: float = 0.0
    exit_swap_fee_quote: float = 0.0
    slippage_quote: float = 0.0
    rebalance_quote: float = 0.0

    @property
    def total_quote(self) -> float:
        return (
            self.gas_quote
            + self.entry_swap_fee_quote
            + self.exit_swap_fee_quote
            + self.slippage_quote
            + self.rebalance_quote
        )


@dataclass(frozen=True)
class SimulationResult:
    status: str
    fee0: float
    fee1: float
    fee_value_quote_at_end: float
    position_value_quote_at_end: float
    hodl_value_quote_at_end: float
    impermanent_loss_quote: float
    net_value_quote: float
    implied_share_p50: float
    implied_share_p99: float
    capacity_violation_pct: float
    observations: int
    frictions: FrictionBreakdown


def simulate_static_position(
    swaps: Iterable[SwapObservation],
    *,
    liquidity_human: float,
    price_lower: float,
    price_upper: float,
    decimals0: int,
    decimals1: int,
    fee_rate: float,
    initial_price: float,
    frictions: FrictionBreakdown | None = None,
    capacity_limit: float = CAPACITY_LIMIT,
) -> SimulationResult:
    """Estimate an unchanged position from per-swap observations.

    A sample is marked ``CAPACITY_VIOLATION`` if *any* eligible observation is
    over the threshold.  Shares are retained as computed and are never capped.
    Input-token fees are computed from gross positive swap deltas.  This is the
    model to calibrate, not chain truth.
    """

    if liquidity_human <= 0 or not 0 <= fee_rate < 1:
        raise ValueError("liquidity must be positive and fee_rate in [0, 1)")
    if not 0 < capacity_limit < 1:
        raise ValueError("capacity_limit must lie in (0, 1)")
    friction = frictions or FrictionBreakdown()
    my_raw = liquidity_human * raw_liquidity_scale(decimals0, decimals1)
    fee0 = fee1 = 0.0
    shares: list[float] = []
    final_price = initial_price

    for swap in swaps:
        price = tick_to_price(swap.tick, decimals0, decimals1)
        final_price = price
        if not price_lower <= price < price_upper:
            continue
        share = implied_share(my_raw, float(swap.active_liquidity_raw))
        shares.append(share)
        if swap.amount0_raw > 0:
            fee0 += swap.amount0_raw / 10**decimals0 * fee_rate * share
        elif swap.amount1_raw > 0:
            fee1 += swap.amount1_raw / 10**decimals1 * fee_rate * share

    entry0, entry1 = position_amounts(
        liquidity_human, initial_price, price_lower, price_upper
    )
    position_end = position_value_quote(
        liquidity_human, final_price, price_lower, price_upper
    )
    hodl_end = entry0 * final_price + entry1
    il = position_end - hodl_end
    fee_quote = fee0 * final_price + fee1
    violation_pct = (
        100.0 * sum(value > capacity_limit for value in shares) / len(shares)
        if shares
        else 0.0
    )
    status = "CAPACITY_VIOLATION" if violation_pct > 0 else "OK"
    net = position_end + fee_quote - friction.total_quote
    return SimulationResult(
        status=status,
        fee0=fee0,
        fee1=fee1,
        fee_value_quote_at_end=fee_quote,
        position_value_quote_at_end=position_end,
        hodl_value_quote_at_end=hodl_end,
        impermanent_loss_quote=il,
        net_value_quote=net,
        implied_share_p50=median(shares) if shares else float("nan"),
        implied_share_p99=_quantile(shares, 0.99),
        capacity_violation_pct=violation_pct,
        observations=len(shares),
        frictions=friction,
    )


def validate_result(result: SimulationResult) -> None:
    """Raise on non-finite or internally inconsistent simulator output."""

    numeric = (
        result.fee0,
        result.fee1,
        result.fee_value_quote_at_end,
        result.position_value_quote_at_end,
        result.hodl_value_quote_at_end,
        result.impermanent_loss_quote,
        result.net_value_quote,
        result.capacity_violation_pct,
    )
    if not all(isfinite(value) for value in numeric):
        raise ValueError("simulation contains a non-finite required value")
    if result.status == "OK" and result.capacity_violation_pct != 0:
        raise ValueError("capacity status is inconsistent")

