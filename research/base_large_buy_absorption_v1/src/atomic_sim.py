"""Local adapted copy of R2 ArchiveSim — research-only, no production writes."""
from __future__ import annotations

import json
import time
from pathlib import Path

from eth_abi import encode
from web3 import Web3

from .common import OUT, WETH

ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
SELL_GAS = 3_000_000
CACHE = OUT / "cache" / "atomic_sell"

_SWAP_SIG = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL_SIG = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW_SIG = Web3.keccak(text="allowance(address,address)")[:4]
_FEE_SIG = Web3.keccak(text="fee()")[:4]


def get_rpc_url() -> str:
    p = Path("/tmp/rpc_url.txt")
    if p.exists():
        return p.read_text().strip()
    raise RuntimeError("Missing /tmp/rpc_url.txt for archive RPC")


class ArchiveSim:
    def __init__(self):
        self.url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": 60}))
        self._slot_cache: dict = {}
        self.calls = 0
        CACHE.mkdir(parents=True, exist_ok=True)

    def _call(self, method, params):
        self.calls += 1
        last = None
        for i in range(3):
            try:
                return self.w3.provider.make_request(method, params)
            except Exception as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise RuntimeError(last)

    def pool_fee(self, pool: str) -> int | None:
        r = self._call(
            "eth_call",
            [{"to": Web3.to_checksum_address(pool), "data": "0x" + _FEE_SIG.hex()}, "latest"],
        )
        res = r.get("result")
        if not res or res == "0x":
            return None
        return int(res, 16)

    def detect_slots(self, token: str, block):
        token = Web3.to_checksum_address(token)
        if token in self._slot_cache:
            return self._slot_cache[token]
        bslot = blay = None
        bdata = "0x" + (_BAL_SIG + encode(["address"], [PROBE])).hex()
        for slot in range(20):
            for lay in ("sol", "vyper"):
                if lay == "sol":
                    key = "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, slot])).hex()
                else:
                    key = "0x" + Web3.keccak(encode(["uint256", "address"], [slot, PROBE])).hex()
                ov = {token: {"stateDiff": {key: "0x" + (0x7777).to_bytes(32, "big").hex()}}}
                r = self._call("eth_call", [{"to": token, "data": bdata}, hex(int(block)), ov])
                val = r.get("result")
                try:
                    if val and int(val, 16) == 0x7777:
                        bslot, blay = slot, lay
                        break
                except Exception:
                    pass
            if bslot is not None:
                break
        aslot = alay = None
        if bslot is not None:
            adata = "0x" + (_ALLOW_SIG + encode(["address", "address"], [PROBE, ROUTER])).hex()
            for slot in range(20):
                for lay in ("sol", "vyper"):
                    if lay == "sol":
                        inner = Web3.keccak(encode(["address", "uint256"], [PROBE, slot]))
                    else:
                        inner = Web3.keccak(encode(["uint256", "address"], [slot, PROBE]))
                    key = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
                    ov = {token: {"stateDiff": {key: "0x" + (0x8888).to_bytes(32, "big").hex()}}}
                    r = self._call("eth_call", [{"to": token, "data": adata}, hex(int(block)), ov])
                    val = r.get("result")
                    try:
                        if val and int(val, 16) == 0x8888:
                            aslot, alay = slot, lay
                            break
                    except Exception:
                        pass
                if aslot is not None:
                    break
        self._slot_cache[token] = (bslot, blay, aslot, alay)
        return self._slot_cache[token]

    def atomic_sell(self, token: str, fee: int, amount: int, block: int) -> dict:
        token = Web3.to_checksum_address(token)
        cache_key = CACHE / f"{token.lower()}_{int(block)}_{int(fee)}_{int(amount)}.json"
        if cache_key.exists():
            return json.loads(cache_key.read_text())
        probe = PROBE
        bslot, blay, aslot, alay = self.detect_slots(token, block)
        if bslot is None or aslot is None:
            out = {
                "simulation_status": "INFRASTRUCTURE_UNKNOWN",
                "atomic_weth_out": None,
                "revert_reason": "slot_undetectable",
                "gas": SELL_GAS,
            }
            cache_key.write_text(json.dumps(out))
            return out
        if blay == "sol":
            bkey = "0x" + Web3.keccak(encode(["address", "uint256"], [probe, bslot])).hex()
        else:
            bkey = "0x" + Web3.keccak(encode(["uint256", "address"], [bslot, probe])).hex()
        if alay == "sol":
            inner = Web3.keccak(encode(["address", "uint256"], [probe, aslot]))
        else:
            inner = Web3.keccak(encode(["uint256", "address"], [aslot, probe]))
        akey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
        grant = max(int(amount) * 2, int(amount) + 1)
        gh = "0x" + grant.to_bytes(32, "big").hex()
        ov = {
            token: {"stateDiff": {bkey: gh, akey: gh}},
            probe: {"balance": "0x" + (1000 * 10**18).to_bytes(32, "big").hex()},
        }
        params = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(token, Web3.to_checksum_address(WETH), int(fee), probe, int(amount), 0, 0)],
        )
        data = "0x" + (_SWAP_SIG + params).hex()
        tx = {"from": probe, "to": ROUTER, "data": data, "gas": hex(SELL_GAS)}
        try:
            r = self._call("eth_call", [tx, hex(int(block)), ov])
        except Exception as e:
            out = {
                "simulation_status": "INFRASTRUCTURE_UNKNOWN",
                "atomic_weth_out": None,
                "revert_reason": f"transport:{type(e).__name__}",
                "gas": SELL_GAS,
            }
            cache_key.write_text(json.dumps(out))
            return out
        if r.get("error") is not None:
            out = {
                "simulation_status": "DETERMINISTIC_REVERT",
                "atomic_weth_out": 0,
                "revert_reason": str(r["error"])[:200],
                "gas": SELL_GAS,
            }
            cache_key.write_text(json.dumps(out))
            return out
        res = r.get("result") or "0x"
        try:
            wout = int(res[:66], 16) if res and res != "0x" else 0
        except Exception:
            wout = 0
        if wout <= 0:
            out = {
                "simulation_status": "DETERMINISTIC_REVERT",
                "atomic_weth_out": 0,
                "revert_reason": "zero_out",
                "gas": SELL_GAS,
            }
        else:
            out = {
                "simulation_status": "SELL_SUCCESS",
                "atomic_weth_out": wout,
                "revert_reason": "",
                "gas": SELL_GAS,
            }
        cache_key.write_text(json.dumps(out))
        return out
