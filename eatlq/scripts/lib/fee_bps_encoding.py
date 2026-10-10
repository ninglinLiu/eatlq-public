"""discovered_pools.fee_bps encoding helpers.

discovered_pools stores fee in **basis-point units** (0.01% steps):
    1 → 0.01%, 5 → 0.05%, 30 → 0.30%, 100 → 1.00%

Uniswap V3 on-chain pool.fee() / Quoter use **protocol encoding**:
    100 → 0.01%, 500 → 0.05%, 3000 → 0.30%, 10000 → 1.00%

Historical scripts that used IN (100, 500, 3000, 10000) against discovered_pools
were filtering on the wrong encoding and silently dropped most pools.
"""

from __future__ import annotations

# Correct allowlist for SQL against eatlq.discovered_pools.fee_bps
FEE_BPS_META_STANDARD: tuple[int, ...] = (1, 5, 30, 100)

# Wrong encoding accidentally used in legacy scripts (protocol units in meta column)
FEE_BPS_LEGACY_PROTOCOL_MISTAKE: tuple[int, ...] = (100, 500, 3000, 10000)

PROTOCOL_TO_META: dict[int, int] = {100: 1, 500: 5, 3000: 30, 10000: 100}
META_TO_PROTOCOL: dict[int, int] = {v: k for k, v in PROTOCOL_TO_META.items()}


def fee_bps_sql_in(*, tiers: tuple[int, ...] | None = None) -> str:
    """Comma-separated list for `fee_bps IN (...)` against discovered_pools."""
    use = tiers if tiers is not None else FEE_BPS_META_STANDARD
    return ", ".join(str(int(x)) for x in use)


def meta_to_protocol_fee(fee_bps_meta: int) -> int:
    """Map discovered_pools.fee_bps → Quoter/protocol fee uint24."""
    if fee_bps_meta in META_TO_PROTOCOL:
        return META_TO_PROTOCOL[fee_bps_meta]
    # Fallback: assume value already protocol-encoded if it looks like a tier
    if fee_bps_meta in PROTOCOL_TO_META:
        return fee_bps_meta
    return max(fee_bps_meta * 100, 0)


def protocol_to_meta_fee(fee_protocol: int) -> int:
    """Map on-chain protocol fee → discovered_pools.fee_bps."""
    if fee_protocol in PROTOCOL_TO_META:
        return PROTOCOL_TO_META[fee_protocol]
    return max(fee_protocol // 100, 0)
