#!/usr/bin/env python3
"""Resolve symbol and decimals for every alt token in census.pit_universe.

eatlq.token_registry knows 7 of the 2,462 alts in the year panel, so it cannot
be used to label anything. Batched through Multicall3 the whole job is about
fifty eth_calls.

symbol() is not reliably a string. Some older tokens declare bytes32, and a few
revert entirely. All three cases are handled and recorded rather than dropped,
because a token whose symbol cannot be read is still a tradeable token and
silently losing it would shrink the universe for a cosmetic reason.
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
RPC_URL = open("/tmp/rpc_url.txt").read().strip()
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client"]
SEL_SYM = Web3.keccak(text="symbol()")[:4]
SEL_DEC = Web3.keccak(text="decimals()")[:4]
_AGG3 = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
PER_CALL = 100          # tokens per eth_call (2 sub-calls each)
_stats = {"calls": 0}


def rpc(method: str, params: list, timeout: int = 120):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def multicall(calls: list[tuple[str, bytes]]) -> list[tuple[bool, bytes]]:
    payload = encode(["(address,bool,bytes)[]"],
                     [[(Web3.to_checksum_address(t), True, cd) for t, cd in calls]])
    for attempt in range(4):
        try:
            r = rpc("eth_call", [{"to": MULTICALL3,
                                  "data": "0x" + (_AGG3 + payload).hex()}, "latest"])
            if "error" in r:
                raise RuntimeError(str(r["error"])[:200])
            _stats["calls"] += 1
            return decode(["(bool,bytes)[]"], bytes.fromhex(r["result"][2:]))[0]
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def as_symbol(ok: bool, raw: bytes) -> str:
    """ABI string, bytes32, or unreadable -- all three occur on Base."""
    if not ok or not raw:
        return ""
    try:
        s = decode(["string"], raw)[0]
        if s:
            return s[:32]
    except Exception:  # noqa: BLE001
        pass
    try:
        s = raw[:32].rstrip(b"\x00").decode("utf-8", "ignore")
        if s.isprintable():
            return s[:32]
    except Exception:  # noqa: BLE001
        pass
    return ""


def as_dec(ok: bool, raw: bytes) -> int:
    if not ok or len(raw) < 32:
        return -1
    return int.from_bytes(raw[:32], "big") & 0xFF


def main() -> None:
    q = "SELECT DISTINCT alt FROM census.pit_universe ORDER BY alt"
    p = subprocess.run(CH + ["--query", q], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(p.stderr.decode()[:400])
    toks = [x for x in p.stdout.decode().split() if x.startswith("0x")]
    print(f"[tok] tokens={len(toks)} est_eth_calls={-(-len(toks)//PER_CALL)} "
          f"est_credits={-(-len(toks)//PER_CALL)*20:,}", flush=True)

    subprocess.run(CH + ["--query", "DROP TABLE IF EXISTS census.tokens"],
                   capture_output=True)
    subprocess.run(CH + ["--query", """CREATE TABLE census.tokens (
        token String, symbol String, decimals Int16
      ) ENGINE = MergeTree ORDER BY token"""], capture_output=True)

    t0, unread = time.time(), 0
    for i in range(0, len(toks), PER_CALL):
        grp = toks[i:i + PER_CALL]
        calls = [c for t in grp for c in ((t, SEL_SYM), (t, SEL_DEC))]
        res = multicall(calls)
        rows = []
        for j, t in enumerate(grp):
            sym = as_symbol(*res[2 * j])
            dec = as_dec(*res[2 * j + 1])
            if not sym:
                unread += 1
                sym = t[:10]
            rows.append("\t".join((t, sym.replace("\t", " ").replace("\n", " "),
                                   str(dec))))
        pr = subprocess.run(CH + ["--query",
                                  "INSERT INTO census.tokens FORMAT TSV"],
                            input="\n".join(rows).encode(), capture_output=True)
        if pr.returncode != 0:
            raise SystemExit(pr.stderr.decode()[:400])
        done = min(i + PER_CALL, len(toks))
        print(f"[tok] {done}/{len(toks)} calls={_stats['calls']} "
              f"unreadable={unread} el={(time.time()-t0)/60:.1f}m", flush=True)

    print(f"[tok] DONE calls={_stats['calls']} credits={_stats['calls']*20:,} "
          f"unreadable_symbols={unread}", flush=True)
    out = subprocess.run(CH + ["--format", "PrettyCompact", "--query", """
        SELECT count() AS tokens, countIf(decimals = 18) AS dec18,
               countIf(decimals = 6) AS dec6, countIf(decimals < 0) AS dec_unknown
        FROM census.tokens"""], capture_output=True)
    print(out.stdout.decode())


if __name__ == "__main__":
    main()
