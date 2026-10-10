#!/usr/bin/env python3
"""Sprint089 — Pipeline gap backfill (2026-05-18 archiver stall → now).

Fetches Swap logs for all WETH-leg pools discovered in the lookback window
and inserts directly into eatlq.raw_evm_logs with source='sprint089_gap_backfill'.

Hex fields use 0x-prefixed lowercase (topic length 66) so uniswap_v{2,3}_swaps_mv
auto-populates normalized tables — no separate SQL materialize step required.

Safe to re-run: may duplicate rows; prefer block-range + source filters before re-insert.

Usage:
  LABEL_RPC_URL=https://... python3 scripts/sprint089_pipeline_gap_backfill.py --dry-run
  LABEL_RPC_URL=https://... python3 scripts/sprint089_pipeline_gap_backfill.py --insert
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CH = "eatlq-clickhouse-1"
CHAIN_ID = 8453
GAP_START_UTC = "2026-05-18 07:43:00"
SWAP_TOPICS = [
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
    "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b",
]
BLUE_CHIPS = [
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0x4c36388be6f416a29c8d8eee81c771ce6be14b18",
    "0x10648ba41b8565907cfa1496765fa4d95390aa0d",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
    "0xfcd08d68b2313cd5e1d9e0e3f3183ad7cfdbf66f",
    "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d",
    "0x82321f3beb69f503380d6b233857d5c43562e2d0",
    "0xdfe5f275020def30993f042174fc2d335678b626",
    "0xbe00ff35af70e8415d0eb605a286d8a45466a4c1",
    "0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6",
]
SOURCE = "sprint089_gap_backfill"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--insert", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--pool-limit", type=int, default=600)
    p.add_argument("--batch-pools", type=int, default=15)
    p.add_argument("--block-chunk", type=int, default=2000)
    p.add_argument("--lookback-days", type=int, default=7)
    p.add_argument("--rpc-url", default=os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", ""))
    return p.parse_args()


def _ch(sql: str, timeout: int = 120) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()


def _rpc(url: str, method: str, params: list, delay: float = 0.05) -> any:
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read())
    time.sleep(delay)
    if "error" in out:
        raise RuntimeError(str(out["error"])[:300])
    return out["result"]


def _hex0x(v) -> str:
    """Normalize to lowercase 0x-prefixed hex (matches swap_poller / ingestor)."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        s = v.hex()
    else:
        s = str(v)
        if s.startswith("0x") or s.startswith("0X"):
            s = s[2:]
    s = s.lower()
    return ("0x" + s) if s else ""


def _topic_hex(topics: list, idx: int) -> str:
    if idx >= len(topics):
        return ""
    return _hex0x(topics[idx])


def _load_env_rpc() -> str:
    env = REPO / "infra/compose/.env"
    for line in env.read_text().splitlines():
        if line.startswith("EVM_HTTP_URL="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def fetch_pools(lookback_days: int, limit: int) -> list[str]:
    sql = f"""
SELECT lower(pool_address) AS pool
FROM eatlq.discovered_pools
WHERE has_weth_leg = 1
  AND discovered_at >= now() - INTERVAL {lookback_days} DAY
GROUP BY pool
ORDER BY max(discovered_at) DESC
LIMIT {limit}
FORMAT TabSeparated
"""
    rows = [r for r in _ch(sql).splitlines() if r]
    pools = {r.lower() for r in rows}
    pools.update(a.lower() for a in BLUE_CHIPS)
    return sorted(pools)


def gap_blocks() -> tuple[int, int]:
    from_b = int(_ch(
        f"SELECT block_number FROM eatlq.uniswap_v3_swaps "
        f"WHERE block_time <= toDateTime('{GAP_START_UTC}', 'UTC') "
        f"ORDER BY block_time DESC LIMIT 1 FORMAT TabSeparated"
    ) or "0")
    to_b = _rpc(_rpc_url_holder[0], "eth_blockNumber", [])
    if isinstance(to_b, str):
        to_b = int(to_b, 16)
    if from_b <= 0:
        from_b = to_b - 100_000
    return from_b, to_b


_rpc_url_holder: list[str] = []


def log_to_row(log_entry: dict, block_ts_hex: str) -> dict:
    topics = log_entry.get("topics") or []
    bn = log_entry.get("blockNumber")
    if isinstance(bn, str):
        bn = int(bn, 16)
    li = log_entry.get("logIndex")
    if isinstance(li, str):
        li = int(li, 16)
    bt_ms = None
    if block_ts_hex:
        bt_ms = int(block_ts_hex, 16) * 1000
    now_ms = int(time.time() * 1000)
    payload = json.dumps({
        "params": {"result": {"blockTimestamp": block_ts_hex}},
    }, separators=(",", ":"))
    return {
        "chain_id": CHAIN_ID,
        "source": SOURCE,
        "subscription_id": "sprint089_gap_backfill",
        "block_time": bt_ms,
        "received_at_ms": now_ms,
        "block_number": int(bn or 0),
        "tx_hash": _hex0x(log_entry.get("transactionHash")),
        "log_index": int(li or 0),
        "address": _hex0x(log_entry.get("address")),
        "topic0": _topic_hex(topics, 0),
        "topic1": _topic_hex(topics, 1),
        "topic2": _topic_hex(topics, 2),
        "topic3": _topic_hex(topics, 3),
        "data": _hex0x(log_entry.get("data")),
        "removed": 0,
        "value_usd": None,
        "payload_json": payload,
    }


def insert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(r) for r in rows).encode()
    r = subprocess.run(
        ["docker", "exec", "-i", CH, "clickhouse-client",
         "--query", "INSERT INTO eatlq.raw_evm_logs FORMAT JSONEachRow"],
        input=payload, capture_output=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])


