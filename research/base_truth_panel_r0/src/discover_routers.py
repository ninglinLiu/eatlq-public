#!/usr/bin/env python3
"""Map every factory in the target set to a router that will actually route it.

A CL router derives pool addresses from its own immutable factory, so a pool
built by factory X is unreachable through a router bound to factory Y. Rather
than hunt addresses per fork, we read the pools' own callers out of the backfill
and keep the caller whose `factory()` matches.
"""
from __future__ import annotations

import json
import subprocess
import urllib.request

from web3 import Web3

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client"]
RPC_URL = open("/tmp/rpc_url.txt").read().strip()
_FACTORY = Web3.keccak(text="factory()")[:4]
# Known-good starting points; auto-discovery fills in the rest.
SEED = {
    "0x33128a8fc17869897dce68ed026d694621f6fdfd": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
    "0xade65c38cd4849adba595a4323a8c7ddfe89716a": "0xcbBb8035cAc7D4B3Ca7aBb74cF7BdF900215Ce0D",
    "0xf8f2eb4940cfe7d13603dddd87f123820fc061ef": "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F",
}
calls = 0


def rpc(method: str, params: list):
    global calls
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    calls += 1
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def factory_of(addr: str) -> str | None:
    r = rpc("eth_call", [{"to": Web3.to_checksum_address(addr), "data": "0x" + _FACTORY.hex()},
                         "latest"])
    res = r.get("result") or ""
    return "0x" + res[26:66] if len(res) >= 66 else None


def top_senders(factory: str, n: int = 12) -> list[str]:
    q = (f"SELECT s.sender FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool=p.pool "
         f"WHERE p.factory='{factory}' GROUP BY s.sender ORDER BY count() DESC LIMIT {n}")
    p = subprocess.run(CH + ["--query", q], capture_output=True)
    return [x for x in p.stdout.decode().split() if x.startswith("0x")]


if __name__ == "__main__":
    hdr, *lines = open("out/targets_631.tsv").read().strip().split("\n")
    cols = hdr.split("\t")
    rows = [dict(zip(cols, ln.split("\t"))) for ln in lines]
    facs: dict[str, int] = {}
    for r in rows:
        facs[r["factory"]] = facs.get(r["factory"], 0) + 1

    out, unresolved = {}, []
    for fac, npools in sorted(facs.items(), key=lambda kv: -kv[1]):
        if fac in SEED and factory_of(SEED[fac]) == fac:
            out[fac] = {"router": SEED[fac], "pools": npools, "how": "seed_verified"}
            print(f"[rt] {fac} pools={npools:<4} router={SEED[fac]} (seed)", flush=True)
            continue
        hit = None
        for s in top_senders(fac):
            if factory_of(s) == fac:
                hit = s
                break
        if hit:
            out[fac] = {"router": Web3.to_checksum_address(hit), "pools": npools,
                        "how": "discovered_from_swap_senders"}
            print(f"[rt] {fac} pools={npools:<4} router={hit} (discovered)", flush=True)
        else:
            unresolved.append((fac, npools))
            print(f"[rt] {fac} pools={npools:<4} NO_ROUTER_FOUND", flush=True)

    json.dump(out, open("out/router_map.json", "w"), indent=1)
    cov = sum(v["pools"] for v in out.values())
    print(f"\n[rt] factories resolved={len(out)}/{len(facs)}  "
          f"pools covered={cov}/{len(rows)} ({100*cov/len(rows):.1f}%)  rpc_calls={calls}")
    if unresolved:
        print("[rt] unresolved:", ", ".join(f"{f}({n})" for f, n in unresolved))
