"""Uniswap V4 Universal Router swap encoding + fork-local execution."""
from __future__ import annotations

import time
from typing import Optional

from eth_abi import encode
from web3 import Web3

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
PERMIT2 = Web3.to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")
UNIVERSAL_ROUTER = Web3.to_checksum_address("0x6ff5693b99212Da76ad316178a184ab56d299b43")

CMD_V4_SWAP = 0x10
ACT_SWAP_EXACT_IN_SINGLE = 0x06
ACT_SETTLE_ALL = 0x0C
ACT_TAKE_ALL = 0x0F

WETH_ABI = [
    {"name": "deposit", "inputs": [], "outputs": [], "stateMutability": "payable", "type": "function"},
    {"name": "approve", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"name": "balanceOf", "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]
ERC20_ABI = [
    {"name": "approve", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"name": "balanceOf", "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
]
PERMIT2_ABI = [
    {"name": "approve", "inputs": [{"name": "token", "type": "address"}, {"name": "spender", "type": "address"}, {"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"}], "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]
ROUTER_ABI = [
    {"name": "execute", "inputs": [{"name": "commands", "type": "bytes"}, {"name": "inputs", "type": "bytes[]"}, {"name": "deadline", "type": "uint256"}], "outputs": [], "stateMutability": "payable", "type": "function"},
]

# fix ERC20 balanceOf return type
ERC20_ABI[1]["outputs"] = [{"name": "", "type": "uint256"}]


def pool_key_from_row(row: dict) -> tuple:
    return (
        Web3.to_checksum_address(row["currency0"]),
        Web3.to_checksum_address(row["currency1"]),
        int(row["fee"]),
        int(row["tickSpacing"]),
        Web3.to_checksum_address(row["hooks"]),
    )


def weth_side(row: dict) -> tuple[str, str, bool]:
    """Return (weth, alt_token, zero_for_one) for WETH -> alt buy."""
    c0 = Web3.to_checksum_address(row["currency0"])
    c1 = Web3.to_checksum_address(row["currency1"])
    if c0.lower() == WETH.lower():
        return WETH, c1, True
    if c1.lower() == WETH.lower():
        return WETH, c0, False
    raise ValueError("pool has no WETH leg")


def encode_v4_swap_input(
    pool_key: tuple,
    *,
    zero_for_one: bool,
    amount_in: int,
    amount_out_min: int = 0,
    hook_data: bytes = b"",
) -> bytes:
    actions = bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL])
    swap_params = encode(
        ["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint256", "bytes"],
        [pool_key, zero_for_one, amount_in, amount_out_min, 0, hook_data],
    )
    currency_in = pool_key[0] if zero_for_one else pool_key[1]
    currency_out = pool_key[1] if zero_for_one else pool_key[0]
    settle_params = encode(["address", "uint256"], [currency_in, 2**256 - 1])
    take_params = encode(["address", "uint256"], [currency_out, amount_out_min])
    return encode(["bytes", "bytes[]"], [actions, [swap_params, settle_params, take_params]])


class V4SwapExecutor:
    """Execute buy/sell on a local anvil fork via Universal Router + Permit2."""

    def __init__(self, w3: Web3, account: str, private_key: str):
        self.w3 = w3
        self.acct = Web3.to_checksum_address(account)
        self.pk = private_key
        self.weth = w3.eth.contract(address=WETH, abi=WETH_ABI)
        self.router = w3.eth.contract(address=UNIVERSAL_ROUTER, abi=ROUTER_ABI)
        self.permit2 = w3.eth.contract(address=PERMIT2, abi=PERMIT2_ABI)

    def _send(self, fn, *, value: int = 0, gas: int = 2_500_000):
        tx = fn.build_transaction({
            "from": self.acct,
            "nonce": self.w3.eth.get_transaction_count(self.acct),
            "value": value,
            "gas": gas,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.w3.eth.chain_id,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.pk)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(h, timeout=90)

    def _approve_permit2(self, token: str, amount: int) -> None:
        tok = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        self._send(tok.functions.approve(PERMIT2, amount))
        exp = int(time.time()) + 3600
        self._send(self.permit2.functions.approve(token, UNIVERSAL_ROUTER, amount, exp))

    def swap_exact_in(self, pool_row: dict, *, token_in: str, amount_in: int, zero_for_one: bool) -> dict:
        out = {"ok": False, "amount_out": 0, "error": None}
        token_in = Web3.to_checksum_address(token_in)
        _, alt, _ = weth_side(pool_row)
        out_token = alt if token_in.lower() == WETH.lower() else WETH
        try:
            if token_in.lower() == WETH.lower():
                self._send(self.weth.functions.deposit(), value=amount_in)
            self._approve_permit2(token_in, amount_in)
            pk = pool_key_from_row(pool_row)
            tok = self.w3.eth.contract(address=out_token, abi=ERC20_ABI)
            before = tok.functions.balanceOf(self.acct).call()
            inputs = [encode_v4_swap_input(pk, zero_for_one=zero_for_one, amount_in=amount_in)]
            rcpt = self._send(
                self.router.functions.execute(bytes([CMD_V4_SWAP]), inputs, int(time.time()) + 600)
            )
            after = tok.functions.balanceOf(self.acct).call()
            got = after - before
            out.update({"ok": rcpt["status"] == 1 and got > 0, "amount_out": got, "token_out": out_token})
            if not out["ok"]:
                out["error"] = "swap_zero_or_revert"
        except Exception as e:
            out["error"] = f"{type(e).__name__}:{str(e)[:120]}"
        return out

    def roundtrip(self, pool_row: dict, spend_wei: int) -> dict:
        _, alt, buy_zfo = weth_side(pool_row)
        res = {"buy_ok": False, "sell_ok": False, "tokens_bought": 0, "weth_back": 0,
               "slippage": None, "error": None, "alt_token": alt}
        buy = self.swap_exact_in(pool_row, token_in=WETH, amount_in=spend_wei, zero_for_one=buy_zfo)
        res["buy_ok"] = buy.get("ok")
        res["tokens_bought"] = buy.get("amount_out", 0)
        res["error"] = buy.get("error")
        if not res["buy_ok"]:
            return res
        sell = self.swap_exact_in(
            pool_row, token_in=alt, amount_in=res["tokens_bought"], zero_for_one=not buy_zfo
        )
        res["sell_ok"] = sell.get("ok")
        res["weth_back"] = sell.get("amount_out", 0)
        if sell.get("error"):
            res["error"] = sell["error"]
        if res["buy_ok"] and res["sell_ok"] and spend_wei:
            res["slippage"] = res["weth_back"] / spend_wei - 1.0
        return res
