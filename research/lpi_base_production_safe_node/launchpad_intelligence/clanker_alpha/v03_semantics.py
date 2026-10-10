"""Corrected Uniswap v4 Swap caller-delta semantics (offline).

Positive event amount = currency received by the swap caller.
Negative event amount = currency paid by the swap caller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

# Official Uniswap v4-periphery Actions.sol
ACTION_SWAP_EXACT_IN_SINGLE = 0x06
ACTION_SETTLE_ALL = 0x0C
ACTION_TAKE_ALL = 0x0F
UR_COMMAND_V4_SWAP = 0x10

DYNAMIC_FEE_FLAG = 0x800000


@dataclass(frozen=True)
class SwapSideLabel:
    side: str  # BUY_TOKEN | SELL_TOKEN | UNKNOWN | AMBIGUOUS
    token_amount: int
    paired_amount: int
    zero_for_one: Optional[bool]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_token_swap(
    *,
    amount0: int,
    amount1: int,
    token: str,
    paired: str,
) -> SwapSideLabel:
    """Label a PoolManager Swap relative to the launch token."""
    token_l = token.lower()
    paired_l = paired.lower()
    token_is_token0 = int(token_l, 16) < int(paired_l, 16)
    if token_is_token0:
        # buy token0: receive token0 (>0), pay token1 (<0)
        if amount0 > 0 and amount1 < 0:
            return SwapSideLabel("BUY_TOKEN", amount0, -amount1, False, "caller_receives_token0")
        if amount0 < 0 and amount1 > 0:
            return SwapSideLabel("SELL_TOKEN", -amount0, amount1, True, "caller_pays_token0")
    else:
        # buy token1: receive token1 (>0), pay token0 (<0)
        if amount1 > 0 and amount0 < 0:
            return SwapSideLabel("BUY_TOKEN", amount1, -amount0, True, "caller_receives_token1")
        if amount1 < 0 and amount0 > 0:
            return SwapSideLabel("SELL_TOKEN", -amount1, amount0, False, "caller_pays_token1")
    return SwapSideLabel("UNKNOWN", 0, 0, None, "amounts_not_single_sided")


def old_incorrect_buy_predicate(amount0: int, amount1: int, token_is_token0: bool) -> bool:
    """The withdrawn v0.2 predicate (inverted). Used only in negative goldens."""
    if token_is_token0:
        return amount0 < 0 and amount1 > 0
    return amount1 < 0 and amount0 > 0
