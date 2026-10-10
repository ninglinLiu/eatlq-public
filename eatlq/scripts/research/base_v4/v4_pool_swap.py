"""V4 fork swap via deployed PoolSwapTest helper (hooks execute)."""
from __future__ import annotations

import json
import os
from typing import Optional

from eth_abi import encode
from web3 import Web3

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
POOL_MANAGER = Web3.to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")

FORGE_OUT = os.path.join(
    os.path.dirname(__file__), "foundry", "out", "V4ReplayHelper.sol", "V4ReplayHelper.json"
)

ERC20_ABI = [
    {"name": "approve", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"name": "balanceOf", "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]
WETH_ABI = ERC20_ABI + [
    {"name": "deposit", "inputs": [], "outputs": [], "stateMutability": "payable", "type": "function"},
]
HELPER_ABI = json.loads(
    open(FORGE_OUT).read()
)["abi"] if os.path.exists(FORGE_OUT) else [
    {"name": "roundtrip", "inputs": [
        {"components": [
            {"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"},
            {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks", "type": "address"},
        ], "name": "key", "type": "tuple"},
        {"name": "zeroForOneBuy", "type": "bool"},
        {"name": "amountIn", "type": "uint128"},
    ], "outputs": [{"name": "tokensOut", "type": "uint256"}, {"name": "wethBack", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
]


def pool_key_from_row(row: dict) -> tuple:
    return (
        Web3.to_checksum_address(row["currency0"]),
        Web3.to_checksum_address(row["currency1"]),
        int(row["fee"]),
        int(row["tickSpacing"]),
        Web3.to_checksum_address(row["hooks"]),
    )


def weth_zero_for_one(row: dict) -> bool:
    return Web3.to_checksum_address(row["currency0"]).lower() == WETH.lower()


def helper_bytecode() -> str:
    data = json.load(open(FORGE_OUT))
    return data["bytecode"]["object"]


def deploy_helper(w3: Web3, deployer: str, pk: str) -> str:
    ctor = encode(["address"], [POOL_MANAGER])
    bytecode = bytes.fromhex(helper_bytecode().replace("0x", "")) + ctor
    tx = {
        "from": deployer,
        "nonce": w3.eth.get_transaction_count(deployer),
        "data": bytecode,
        "gas": 8_000_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    signed = w3.eth.account.sign_transaction(tx, pk)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=90)
    if rcpt["status"] != 1:
        raise RuntimeError("helper deploy failed")
    return rcpt["contractAddress"]


class V4PoolSwapExecutor:
    def __init__(self, w3: Web3, account: str, private_key: str, helper: Optional[str] = None):
        self.w3 = w3
        self.acct = Web3.to_checksum_address(account)
        self.pk = private_key
        self.helper = Web3.to_checksum_address(helper) if helper else deploy_helper(w3, account, private_key)
        self.contract = w3.eth.contract(address=self.helper, abi=HELPER_ABI)
        self.weth = w3.eth.contract(address=WETH, abi=WETH_ABI)

    def _send(self, fn, *, value=0, gas=2_500_000):
        tx = fn.build_transaction({
            "from": self.acct, "nonce": self.w3.eth.get_transaction_count(self.acct),
            "value": value, "gas": gas, "gasPrice": self.w3.eth.gas_price, "chainId": self.w3.eth.chain_id,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.pk)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(h, timeout=90)

    def _approve_spender(self, token: str, spender: str) -> None:
        tok = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        self._send(tok.functions.approve(Web3.to_checksum_address(spender), 2**256 - 1))

    def roundtrip(self, pool_row: dict, spend_wei: int) -> dict:
        out = {"buy_ok": False, "sell_ok": False, "tokens_bought": 0, "weth_back": 0,
               "slippage": None, "error": None, "helper": self.helper, "spend_wei": spend_wei}
        zfo = weth_zero_for_one(pool_row)
        pk = pool_key_from_row(pool_row)
        alt = pk[1] if zfo else pk[0]
        try:
            weth_before = self.weth.functions.balanceOf(self.acct).call()
            self._send(self.weth.functions.deposit(), value=spend_wei)
            # PoolSwapTest settle: transferFrom(payer, PM) called BY helper — approve helper not PM
            self._approve_spender(WETH, self.helper)
            self._approve_spender(alt, self.helper)
            self._approve_spender(WETH, POOL_MANAGER)
            self._approve_spender(alt, POOL_MANAGER)
            tokens_out, _ignored = self.contract.functions.roundtrip(pk, zfo, spend_wei).call(
                {"from": self.acct}
            )
            rcpt = self._send(self.contract.functions.roundtrip(pk, zfo, spend_wei), gas=5_000_000)
            weth_after = self.weth.functions.balanceOf(self.acct).call()
            weth_net = weth_after - weth_before
            out.update({
                "buy_ok": rcpt["status"] == 1 and tokens_out > 0,
                "sell_ok": rcpt["status"] == 1 and weth_net > 0,
                "tokens_bought": int(tokens_out),
                "weth_back": int(weth_net),
                "alt_token": alt,
            })
            if out["buy_ok"] and out["sell_ok"]:
                out["slippage"] = weth_net / spend_wei - 1.0
            elif rcpt["status"] != 1:
                out["error"] = "roundtrip_revert"
            else:
                out["error"] = "partial_roundtrip"
        except Exception as e:
            out["error"] = f"{type(e).__name__}:{str(e)[:120]}"
        return out
