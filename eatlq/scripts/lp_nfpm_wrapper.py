#!/usr/bin/env python3
"""
Uniswap V3 NonfungiblePositionManager wrapper (Path B plumbing).

Base NFPM: 0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1

Not invoked by tiny live tonight — use after reviewing lp_candidates.csv.
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

log = logging.getLogger("lp_nfpm")

NFPM_BASE = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
WETH_BASE = "0x4200000000000000000000000000000000000006"

# Minimal NFPM ABI fragments
NFPM_ABI = json.loads("""[
  {"inputs":[{"components":[
    {"name":"token0","type":"address"},{"name":"token1","type":"address"},
    {"name":"fee","type":"uint24"},{"name":"tickLower","type":"int24"},
    {"name":"tickUpper","type":"int24"},{"name":"amount0Desired","type":"uint256"},
    {"name":"amount1Desired","type":"uint256"},{"name":"amount0Min","type":"uint256"},
    {"name":"amount1Min","type":"uint256"},{"name":"recipient","type":"address"},
    {"name":"deadline","type":"uint256"}],"name":"params","type":"tuple"}],
   "name":"mint","outputs":[{"name":"tokenId","type":"uint256"},
   {"name":"liquidity","type":"uint128"},{"name":"amount0","type":"uint256"},
   {"name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"name":"tokenId","type":"uint256"}],
   "name":"positions","outputs":[
    {"name":"nonce","type":"uint96"},{"name":"operator","type":"address"},
    {"name":"token0","type":"address"},{"name":"token1","type":"address"},
    {"name":"fee","type":"uint24"},{"name":"tickLower","type":"int24"},
    {"name":"tickUpper","type":"int24"},{"name":"liquidity","type":"uint128"},
    {"name":"feeGrowthInside0LastX128","type":"uint256"},
    {"name":"feeGrowthInside1LastX128","type":"uint256"},
    {"name":"tokensOwed0","type":"uint128"},{"name":"tokensOwed1","type":"uint128"}],
   "stateMutability":"view","type":"function"},
  {"inputs":[{"components":[
    {"name":"tokenId","type":"uint256"},{"name":"recipient","type":"address"},
    {"name":"amount0Max","type":"uint128"},{"name":"amount1Max","type":"uint128"}],
    "name":"params","type":"tuple"}],
   "name":"collect","outputs":[
    {"name":"amount0","type":"uint256"},{"name":"amount1","type":"uint256"}],
   "stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[
    {"name":"tokenId","type":"uint256"},{"name":"liquidity","type":"uint128"},
    {"name":"amount0Min","type":"uint256"},{"name":"amount1Min","type":"uint256"},
    {"name":"deadline","type":"uint256"}],"name":"params","type":"tuple"}],
   "name":"decreaseLiquidity","outputs":[
    {"name":"amount0","type":"uint256"},{"name":"amount1","type":"uint256"}],
   "stateMutability":"payable","type":"function"},
  {"inputs":[{"name":"tokenId","type":"uint256"}],
   "name":"burn","outputs":[],"stateMutability":"payable","type":"function"}
]""")


@dataclass
class MintResult:
    success: bool
    token_id: Optional[int]
    tx_hash: Optional[str]
    error: Optional[str] = None


class NfpmWrapper:
    def __init__(self, w3: Web3, account) -> None:
        self.w3 = w3
        self.account = account
        self.nfpm = w3.eth.contract(
            address=Web3.to_checksum_address(NFPM_BASE), abi=NFPM_ABI,
        )

    def _deadline(self, offset: int = 600) -> int:
        return int(time.time()) + offset

    def _send(self, fn) -> str:
        tx = fn.build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": self.w3.eth.chain_id,
        })
        if "gas" not in tx:
            try:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            except Exception:
                tx["gas"] = 800_000
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt["status"] != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    def mint(
        self,
        token0: str,
        token1: str,
        fee: int,
        tick_lower: int,
        tick_upper: int,
        amount0: int,
        amount1: int,
        amount0_min: int = 0,
        amount1_min: int = 0,
    ) -> MintResult:
        try:
            params = (
                Web3.to_checksum_address(token0),
                Web3.to_checksum_address(token1),
                fee,
                tick_lower,
                tick_upper,
                amount0,
                amount1,
                amount0_min,
                amount1_min,
                self.account.address,
                self._deadline(),
            )
            fn = self.nfpm.functions.mint(params)
            tx_hash = self._send(fn)
            # tokenId from logs — simplified: caller parses receipt
            return MintResult(success=True, token_id=None, tx_hash=tx_hash)
        except Exception as e:
            log.error("mint failed: %s", e)
            return MintResult(success=False, token_id=None, tx_hash=None, error=str(e))

    def collect(self, token_id: int, amount0_max: int = 2**128 - 1, amount1_max: int = 2**128 - 1) -> str:
        params = (token_id, self.account.address, amount0_max, amount1_max)
        return self._send(self.nfpm.functions.collect(params))

    def decrease_and_burn(self, token_id: int, liquidity: int) -> str:
        dec_params = (token_id, liquidity, 0, 0, self._deadline())
        tx1 = self._send(self.nfpm.functions.decreaseLiquidity(dec_params))
        tx2 = self._send(self.nfpm.functions.burn(token_id))
        return f"{tx1},{tx2}"


def main() -> None:
    """CLI smoke test — requires LIVE_TRADING_MODE and session key."""
    import getpass
    from eth_account import Account

    logging.basicConfig(level=logging.INFO)
    rpc = os.environ.get("EXECUTOR_RPC_URL") or os.environ.get("EVM_HTTP_URL")
    w3 = Web3(Web3.HTTPProvider(rpc))
    pk = getpass.getpass("Private key: ").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    acct = Account.from_key(pk)
    w = NfpmWrapper(w3, acct)
    print(f"NFPM wrapper ready for {acct.address}")
    print("mint/collect/decrease_and_burn — call from Python after pool selection")


if __name__ == "__main__":
    main()
