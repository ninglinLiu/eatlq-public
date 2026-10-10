"""Archive router buy + atomic sell. Adapted from greenfield V1.1 / keystone R2. Local cache only."""
from __future__ import annotations

import json
import time
from pathlib import Path

from eth_abi import encode
from web3 import Web3

from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "out" / "cache_atomic"
CACHE.mkdir(parents=True, exist_ok=True)

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
EXECUTOR = Web3.to_checksum_address("0x4B7cd953871663474a0A25B81bddDf5C1f1195A7")
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
AMT_IN = 16666666666666666
GAS_DEBIT = 0.0002
ECONOMIC_TAX = 0.90

_FEE = Web3.keccak(text="fee()")[:4]
_QUOTE = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_SWAP = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW = Web3.keccak(text="allowance(address,address)")[:4]


def get_rpc_url() -> str:
    return Path("/tmp/rpc_url.txt").read_text().strip().splitlines()[0].strip()


class AtomicSim:
    def __init__(self):
        research_entry_guard()
        self.w3 = Web3(Web3.HTTPProvider(get_rpc_url(), request_kwargs={"timeout": 60}))
        self.calls = 0
        self._weth_slots = None
        self._token_slots: dict[str, tuple] = {}

    def _rpc(self, method, params):
        self.calls += 1
        last = None
        for i in range(3):
            try:
                return self.w3.provider.make_request(method, params)
            except Exception as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise RuntimeError(last)

    def _call(self, tx, block, ov=None):
        blk = hex(int(block))
        if ov is None:
            return self._rpc("eth_call", [tx, blk])
        return self._rpc("eth_call", [tx, blk, ov])

    def _cache(self, name: str, payload: dict) -> dict:
        p = CACHE / f"{name}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _cached(self, name: str) -> dict | None:
        p = CACHE / f"{name}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def onchain_fee(self, pool: str, block: int) -> dict:
        key = f"fee_{pool.lower()}_{block}"
        hit = self._cached(key)
        if hit:
            return hit
        r = self._call({"to": Web3.to_checksum_address(pool), "data": "0x" + _FEE.hex()}, block)
        if r.get("error") or not r.get("result") or r["result"] == "0x":
            return self._cache(key, {"ok": False, "fee": None, "reason": str(r.get("error", "empty"))[:200]})
        return self._cache(key, {"ok": True, "fee": int(r["result"], 16), "reason": ""})

    def quote(self, token_in: str, token_out: str, fee: int, amt: int, block: int) -> dict:
        key = f"q_{token_in.lower()}_{token_out.lower()}_{fee}_{amt}_{block}"
        hit = self._cached(key)
        if hit:
            return hit
        params = encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), int(amt), int(fee), 0)],
        )
        r = self._call({"to": QUOTER, "data": "0x" + (_QUOTE + params).hex()}, block)
        if r.get("error"):
            return self._cache(key, {"ok": False, "amount_out": 0, "reason": str(r["error"])[:200]})
        res = r.get("result") or "0x"
        try:
            out = int(res[2:66], 16) if len(res) >= 66 else 0
        except Exception:
            out = 0
        if out <= 0:
            return self._cache(key, {"ok": False, "amount_out": 0, "reason": "zero_out"})
        return self._cache(key, {"ok": True, "amount_out": out, "reason": ""})

    def _slot_key(self, owner, slot, lay, kind, spender=None):
        if kind == "bal":
            if lay == "sol":
                return "0x" + Web3.keccak(encode(["address", "uint256"], [owner, slot])).hex()
            return "0x" + Web3.keccak(encode(["uint256", "address"], [slot, owner])).hex()
        inner = (
            Web3.keccak(encode(["address", "uint256"], [owner, slot]))
            if lay == "sol"
            else Web3.keccak(encode(["uint256", "address"], [slot, owner]))
        )
        return "0x" + Web3.keccak(encode(["address", "bytes32"], [spender, inner])).hex()

    def _detect_erc20_slots(self, token: str, owner, spender, block: int, probe_val: int):
        token = Web3.to_checksum_address(token)
        bdata = "0x" + (_BAL + encode(["address"], [owner])).hex()
        bslot = blay = None
        for slot in range(20):
            for lay in ("sol", "vyper"):
                skey = self._slot_key(owner, slot, lay, "bal")
                ov = {token: {"stateDiff": {skey: "0x" + probe_val.to_bytes(32, "big").hex()}}}
                r = self._call({"to": token, "data": bdata}, block, ov)
                val = r.get("result")
                try:
                    if val and int(val, 16) == probe_val:
                        bslot, blay = slot, lay
                        break
                except Exception:
                    pass
            if bslot is not None:
                break
        if bslot is None:
            return None
        adata = "0x" + (_ALLOW + encode(["address", "address"], [owner, spender])).hex()
        aslot = alay = None
        for slot in range(20):
            for lay in ("sol", "vyper"):
                skey = self._slot_key(owner, slot, lay, "allow", spender)
                ov = {token: {"stateDiff": {skey: "0x" + probe_val.to_bytes(32, "big").hex()}}}
                r = self._call({"to": token, "data": adata}, block, ov)
                val = r.get("result")
                try:
                    if val and int(val, 16) == probe_val:
                        aslot, alay = slot, lay
                        break
                except Exception:
                    pass
            if aslot is not None:
                break
        if aslot is None:
            aslot, alay = bslot, blay
        return bslot, blay, aslot, alay

    def router_buy(self, token: str, fee: int, amt: int, block: int) -> dict:
        token = Web3.to_checksum_address(token)
        key = f"buy_{token.lower()}_{fee}_{amt}_{block}"
        hit = self._cached(key)
        if hit:
            return hit
        if self._weth_slots is None:
            self._weth_slots = self._detect_erc20_slots(WETH, EXECUTOR, ROUTER, block, amt * 3)
        if self._weth_slots is None:
            return self._cache(key, {"class": "INFRASTRUCTURE_UNKNOWN", "token_out": None, "reason": "weth_slot"})
        bslot, blay, aslot, alay = self._weth_slots
        bkey = self._slot_key(EXECUTOR, bslot, blay, "bal")
        akey = self._slot_key(EXECUTOR, aslot, alay, "allow", ROUTER)
        grant = "0x" + (amt * 3).to_bytes(32, "big").hex()
        ov = {WETH: {"stateDiff": {bkey: grant, akey: grant}}}
        params = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(WETH, token, int(fee), EXECUTOR, int(amt), 0, 0)],
        )
        tx = {"from": EXECUTOR, "to": ROUTER, "data": "0x" + (_SWAP + params).hex(), "gas": hex(3_000_000)}
        try:
            r = self._call(tx, block, ov)
        except Exception as e:
            return self._cache(key, {"class": "INFRASTRUCTURE_UNKNOWN", "token_out": None, "reason": f"transport:{type(e).__name__}"})
        if r.get("error"):
            return self._cache(key, {"class": "ENTRY_TX_REVERT", "token_out": 0, "reason": str(r["error"])[:240]})
        res = r.get("result") or "0x"
        try:
            token_out = int(res[:66], 16) if res != "0x" else 0
        except Exception:
            token_out = 0
        if token_out <= 0:
            return self._cache(key, {"class": "ENTRY_TX_REVERT", "token_out": 0, "reason": "zero_out"})
        return self._cache(key, {"class": "BUY_TX_SUCCESS", "token_out": token_out, "reason": ""})

    def atomic_sell(self, token: str, fee: int, amount: int, block: int) -> dict:
        token = Web3.to_checksum_address(token)
        key = f"sell_{token.lower()}_{fee}_{amount}_{block}"
        hit = self._cached(key)
        if hit:
            return hit
        slots = self._token_slots.get(token)
        if slots is None:
            slots = self._detect_erc20_slots(token, PROBE, ROUTER, block, 0x7777)
            self._token_slots[token] = slots
        if slots is None:
            return self._cache(key, {"class": "INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": "token_slot"})
        bslot, blay, aslot, alay = slots
        bkey = self._slot_key(PROBE, bslot, blay, "bal")
        akey = self._slot_key(PROBE, aslot, alay, "allow", ROUTER)
        grant = "0x" + max(int(amount) * 2, int(amount) + 1).to_bytes(32, "big").hex()
        ov = {
            token: {"stateDiff": {bkey: grant, akey: grant}},
            PROBE: {"balance": "0x" + (10**21).to_bytes(32, "big").hex()},
        }
        params = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(token, WETH, int(fee), PROBE, int(amount), 0, 0)],
        )
        tx = {"from": PROBE, "to": ROUTER, "data": "0x" + (_SWAP + params).hex(), "gas": hex(3_000_000)}
        try:
            r = self._call(tx, block, ov)
        except Exception as e:
            return self._cache(key, {"class": "INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": f"transport:{type(e).__name__}"})
        if r.get("error"):
            return self._cache(key, {"class": "DETERMINISTIC_REVERT", "weth_out": 0, "reason": str(r["error"])[:240]})
        res = r.get("result") or "0x"
        try:
            wout = int(res[:66], 16) if res != "0x" else 0
        except Exception:
            wout = 0
        if wout <= 0:
            return self._cache(key, {"class": "DETERMINISTIC_REVERT", "weth_out": 0, "reason": "zero_out"})
        if wout < AMT_IN * (1.0 - ECONOMIC_TAX):
            return self._cache(key, {"class": "ECONOMICALLY_UNSELLABLE", "weth_out": wout, "reason": "tax>=90%"})
        return self._cache(key, {"class": "SELL_SUCCESS", "weth_out": wout, "reason": ""})

    def calibrate_smoke(self) -> dict:
        """One known success + one known rug from keystone calibration table."""
        cal = Path("/root/keystone_r2_atomic/out/atomic_simulator_calibration.csv")
        import pandas as pd

        df = pd.read_csv(cal)
        succ = df[df.truth == "SUCCESS_SELL"].iloc[0]
        rug = df[df.truth == "RUG_UNSELLABLE"].iloc[0]
        s = self.atomic_sell(succ.token, int(succ.fee), int(succ.token_amount), int(succ.block))
        u = self.atomic_sell(rug.token, int(rug.fee), int(rug.token_amount), int(rug.block))
        ok = s["class"] == "SELL_SUCCESS" and u["class"] in ("DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE")
        return {
            "pass": bool(ok),
            "success_case": {"token": succ.token, "sim": s},
            "rug_case": {"token": rug.token, "sim": u},
        }
