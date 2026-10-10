"""Uniswap V4 hook address LSB permission flags (v4-core Hooks.sol)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

# Lower 14 bits of hook address encode permissions (Uniswap v4-core)
FLAGS = {
    "BEFORE_INITIALIZE": 1 << 13,
    "AFTER_INITIALIZE": 1 << 12,
    "BEFORE_ADD_LIQUIDITY": 1 << 11,
    "AFTER_ADD_LIQUIDITY": 1 << 10,
    "BEFORE_REMOVE_LIQUIDITY": 1 << 9,   # 0x200
    "AFTER_REMOVE_LIQUIDITY": 1 << 8,
    "BEFORE_SWAP": 1 << 7,               # 0x80
    "AFTER_SWAP": 1 << 6,                # 0x40
    "BEFORE_DONATE": 1 << 5,
    "AFTER_DONATE": 1 << 4,
    "BEFORE_SWAP_RETURNS_DELTA": 1 << 3,
    "AFTER_SWAP_RETURNS_DELTA": 1 << 2,
    "AFTER_ADD_LIQUIDITY_RETURNS_DELTA": 1 << 1,
    "AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA": 1 << 0,
}

RETURNS_DELTA_FLAGS = (
    "BEFORE_SWAP_RETURNS_DELTA",
    "AFTER_SWAP_RETURNS_DELTA",
    "AFTER_ADD_LIQUIDITY_RETURNS_DELTA",
    "AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA",
)

PRIVILEGED_FLAGS = RETURNS_DELTA_FLAGS + (
    "BEFORE_SWAP",
    "AFTER_SWAP",
    "BEFORE_REMOVE_LIQUIDITY",
)


@dataclass
class HookProfile:
    address: str
    flags_raw: int
    permissions: list[str]
    has_returns_delta: bool
    returns_delta_bits: list[str]
    is_vanilla: bool
    risk_tier: str  # vanilla | standard | returns_delta_high | unknown

    def to_dict(self) -> dict:
        return asdict(self)


def decode_hook(hooks_addr: str) -> HookProfile:
    """Decode hook permission matrix from address LSB bits."""
    if not hooks_addr or int(hooks_addr, 16) == 0:
        return HookProfile(
            address="0x0000000000000000000000000000000000000000",
            flags_raw=0,
            permissions=[],
            has_returns_delta=False,
            returns_delta_bits=[],
            is_vanilla=True,
            risk_tier="vanilla",
        )
    addr_int = int(hooks_addr, 16)
    flags_raw = addr_int & ((1 << 14) - 1)
    perms = [name for name, bit in FLAGS.items() if flags_raw & bit]
    rd_bits = [n for n in RETURNS_DELTA_FLAGS if n in perms]
    has_rd = bool(rd_bits)
    if has_rd:
        tier = "returns_delta_high"
    elif perms:
        tier = "standard"
    else:
        tier = "unknown"
    return HookProfile(
        address=hooks_addr.lower(),
        flags_raw=flags_raw,
        permissions=perms,
        has_returns_delta=has_rd,
        returns_delta_bits=rd_bits,
        is_vanilla=False,
        risk_tier=tier,
    )


def classify_hook_bucket(profile: HookProfile, *, template_match: Optional[str] = None) -> str:
    """Phase-2 bucket label."""
    if profile.is_vanilla:
        return "no_hook"
    if template_match:
        return f"known_template:{template_match}"
    if profile.has_returns_delta:
        return "returns_delta_high"
    if profile.risk_tier == "standard":
        return "standard_hook"
    return "unknown_hook"
