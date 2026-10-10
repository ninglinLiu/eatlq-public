"""Lightweight single-tx fork simulation — no cast run block replay, no ResearchRpc."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from typing import Optional

from web3 import Web3

ARCHIVE_RPC = os.environ.get("RESEARCH_ARCHIVE_RPC_URL") or os.environ.get("RESEARCH_RPC_URL", "")
if not ARCHIVE_RPC:
    raise SystemExit("RESEARCH_ARCHIVE_RPC_URL is not set — export a research RPC endpoint (the token must never be committed)")
ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
SWAP_INDEX = "/tmp/v4_pool_swap_index.json"
POOL_MANAGER = "0x498581fF718922c3f8e6A244956aF099B2652b2b"
SWAP_TOPIC = Web3.keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
).hex()
LOG_CHUNK = 10_000
RPC_WARN_TOTAL = 18_000


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _rpc(method: str, params: list, url: str):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.load(resp)
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def load_swap_index() -> dict:
    if os.path.exists(SWAP_INDEX):
        try:
            return json.load(open(SWAP_INDEX))
        except Exception:
            pass
    return {"pools": {}, "meta": {}}


def save_swap_index(data: dict) -> None:
    tmp = SWAP_INDEX + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2)
    os.replace(tmp, SWAP_INDEX)


def _norm_pid(raw) -> str:
    pid = raw.hex() if hasattr(raw, "hex") else raw
    pid = str(pid)
    return pid if pid.startswith("0x") else "0x" + pid


def _merge_logs(logs: list, pools: dict) -> int:
    added = 0
    for raw in logs:
        pid = _norm_pid(raw["topics"][1])
        bn = raw["blockNumber"]
        bn = int(bn, 16) if isinstance(bn, str) else bn
        txh = raw["transactionHash"]
        txh = txh.hex() if hasattr(txh, "hex") else txh
        txh = str(txh)
        if not txh.startswith("0x"):
            txh = "0x" + txh
        prev = pools.get(pid)
        if prev and prev.get("block", 0) >= bn:
            continue
        pools[pid] = {"tx_hash": txh, "block": bn}
        added += 1
    return added


def _meta_earliest(meta: dict) -> Optional[int]:
    if meta.get("earliest_block") is not None:
        return int(meta["earliest_block"])
    head = meta.get("head")
    lb = meta.get("lookback") or meta.get("lookback_target")
    if head is not None and lb is not None:
        return max(0, int(head) - int(lb))
    return None


def estimate_index_rpc(existing: dict, *, lookback_blocks: int, head: Optional[int] = None) -> dict:
    """Dry-run: estimate eth_* calls without fetching logs."""
    meta = existing.get("meta", {})
    old_head = meta.get("head")
    old_earliest = _meta_earliest(meta)
    if head is None:
        head = old_head or 0
    target_earliest = max(0, head - lookback_blocks)
    ranges = []
    if old_head and head > old_head:
        ranges.append((old_head + 1, head))
    if old_earliest is None:
        ranges.append((target_earliest, head))
    elif target_earliest < old_earliest:
        ranges.append((target_earliest, old_earliest - 1))
    n_chunks = sum(max(1, (hi - lo + 1 + LOG_CHUNK - 1) // LOG_CHUNK) for lo, hi in ranges) if ranges else 0
    return {
        "needs_block_number": head is None or head != old_head,
        "estimated_rpc": (1 if head is None else 0) + n_chunks,
        "ranges": [{"from": lo, "to": hi} for lo, hi in ranges],
        "target_earliest": target_earliest,
        "current_earliest": old_earliest,
    }


def build_swap_index(*, lookback_blocks: int = 500, dry_run: bool = False) -> dict:
    """Incremental swap index — chunked eth_getLogs with checkpoint."""
    from research_rpc import RpcBudgetExceeded, get_research_rpc

    existing = load_swap_index()
    pools = dict(existing.get("pools", {}))
    meta = existing.get("meta", {})
    old_head = meta.get("head")
    old_earliest = _meta_earliest(meta)

    if dry_run:
        est = estimate_index_rpc(existing, lookback_blocks=lookback_blocks)
        est["current_pools"] = len(pools)
        return est

    rpc = get_research_rpc()
    if rpc.count() >= RPC_WARN_TOTAL:
        raise RpcBudgetExceeded(
            f"Total RPC {rpc.count()} >= warn threshold {RPC_WARN_TOTAL}; stop per Phase2 guard"
        )
    rpc_start = rpc.count()
    head = rpc.eth_block_number()
    target_earliest = max(0, head - lookback_blocks)

    ranges: list[tuple[int, int]] = []
    if old_head is not None and head > old_head:
        ranges.append((old_head + 1, head))
    if old_earliest is None:
        ranges.append((target_earliest, head))
    elif target_earliest < old_earliest:
        ranges.append((target_earliest, old_earliest - 1))

    chunks_scanned = []
    for lo, hi in ranges:
        pos = lo
        while pos <= hi:
            if rpc.count() >= RPC_WARN_TOTAL:
                raise RpcBudgetExceeded(f"Hit RPC warn {RPC_WARN_TOTAL} mid-index build")
            chunk_hi = min(hi, pos + LOG_CHUNK - 1)
            logs = rpc.eth_get_logs({
                "address": POOL_MANAGER,
                "fromBlock": hex(pos),
                "toBlock": hex(chunk_hi),
                "topics": [SWAP_TOPIC],
            })
            n_new = _merge_logs(logs, pools)
            chunks_scanned.append({"from": pos, "to": chunk_hi, "logs": len(logs), "pools_updated": n_new})
            pos = chunk_hi + 1

    new_earliest = target_earliest if old_earliest is None else min(target_earliest, old_earliest)
    weth_n = 0  # filled by caller report if needed
    out = {
        "pools": pools,
        "meta": {
            "head": head,
            "earliest_block": new_earliest,
            "lookback_target": lookback_blocks,
            "chunks": meta.get("chunks", [])[-20:] + chunks_scanned,
            "pools_count": len(pools),
            "rpc_added": rpc.count() - rpc_start,
            "rpc_total": rpc.count(),
            "ranges_scanned": [{"from": lo, "to": hi} for lo, hi in ranges],
        },
    }
    save_swap_index(out)
    return out


def simulate_tx_call(tx_hash: str, fork_block: int, *, wait_s: int = 90) -> dict:
    """eth_call one tx on anvil fork at parent block — no cast run / no block replay."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["nice", "-n", "19", ANVIL, "--fork-url", ARCHIVE_RPC,
         "--fork-block-number", str(fork_block), "--port", str(port),
         "--silent", "--no-storage-caching"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                _rpc("eth_blockNumber", [], url)
                break
            except Exception:
                time.sleep(0.5)
        else:
            return {"ok": False, "error": "anvil_start_timeout", "tx_hash": tx_hash}

        tx = _rpc("eth_getTransactionByHash", [tx_hash], url)
        if not tx:
            return {"ok": False, "error": "tx_not_found", "tx_hash": tx_hash}

        sender = Web3.to_checksum_address(tx["from"])
        _rpc("anvil_impersonateAccount", [sender], url)
        _rpc("anvil_setBalance", [sender, hex(10**20)], url)
        gas = int(tx.get("gas", "0x2dc6c0"), 16)
        call = {
            "from": sender,
            "to": tx["to"],
            "data": tx["input"],
            "value": tx.get("value", "0x0"),
            "gas": hex(min(gas, 30_000_000)),
        }
        _rpc("eth_call", [call, "latest"], url)
        return {"ok": True, "tx_hash": tx_hash, "from": sender, "to": tx["to"], "fork_block": fork_block}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}", "tx_hash": tx_hash}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
