#!/usr/bin/env python3
"""Phase 1: Base V4 PoolManager Initialize listener + hook flag decode.

Backfills Initialize events into /tmp/v4_created_pools.jsonl with checkpoint resume.
Constraints: eth_getLogs chunk <= 10_000 blocks; initial backfill 7 days.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterator, Optional

from eth_abi import decode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hook_flags import decode_hook
from hook_template_match import enrich_pool_row
from research_rpc import ResearchRpc, RpcBudgetExceeded, get_research_rpc

# Chain-verified Base mainnet PoolManager (code_len=24009, BaseScan + Arrakis migration table)
POOL_MANAGER = Web3.to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
INIT_TOPIC = Web3.keccak(
    text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
).hex()

OUT_PATH = "/tmp/v4_created_pools.jsonl"
CHECKPOINT_PATH = "/tmp/v4_listener_checkpoint.json"
CHUNK_BLOCKS = 10_000
BLOCKS_PER_DAY = 43_200  # ~2s Base block time
DEFAULT_BACKFILL_DAYS = 7


def _load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_PATH):
        try:
            return json.load(open(CHECKPOINT_PATH))
        except Exception:
            pass
    return {}


def _save_checkpoint(state: dict) -> None:
    tmp = CHECKPOINT_PATH + ".tmp"
    json.dump(state, open(tmp, "w"), indent=2)
    os.replace(tmp, CHECKPOINT_PATH)


def _existing_pool_ids(path: str) -> set[str]:
    ids: set[str] = set()
    if not os.path.exists(path):
        return ids
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["poolId"])
            except Exception:
                pass
    return ids


def _hex_field(v) -> str:
    if hasattr(v, "hex"):
        h = v.hex()
        return h if h.startswith("0x") else "0x" + h
    return v if isinstance(v, str) else str(v)


def _int_field(v) -> int:
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return int(v)


def decode_initialize_log(log: dict) -> dict:
    topics = [_hex_field(t) for t in log["topics"]]
    pool_id = topics[1]
    c0 = Web3.to_checksum_address("0x" + topics[2][-40:])
    c1 = Web3.to_checksum_address("0x" + topics[3][-40:])
    data_hex = _hex_field(log["data"])
    data = bytes.fromhex(data_hex[2:])
    fee, tick_spacing, hooks, sqrt_price_x96, tick = decode(
        ["uint24", "int24", "address", "uint160", "int24"], data
    )
    hooks_cs = Web3.to_checksum_address(hooks)
    hook_profile = decode_hook(hooks_cs)
    base = {
        "poolId": pool_id,
        "currency0": c0,
        "currency1": c1,
        "fee": int(fee),
        "tickSpacing": int(tick_spacing),
        "hooks": hooks_cs,
        "sqrtPriceX96": str(int(sqrt_price_x96)),
        "tick": int(tick),
        "blockNumber": _int_field(log["blockNumber"]),
        "txHash": _hex_field(log["transactionHash"]),
        "logIndex": _int_field(log["logIndex"]),
        "has_weth": c0.lower() == WETH.lower() or c1.lower() == WETH.lower(),
        "hook_profile": hook_profile.to_dict(),
    }
    return enrich_pool_row(base)


def retag_jsonl(
    src: str = OUT_PATH,
    dst: Optional[str] = None,
    *,
    in_place: bool = True,
) -> dict:
    """Add hook_template fields to existing jsonl (0 RPC)."""
    dst = dst or (src + ".retagged") if not in_place else src + ".tmp"
    n = updated = 0
    with open(src) as fin, open(dst, "w") as fout:
        for line in fin:
            if not line.strip():
                continue
            row = json.loads(line)
            new_row = enrich_pool_row(row)
            if new_row.get("hook_template") != row.get("hook_template"):
                updated += 1
            fout.write(json.dumps(new_row, separators=(",", ":")) + "\n")
            n += 1
    if in_place:
        os.replace(dst, src)
    stats = {"pools": n, "updated": updated, "path": src if in_place else dst}
    json.dump(stats, open(os.path.join(HERE, "_listener_retag.json"), "w"), indent=2)
    return stats


def _infer_earliest_block(path: str) -> Optional[int]:
    lo: Optional[int] = None
    if not os.path.exists(path):
        return lo
    with open(path) as f:
        for line in f:
            if line.strip():
                b = json.loads(line)["blockNumber"]
                lo = b if lo is None else min(lo, b)
    return lo


def backfill(
    rpc: ResearchRpc,
    *,
    days: int = DEFAULT_BACKFILL_DAYS,
    follow: bool = False,
    poll_s: int = 30,
) -> dict:
    seen = _existing_pool_ids(OUT_PATH)
    cp = _load_checkpoint()
    head = rpc.eth_block_number()
    target_lo = max(0, head - days * BLOCKS_PER_DAY)
    earliest = cp.get("earliest_scanned_block") or _infer_earliest_block(OUT_PATH)
    last_hi = cp.get("last_scanned_block", head)
    stats = {
        "pool_manager": POOL_MANAGER,
        "target_days": days,
        "target_lo_block": target_lo,
        "earliest_before": earliest,
        "head_at_start": head,
        "new_pools": 0,
        "skipped_dup": 0,
        "chunks": 0,
        "backward_chunks": 0,
        "forward_chunks": 0,
        "rpc_calls": rpc.count(),
    }
    print(f"[listener] PoolManager={POOL_MANAGER}", flush=True)
    print(f"[listener] target window: {target_lo} .. {head} ({days}d)", flush=True)
    print(f"[listener] existing pools in jsonl: {len(seen)}", flush=True)
    if earliest:
        print(f"[listener] earliest_scanned={earliest} last_scanned={last_hi}", flush=True)

    def scan_range(lo: int, hi: int, *, direction: str = "forward") -> None:
        nonlocal stats
        if direction == "forward":
            b = lo
            while b <= hi:
                end = min(b + CHUNK_BLOCKS - 1, hi)
                _scan_chunk(b, end, direction)
                b = end + 1
        else:
            b = lo
            while b >= hi:
                end = max(b - CHUNK_BLOCKS + 1, hi)
                _scan_chunk(end, b, direction)
                b = end - 1

    def _scan_chunk(chunk_lo: int, chunk_hi: int, direction: str) -> None:
        nonlocal stats
        flt = {
            "address": POOL_MANAGER,
            "fromBlock": hex(chunk_lo),
            "toBlock": hex(chunk_hi),
            "topics": [INIT_TOPIC],
        }
        logs = rpc.eth_get_logs(flt)
        stats["chunks"] += 1
        stats[f"{direction}_chunks"] += 1
        with open(OUT_PATH, "a") as out:
            for raw in logs:
                row = decode_initialize_log(raw)
                if row["poolId"] in seen:
                    stats["skipped_dup"] += 1
                    continue
                seen.add(row["poolId"])
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
                stats["new_pools"] += 1
        cp.update({
            "updated_at": time.time(),
            "total_pools": len(seen),
            "rpc_calls": rpc.count(),
            "target_days": days,
        })
        if direction == "forward":
            cp["last_scanned_block"] = chunk_hi
            cp.setdefault("earliest_scanned_block", chunk_lo)
            cp["earliest_scanned_block"] = min(cp.get("earliest_scanned_block", chunk_lo), chunk_lo)
        else:
            cp["earliest_scanned_block"] = chunk_lo
        _save_checkpoint(cp)
        if stats["chunks"] % 5 == 0:
            print(
                f"  [{direction}] chunk {stats['chunks']} blocks {chunk_lo}-{chunk_hi} | "
                f"+{stats['new_pools']} new | rpc={rpc.count()}",
                flush=True,
            )

    try:
        # 1) expand backward if --days grew (e.g. 7 -> 14)
        if earliest and target_lo < earliest:
            print(f"[listener] backward expand {target_lo} -> {earliest - 1}", flush=True)
            scan_range(earliest - 1, target_lo, direction="backward")
        elif not earliest and target_lo < head:
            print(f"[listener] fresh backward {target_lo} -> {head}", flush=True)
            scan_range(target_lo, head, direction="forward")
            cp["earliest_scanned_block"] = target_lo
            _save_checkpoint(cp)
        # 2) catch up forward to head
        last_hi = cp.get("last_scanned_block", 0)
        if last_hi < head:
            print(f"[listener] forward catch-up {last_hi + 1} -> {head}", flush=True)
            scan_range(last_hi + 1, head, direction="forward")
        stats["backfill_done"] = True
        if follow:
            print("[listener] follow mode — polling new blocks", flush=True)
            while True:
                new_head = rpc.eth_block_number()
                last = cp.get("last_scanned_block", head)
                if new_head > last:
                    scan_range(last + 1, new_head)
                    head = new_head
                time.sleep(poll_s)
    except RpcBudgetExceeded as e:
        stats["stopped_reason"] = str(e)
        print(f"[listener] STOP: {e}", flush=True)
    stats["rpc_calls"] = rpc.count()
    stats["total_pools"] = len(seen)
    return stats


def verify_pool_manager(rpc: ResearchRpc) -> bool:
    code = rpc.eth_get_code(POOL_MANAGER)
    ok = len(code) > 1000
    print(f"[verify] PoolManager code_len={len(code)} ok={ok}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="V4 Initialize listener (Base)")
    ap.add_argument("--days", type=int, default=DEFAULT_BACKFILL_DAYS, help="backfill window days")
    ap.add_argument("--follow", action="store_true", help="keep polling after backfill")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--retag-jsonl", action="store_true", help="add hook_template to existing jsonl (0 RPC)")
    args = ap.parse_args()

    if args.retag_jsonl:
        stats = retag_jsonl(in_place=True)
        print(f"retag done: {stats['pools']} pools, {stats['updated']} updated -> {OUT_PATH}")
        return 0

    rpc = get_research_rpc()
    if not rpc.w3.is_connected():
        print("ERROR: cannot connect RESEARCH_RPC_URL", file=sys.stderr)
        return 1
    if not verify_pool_manager(rpc):
        print("ERROR: PoolManager has no code at verified address", file=sys.stderr)
        return 1
    if args.verify_only:
        # smoke: one small getLogs
        head = rpc.eth_block_number()
        flt = {
            "address": POOL_MANAGER,
            "fromBlock": hex(head - 100),
            "toBlock": hex(head),
            "topics": [INIT_TOPIC],
        }
        logs = rpc.eth_get_logs(flt)
        print(f"smoke: {len(logs)} Initialize events in last 100 blocks")
        return 0

    stats = backfill(rpc, days=args.days, follow=args.follow)
    summary_path = os.path.join(HERE, "_listener_run.json")
    json.dump(stats, open(summary_path, "w"), indent=2)
    print(f"\n=== DONE === new={stats['new_pools']} total={stats['total_pools']} "
          f"rpc={stats['rpc_calls']} chunks={stats['chunks']}")
    print(f"output: {OUT_PATH}")
    print(f"checkpoint: {CHECKPOINT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
