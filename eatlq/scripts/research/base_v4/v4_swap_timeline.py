"""Per-pool V4 swap + liquidity timeline from archive RPC (research counter)."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

from eth_abi import decode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from research_rpc import RpcBudgetExceeded, get_research_rpc

POOL_MANAGER = Web3.to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
SWAP_TOPIC = Web3.keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
).hex()
MODIFY_TOPIC = Web3.keccak(
    text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
).hex()
SWAP_TOPIC0 = SWAP_TOPIC if SWAP_TOPIC.startswith("0x") else "0x" + SWAP_TOPIC
MODIFY_TOPIC0 = MODIFY_TOPIC if MODIFY_TOPIC.startswith("0x") else "0x" + MODIFY_TOPIC
TIMELINE_PATH = os.path.join(HERE, "_v4_pool_swap_timeline.json")
DEPLOYER_CACHE = os.path.join(HERE, "_v4_init_tx_from.json")
RPC_WARN = 18_000
DEFAULT_HORIZON = 7200
LOG_CHUNK = 10_000


def _hex(v) -> str:
    if hasattr(v, "hex"):
        h = v.hex()
        return h if h.startswith("0x") else "0x" + h
    return str(v)


def _int(v) -> int:
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return int(v)


def _norm_pid(raw) -> str:
    pid = _hex(raw)
    return pid if pid.startswith("0x") else "0x" + pid


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return {}


def _save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


def weth_is_currency0(row: dict) -> bool:
    return Web3.to_checksum_address(row["currency0"]).lower() == WETH.lower()


def decode_swap_log(log: dict, weth_c0: bool) -> dict:
    topics = [_hex(t) for t in log["topics"]]
    sender = Web3.to_checksum_address("0x" + topics[2][-40:])
    data = bytes.fromhex(_hex(log["data"])[2:])
    amount0, amount1, sqrt_price, liquidity, tick, fee = decode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"], data
    )
    a0, a1 = int(amount0), int(amount1)
    if weth_c0:
        weth_in = a0 > 0
        weth_out = a0 < 0
    else:
        weth_in = a1 > 0
        weth_out = a1 < 0
    return {
        "block": _int(log["blockNumber"]),
        "tx_hash": _hex(log["transactionHash"]),
        "log_index": _int(log["logIndex"]),
        "sender": sender,
        "amount0": a0,
        "amount1": a1,
        "sqrt_price_x96": str(int(sqrt_price)),
        "tick": int(tick),
        "weth_in": weth_in,
        "weth_out": weth_out,
    }


def decode_modify_log(log: dict) -> dict:
    topics = [_hex(t) for t in log["topics"]]
    sender = Web3.to_checksum_address("0x" + topics[2][-40:])
    data = bytes.fromhex(_hex(log["data"])[2:])
    tick_lower, tick_upper, liq_delta, salt = decode(
        ["int24", "int24", "int256", "bytes32"], data
    )
    return {
        "block": _int(log["blockNumber"]),
        "tx_hash": _hex(log["transactionHash"]),
        "sender": sender,
        "tick_lower": int(tick_lower),
        "tick_upper": int(tick_upper),
        "liquidity_delta": int(liq_delta),
        "remove": int(liq_delta) < 0,
    }


def fetch_init_deployer(tx_hash: str, rpc) -> Optional[str]:
    tx = rpc.eth_get_transaction_by_hash(tx_hash)
    if not tx:
        return None
    return Web3.to_checksum_address(tx["from"])


def ensure_deployers(pool_rows: dict[str, dict], rpc=None) -> dict:
    """Cache Initialize tx.from per poolId."""
    rpc = rpc or get_research_rpc()
    cache = _load_json(DEPLOYER_CACHE)
    pools = cache.setdefault("pools", {})
    added = 0
    for pid, row in pool_rows.items():
        if pid in pools and pools[pid].get("init_from"):
            continue
        if rpc.count() >= RPC_WARN:
            raise RpcBudgetExceeded(f"RPC {rpc.count()} >= warn {RPC_WARN}")
        frm = fetch_init_deployer(row["txHash"], rpc)
        pools[pid] = {"poolId": pid, "init_tx": row["txHash"], "init_from": frm}
        added += 1
    cache["meta"] = {"rpc_total": rpc.count(), "n": len(pools)}
    _save_json(DEPLOYER_CACHE, cache)
    return cache


def fetch_pool_timeline(
    pool_id: str,
    pool_row: dict,
    *,
    from_block: int,
    to_block: int,
    rpc=None,
) -> dict:
    rpc = rpc or get_research_rpc()
    if rpc.count() >= RPC_WARN:
        raise RpcBudgetExceeded(f"RPC {rpc.count()} >= warn {RPC_WARN}")
    pid = pool_id if pool_id.startswith("0x") else "0x" + pool_id
    pid = pid.lower()
    pid_topic = "0x" + pid[2:].zfill(64)
    weth_c0 = weth_is_currency0(pool_row)
    flt_base = {
        "address": POOL_MANAGER,
        "topics": [[SWAP_TOPIC0, MODIFY_TOPIC0], [pid_topic]],
    }
    logs = []
    pos = from_block
    while pos <= to_block:
        if rpc.count() >= RPC_WARN:
            raise RpcBudgetExceeded(f"RPC {rpc.count()} >= warn {RPC_WARN}")
        chunk_hi = min(to_block, pos + LOG_CHUNK - 1)
        flt = {**flt_base, "fromBlock": hex(pos), "toBlock": hex(chunk_hi)}
        logs.extend(rpc.eth_get_logs(flt))
        pos = chunk_hi + 1
    swaps, mods = [], []
    for raw in logs:
        t0 = _hex(raw["topics"][0]).lower()
        if t0 == SWAP_TOPIC0.lower():
            if _norm_pid(raw["topics"][1]).lower() == pid:
                swaps.append(decode_swap_log(raw, weth_c0))
        elif t0 == MODIFY_TOPIC0.lower():
            if _norm_pid(raw["topics"][1]).lower() == pid:
                mods.append(decode_modify_log(raw))
    swaps.sort(key=lambda x: (x["block"], x["log_index"]))
    mods.sort(key=lambda x: x["block"])
    return {
        "poolId": pid,
        "from_block": from_block,
        "to_block": to_block,
        "swaps": swaps,
        "modify_liquidity": mods,
        "n_swaps": len(swaps),
        "n_mods": len(mods),
    }


def build_timelines(
    pool_rows: dict[str, dict],
    *,
    horizon: int = DEFAULT_HORIZON,
    incremental: bool = True,
    swap_index: Optional[dict] = None,
    pre_swap_lookback: int = 200,
) -> dict:
    rpc = get_research_rpc()
    swap_index = swap_index or {}
    out = _load_json(TIMELINE_PATH) if incremental else {"pools": {}, "meta": {}}
    pools = out.setdefault("pools", {})
    rpc_start = rpc.count()
    built = 0
    skipped = 0
    for pid, row in pool_rows.items():
        if incremental and pid in pools and pools[pid].get("swaps") is not None and pools[pid].get("n_swaps", 0) > 0:
            anchor_blk = int((swap_index.get(pid) or {}).get("block") or pools[pid].get("activity_anchor_block") or 0)
            if anchor_blk and pools[pid].get("to_block", 0) >= anchor_blk + horizon:
                continue
        anchor = (swap_index.get(pid) or {}).get("block")
        if anchor is None:
            skipped += 1
            pools[pid] = {"poolId": pid, "skipped": True, "reason": "no_swap_anchor", "swaps": [], "modify_liquidity": []}
            continue
        from_blk = max(int(row["blockNumber"]), int(anchor) - pre_swap_lookback)
        to_blk = int(anchor) + horizon
        pools[pid] = fetch_pool_timeline(pid, row, from_block=from_blk, to_block=to_blk, rpc=rpc)
        pools[pid]["activity_anchor_block"] = int(anchor)
        built += 1
        if built % 10 == 0:
            out["meta"] = {
                "horizon_blocks": horizon,
                "rpc_added": rpc.count() - rpc_start,
                "rpc_total": rpc.count(),
                "built": built,
            }
            _save_json(TIMELINE_PATH, out)
    out["meta"] = {
        "horizon_blocks": horizon,
        "pre_swap_lookback": pre_swap_lookback,
        "rpc_added": rpc.count() - rpc_start,
        "rpc_total": rpc.count(),
        "n_pools": len(pools),
        "skipped_no_anchor": skipped,
    }
    _save_json(TIMELINE_PATH, out)
    return out
