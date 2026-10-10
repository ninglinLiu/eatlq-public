#!/usr/bin/env python3
"""Backfill every concentrated-liquidity Swap log on Base into census.cl_swaps.

No address filter: topic0 alone catches Uniswap V3 *and* Aerodrome Slipstream,
so the resulting panel is not limited to what discovery_poller happened to see.
Resumable via census.bf_progress; safe to kill and restart.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
CHUNK = 10_000
WORKERS = 4
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client"]

RPC_URL = open("/tmp/rpc_url.txt").read().strip()
_print_lock = threading.Lock()
_stats = {"calls": 0, "logs": 0, "bytes": 0, "chunks": 0, "t0": time.time()}


def rpc(method: str, params: list, timeout: int = 180) -> tuple[dict, int]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw), len(raw)


def to_int256(word: str) -> int:
    """32-byte hex word -> signed int."""
    v = int(word, 16)
    return v - (1 << 256) if v >> 255 else v


def get_logs(frm: int, to: int) -> list[dict]:
    """Fetch with adaptive halving so a dense range cannot wedge the run."""
    out: list[dict] = []
    stack = [(frm, to)]
    while stack:
        a, b = stack.pop()
        for attempt in range(5):
            try:
                r, nb = rpc("eth_getLogs", [{"topics": [SWAP_TOPIC],
                                             "fromBlock": hex(a), "toBlock": hex(b)}])
                if "error" in r:
                    msg = r["error"].get("message", "")
                    if a < b and ("too large" in msg.lower() or "range" in msg.lower()
                                  or "limit" in msg.lower()):
                        mid = (a + b) // 2
                        stack += [(a, mid), (mid + 1, b)]
                        break
                    raise RuntimeError(f"rpc error {a}-{b}: {msg[:160]}")
                _stats["calls"] += 1
                _stats["bytes"] += nb
                out += r["result"]
                break
            except urllib.error.HTTPError as e:
                if e.code == 413 and a < b:
                    mid = (a + b) // 2
                    stack += [(a, mid), (mid + 1, b)]
                    break
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
    return out


def rows_from(logs: list[dict]) -> str:
    lines = []
    for lg in logs:
        d = lg["data"][2:]
        if len(d) < 320 or len(lg.get("topics", [])) < 3:
            continue
        w = [d[i * 64:(i + 1) * 64] for i in range(5)]
        tick = to_int256(w[4])
        lines.append("\t".join((
            str(int(lg["blockNumber"], 16)),
            str(int(lg["logIndex"], 16)),
            lg["address"].lower(),
            lg["transactionHash"],
            "0x" + lg["topics"][1][-40:],
            "0x" + lg["topics"][2][-40:],
            str(to_int256(w[0])),
            str(to_int256(w[1])),
            str(int(w[2], 16)),
            str(int(w[3], 16)),
            str(tick),
        )))
    return "\n".join(lines)


def insert(tsv: str, table: str, cols: str) -> None:
    if not tsv:
        return
    p = subprocess.run(CH + ["--query", f"INSERT INTO {table} ({cols}) FORMAT TSV"],
                       input=tsv.encode(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"insert failed: {p.stderr.decode()[:300]}")


COLS = ("block_number,log_index,pool,tx_hash,sender,recipient,"
        "amount0,amount1,sqrt_price_x96,liquidity,tick")


def do_chunk(frm: int, to: int) -> None:
    logs = get_logs(frm, to)
    insert(rows_from(logs), "census.cl_swaps", COLS)
    insert(f"{frm}\t{to}\t{len(logs)}", "census.bf_progress", "from_block,to_block,n_logs")
    _stats["logs"] += len(logs)
    _stats["chunks"] += 1
    with _print_lock:
        el = time.time() - _stats["t0"]
        print(f"[bf] {frm}-{to} logs={len(logs):<7} "
              f"cum_logs={_stats['logs']:<10} chunks={_stats['chunks']}/{TOTAL} "
              f"gb={_stats['bytes']/1e9:6.2f} el={el/60:6.1f}m "
              f"eta={(el/max(_stats['chunks'],1))*(TOTAL-_stats['chunks'])/60:6.1f}m", flush=True)


def done_ranges() -> set[int]:
    p = subprocess.run(CH + ["--query", "SELECT from_block FROM census.bf_progress FINAL"],
                       capture_output=True)
    return {int(x) for x in p.stdout.decode().split() if x.strip()}


if __name__ == "__main__":
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 46_000_000
    head, _ = rpc("eth_blockNumber", [])
    end = int(sys.argv[2]) if len(sys.argv) > 2 else int(head["result"], 16)

    todo = [(a, min(a + CHUNK - 1, end)) for a in range(start, end + 1, CHUNK)]
    already = done_ranges()
    todo = [c for c in todo if c[0] not in already]
    TOTAL = len(todo)
    print(f"[bf] window {start}-{end}  chunks_todo={TOTAL}  already_done={len(already)}  "
          f"workers={WORKERS}  est_credits={TOTAL*20:,}", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(lambda c: do_chunk(*c), todo))

    print(f"[bf] DONE chunks={_stats['chunks']} logs={_stats['logs']} "
          f"gb={_stats['bytes']/1e9:.2f} min={(time.time()-_stats['t0'])/60:.1f} "
          f"credits={_stats['calls']*20:,}", flush=True)
