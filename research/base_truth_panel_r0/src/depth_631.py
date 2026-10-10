#!/usr/bin/env python3
"""Atomic round-trip depth on the 631 persistent WETH pools.

Both legs run inside ONE eth_call via Multicall3, so the sell executes against
the pool the buy just moved. The previous census simulated the legs separately,
which sells into an unmoved pool and therefore understates round-trip cost.

Routers differ by venue: SwapRouter02 keys pools by fee, Slipstream by
tickSpacing and takes a deadline, so a single encoder cannot serve both.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from eth_abi import decode, encode
from web3 import Web3

from slot_detect import SlotDetector

MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
ROUTERS = {k: Web3.to_checksum_address(v["router"])
           for k, v in json.load(open("out/router_map.json")).items()}
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ETH_USD = 4000.0
SIZES_USD = [50, 250, 1000, 5000]
DEADLINE = 1 << 40

_AGG3 = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
_V3_IN = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_AERO_IN = Web3.keccak(text="exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))")[:4]

RPC_URL = open("/tmp/rpc_url.txt").read().strip()
_lock = threading.Lock()
_stats = {"calls": 0, "t0": time.time(), "done": 0}


def rpc(method: str, params: list, timeout: int = 120):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            with _lock:
                _stats["calls"] += 1
            return r
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(1.0 * (attempt + 1))
    return {}


def swap_call(dialect: str, tin: str, tout: str, key: int, amt: int) -> bytes:
    """Slipstream keys pools by tickSpacing and takes a deadline; SwapRouter02 does neither."""
    if dialect == "slipstream":
        return _AERO_IN + encode(
            ["(address,address,int24,address,uint256,uint256,uint256,uint160)"],
            [(tin, tout, int(key), MULTICALL3, DEADLINE, int(amt), 0, 0)])
    return _V3_IN + encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(tin, tout, int(key), MULTICALL3, int(amt), 0, 0)])


# Forks are not labelled on-chain, so the working dialect is learned once per factory.
# Only the dialect is shared: the pool key is per-pool (fee tiers differ within a factory).
_dialect_cache: dict[str, str] = {}


def dialects_for(row: dict) -> list[tuple[str, int]]:
    """(dialect, pool key) pairs to try, best guess first."""
    fee, tsp = int(row["fee"]), int(row["tick_spacing"])
    opts = []
    if tsp >= 0:
        opts.append(("slipstream", tsp))
    if fee >= 0:
        opts.append(("univ3", fee))
    return opts if row["venue"] != "univ3" else list(reversed(opts))


def key_for(row: dict, dialect: str) -> int:
    return int(row["tick_spacing"]) if dialect == "slipstream" else int(row["fee"])


def aggregate3(calls: list[tuple[str, bytes]], ov: dict, block) -> list[tuple[bool, bytes]] | None:
    payload = encode(["(address,bool,bytes)[]"], [[(t, True, cd) for t, cd in calls]])
    blk = block if isinstance(block, str) else hex(int(block))
    r = rpc("eth_call", [{"to": MULTICALL3, "data": "0x" + (_AGG3 + payload).hex(),
                          "from": MULTICALL3}, blk, ov])
    if "error" in r or not r.get("result") or r["result"] == "0x":
        return None
    try:
        return decode(["(bool,bytes)[]"], bytes.fromhex(r["result"][2:]))[0]
    except Exception:  # noqa: BLE001
        return None


def amt_out(res: tuple[bool, bytes]) -> int | None:
    ok, raw = res
    if not ok or len(raw) < 32:
        return None
    return int.from_bytes(raw[:32], "big")


def probe_pool(row: dict, det: SlotDetector, block) -> list[dict]:
    alt = Web3.to_checksum_address(row["alt"])
    base = {k: row[k] for k in ("pool", "venue", "factory", "alt", "n_swaps", "n_days")}
    fail = lambda k: [{**base, "usd": u, "klass": k, "rt_bps": None} for u in SIZES_USD]  # noqa: E731

    router = ROUTERS.get(row["factory"])
    if not router:
        return fail("NO_ROUTER")
    ws = det.detect(WETH, MULTICALL3, router, block)
    als = det.detect(alt, MULTICALL3, router, block)
    if not (ws and als):
        return fail("UNRESOLVED_" + det.reason.get(alt, "SLOT_NOT_CONFIRMED"))

    big = 1 << 128
    ov = {WETH: {"stateDiff": {ws["bal"]: _w(big), ws["allow"]: _w(big)}},
          alt: {"stateDiff": {als["bal"]: _w(big), als["allow"]: _w(big)}}}

    # Settle the dialect on the smallest size, then reuse it for the rest.
    probe_amt = int(SIZES_USD[0] / ETH_USD * 1e18)
    known = _dialect_cache.get(row["factory"])
    tries = [(known, key_for(row, known))] if known else dialects_for(row)
    for d, key in tries:
        if key < 0:
            continue
        r = aggregate3([(router, swap_call(d, WETH, alt, key, probe_amt))], ov, block)
        if r and amt_out(r[0]):
            _dialect_cache.setdefault(row["factory"], d)
            break
    else:
        return fail("BUY_REVERT")
    out = []
    for usd in SIZES_USD:
        amt_in = int(usd / ETH_USD * 1e18)
        buy = (router, swap_call(d, WETH, alt, int(key), amt_in))
        r1 = aggregate3([buy], ov, block)
        got = amt_out(r1[0]) if r1 else None
        if not got:
            out.append({**base, "usd": usd, "klass": "BUY_REVERT", "rt_bps": None, "dialect": d})
            continue
        # Replay the buy so the sell hits the pool the buy moved, then sell exactly its output.
        sell = (router, swap_call(d, alt, WETH, int(key), got))
        r2 = aggregate3([buy, sell], ov, block)
        back = amt_out(r2[1]) if r2 and len(r2) > 1 else None
        if not back:
            out.append({**base, "usd": usd, "klass": "SELL_REVERT", "rt_bps": None,
                        "alt_out": got, "dialect": d})
            continue
        out.append({**base, "usd": usd, "klass": "OK", "alt_out": got, "dialect": d,
                    "weth_back": back / 1e18,
                    "rt_bps": (1.0 - back / amt_in) * 10000.0})
    return out


def _w(v: int) -> str:
    return "0x" + int(v).to_bytes(32, "big").hex()


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "out/targets_631.tsv"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    dest = sys.argv[3] if len(sys.argv) > 3 else "out/depth_631.json"
    hdr, *lines = open(src).read().strip().split("\n")
    cols = hdr.split("\t")
    rows = [dict(zip(cols, ln.split("\t"))) for ln in lines]
    if limit:
        rows = rows[:limit]
    block = int(rpc("eth_blockNumber", [])["result"], 16) - 20
    print(f"[d631] pools={len(rows)} block={block} sizes={SIZES_USD}", flush=True)

    det = SlotDetector(RPC_URL)
    results: list[dict] = []

    def work(r):
        try:
            got = probe_pool(r, det, block)
        except Exception as e:  # noqa: BLE001
            got = [{"pool": r["pool"], "venue": r["venue"], "factory": r["factory"],
                    "alt": r["alt"], "n_swaps": r["n_swaps"], "n_days": r["n_days"],
                    "usd": u, "klass": f"ERROR_{type(e).__name__}", "rt_bps": None}
                   for u in SIZES_USD]
        with _lock:
            results.extend(got)
            _stats["done"] += 1
            d, el = _stats["done"], time.time() - _stats["t0"]
            if d % 20 == 0 or d == len(rows):
                print(f"[d631] {d}/{len(rows)} calls={_stats['calls']} "
                      f"el={el/60:.1f}m eta={(el/d)*(len(rows)-d)/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, rows))

    with open(dest, "w") as f:
        json.dump({"block": block, "sizes_usd": SIZES_USD, "eth_usd": ETH_USD,
                   "rpc_calls": _stats["calls"] + det.calls, "rows": results}, f, indent=1)
    print(f"[d631] DONE rows={len(results)} calls={_stats['calls']+det.calls} "
          f"credits={(_stats['calls']+det.calls)*20:,} min={(time.time()-_stats['t0'])/60:.1f}",
          flush=True)
