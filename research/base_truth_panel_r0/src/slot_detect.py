#!/usr/bin/env python3
"""Layout-agnostic ERC-20 storage slot detection.

The blind scan it replaces guessed `keccak(owner, slot)` for slot in 0..39 and
so could never reach ERC-7201 namespaced bases, Solady seeds, or any mapping
placed above the scanned range. Here we ask the chain which slots the call
actually touches (`eth_createAccessList`) and then confirm by override, which
is both deterministic and independent of the token's storage layout.
"""
from __future__ import annotations

import json
import urllib.request

from eth_abi import encode
from web3 import Web3

_BAL = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW = Web3.keccak(text="allowance(address,address)")[:4]
# EIP-1967 implementation pointer; always touched by a proxy, never the balance.
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
SKIP_SLOTS = {EIP1967_IMPL, EIP1967_ADMIN}


class SlotDetector:
    def __init__(self, rpc_url: str):
        self.url = rpc_url
        self.calls = 0
        self.cache: dict[str, dict | None] = {}
        self.reason: dict[str, str] = {}

    def _rpc(self, method: str, params: list, timeout: int = 60):
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def _candidates(self, token: str, data: str, block) -> tuple[list[str], bool]:
        """Storage keys the call reads, proxy bookkeeping slots removed.

        Second element flags balances that live on some *other* contract, e.g.
        Aave aTokens scale by a liquidity index held in the Pool. Those are not
        drivable by overriding the token alone and must not be silently failed.
        """
        r = self._rpc("eth_createAccessList",
                      [{"to": token, "data": data, "from": token}, _blk(block)])
        if "error" in r or not r.get("result"):
            return [], False
        out, external = [], False
        for e in r["result"].get("accessList", []):
            keys = [k for k in e["storageKeys"] if k.lower() not in SKIP_SLOTS]
            if e["address"].lower() != token.lower():
                external = external or bool(keys)
                continue          # implementation entry carries no state of its own
            out += keys
        return out, external

    def _reads(self, token: str, data: str, block, ov: dict | None = None) -> int | None:
        p = [{"to": token, "data": data}, _blk(block)] + ([ov] if ov else [])
        r = self._rpc("eth_call", p)
        if "error" in r or not r.get("result") or r["result"] == "0x":
            return None
        try:
            return int(r["result"][:66], 16)
        except ValueError:
            return None

    def detect(self, token: str, holder: str, spender: str, block="latest",
               probe: int = 0x7777) -> dict | None:
        """Return {'bal','allow'} on success, else None. `self.reason[token]` says why."""
        token = Web3.to_checksum_address(token)
        ck = f"{token}:{holder}:{spender}"
        if ck in self.cache:
            return self.cache[ck]

        bal_data = "0x" + (_BAL + encode(["address"], [holder])).hex()
        allow_data = "0x" + (_ALLOW + encode(["address", "address"], [holder, spender])).hex()
        pv = "0x" + probe.to_bytes(32, "big").hex()

        cands, external = self._candidates(token, bal_data, block)
        bal_key = self._confirm(token, bal_data, cands, block, probe, pv)
        if not bal_key:
            self.reason[token] = ("EXTERNAL_STATE_DEPENDENT" if external else
                                  "NO_CODE" if not self._has_code(token, block) else
                                  "SLOT_NOT_CONFIRMED")
            self.cache[ck] = None
            return None
        acands, _ = self._candidates(token, allow_data, block)
        allow_key = self._confirm(token, allow_data, acands, block, probe, pv)
        out = {"bal": bal_key, "allow": allow_key or bal_key}
        self.reason[token] = "OK"
        self.cache[ck] = out
        return out

    # A real ERC-20 is >1kB. Anything tiny is a stub, a destroyed contract, or an
    # EIP-7702-style designator, none of which can hold a balance we could drive.
    MIN_ERC20_CODE_BYTES = 64

    def _has_code(self, token: str, block) -> bool:
        r = self._rpc("eth_getCode", [token, _blk(block)])
        code = r.get("result") or "0x"
        return (len(code) - 2) // 2 >= self.MIN_ERC20_CODE_BYTES

    def _confirm(self, token: str, data: str, cands: list[str], block, probe: int,
                 pv: str) -> str | None:
        """A candidate is the right slot only if overriding it changes the return value."""
        for k in cands:
            ov = {token: {"stateDiff": {k: pv}}}
            if self._reads(token, data, block, ov) == probe:
                return k
        return None

    def grant(self, token: str, slots: dict, amount: int) -> dict:
        v = "0x" + int(amount).to_bytes(32, "big").hex()
        return {Web3.to_checksum_address(token): {"stateDiff": {slots["bal"]: v,
                                                                slots["allow"]: v}}}


def _blk(block) -> str:
    return block if isinstance(block, str) else hex(int(block))


if __name__ == "__main__":
    d = SlotDetector(open("/tmp/rpc_url.txt").read().strip())
    HOLDER = "0x1111111111111111111111111111111111111111"
    ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
    targets = {
        "WETH  (plain, control)": "0x4200000000000000000000000000000000000006",
        "USDC  (FiatToken proxy)": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDbC (FiatToken proxy)": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        "cbETH (OZ upgradeable)": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        "Basecat": "0x6a4bfe36ec1cc0b3e2ec9bb92b2ba60c94d78045",
        "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
    }
    print(f"{'token':<26} {'balance slot':<68} allowance slot")
    ok = 0
    for label, t in targets.items():
        s = d.detect(t, HOLDER, ROUTER)
        if s:
            ok += 1
            same = " (same as balance)" if s["allow"] == s["bal"] else ""
            print(f"{label:<26} {s['bal']:<68} {s['allow']}{same}")
        else:
            print(f"{label:<26} {'FAILED':<68} -")
    print(f"\nresolved {ok}/{len(targets)}   rpc_calls={d.calls}")
