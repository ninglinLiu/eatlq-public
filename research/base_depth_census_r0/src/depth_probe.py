"""Atomic round-trip depth probe for a fixed pool list. Read-only eth_call + state override."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from eth_abi import encode
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "NO_LIVE.lock"
if not LOCK.exists():
    raise SystemExit("NO_LIVE.lock missing: refusing to run")

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
HOLDER = Web3.to_checksum_address("0x4B7cd953871663474a0A25B81bddDf5C1f1195A7")

QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")

_SWAP = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW = Web3.keccak(text="allowance(address,address)")[:4]
_FEE = Web3.keccak(text="fee()")[:4]
_QUOTE = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]

ETH_USD = 3000.0
SIZES_USD = [50, 250, 1000, 5000]


class Probe:
    def __init__(self):
        url = Path("/tmp/rpc_url.txt").read_text().strip().splitlines()[0].strip()
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
        self.slots: dict[str, tuple | None] = {}
        self.calls = 0

    def _rpc(self, method, params):
        self.calls += 1
        last = None
        for i in range(4):
            try:
                return self.w3.provider.make_request(method, params)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.3 * (i + 1))
        raise RuntimeError(last)

    def _call(self, tx, block, ov=None):
        blk = hex(int(block))
        return self._rpc("eth_call", [tx, blk] + ([ov] if ov else []))

    @staticmethod
    def _bal_key(owner, slot, lay):
        if lay == "sol":
            return "0x" + Web3.keccak(encode(["address", "uint256"], [owner, slot])).hex()
        return "0x" + Web3.keccak(encode(["uint256", "address"], [slot, owner])).hex()

    @staticmethod
    def _allow_key(owner, slot, lay, spender):
        inner = (
            Web3.keccak(encode(["address", "uint256"], [owner, slot]))
            if lay == "sol"
            else Web3.keccak(encode(["uint256", "address"], [slot, owner]))
        )
        return "0x" + Web3.keccak(encode(["address", "bytes32"], [spender, inner])).hex()

    def detect(self, token: str, block: int, probe_val: int = 0x7777):
        token = Web3.to_checksum_address(token)
        if token in self.slots:
            return self.slots[token]
        bdata = "0x" + (_BAL + encode(["address"], [HOLDER])).hex()
        found = None
        for slot in range(40):
            for lay in ("sol", "vyper"):
                key = self._bal_key(HOLDER, slot, lay)
                ov = {token: {"stateDiff": {key: "0x" + probe_val.to_bytes(32, "big").hex()}}}
                r = self._call({"to": token, "data": bdata}, block, ov)
                try:
                    if r.get("result") and int(r["result"], 16) == probe_val:
                        found = (slot, lay)
                        break
                except Exception:  # noqa: BLE001
                    pass
            if found:
                break
        if not found:
            self.slots[token] = None
            return None
        bslot, blay = found
        adata = "0x" + (_ALLOW + encode(["address", "address"], [HOLDER, ROUTER])).hex()
        afound = None
        for slot in range(40):
            for lay in ("sol", "vyper"):
                key = self._allow_key(HOLDER, slot, lay, ROUTER)
                ov = {token: {"stateDiff": {key: "0x" + probe_val.to_bytes(32, "big").hex()}}}
                r = self._call({"to": token, "data": adata}, block, ov)
                try:
                    if r.get("result") and int(r["result"], 16) == probe_val:
                        afound = (slot, lay)
                        break
                except Exception:  # noqa: BLE001
                    pass
            if afound:
                break
        out = (bslot, blay) + (afound if afound else (bslot, blay))
        self.slots[token] = out
        return out

    def swap(self, token_in: str, token_out: str, fee: int, amt: int, block: int) -> dict:
        token_in = Web3.to_checksum_address(token_in)
        token_out = Web3.to_checksum_address(token_out)
        slots = self.detect(token_in, block)
        if slots is None:
            return {"class": "INFRASTRUCTURE_UNKNOWN", "out": None, "reason": "slot_undetected"}
        bslot, blay, aslot, alay = slots
        grant = "0x" + (int(amt) * 3).to_bytes(32, "big").hex()
        ov = {
            token_in: {
                "stateDiff": {
                    self._bal_key(HOLDER, bslot, blay): grant,
                    self._allow_key(HOLDER, aslot, alay, ROUTER): grant,
                }
            },
            HOLDER: {"balance": "0x" + (10**21).to_bytes(32, "big").hex()},
        }
        params = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(token_in, token_out, int(fee), HOLDER, int(amt), 0, 0)],
        )
        tx = {"from": HOLDER, "to": ROUTER, "data": "0x" + (_SWAP + params).hex(), "gas": hex(3_000_000)}
        try:
            r = self._call(tx, block, ov)
        except Exception as e:  # noqa: BLE001
            return {"class": "INFRASTRUCTURE_UNKNOWN", "out": None, "reason": f"transport:{type(e).__name__}"}
        if r.get("error"):
            return {"class": "DETERMINISTIC_REVERT", "out": 0, "reason": str(r["error"])[:200]}
        res = r.get("result") or "0x"
        try:
            got = int(res[:66], 16) if res != "0x" else 0
        except Exception:  # noqa: BLE001
            got = 0
        if got <= 0:
            return {"class": "DETERMINISTIC_REVERT", "out": 0, "reason": "zero_out"}
        return {"class": "SUCCESS", "out": got, "reason": ""}

    def quote(self, token_in: str, token_out: str, fee: int, amt: int, block: int) -> int | None:
        """Comparison-only leg. Valid for majors where DETERMINISTIC_REVERT is not in play."""
        params = encode(
            ["(address,address,uint256,uint24,uint160)"],
            [
                (
                    Web3.to_checksum_address(token_in),
                    Web3.to_checksum_address(token_out),
                    int(amt),
                    int(fee),
                    0,
                )
            ],
        )
        r = self._call({"to": QUOTER, "data": "0x" + (_QUOTE + params).hex()}, block)
        if r.get("error"):
            return None
        res = r.get("result") or "0x"
        try:
            return int(res[2:66], 16) if len(res) >= 66 else None
        except Exception:  # noqa: BLE001
            return None

    def quote_round_trip(self, alt: str, fee: int, usd: float, block: int) -> dict:
        amt_in = int(usd / ETH_USD * 1e18)
        mid = self.quote(WETH, alt, fee, amt_in, block)
        row = {"usd": usd, "qv2_alt_out": mid, "qv2_weth_back": None, "qv2_round_trip_bps": None}
        if not mid:
            return row
        back = self.quote(alt, WETH, fee, int(mid), block)
        if not back:
            return row
        row["qv2_weth_back"] = back / 1e18
        row["qv2_round_trip_bps"] = (1.0 - back / amt_in) * 10000.0
        return row

    def pool_fee(self, pool: str, block: int) -> int | None:
        r = self._call({"to": Web3.to_checksum_address(pool), "data": "0x" + _FEE.hex()}, block)
        if r.get("error") or not r.get("result") or r["result"] == "0x":
            return None
        return int(r["result"], 16)

    def round_trip(self, alt: str, fee: int, usd: float, block: int) -> dict:
        amt_in = int(usd / ETH_USD * 1e18)
        buy = self.swap(WETH, alt, fee, amt_in, block)
        row = {
            "usd": usd,
            "amt_in_weth": amt_in / 1e18,
            "buy_class": buy["class"],
            "buy_out": buy["out"],
            "sell_class": None,
            "weth_back": None,
            "round_trip_bps": None,
            "reason": buy["reason"],
        }
        if buy["class"] != "SUCCESS":
            return row
        sell = self.swap(alt, WETH, fee, int(buy["out"]), block)
        row["sell_class"] = sell["class"]
        row["reason"] = sell["reason"]
        if sell["class"] != "SUCCESS":
            return row
        back = int(sell["out"])
        row["weth_back"] = back / 1e18
        row["round_trip_bps"] = (1.0 - back / amt_in) * 10000.0
        return row


def main() -> int:
    targets = json.loads((ROOT / "out" / "probe_targets.json").read_text())
    p = Probe()
    block = int(p.w3.eth.block_number) - 20
    print(f"[probe] block={block}", flush=True)
    rows = []
    for t in targets:
        fee = t.get("fee") or p.pool_fee(t["pool"], block)
        if fee is None:
            print(f"[probe] {t['label']}: fee() unavailable, skip", flush=True)
            continue
        for usd in SIZES_USD:
            r = p.round_trip(t["alt"], int(fee), usd, block)
            r.update(p.quote_round_trip(t["alt"], int(fee), usd, block))
            r.update({"label": t["label"], "pool": t["pool"], "alt": t["alt"], "fee": int(fee), "block": block})
            rows.append(r)
            bps, qbps = r["round_trip_bps"], r["qv2_round_trip_bps"]
            print(
                f"[probe] {t['label']:<18} ${usd:>5} fee={fee:<6} "
                f"buy={r['buy_class']:<22} sell={str(r['sell_class']):<22} "
                f"router_rt_bps={'NA' if bps is None else f'{bps:8.1f}'} "
                f"qv2_rt_bps={'NA' if qbps is None else f'{qbps:8.1f}'}",
                flush=True,
            )
    out = ROOT / "out" / "depth_probe.json"
    out.write_text(json.dumps({"block": block, "rpc_calls": p.calls, "rows": rows}, indent=2))
    print(f"[probe] wrote {out} calls={p.calls}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
