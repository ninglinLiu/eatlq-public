"""
Low-level swap execution for Base chain DEXes.

Supports:
- Uniswap V2 Router (swapExactETHForTokens / swapExactTokensForETH)
- Aerodrome vAMM Router (same ABI as V2 with `stable` bool parameter)
- Uniswap V3 SwapRouter02 (exactInputSingle)
- Aerodrome CL (same SwapRouter02 interface)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from eth_abi import encode
from web3 import Web3
from web3.types import TxReceipt

from registry import PoolInfo, WETH_BASE
from tx_sender import send_raw_transaction, wait_for_receipt

QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"

# WETH Withdrawal(address indexed dst, uint256 wad) — stored without 0x for comparison
_WETH_WITHDRAWAL_TOPIC_BARE = "7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
# ERC20 Transfer(address indexed from, address indexed to, uint256 value)
_TRANSFER_TOPIC_BARE = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _normalize_hex(val) -> str:
    """Convert HexBytes / bytes / str to lowercase hex without 0x prefix."""
    if isinstance(val, bytes):
        return val.hex().lower()
    s = str(val).lower()
    return s[2:] if s.startswith("0x") else s


def _extract_eth_from_receipt(receipt: "TxReceipt") -> int:
    """Parse WETH Withdrawal events from tx receipt to get exact ETH received.
    This is deterministic — no RPC balance propagation delay."""
    weth_bare = WETH_BASE.lower().replace("0x", "")
    total = 0
    for entry in receipt.get("logs", []):
        addr_raw = entry.get("address") or ""
        addr_bare = _normalize_hex(addr_raw)
        topics = entry.get("topics", [])
        if addr_bare != weth_bare or not topics:
            continue
        t0_bare = _normalize_hex(topics[0])
        if t0_bare == _WETH_WITHDRAWAL_TOPIC_BARE:
            raw = entry.get("data", b"")
            if isinstance(raw, bytes):
                total += int.from_bytes(raw, "big") if raw else 0
            elif isinstance(raw, int):
                total += raw
            else:
                cleaned = str(raw).lower().replace("0x", "")
                total += int(cleaned, 16) if cleaned else 0
            log.info("parsed WETH Withdrawal from receipt: %s wei", total)
    return total


# Local nonce tracker: prevents "nonce too low" when dRPC's pending count lags
_account_nonces: dict = {}


def _get_next_nonce(w3: Web3, account) -> int:
    """Return the next nonce, always > any locally tracked nonce to beat RPC lag.

    When the mempool holds nothing for this account (pending == latest) the local
    tracker is discarded and the chain nonce wins. EIP-7702 delegated accounts
    reject gapped nonces outright, so a tracker left inflated by failed sends
    would otherwise brick every subsequent transaction until process restart.
    """
    addr = account.address.lower()
    pending = w3.eth.get_transaction_count(account.address, "pending")
    latest = w3.eth.get_transaction_count(account.address, "latest")
    if pending == latest:
        nonce = pending
    else:
        nonce = max(pending, _account_nonces.get(addr, -1) + 1)
    _account_nonces[addr] = nonce
    return nonce


def _release_nonce(account, nonce: int) -> None:
    """Return a nonce to the pool after a send failed, so the next attempt reuses it."""
    addr = account.address.lower()
    if _account_nonces.get(addr) == nonce:
        _account_nonces[addr] = nonce - 1


_delegated_accounts: dict = {}


def _is_delegated(w3: Web3, account) -> bool:
    """True if the account carries an EIP-7702 delegation designator (0xef0100 || addr).

    The sequencer allows only one in-flight transaction per delegated account, so
    same-nonce fee replacements are always rejected for them.
    """
    addr = account.address.lower()
    cached = _delegated_accounts.get(addr)
    if cached is not None:
        return cached
    try:
        code = bytes(w3.eth.get_code(account.address))
    except Exception as e:
        log.warning("delegation check failed for %s: %s", account.address, e)
        return False
    delegated = len(code) == 23 and code[:3] == b"\xef\x01\x00"
    _delegated_accounts[addr] = delegated
    if delegated:
        log.warning(
            "account %s carries an EIP-7702 delegation to 0x%s — fee replacement is "
            "disabled (sequencer caps delegated accounts at one in-flight tx)",
            account.address, code[3:].hex(),
        )
    return delegated

PAIR_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"getReserves","outputs":[{"name":"_reserve0","type":"uint112"},{"name":"_reserve1","type":"uint112"},{"name":"_blockTimestampLast","type":"uint32"}],"type":"function"}]'
)

log = logging.getLogger(__name__)

ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

MAX_UINT256 = 2**256 - 1
DEFAULT_DEADLINE_OFFSET = 300  # 5 min


@dataclass
class SwapResult:
    success: bool
    tx_hash: Optional[str]
    amount_in: int
    amount_out: int
    gas_used: int
    gas_cost_wei: int
    error: Optional[str] = None
    cancelled: bool = False
    # Execution-quality instrumentation (P0.5): the QuoterV2 quote used to set
    # minOut and the mined block. Lets the caller measure realized slippage
    # (actual amount_out vs expected_out) and signal->on-chain block delay.
    expected_out: Optional[int] = None
    block_number: Optional[int] = None


def _deadline() -> int:
    return int(time.time()) + DEFAULT_DEADLINE_OFFSET


# Slippage tolerance per urgency/reason
_SLIPPAGE_TOLERANCE = {
    "buy": 0.03,           # 3% max slippage for buys (meme coins are volatile)
    "take_profit": 0.01,   # 1% for TP (not urgent, can be tight)
    "stop_loss": 0.05,     # 5% for SL (must execute, looser slippage)
    "hard_stop": 0.05,     # alias: monitor uses "hard_stop" as exit_reason
    "trailing": 0.02,      # alias: monitor uses "trailing" as exit_reason
    "trailing_stop": 0.02, # 2% for trailing stop
    "time_exit": 0.03,     # alias: monitor uses "time_exit" as exit_reason
    "time_decay": 0.03,    # 3% for timeout exits
}


def _is_emergency_sell(reason: str) -> bool:
    """Exit reasons where selling now is more important than perfect pricing.

    In Keystone the dominant real exit is `time_exit`, not only `hard_stop`.
    If QuoterV2 returns 0 during a rug / liquidity drain / RPC blindness, refusing
    to sign a time/trailing exit can strand the wallet in a token that is racing
    to zero. Take-profit stays non-emergency because skipping a profit-taking
    attempt is not the same risk as missing a protective exit.
    """
    return reason in (
        "stop_loss",
        "hard_stop",
        "blind_mark_panic",
        "trailing",
        "trailing_stop",
        "time_exit",
        "time_decay",
    )


def _compute_min_amount_out(expected: int, reason: str = "buy") -> int:
    """Compute amountOutMinimum based on expected output and trade urgency.
    Non-emergency quote failures must fail closed before calling this helper.
    Emergency sells may pass 0 to prioritize escaping a collapsing position."""
    tolerance = _SLIPPAGE_TOLERANCE.get(reason, 0.03)
    if expected <= 0:
        return 0 if _is_emergency_sell(reason) else 1
    return max(1, int(expected * (1 - tolerance)))


PRIORITY_FEE_MULTIPLIERS = {
    "buy": 1.1,
    "take_profit": 1.0,
    "stop_loss": 2.0,
    "approval": 1.0,
}


def _sell_urgency(reason: str) -> str:
    """Map sell reason to transaction urgency.

    Any protective exit gets stop-loss priority and the no-cancel send policy.
    """
    return "stop_loss" if _is_emergency_sell(reason) else "take_profit"


def compute_priority_fee(w3: Web3, urgency: str = "buy") -> tuple:
    """Compute (maxPriorityFeePerGas, maxFeePerGas) based on urgency.
    Returns (priority_fee, max_fee) in wei."""
    multiplier = PRIORITY_FEE_MULTIPLIERS.get(urgency, 1.1)
    try:
        base_priority = w3.eth.max_priority_fee
    except Exception:
        base_priority = max(w3.eth.gas_price // 5, 1_000_000)
    base_fee = w3.eth.gas_price
    priority = max(int(base_priority * multiplier), 1_000_000)
    max_fee = base_fee + priority
    return priority, max_fee


def _build_and_send(w3: Web3, account, tx: dict, urgency: str = "buy") -> TxReceipt:
    tx["from"] = account.address
    nonce = _get_next_nonce(w3, account)
    tx["nonce"] = nonce
    tx["chainId"] = w3.eth.chain_id

    priority_fee, max_fee = compute_priority_fee(w3, urgency)
    tx["maxPriorityFeePerGas"] = priority_fee
    tx["maxFeePerGas"] = max_fee

    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.15)  # 15% buffer
    except Exception as e:
        if urgency in ("stop_loss", "hard_stop"):
            tx["gas"] = 500_000
            log.warning("gas estimation failed on STOP LOSS, using 500k fallback: %s", e)
        else:
            raise RuntimeError(f"gas estimation failed — aborting tx to prevent wasted gas: {e}")

    # Gas cap applies ONLY to buys. Every exit (take_profit / trailing / time_exit /
    # stop_loss / hard_stop) and the approval needed to sell MUST never be blocked by
    # a gas ceiling: refusing to sell turns a bounded gas overspend into an unbounded
    # position loss (you get stuck holding a memecoin into a -100% rug). Skipping a
    # BUY, by contrast, is harmless. This guarantees exits fire even during gas spikes
    # / burst congestion — the exact failure mode that would otherwise strand a sell.
    if urgency == "buy":
        # Prefer an ETH-denominated gas cap (oracle-free). Fall back to the USD
        # cap, which depends on the static EXECUTOR_ETH_PRICE_USD constant.
        max_gas_eth = float(os.environ.get("EXECUTOR_MAX_GAS_ETH", "0") or 0)
        estimated_gas_eth = tx["gas"] * max_fee / 1e18
        if max_gas_eth > 0:
            if estimated_gas_eth > max_gas_eth:
                raise RuntimeError(
                    f"gas cap exceeded: estimated={estimated_gas_eth:.6f} ETH > max={max_gas_eth:.6f} ETH"
                )
        else:
            max_gas_usd = float(os.environ.get("EXECUTOR_MAX_GAS_USD", "0") or 0)
            eth_price_usd = float(os.environ.get("EXECUTOR_ETH_PRICE_USD", "3500") or 3500)
            if max_gas_usd > 0:
                estimated_gas_usd = estimated_gas_eth * eth_price_usd
                if estimated_gas_usd > max_gas_usd:
                    raise RuntimeError(
                        f"gas cap exceeded: estimated=${estimated_gas_usd:.2f} > max=${max_gas_usd:.2f}"
                    )

    signed = account.sign_transaction(tx)
    try:
        tx_hash = send_raw_transaction(signed.raw_transaction, w3)
    except Exception:
        _release_nonce(account, nonce)
        raise

    receipt = _wait_or_replace(w3, account, tx, tx_hash, nonce, urgency)
    return receipt


def _wait_or_replace(
    w3: Web3, account, original_tx: dict,
    tx_hash, nonce: int, urgency: str,
) -> TxReceipt:
    """Wait for tx receipt with replacement/cancellation if stuck.

    Timeline:
      0-500ms:   wait for original tx
      500ms-2s:  send replacement at +20% priority fee
      2s-4s:     send cancellation (self-transfer) at +50% priority fee
      4s+:       wait for any nonce-matching receipt up to 60s total
    """
    # Phase 1: Quick check (500ms) — Flashblocks-aware 200ms poll
    try:
        receipt = wait_for_receipt(w3, tx_hash, timeout_sec=0.5, poll_ms=200)
        return receipt
    except Exception:
        pass

    # Phase 2: Replace with +20% fee (same nonce, same tx data). Skipped for
    # delegated accounts, whose one-in-flight cap makes replacement impossible.
    replace_hash = None
    if _is_delegated(w3, account):
        log.info("tx %s not confirmed after 500ms; delegated account, waiting without replacement",
                 tx_hash.hex())
    else:
        log.info("tx %s not confirmed after 500ms, sending replacement (+20%% fee)", tx_hash.hex())
        try:
            base_priority = original_tx.get("maxPriorityFeePerGas", 1_000_000)
            base_max = original_tx.get("maxFeePerGas", w3.eth.gas_price * 2)
            replace_tx = dict(original_tx)
            replace_tx["nonce"] = nonce
            replace_tx["maxPriorityFeePerGas"] = int(base_priority * 1.2)
            replace_tx["maxFeePerGas"] = int(base_max * 1.2)
            signed = account.sign_transaction(replace_tx)
            replace_hash = send_raw_transaction(signed.raw_transaction, w3)
            log.info("replacement tx sent: %s", replace_hash.hex())
        except Exception as e:
            log.debug("replacement send failed (original may have landed): %s", e)

    # Wait 1.5s more for either original or replacement
    for h in [tx_hash, replace_hash]:
        if h is None:
            continue
        try:
            receipt = wait_for_receipt(w3, h, timeout_sec=1.5, poll_ms=200)
            return receipt
        except Exception:
            pass

    # Phase 3:
    # - BUY/approval can be cancelled to recover the nonce; missing an entry is safe.
    # - EXIT transactions must NOT be cancelled. Cancelling a sell during congestion
    #   is worse than paying extra gas because it leaves the wallet holding the rug.
    cancellable = urgency in ("buy", "approval")
    if not cancellable:
        replace2_hash = None
        if _is_delegated(w3, account):
            log.warning("exit tx still pending after 2s; delegated account, cannot replace — waiting")
        else:
            log.warning("exit tx still pending after 2s, sending second replacement (+50%% fee), never cancel")
            try:
                base_priority = original_tx.get("maxPriorityFeePerGas", 1_000_000)
                base_max = original_tx.get("maxFeePerGas", w3.eth.gas_price * 2)
                replace2_tx = dict(original_tx)
                replace2_tx["nonce"] = nonce
                replace2_tx["maxPriorityFeePerGas"] = int(base_priority * 1.5)
                replace2_tx["maxFeePerGas"] = int(base_max * 1.5)
                signed = account.sign_transaction(replace2_tx)
                replace2_hash = send_raw_transaction(signed.raw_transaction, w3)
                log.info("second replacement tx sent: %s", replace2_hash.hex())
            except Exception as e:
                log.debug("second replacement send failed (earlier tx may land): %s", e)
                replace2_hash = None

        all_hashes = [h for h in [tx_hash, replace_hash, replace2_hash] if h is not None]
        deadline = time.time() + 86
        while time.time() < deadline:
            for h in all_hashes:
                try:
                    receipt = w3.eth.get_transaction_receipt(h)
                except Exception:
                    continue
                if receipt is not None:
                    log.info("exit tx confirmed: %s (status=%s)", h.hex(), receipt["status"])
                    return receipt
            time.sleep(0.5)
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

    # Cancellable path for entries / explicit approvals.
    log.warning("tx still pending after 2s, sending cancellation (self-transfer, +50%% fee)")
    cancel_hash = None
    try:
        base_priority = original_tx.get("maxPriorityFeePerGas", 1_000_000)
        base_max = original_tx.get("maxFeePerGas", w3.eth.gas_price * 2)
        cancel_tx = {
            "from": account.address,
            "to": account.address,
            "value": 0,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
            "gas": 21_000,
            "maxPriorityFeePerGas": int(base_priority * 1.5),
            "maxFeePerGas": int(base_max * 1.5),
        }
        signed = account.sign_transaction(cancel_tx)
        cancel_hash = send_raw_transaction(signed.raw_transaction, w3)
        log.info("cancellation tx sent: %s", cancel_hash.hex())
    except Exception as e:
        log.debug("cancellation send failed: %s", e)

    # Phase 4: Wait for ANY of the three to land (up to 56s more)
    all_hashes = [h for h in [tx_hash, replace_hash, cancel_hash] if h is not None]
    deadline = time.time() + 56
    while time.time() < deadline:
        for h in all_hashes:
            try:
                receipt = w3.eth.get_transaction_receipt(h)
            except Exception:
                continue
            if receipt is not None:
                if cancel_hash is not None and h == cancel_hash:
                    raise RuntimeError(f"tx cancelled by self-transfer: {h.hex()}")
                log.info("tx confirmed: %s (status=%s)", h.hex(), receipt["status"])
                return receipt
        time.sleep(0.5)

    # Last resort: block on the original hash
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)


def _ensure_approval(w3: Web3, account, token_addr: str, spender: str, amount: int):
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    allowance = token.functions.allowance(account.address, Web3.to_checksum_address(spender)).call()
    if allowance >= amount:
        return
    log.info("approving %s to spend token %s", spender, token_addr)
    priority_fee, max_fee = compute_priority_fee(w3, "approval")
    tx = token.functions.approve(
        Web3.to_checksum_address(spender), MAX_UINT256
    ).build_transaction({
        "from": account.address,
        "nonce": _get_next_nonce(w3, account),
        "chainId": w3.eth.chain_id,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
    })
    signed = account.sign_transaction(tx)
    tx_hash = send_raw_transaction(signed.raw_transaction, w3)
    wait_for_receipt(w3, tx_hash, timeout_sec=60)
    log.info("approval tx confirmed: %s", tx_hash.hex())


def _get_token_balance(w3: Web3, token_addr: str, wallet: str) -> int:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    return token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()


def _decode_uint256_data(raw) -> int:
    if isinstance(raw, bytes):
        return int.from_bytes(raw, "big") if raw else 0
    if isinstance(raw, int):
        return raw
    cleaned = str(raw).lower().replace("0x", "")
    return int(cleaned, 16) if cleaned else 0


def _topic_address_bare(topic) -> str:
    """Extract the 20-byte address from an indexed log topic."""
    return _normalize_hex(topic)[-40:]


def _extract_tokens_from_receipt(receipt: TxReceipt, token_addr: str, wallet: str) -> int:
    """Sum ERC20 Transfer logs crediting `wallet` for `token_addr` in a buy receipt."""
    token_bare = token_addr.lower().replace("0x", "")
    wallet_bare = wallet.lower().replace("0x", "")
    total = 0
    for entry in receipt.get("logs", []):
        if _normalize_hex(entry.get("address") or "") != token_bare:
            continue
        topics = entry.get("topics", [])
        if not topics or _normalize_hex(topics[0]) != _TRANSFER_TOPIC_BARE:
            continue
        if len(topics) < 3:
            continue
        if _topic_address_bare(topics[2]) != wallet_bare:
            continue
        total += _decode_uint256_data(entry.get("data", b""))
    return total


def _tokens_received_from_buy(
    w3: Web3,
    pool: PoolInfo,
    account,
    receipt: TxReceipt,
    balance_before: int,
) -> int:
    """Determine tokens received from a successful buy.

    Base Flashblocks can return a receipt before `balanceOf` reflects the credit.
    Prefer balance delta; retry briefly; fall back to Transfer logs in the receipt.
    """
    wallet = account.address
    token = pool.non_weth_token

    balance_after = _get_token_balance(w3, token, wallet)
    delta = balance_after - balance_before
    if delta > 0:
        return delta

    for delay_sec in (0.25, 0.5, 1.0):
        time.sleep(delay_sec)
        balance_after = _get_token_balance(w3, token, wallet)
        delta = balance_after - balance_before
        if delta > 0:
            log.info(
                "buy token balance visible after %.2fs delay pool=%s amount=%s",
                delay_sec, pool.pool_address, delta,
            )
            return delta

    from_logs = _extract_tokens_from_receipt(receipt, token, wallet)
    if from_logs > 0:
        log.warning(
            "buy amount_out from receipt Transfer logs pool=%s amount=%s (balanceOf lag)",
            pool.pool_address, from_logs,
        )
        return from_logs
    return 0


def recover_buy_amount_out(w3: Web3, pool: PoolInfo, account, tx_hash: str) -> int:
    """Last-resort recovery when buy receipt succeeded but amount_out was reported as 0."""
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        log.debug("recover_buy_amount_out receipt fetch failed tx=%s: %s", tx_hash, e)
        return 0
    if receipt.get("status") != 1:
        return 0
    amount = _extract_tokens_from_receipt(receipt, pool.non_weth_token, account.address)
    if amount > 0:
        return amount
    bal = _get_token_balance(w3, pool.non_weth_token, account.address)
    return bal if bal > 0 else 0


def _refresh_sell_amount(w3: Web3, account, pool: PoolInfo, token_amount: int) -> int:
    """Use the wallet's current token balance before selling.

    The open-position record is written at BUY time. Transfer-tax tokens, manual
    rescue operations, partial accounting drift, or a previous in-flight tx can make
    that stored amount stale. Selling more than the current balance reverts; selling
    the min(stored, balance) is the safer escape path.
    """
    balance = _get_token_balance(w3, pool.non_weth_token, account.address)
    if balance <= 0:
        log.error("sell ABORT: token balance is 0 for pool=%s token=%s", pool.pool_address, pool.non_weth_token)
        return 0
    if balance < token_amount:
        log.warning(
            "sell amount reduced to wallet balance pool=%s stored=%d balance=%d",
            pool.pool_address,
            token_amount,
            balance,
        )
        return int(balance)
    return token_amount


# ═══════════════════════════════════════════════
#  Buy Output Estimation (for slippage protection)
# ═══════════════════════════════════════════════

def _estimate_buy_output_v2(w3: Web3, pool: PoolInfo, eth_wei: int) -> int:
    """Estimate tokens received for eth_wei via V2/vAMM constant product."""
    try:
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pool.pool_address), abi=PAIR_ABI,
        )
        r0, r1, _ = pair.functions.getReserves().call()
        weth_is_t0 = pool.token0.lower() == WETH_BASE.lower()
        if weth_is_t0:
            reserve_in, reserve_out = int(r0), int(r1)
        else:
            reserve_in, reserve_out = int(r1), int(r0)
        return _get_amount_out_cpmm(eth_wei, reserve_in, reserve_out, pool.fee_bps)
    except Exception as e:
        log.debug("_estimate_buy_output_v2 failed: %s", e)
        return 0


def _estimate_buy_output_v3(w3: Web3, pool: PoolInfo, eth_wei: int) -> int:
    """Estimate tokens received for eth_wei via V3 QuoterV2."""
    try:
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=_QUOTER_ABI,
        )
        weth = Web3.to_checksum_address(WETH_BASE)
        token_out_addr = Web3.to_checksum_address(pool.non_weth_token)
        fee = int(pool.fee_bps) * 100
        result = quoter.functions.quoteExactInputSingle(
            (weth, token_out_addr, eth_wei, fee, 0)
        ).call()
        return int(result[0])
    except Exception as e:
        log.debug("_estimate_buy_output_v3 failed: %s", e)
        return 0


# ═══════════════════════════════════════════════
#  BUY: ETH -> Token
# ═══════════════════════════════════════════════

def buy_v2(w3: Web3, account, pool: PoolInfo, amount_eth_wei: int) -> SwapResult:
    """Buy token with ETH via Uniswap V2 Router."""
    router = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)

    expected = _estimate_buy_output_v2(w3, pool, amount_eth_wei)
    if expected <= 0:
        log.error("buy_v2 ABORT: reserve quote returned 0 — cannot set safe slippage for %s", pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="quote_failed_zero_slippage")
    min_out = _compute_min_amount_out(expected, "buy")

    fn_sig = Web3.keccak(text="swapExactETHForTokens(uint256,address[],address,uint256)")[:4]
    params = encode(
        ["uint256", "address[]", "address", "uint256"],
        [min_out, [weth, target_token], account.address, _deadline()]
    )
    data = fn_sig + params

    try:
        balance_before = _get_token_balance(w3, pool.non_weth_token, account.address)
        receipt = _build_and_send(w3, account, {"to": router, "value": amount_eth_wei, "data": data}, urgency="buy")
        tokens_received = _tokens_received_from_buy(w3, pool, account, receipt, balance_before)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)

        return SwapResult(
            success=receipt["status"] == 1,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=amount_eth_wei,
            amount_out=tokens_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            expected_out=expected,
            block_number=receipt.get("blockNumber"),
        )
    except Exception as e:
        log.error("buy_v2 failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


def buy_aero_vamm(w3: Web3, account, pool: PoolInfo, amount_eth_wei: int) -> SwapResult:
    """Buy token with ETH via Aerodrome vAMM Router."""
    router = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)
    factory = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

    expected = _estimate_buy_output_v2(w3, pool, amount_eth_wei)
    if expected <= 0:
        log.error("buy_aero ABORT: reserve quote returned 0 — cannot set safe slippage for %s", pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="quote_failed_zero_slippage")
    min_out = _compute_min_amount_out(expected, "buy")

    fn_sig = Web3.keccak(text="swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)")[:4]
    params = encode(
        ["uint256", "(address,address,bool,address)[]", "address", "uint256"],
        [min_out, [(weth, target_token, False, factory)], account.address, _deadline()]
    )
    data = fn_sig + params

    try:
        balance_before = _get_token_balance(w3, pool.non_weth_token, account.address)
        receipt = _build_and_send(w3, account, {"to": router, "value": amount_eth_wei, "data": data}, urgency="buy")
        tokens_received = _tokens_received_from_buy(w3, pool, account, receipt, balance_before)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)

        return SwapResult(
            success=receipt["status"] == 1,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=amount_eth_wei,
            amount_out=tokens_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            expected_out=expected,
            block_number=receipt.get("blockNumber"),
        )
    except Exception as e:
        log.error("buy_aero_vamm failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


def buy_v3(w3: Web3, account, pool: PoolInfo, amount_eth_wei: int) -> SwapResult:
    """Buy token with ETH via Uniswap V3 SwapRouter02 exactInputSingle."""
    router = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)
    fee = pool.fee_bps * 100

    expected = _estimate_buy_output_v3(w3, pool, amount_eth_wei)
    if expected <= 0:
        log.error("buy_v3 ABORT: QuoterV2 returned 0 — cannot set safe slippage for %s", pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="quote_failed_zero_slippage")
    min_out = _compute_min_amount_out(expected, "buy")

    fn_sig = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
    params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(weth, target_token, fee, account.address, amount_eth_wei, min_out, 0)]
    )
    data = fn_sig + params

    try:
        balance_before = _get_token_balance(w3, pool.non_weth_token, account.address)
        receipt = _build_and_send(w3, account, {"to": router, "value": amount_eth_wei, "data": data}, urgency="buy")
        tokens_received = _tokens_received_from_buy(w3, pool, account, receipt, balance_before)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)

        return SwapResult(
            success=receipt["status"] == 1,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=amount_eth_wei,
            amount_out=tokens_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            expected_out=expected,
            block_number=receipt.get("blockNumber"),
        )
    except Exception as e:
        log.error("buy_v3 failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


# ═══════════════════════════════════════════════
#  SELL: Token -> ETH
# ═══════════════════════════════════════════════

def _eth_received_from_sell(w3: Web3, account, receipt: TxReceipt, eth_before: int) -> int:
    """Determine ETH received from a sell tx. Uses receipt logs first (reliable),
    falls back to balance delta if no Withdrawal event found."""
    from_logs = _extract_eth_from_receipt(receipt)
    if from_logs > 0:
        log.debug("sell eth_received from receipt logs: %s wei", from_logs)
        return from_logs

    gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)
    eth_after = w3.eth.get_balance(account.address)
    delta = eth_after - eth_before + gas_cost
    if delta > 0:
        return delta

    time.sleep(3)
    eth_after = w3.eth.get_balance(account.address)
    delta = eth_after - eth_before + gas_cost
    return max(delta, 0)


def sell_v2(w3: Web3, account, pool: PoolInfo, token_amount: int,
            reason: str = "take_profit") -> SwapResult:
    """Sell token for ETH via Uniswap V2 Router."""
    router_addr = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)
    token_amount = _refresh_sell_amount(w3, account, pool, token_amount)
    if token_amount <= 0:
        return SwapResult(success=False, tx_hash=None, amount_in=0, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_zero_token_balance")
    _ensure_approval(w3, account, pool.non_weth_token, pool.router, token_amount)

    expected = quote_sell_eth_wei(w3, pool, token_amount)
    if expected <= 0 and not _is_emergency_sell(reason):
        log.error("sell_v2 ABORT: quote returned 0 for non-emergency sell reason=%s pool=%s",
                  reason, pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_quote_failed_fail_closed")
    min_out = _compute_min_amount_out(expected, reason)

    fn_sig = Web3.keccak(text="swapExactTokensForETH(uint256,uint256,address[],address,uint256)")[:4]
    params = encode(
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [token_amount, min_out, [target_token, weth], account.address, _deadline()]
    )
    data = fn_sig + params

    urgency = _sell_urgency(reason)
    try:
        eth_before = w3.eth.get_balance(account.address)
        receipt = _build_and_send(w3, account, {"to": router_addr, "value": 0, "data": data}, urgency=urgency)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)
        eth_received = _eth_received_from_sell(w3, account, receipt, eth_before)
        ok = receipt["status"] == 1 and eth_received > 0

        return SwapResult(
            success=ok,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=token_amount,
            amount_out=eth_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            error=None if ok else "sell_no_eth_received",
        )
    except Exception as e:
        log.error("sell_v2 failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


def sell_aero_vamm(w3: Web3, account, pool: PoolInfo, token_amount: int,
                   reason: str = "take_profit") -> SwapResult:
    """Sell token for ETH via Aerodrome vAMM Router."""
    router_addr = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)
    factory = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    token_amount = _refresh_sell_amount(w3, account, pool, token_amount)
    if token_amount <= 0:
        return SwapResult(success=False, tx_hash=None, amount_in=0, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_zero_token_balance")
    _ensure_approval(w3, account, pool.non_weth_token, pool.router, token_amount)

    expected = quote_sell_eth_wei(w3, pool, token_amount)
    if expected <= 0 and not _is_emergency_sell(reason):
        log.error("sell_aero_vamm ABORT: quote returned 0 for non-emergency sell reason=%s pool=%s",
                  reason, pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_quote_failed_fail_closed")
    min_out = _compute_min_amount_out(expected, reason)

    fn_sig = Web3.keccak(text="swapExactTokensForETH(uint256,uint256,(address,address,bool,address)[],address,uint256)")[:4]
    params = encode(
        ["uint256", "uint256", "(address,address,bool,address)[]", "address", "uint256"],
        [token_amount, min_out, [(target_token, weth, False, factory)], account.address, _deadline()]
    )
    data = fn_sig + params

    urgency = _sell_urgency(reason)
    try:
        eth_before = w3.eth.get_balance(account.address)
        receipt = _build_and_send(w3, account, {"to": router_addr, "value": 0, "data": data}, urgency=urgency)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)
        eth_received = _eth_received_from_sell(w3, account, receipt, eth_before)
        ok = receipt["status"] == 1 and eth_received > 0

        return SwapResult(
            success=ok,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=token_amount,
            amount_out=eth_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            error=None if ok else "sell_no_eth_received",
        )
    except Exception as e:
        log.error("sell_aero_vamm failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


def sell_v3(w3: Web3, account, pool: PoolInfo, token_amount: int,
            reason: str = "take_profit") -> SwapResult:
    """Sell token for ETH via V3 SwapRouter02 exactInputSingle -> unwrapWETH9."""
    router_addr = Web3.to_checksum_address(pool.router)
    weth = Web3.to_checksum_address(WETH_BASE)
    target_token = Web3.to_checksum_address(pool.non_weth_token)
    fee = pool.fee_bps * 100
    token_amount = _refresh_sell_amount(w3, account, pool, token_amount)
    if token_amount <= 0:
        return SwapResult(success=False, tx_hash=None, amount_in=0, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_zero_token_balance")
    _ensure_approval(w3, account, pool.non_weth_token, pool.router, token_amount)

    expected = quote_sell_eth_wei(w3, pool, token_amount)
    if expected <= 0 and not _is_emergency_sell(reason):
        log.error("sell_v3 ABORT: quote returned 0 for non-emergency sell reason=%s pool=%s",
                  reason, pool.pool_address)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error="sell_quote_failed_fail_closed")
    min_out = _compute_min_amount_out(expected, reason)

    swap_sig = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
    swap_params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(target_token, weth, fee, router_addr, token_amount, min_out, 0)]
    )

    unwrap_sig = Web3.keccak(text="unwrapWETH9(uint256,address)")[:4]
    unwrap_params = encode(["uint256", "address"], [0, account.address])

    multicall_sig = Web3.keccak(text="multicall(bytes[])")[:4]
    multicall_params = encode(["bytes[]"], [[swap_sig + swap_params, unwrap_sig + unwrap_params]])
    data = multicall_sig + multicall_params

    urgency = _sell_urgency(reason)
    try:
        eth_before = w3.eth.get_balance(account.address)
        receipt = _build_and_send(w3, account, {"to": router_addr, "value": 0, "data": data}, urgency=urgency)
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)
        eth_received = _eth_received_from_sell(w3, account, receipt, eth_before)
        ok = receipt["status"] == 1 and eth_received > 0

        return SwapResult(
            success=ok,
            tx_hash=receipt["transactionHash"].hex(),
            amount_in=token_amount,
            amount_out=eth_received,
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
            error=None if ok else "sell_no_eth_received",
        )
    except Exception as e:
        log.error("sell_v3 failed: %s", e)
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0, gas_used=0, gas_cost_wei=0, error=str(e))


def _get_amount_out_cpmm(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    fee_mul = max(1, 10000 - int(fee_bps))
    amount_in_with_fee = amount_in * fee_mul
    num = amount_in_with_fee * reserve_out
    den = reserve_in * 10000 + amount_in_with_fee
    return num // den


def quote_sell_eth_wei(w3: Web3, pool: PoolInfo, token_amount: int) -> int:
    """Estimated ETH (wei) received if selling token_amount of non-WETH leg to WETH."""
    if token_amount <= 0:
        return 0
    nw = pool.non_weth_token.lower()
    t0 = pool.token0.lower()

    if pool.pool_type in ("v2", "aero_vamm"):
        pair = w3.eth.contract(address=Web3.to_checksum_address(pool.pool_address), abi=PAIR_ABI)
        r0, r1, _ = pair.functions.getReserves().call()
        if nw == t0:
            reserve_in, reserve_out = int(r0), int(r1)
        else:
            reserve_in, reserve_out = int(r1), int(r0)
        return _get_amount_out_cpmm(token_amount, reserve_in, reserve_out, pool.fee_bps)

    if pool.pool_type in ("v3", "aero_cl"):
        quoter = Web3.to_checksum_address(QUOTER_V2_BASE)
        weth = Web3.to_checksum_address(WETH_BASE)
        token_in = Web3.to_checksum_address(pool.non_weth_token)
        fee = int(pool.fee_bps) * 100
        quoter_abi = [
            {
                "inputs": [
                    {
                        "components": [
                            {"name": "tokenIn", "type": "address"},
                            {"name": "tokenOut", "type": "address"},
                            {"name": "amountIn", "type": "uint256"},
                            {"name": "fee", "type": "uint24"},
                            {"name": "sqrtPriceLimitX96", "type": "uint160"},
                        ],
                        "name": "params",
                        "type": "tuple",
                    }
                ],
                "name": "quoteExactInputSingle",
                "outputs": [
                    {"name": "amountOut", "type": "uint256"},
                    {"name": "sqrtPriceX96After", "type": "uint160"},
                    {"name": "initializedTicksCrossed", "type": "uint32"},
                    {"name": "gasEstimate", "type": "uint256"},
                ],
                "stateMutability": "nonpayable",
                "type": "function",
            }
        ]
        c = w3.eth.contract(address=quoter, abi=quoter_abi)
        try:
            params = (token_in, weth, token_amount, fee, 0)
            out = c.functions.quoteExactInputSingle(params).call()
            return int(out[0])
        except Exception as e:
            log.debug("quoteExactInputSingle failed for %s: %s", pool.pool_address, e)
            return 0

    return 0


# ═══════════════════════════════════════════════
#  Honeypot Detection
# ═══════════════════════════════════════════════

_QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


_PAIR_ABI = [
    {"constant": True, "inputs": [], "name": "getReserves",
     "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"},
                 {"name": "", "type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]


def check_honeypot(w3: Web3, token_address: str, pool: PoolInfo) -> bool:
    """Simulate selling a small amount of tokens back to WETH.
    Returns True if the token is a honeypot (cannot be sold)."""
    token_in = Web3.to_checksum_address(token_address)
    weth = Web3.to_checksum_address(WETH_BASE)
    test_amount = 10 ** max(pool.non_weth_decimals - 3, 0)

    if pool.pool_type in ("v2", "aero_vamm"):
        ok = _honeypot_check_v2(w3, token_in, pool.pool_address, test_amount)
    else:
        ok = _honeypot_check_v3(w3, token_in, weth, test_amount, pool.fee_bps)

    if not ok:
        log.warning("honeypot detected: token %s pool %s type %s",
                    token_address, pool.pool_address, pool.pool_type)
    return not ok


def _honeypot_check_v2(w3: Web3, token_cs: str, pool_address: str,
                       test_amount: int) -> bool:
    """V2-style check: call getReserves() on pair directly, compute x*y=k."""
    try:
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=_PAIR_ABI,
        )
        t0 = pair.functions.token0().call().lower()
        r0, r1, _ = pair.functions.getReserves().call()
        if t0 == token_cs.lower():
            reserve_in, reserve_out = r0, r1
        else:
            reserve_in, reserve_out = r1, r0
        if reserve_in == 0 or reserve_out == 0:
            return False
        amt_fee = test_amount * 997
        amount_out = (amt_fee * reserve_out) // (reserve_in * 1000 + amt_fee)
        return amount_out > 0
    except Exception:
        return False


def _honeypot_check_v3(w3: Web3, token_in: str, weth: str,
                       test_amount: int, fee_bps: int) -> bool:
    """V3/CL check via QuoterV2."""
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=_QUOTER_ABI,
    )
    fee_tiers = [fee_bps * 100]
    if fee_bps * 100 not in (3000, 10000, 500):
        fee_tiers.extend([3000, 10000, 500])
    for fee in fee_tiers:
        try:
            result = quoter.functions.quoteExactInputSingle(
                (token_in, weth, test_amount, fee, 0)
            ).call()
            if int(result[0]) > 0:
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════
#  Executable-sell simulation (gas-bomb honeypot defense)
# ═══════════════════════════════════════════════
#
# WHY: QuoterV2 / plain eth_call simulate the sell with the node's *default*
# (near-unlimited) gas. A class of honeypots ("gas-bomb") makes the sell path
# succeed ONLY at absurd gas (~block limit) and revert at every realistic gas.
# Such tokens quote healthy via QuoterV2 yet can NEVER be sold by a real tx
# (which is capped at realistic gas). This was observed live on 0x24e39227…:
#   eth_call(no gas)=OK 0.00109 ETH; eth_call(gas=300k..10M)=REVERT;
#   eth_call(gas=30M)=OK; real broadcast (500k gas)=status 0 (revert).
#
# This check replays the REAL router sell at a realistic gas cap, using state
# overrides to grant a probe address the token balance + router allowance, so it
# can run BEFORE we ever buy. A revert at realistic gas == unsellable == block.

SELL_SIM_GAS = int(os.environ.get("EXECUTOR_SELL_SIM_GAS", "3000000"))
_DEAD_PROBE = "0x000000000000000000000000000000000000dEaD"
# 1000 ETH balance for the probe address so gas is never the limiter in the sim
_PROBE_ETH_HEX = "0x" + (1000 * 10 ** 18).to_bytes(32, "big").hex()


def _slot_value_hex(v: int) -> str:
    return "0x" + int(v).to_bytes(32, "big").hex()


def _parse_uint_result(res) -> Optional[int]:
    """Safely parse an eth_call hex result into an int. None if empty/invalid."""
    if not res or res == "0x":
        return None
    try:
        return int(res, 16)
    except (ValueError, TypeError):
        return None


def _detect_balance_slot(w3: Web3, token: str, probe: str, max_slots: int = 30):
    """Find the storage slot of balanceOf[probe]. Returns (slot, layout) or (None, None).
    layout: 'sol' = keccak(addr,slot), 'vyper' = keccak(slot,addr)."""
    sig = Web3.keccak(text="balanceOf(address)")[:4]
    data = "0x" + (sig + encode(["address"], [probe])).hex()
    sentinel = 0x7777
    sval = _slot_value_hex(sentinel)
    for slot in range(max_slots):
        for layout in ("sol", "vyper"):
            if layout == "sol":
                key = "0x" + Web3.keccak(encode(["address", "uint256"], [probe, slot])).hex()
            else:
                key = "0x" + Web3.keccak(encode(["uint256", "address"], [slot, probe])).hex()
            ov = {token: {"stateDiff": {key: sval}}}
            try:
                r = w3.provider.make_request("eth_call", [{"to": token, "data": data}, "latest", ov])
            except Exception:
                return None, None
            if _parse_uint_result(r.get("result")) == sentinel:
                return slot, layout
    return None, None


def _detect_allowance_slot(w3: Web3, token: str, owner: str, spender: str, max_slots: int = 30):
    """Find storage slot of allowance[owner][spender]. Returns (slot, layout) or (None, None)."""
    sig = Web3.keccak(text="allowance(address,address)")[:4]
    data = "0x" + (sig + encode(["address", "address"], [owner, spender])).hex()
    sentinel = 0x8888
    sval = _slot_value_hex(sentinel)
    for slot in range(max_slots):
        for layout in ("sol", "vyper"):
            if layout == "sol":
                inner = Web3.keccak(encode(["address", "uint256"], [owner, slot]))
            else:
                inner = Web3.keccak(encode(["uint256", "address"], [slot, owner]))
            key = "0x" + Web3.keccak(encode(["address", "bytes32"], [spender, inner])).hex()
            ov = {token: {"stateDiff": {key: sval}}}
            try:
                r = w3.provider.make_request("eth_call", [{"to": token, "data": data}, "latest", ov])
            except Exception:
                return None, None
            if _parse_uint_result(r.get("result")) == sentinel:
                return slot, layout
    return None, None


def _balance_key(probe: str, slot: int, layout: str) -> str:
    if layout == "sol":
        return "0x" + Web3.keccak(encode(["address", "uint256"], [probe, slot])).hex()
    return "0x" + Web3.keccak(encode(["uint256", "address"], [slot, probe])).hex()


def _allowance_key(owner: str, spender: str, slot: int, layout: str) -> str:
    if layout == "vyper":
        inner = Web3.keccak(encode(["uint256", "address"], [slot, owner]))
    else:
        inner = Web3.keccak(encode(["address", "uint256"], [owner, slot]))
    return "0x" + Web3.keccak(encode(["address", "bytes32"], [spender, inner])).hex()


def simulate_real_sell(
    w3: Web3,
    pool: PoolInfo,
    token_amount_wei: int,
    wallet: Optional[str] = None,
    gas_cap: Optional[int] = None,
):
    """Replay the REAL router sell at a realistic gas cap to catch gas-bomb honeypots.

    Returns (ok: bool, reason: str).
      ok=True  -> a real sell tx at `gas_cap` would execute (sellable)
      ok=False -> sell reverts / returns zero at realistic gas (unsellable)
    Uses eth_call state-override to grant `wallet` (or a dead probe) the token
    balance + router allowance, so it works BEFORE buying. Only v3/aero_cl are
    actively simulated (the KEYSTONE universe + the proven attack path); other
    pool types return ok=True with an explanatory reason (fail-open by design).
    """
    if token_amount_wei <= 0:
        return True, "sim_skip_zero_amount"
    if pool.pool_type not in ("v3", "aero_cl"):
        return True, "sim_skip_pooltype_" + pool.pool_type

    gas_cap = int(gas_cap or SELL_SIM_GAS)
    token = Web3.to_checksum_address(pool.non_weth_token)
    weth = Web3.to_checksum_address(WETH_BASE)
    router = Web3.to_checksum_address(pool.router)
    probe = Web3.to_checksum_address(wallet) if wallet else Web3.to_checksum_address(_DEAD_PROBE)

    bslot, blayout = _detect_balance_slot(w3, token, probe)
    if bslot is None:
        return True, "sim_unavailable_balance_slot"
    aslot, alayout = _detect_allowance_slot(w3, token, probe, router)
    if aslot is None:
        return True, "sim_unavailable_allowance_slot"

    bkey = _balance_key(probe, bslot, blayout)
    akey = _allowance_key(probe, router, aslot, alayout)
    # IMPORTANT: do NOT use MAX_UINT256 for the balance. Tokens like USDC
    # (FiatToken v2.2) pack a blacklist flag into the TOP bit of the balance
    # slot, so all-ones marks the account blacklisted -> transferFrom reverts
    # ("STF"), a false positive. Grant exactly what we need (high bits clear).
    grant = max(int(token_amount_wei) * 2, int(token_amount_wei) + 1)
    grant_hex = _slot_value_hex(grant)
    overrides = {
        token: {"stateDiff": {bkey: grant_hex, akey: grant_hex}},
        probe: {"balance": _PROBE_ETH_HEX},
    }

    sig = Web3.keccak(
        text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
    )[:4]
    params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(token, weth, int(pool.fee_bps) * 100, probe, int(token_amount_wei), 0, 0)],
    )
    tx = {"from": probe, "to": router, "data": "0x" + (sig + params).hex(), "gas": hex(gas_cap)}
    try:
        r = w3.provider.make_request("eth_call", [tx, "latest", overrides])
    except Exception as e:
        log.warning("sell sim rpc error pool=%s: %s", pool.pool_address, e)
        return True, "sim_rpc_error"
    if r.get("error") is not None:
        return False, f"sell_reverts_at_{gas_cap}gas"
    res = r.get("result")
    out = _parse_uint_result(res[:66] if res and len(res) >= 66 else res) or 0
    if out <= 0:
        return False, f"sell_zero_out_at_{gas_cap}gas"
    return True, "ok"


# ═══════════════════════════════════════════════
#  slot0() Price Monitoring
# ═══════════════════════════════════════════════

_SLOT0_ABI = json.loads(
    '[{"inputs":[],"name":"slot0","outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},{"name":"observationIndex","type":"uint16"},{"name":"observationCardinality","type":"uint16"},{"name":"observationCardinalityNext","type":"uint16"},{"name":"feeProtocol","type":"uint8"},{"name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]'
)


def get_slot0_price_eth(w3: Web3, pool: PoolInfo, token_amount: int) -> int:
    """Get position value in ETH wei using slot0() for V3/CL pools.
    More efficient than QuoterV2 — single static call, no simulation."""
    if pool.pool_type not in ("v3", "aero_cl"):
        return 0
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(pool.pool_address), abi=_SLOT0_ABI
        )
        slot0 = contract.functions.slot0().call()
        sqrt_price_x96 = int(slot0[0])
        if sqrt_price_x96 == 0:
            return 0

        price_x192 = sqrt_price_x96 * sqrt_price_x96
        t0_dec = pool.token0_decimals
        t1_dec = pool.token1_decimals

        nw = pool.non_weth_token.lower()
        if nw == pool.token0.lower():
            # non_weth is token0: price = token1_per_token0 = sqrtP^2 / 2^192 * 10^(t0dec-t1dec)
            eth_value = (token_amount * price_x192) >> 192
            if t0_dec != t1_dec:
                eth_value = eth_value * (10 ** t1_dec) // (10 ** t0_dec)
        else:
            # non_weth is token1: price = token0_per_token1 = 2^192 / sqrtP^2 * 10^(t1dec-t0dec)
            if price_x192 == 0:
                return 0
            eth_value = (token_amount << 192) // price_x192
            if t0_dec != t1_dec:
                eth_value = eth_value * (10 ** t0_dec) // (10 ** t1_dec)

        return max(0, int(eth_value))
    except Exception as e:
        log.debug("slot0 price query failed for %s: %s", pool.pool_address, e)
        return 0


# ═══════════════════════════════════════════════
#  Live ETH/USD price (on-chain, no external API)
# ═══════════════════════════════════════════════

# Base USDC (6 decimals) and the deep WETH/USDC 0.05% pool fee param (500 = 0.05%).
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def get_eth_usd_price(w3: Web3) -> Optional[float]:
    """Live ETH/USD from an on-chain QuoterV2 WETH->USDC quote (0.05% pool).

    Trustless and dependency-free (no price API). Returns None on failure so the
    caller can fall back to the static EXECUTOR_ETH_PRICE_USD config.
    """
    try:
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=_QUOTER_ABI,
        )
        weth = Web3.to_checksum_address(WETH_BASE)
        usdc = Web3.to_checksum_address(USDC_BASE)
        result = quoter.functions.quoteExactInputSingle(
            (weth, usdc, 10 ** 18, 500, 0)
        ).call()
        price = int(result[0]) / 1e6  # USDC is 6 decimals → USD per 1 ETH
        if 50.0 < price < 100000.0:
            return price
        return None
    except Exception as e:
        log.debug("get_eth_usd_price failed: %s", e)
        return None


# ═══════════════════════════════════════════════
#  Dispatch
# ═══════════════════════════════════════════════

_BUY_DISPATCH = {
    "v2": buy_v2,
    "aero_vamm": buy_aero_vamm,
    "v3": buy_v3,
    "aero_cl": buy_v3,
}

_SELL_DISPATCH = {
    "v2": sell_v2,
    "aero_vamm": sell_aero_vamm,
    "v3": sell_v3,
    "aero_cl": sell_v3,
}


def execute_buy(w3: Web3, account, pool: PoolInfo, amount_eth_wei: int) -> SwapResult:
    fn = _BUY_DISPATCH.get(pool.pool_type)
    if fn is None:
        return SwapResult(success=False, tx_hash=None, amount_in=amount_eth_wei, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error=f"unsupported pool_type: {pool.pool_type}")
    return fn(w3, account, pool, amount_eth_wei)


def execute_sell(w3: Web3, account, pool: PoolInfo, token_amount: int,
                 reason: str = "take_profit") -> SwapResult:
    """Execute a sell. `reason` determines urgency for gas priority and slippage."""
    fn = _SELL_DISPATCH.get(pool.pool_type)
    if fn is None:
        return SwapResult(success=False, tx_hash=None, amount_in=token_amount, amount_out=0,
                          gas_used=0, gas_cost_wei=0, error=f"unsupported pool_type: {pool.pool_type}")
    return fn(w3, account, pool, token_amount, reason=reason)