def main() -> int:
    args = parse_args()
    if args.insert:
        args.dry_run = False
    rpc = args.rpc_url or _load_env_rpc()
    if not rpc:
        print("ERROR: set LABEL_RPC_URL or EVM_HTTP_URL", file=sys.stderr)
        return 2
    _rpc_url_holder[:] = [rpc]

    out_dir = REPO / "mvp/audits/sprint089_pipeline_recovery"
    out_dir.mkdir(parents=True, exist_ok=True)

    pools = fetch_pools(args.lookback_days, args.pool_limit)
    from_block, to_block = gap_blocks()
    print(f"[089] pools={len(pools)} blocks={from_block}..{to_block} dry_run={args.dry_run}")

    total_logs = 0
    total_inserted = 0
    block_ts_cache: dict[int, str] = {}
    buf: list[dict] = []

    for i in range(0, len(pools), args.batch_pools):
        batch = pools[i:i + args.batch_pools]
        for blk in range(from_block, to_block + 1, args.block_chunk):
            end = min(blk + args.block_chunk - 1, to_block)
            try:
                logs = _rpc(rpc, "eth_getLogs", [{
                    "fromBlock": hex(blk),
                    "toBlock": hex(end),
                    "address": batch,
                    "topics": [SWAP_TOPICS],
                }], delay=0.08)
            except Exception as e:
                print(f"  WARN getLogs {blk}-{end} batch={len(batch)}: {e}", flush=True)
                time.sleep(1)
                continue
            if not logs:
                continue
            total_logs += len(logs)
            for entry in logs:
                bn = entry.get("blockNumber")
                if isinstance(bn, str):
                    bn = int(bn, 16)
                if bn not in block_ts_cache:
                    try:
                        block = _rpc(rpc, "eth_getBlockByNumber", [hex(bn), False], delay=0.05)
                        block_ts_cache[bn] = hex(int(block["timestamp"], 16))
                    except Exception:
                        block_ts_cache[bn] = ""
                buf.append(log_to_row(entry, block_ts_cache.get(bn, "")))
            if len(buf) >= 500 and not args.dry_run:
                insert_rows(buf)
                total_inserted += len(buf)
                buf.clear()
            if total_logs and total_logs % 5000 == 0:
                print(f"  progress logs={total_logs} inserted={total_inserted}", flush=True)

    if buf and not args.dry_run:
        insert_rows(buf)
        total_inserted += len(buf)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gap_start_utc": GAP_START_UTC,
        "from_block": from_block,
        "to_block": to_block,
        "pools": len(pools),
        "logs_fetched": total_logs,
        "rows_inserted": total_inserted,
        "dry_run": args.dry_run,
        "source": SOURCE,
    }
    (out_dir / "gap_backfill_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
