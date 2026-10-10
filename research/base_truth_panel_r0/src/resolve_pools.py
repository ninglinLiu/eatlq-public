#!/usr/bin/env python3
"""Resolve token0/token1/fee/tickSpacing/factory for every pool in census.cl_swaps.

Batched through Multicall3 so the whole job costs a few hundred credits instead
of a few hundred thousand: one eth_call carries ~150 sub-calls.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

from eth_abi import decode, encode
from web3 import Web3

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
VENUE_BY_FACTORY = {
    "0x33128a8fc17869897dce68ed026d694621f6fdfd": "univ3",
    "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": "aero_cl",
    "0x420dd381b31aef6683db6b902084cb0ffece40da": "aero_vamm",
    "0x8909dc15e40173ff4699343b6eb8132c65e18ec6": "univ2",
}
UNIV3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
AERO_CL_FACTORY = "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a"
BATCH = 500
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client"]
RPC_URL = open("/tmp/rpc_url.txt").read().strip()

SEL = {
    "token0": Web3.keccak(text="token0()")[:4],
    "token1": Web3.keccak(text="token1()")[:4],
    "fee": Web3.keccak(text="fee()")[:4],
    "tickSpacing": Web3.keccak(text="tickSpacing()")[:4],
    "factory": Web3.keccak(text="factory()")[:4],
}
FIELDS = ["token0", "token1", "fee", "tickSpacing", "factory"]
_AGG3 = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
_stats = {"calls": 0}


def rpc(method: str, params: list, timeout: int = 120):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def multicall(calls: list[tuple[str, bytes]]) -> list[tuple[bool, bytes]]:
    """calls = [(target, calldata)]; allowFailure=True so one bad pool cannot kill a batch."""
    payload = encode(
        ["(address,bool,bytes)[]"],
        [[(Web3.to_checksum_address(t), True, cd) for t, cd in calls]],
    )
    for attempt in range(4):
        try:
            r = rpc("eth_call", [{"to": MULTICALL3, "data": "0x" + (_AGG3 + payload).hex()}, "latest"])
            if "error" in r:
                raise RuntimeError(r["error"].get("message", "")[:200])
            _stats["calls"] += 1
            return decode(["(bool,bytes)[]"], bytes.fromhex(r["result"][2:]))[0]
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def pools_to_do(min_swaps: int = 0, min_days: int = 0) -> list[str]:
    """Unresolved pools, optionally pre-filtered on panel-computable activity.

    The year-long panel contains well over a hundred thousand pools that traded
    briefly and vanished; resolving all of them would cost more than the backfill
    itself. Swap count and active-day count need no on-chain lookup, so filtering
    on them first is free. Defaults resolve everything, keeping earlier rounds
    reproducible.
    """
    if min_swaps <= 0 and min_days <= 0:
        q = """
        SELECT DISTINCT pool FROM census.cl_swaps
        WHERE pool NOT IN (SELECT pool FROM census.pools)
        """
    else:
        q = f"""
        SELECT pool FROM (
            SELECT pool, count() AS n_swaps,
                   countDistinct(toDate(block_time)) AS n_days
            FROM census.cl_swaps
            WHERE pool NOT IN (SELECT pool FROM census.pools)
            GROUP BY pool)
        WHERE n_swaps >= {min_swaps} AND n_days >= {min_days}
        """
    p = subprocess.run(CH + ["--query", q], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return [x for x in p.stdout.decode().split() if x.startswith("0x")]


def insert(tsv: str) -> None:
    if not tsv:
        return
    cols = "pool,token0,token1,fee,tick_spacing,factory,venue"
    p = subprocess.run(CH + ["--query", f"INSERT INTO census.pools ({cols}) FORMAT TSV"],
                       input=tsv.encode(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"insert failed: {p.stderr.decode()[:300]}")


def as_addr(ok: bool, raw: bytes) -> str:
    return "0x" + raw[-20:].hex() if ok and len(raw) >= 32 else ""


def as_int(ok: bool, raw: bytes) -> int:
    if not ok or len(raw) < 32:
        return -1
    v = int.from_bytes(raw[:32], "big")
    return v - (1 << 256) if v >> 255 else v


if __name__ == "__main__":
    min_swaps = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    min_days = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    todo = pools_to_do(min_swaps, min_days)
    print(f"[res] pools_to_resolve={len(todo)}  filter(min_swaps={min_swaps},"
          f"min_days={min_days})  batch={BATCH}  "
          f"sub_calls={len(todo)*len(FIELDS):,}  "
          f"est_eth_calls={-(-len(todo)*len(FIELDS)//BATCH)}  "
          f"est_credits={-(-len(todo)*len(FIELDS)//BATCH)*20:,}", flush=True)

    t0 = time.time()
    per = BATCH // len(FIELDS)          # whole pools per eth_call, keeps decoding aligned
    for i in range(0, len(todo), per):
        grp = todo[i:i + per]
        calls = [(p, SEL[f]) for p in grp for f in FIELDS]
        res = multicall(calls)
        rows = []
        for j, p in enumerate(grp):
            r = res[j * len(FIELDS):(j + 1) * len(FIELDS)]
            t0_, t1_ = as_addr(*r[0]), as_addr(*r[1])
            fee, tsp = as_int(*r[2]), as_int(*r[3])
            fac = as_addr(*r[4])
            venue = VENUE_BY_FACTORY.get(fac, "unknown" if fac else "no_factory_fn")
            rows.append("\t".join((p, t0_, t1_, str(fee), str(tsp), fac, venue)))
        insert("\n".join(rows))
        el = time.time() - t0
        done = min(i + per, len(todo))
        print(f"[res] {done}/{len(todo)} eth_calls={_stats['calls']} "
              f"el={el/60:.1f}m eta={(el/done)*(len(todo)-done)/60:.1f}m", flush=True)

    print(f"[res] DONE eth_calls={_stats['calls']} credits={_stats['calls']*20:,} "
          f"min={(time.time()-t0)/60:.1f}", flush=True)
