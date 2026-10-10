"""v0.2 executable quotes: V4Quoter eth_call, else real Swap fill proxy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from eth_abi import decode, encode
from eth_hash.auto import keccak

from .rpc_public import PublicRpcClient

V4_QUOTER = "0x0d5e0F971ED27FBfF6c2837bf31316121532048d"
STATE_VIEW = "0xA3c0c9b65baD0b08107Aa264b0f3dB444B867A71"
DYNAMIC_FEE_FLAG = 0x800000

QUOTE_EXACT_INPUT_SINGLE_SEL = keccak(
    b"quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
)[:4]
GET_SLOT0_SEL = keccak(b"getSlot0(bytes32)")[:4]
GET_LIQUIDITY_SEL = keccak(b"getLiquidity(bytes32)")[:4]


@dataclass
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def as_tuple(self) -> tuple:
        return (
            self.currency0,
            self.currency1,
            self.fee,
            self.tick_spacing,
            self.hooks,
        )


@dataclass
class QuoteResult:
    ok: bool
    method: str
    amount_in: int
    amount_out: int
    quote_block: Optional[int]
    transaction_hash: Optional[str]
    log_index: Optional[int]
    eth_call_block: Optional[int]
    fee_raw: Optional[int]
    fee_mode: str
    sqrt_price_x96: Optional[int]
    liquidity: Optional[int]
    price_impact: Optional[float]
    revert_error: Optional[str] = None
    size_ratio_vs_proxy: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def paired_decimals(paired: str) -> int:
    return 6 if paired.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913" else 18


def notional_to_amount(paired: str, notional_usd: float, eth_usd: Optional[float]) -> tuple[Optional[int], str]:
    p = paired.lower()
    if p == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913":
        return int(notional_usd * 1_000_000), "USDC"
    if p == "0x4200000000000000000000000000000000000006":
        if eth_usd is None or eth_usd <= 0:
            # ETH-denominated primary: interpret notional as USD-equivalent units only if eth_usd known
            return None, "ETH_USD_UNKNOWN"
        eth = notional_usd / eth_usd
        return int(eth * 1e18), "WETH"
    return None, "UNSUPPORTED_PAIR"


def try_quoter(
    rpc: PublicRpcClient,
    pool: PoolKey,
    *,
    zero_for_one: bool,
    amount_in: int,
    block: int,
) -> QuoteResult:
    params = (pool.as_tuple(), zero_for_one, amount_in, b"")
    data = "0x" + (QUOTE_EXACT_INPUT_SINGLE_SEL + encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"], [params]
    )).hex()
    try:
        raw = rpc.call("eth_call", [{"to": V4_QUOTER, "data": data}, hex(block)])
        if not raw or raw == "0x":
            return QuoteResult(
                False, "V4_QUOTER", amount_in, 0, block, None, None, block,
                pool.fee, "DYNAMIC" if pool.fee & DYNAMIC_FEE_FLAG else "STATIC",
                None, None, None, revert_error="empty_return",
            )
        amount_out, gas_est = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
        fee_mode = "DYNAMIC_FLAG" if pool.fee & DYNAMIC_FEE_FLAG else "STATIC"
        return QuoteResult(
            True, "V4_QUOTER", amount_in, int(amount_out), block, None, None, block,
            pool.fee, fee_mode, None, None, None,
        )
    except Exception as exc:  # noqa: BLE001
        return QuoteResult(
            False, "V4_QUOTER", amount_in, 0, block, None, None, block,
            pool.fee, "DYNAMIC" if pool.fee & DYNAMIC_FEE_FLAG else "STATIC",
            None, None, None, revert_error=str(exc)[:300],
        )


def slot0_liquidity(rpc: PublicRpcClient, pool_id: str, block: int) -> tuple[Optional[int], Optional[int], Optional[int]]:
    pid = pool_id[2:].rjust(64, "0")
    try:
        s0 = rpc.call("eth_call", [{"to": STATE_VIEW, "data": "0x" + GET_SLOT0_SEL.hex() + pid}, hex(block)])
        liq = rpc.call("eth_call", [{"to": STATE_VIEW, "data": "0x" + GET_LIQUIDITY_SEL.hex() + pid}, hex(block)])
        sqrt_p, tick, _pr, _lpfee = decode(["uint160", "int24", "uint24", "uint24"], bytes.fromhex(s0[2:]))
        liquidity = int(liq, 16) if liq and liq != "0x" else 0
        return int(sqrt_p), int(tick), liquidity
    except Exception:  # noqa: BLE001
        return None, None, None


def swap_fill_proxy(
    *,
    swap: dict[str, Any],
    token_is_token0: bool,
    target_amount_in: int,
    pool_fee: int,
) -> QuoteResult:
    """Use a real Swap's paired/token amounts as executable print proxy."""
    a0 = int(swap["amount0"])
    a1 = int(swap["amount1"])
    # V4 Swap event amounts are the swap caller's deltas: positive is received
    # by the caller, negative is paid by the caller.  This is also the sign
    # convention of BalanceDelta returned by PoolManager.swap.
    if token_is_token0:
        if not (a0 > 0 and a1 < 0):
            return QuoteResult(False, "REAL_SWAP_FILL_PROXY", target_amount_in, 0, swap["block_number"],
                               swap.get("transaction_hash"), swap.get("log_index"), None,
                               swap.get("fee"), "SWAP_EVENT", swap.get("sqrt_price_x96"),
                               swap.get("liquidity"), None, revert_error="swap_not_buy_side")
        amount_in = -a1
        amount_out = a0
    else:
        if not (a1 > 0 and a0 < 0):
            return QuoteResult(False, "REAL_SWAP_FILL_PROXY", target_amount_in, 0, swap["block_number"],
                               swap.get("transaction_hash"), swap.get("log_index"), None,
                               swap.get("fee"), "SWAP_EVENT", swap.get("sqrt_price_x96"),
                               swap.get("liquidity"), None, revert_error="swap_not_buy_side")
        amount_in = -a0
        amount_out = a1
    ratio = (amount_in / target_amount_in) if target_amount_in else None
    # scale out linearly (documented approximation; size mismatch penalized in costs)
    scaled_out = int(amount_out * (target_amount_in / amount_in)) if amount_in else 0
    fee_mode = "DYNAMIC_FLAG" if pool_fee & DYNAMIC_FEE_FLAG else "SWAP_EVENT_FEE"
    return QuoteResult(
        True, "REAL_SWAP_FILL_PROXY", target_amount_in, scaled_out,
        int(swap["block_number"]), swap.get("transaction_hash"), swap.get("log_index"), None,
        int(swap.get("fee") or pool_fee), fee_mode,
        swap.get("sqrt_price_x96"), swap.get("liquidity"), None,
        size_ratio_vs_proxy=ratio,
    )
