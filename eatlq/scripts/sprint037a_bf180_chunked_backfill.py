"""sprint037a_bf180_chunked_backfill.py
=========================================
Sprint037B-BF180: Chunked 180-day historical backfill.

Fills Base Uniswap V3 WETH meme pool swap history from before the
discovery-poller start (2025-10-01 → 2026-04-04) into raw_evm_logs
so the Sprint037A regime mining can find genuine historical windows.

Safety guarantees (all six constraints):
  1. Pilot is NOT used as alpha. This is data engineering only.
  2. No training/labeling/live/H12/promotion anywhere in this script.
  3. Chunked (30-day default), logged, never brute-force.
  4. After each chunk, discovered_pools is synced with metadata (symbol,
     decimals, has_weth_leg, fee_bps, created_block, created_tx_hash).
  5. Each chunk is tagged with source='bf180_YYYY_MM'. The script checks
     for this tag before inserting, making each chunk fully resumable.
  6. received_at_ms = NOW (not block_time) so data avoids 180-day TTL
     expiry. block_time carries the historically correct timestamp.

Usage:
  # Show status of all chunks (default)
  LABEL_RPC_URL=https://... python3 scripts/sprint037a_bf180_chunked_backfill.py --status

  # Dry-run a specific chunk (no CH write)
  LABEL_RPC_URL=https://... python3 scripts/sprint037a_bf180_chunked_backfill.py \
      --chunk 2025-11 --dry-run

  # Live insert a specific chunk
  LABEL_RPC_URL=https://... python3 scripts/sprint037a_bf180_chunked_backfill.py \
      --chunk 2025-11 --insert --docker-exec

  # Run all pending chunks sequentially (live)
  LABEL_RPC_URL=https://... python3 scripts/sprint037a_bf180_chunked_backfill.py \
      --run-all --insert --docker-exec

Env:
  LABEL_RPC_URL       Archive RPC (required for --insert / --dry-run)
  CLICKHOUSE_URL      HTTP CH endpoint (alternative to --docker-exec)
  CLICKHOUSE_USER / CLICKHOUSE_PASSWORD
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_STATE_FILE = _REPO / "mvp" / "diagnostics" / "sprint037a_bf180_state.json"
_CHUNK_DIR = _REPO / "mvp" / "diagnostics" / "bf180_chunks"
_AUDIT_DIR = _REPO / "mvp" / "audits"

_CHAIN_ID = 8453
_WETH = "0x4200000000000000000000000000000000000006"
_V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
_MEME_FEE_TIERS: set[int] = {100, 500, 3000, 10000}

# Block anchor (verified): block 44343068 = 2026-04-08 06:09:31 UTC
_ANCHOR_BLOCK = 44_343_068
_ANCHOR_TS = 1_775_628_571
_SEC_PER_BLOCK = 2.0

# Chunk calendar: 30-day windows from 2025-10-01 to 2026-04-04
# Format: (chunk_id, start_date_inclusive, end_date_exclusive)
# end_date is the first day NOT included (we scan blocks up to end-1 sec)
_CHUNK_CALENDAR = [
    ("2025-10", "2025-10-01", "2025-11-01"),
    ("2025-11", "2025-11-01", "2025-12-01"),
    ("2025-12", "2025-12-01", "2026-01-01"),
    ("2026-01", "2026-01-01", "2026-02-01"),
    ("2026-02", "2026-02-01", "2026-03-01"),
    ("2026-03", "2026-03-01", "2026-04-01"),
    ("2026-04a", "2026-04-01", "2026-04-05"),  # gap before pilot (Apr 5-12 done)
]

# RPC / CH config
_RPC_URL = os.environ.get("LABEL_RPC_URL", "").strip()
_CH_URL = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123/").strip()
_CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
_CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
_CH_CONTAINER = "eatlq-clickhouse-1"

_BLOCKS_PER_CHUNK = 2000    # getLogs block range
_RPC_SLEEP_S = 0.05
_MAX_ADDR_PER_CALL = 50     # addresses per getLogs call
_INSERT_BATCH = 5000        # rows per CH INSERT call


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[bf180 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Block ↔ timestamp helpers
# ---------------------------------------------------------------------------
def _ts_for_date(date_str: str) -> float:
    """Parse YYYY-MM-DD → UTC midnight timestamp."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _block_for_ts(ts: float) -> int:
    return max(0, int(_ANCHOR_BLOCK + (ts - _ANCHOR_TS) / _SEC_PER_BLOCK))


def _ts_for_block(block: int) -> float:
    return _ANCHOR_TS + (block - _ANCHOR_BLOCK) * _SEC_PER_BLOCK


def _now_dt_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
def _rpc(rpc_url: str, method: str, params: list[Any], timeout: float = 30.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = Request(rpc_url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data["result"]


def _get_logs_range(
    rpc_url: str,
    from_block: int,
    to_block: int,
    address: str | list[str] | None = None,
    topics: list[str | None] | None = None,
) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    chunk = _BLOCKS_PER_CHUNK
    pos = from_block
    while pos <= to_block:
        end = min(pos + chunk - 1, to_block)
        try:
            time.sleep(_RPC_SLEEP_S)
            params: dict[str, Any] = {"fromBlock": hex(pos), "toBlock": hex(end)}
            if address is not None:
                params["address"] = address
            if topics:
                params["topics"] = topics
            logs.extend(_rpc(rpc_url, "eth_getLogs", [params]))
        except (HTTPError, URLError, RuntimeError) as exc:
            if chunk > 100:
                chunk = chunk // 2
                continue
            raise
        pos = end + 1
    return logs


# ---------------------------------------------------------------------------
# PoolCreated parsing
# ---------------------------------------------------------------------------
def _parse_pool_created(log: dict[str, Any]) -> dict[str, Any] | None:
    topics = log.get("topics", [])
    data = log.get("data", "0x")
    if len(topics) < 4:
        return None
    try:
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        fee = int(topics[3], 16)
        data_hex = data[2:]
        if len(data_hex) < 128:
            return None
        pool = "0x" + data_hex[88:128]
        return {
            "token0": token0.lower(),
            "token1": token1.lower(),
            "fee": fee,
            "pool": pool.lower(),
            "block_number": int(log.get("blockNumber", "0x0"), 16),
            "tx_hash": log.get("transactionHash", ""),
        }
    except Exception:
        return None


def _is_weth_meme_pool(parsed: dict[str, Any]) -> bool:
    weth = _WETH.lower()
    return (
        (parsed["token0"] == weth or parsed["token1"] == weth)
        and parsed["fee"] in _MEME_FEE_TIERS
    )


# ---------------------------------------------------------------------------
# ERC20 metadata (symbol, decimals) via eth_call
# ---------------------------------------------------------------------------
def _decode_erc20_str(hex_result: str) -> str:
    """Decode ABI-encoded string OR bytes32 result from eth_call."""
    r = hex_result[2:] if hex_result.startswith("0x") else hex_result
    if len(r) < 64:
        return ""
    # Check if it looks like an ABI-encoded string (first 32 bytes = offset 0x20 = 32)
    try:
        offset = int(r[:64], 16)
        if offset == 32 and len(r) >= 128:
            length = int(r[64:128], 16)
            if length == 0:
                return ""
            raw = r[128: 128 + length * 2]
            return bytes.fromhex(raw).decode("utf-8", errors="replace").rstrip("\x00")
    except Exception:
        pass
    # Fallback: bytes32 style (strip null bytes)
    try:
        raw = bytes.fromhex(r[:64])
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _fetch_erc20_metadata(
    rpc_url: str,
    token_addresses: list[str],
    cache: dict[str, dict[str, Any]],
) -> None:
    """Fetch symbol + decimals for tokens not already in cache."""
    for addr in token_addresses:
        if addr in cache:
            continue
        sym = ""
        dec = 18
        try:
            r = _rpc(rpc_url, "eth_call",
                     [{"to": addr, "data": "0x95d89b41"}, "latest"])
            sym = _decode_erc20_str(r) if r and r != "0x" else ""
            time.sleep(_RPC_SLEEP_S)
        except Exception:
            pass
        try:
            r2 = _rpc(rpc_url, "eth_call",
                      [{"to": addr, "data": "0x313ce567"}, "latest"])
            if r2 and r2 != "0x" and len(r2) >= 66:
                dec = int(r2[2:66], 16)
            time.sleep(_RPC_SLEEP_S)
        except Exception:
            pass
        cache[addr] = {"symbol": sym[:20], "decimals": min(dec, 255)}


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------
def _ch_guard_ro(sql: str) -> None:
    t = sql.strip().lstrip("(").lower()
    if not (t.startswith("select") or t.startswith("with")):
        raise ValueError(f"read-only guard: got: {sql[:60]}")


def _ch_query(sql: str, use_docker: bool) -> list[dict[str, Any]]:
    _ch_guard_ro(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    if use_docker:
        r = subprocess.run(
            ["docker", "exec", _CH_CONTAINER, "clickhouse-client", "--query", q],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"CH query error: {r.stderr[:300]}")
        body = r.stdout
    else:
        req = Request(_CH_URL.rstrip("/") + "/", data=q.encode(), method="POST")
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        if _CH_USER:
            tok = b64encode(f"{_CH_USER}:{_CH_PASSWORD}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
        with urlopen(req, timeout=120.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _ch_insert_rows(
    table: str,
    rows: list[dict[str, Any]],
    use_docker: bool,
) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(r, default=str) for r in rows).encode()
    if use_docker:
        r = subprocess.run(
            ["docker", "exec", "-i", _CH_CONTAINER, "clickhouse-client",
             "--query", f"INSERT INTO {table} FORMAT JSONEachRow"],
            input=payload,
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"CH insert error ({table}): {r.stderr[:300]}"
            )
    else:
        url = _CH_URL.rstrip("/") + f"/?query=INSERT+INTO+{table}+FORMAT+JSONEachRow"
        req = Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        if _CH_USER:
            tok = b64encode(f"{_CH_USER}:{_CH_PASSWORD}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
        with urlopen(req, timeout=300.0) as resp:
            resp.read()


def _ch_insert_batched(
    table: str,
    rows: list[dict[str, Any]],
    use_docker: bool,
    batch_size: int = _INSERT_BATCH,
) -> int:
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i: i + batch_size]
        _ch_insert_rows(table, batch, use_docker)
        total += len(batch)
        _log(f"  inserted {total}/{len(rows)} rows into {table}")
    return total


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"chunks": {}, "token_symbol_cache": {}, "created_at": _now_dt_str()}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ---------------------------------------------------------------------------
# Chunk resumability check
# ---------------------------------------------------------------------------
def _chunk_already_done(source_tag: str, use_docker: bool) -> tuple[bool, int]:
    """Returns (done, existing_row_count) for the given source tag."""
    q = f"""
    SELECT count() AS n FROM eatlq.raw_evm_logs
    WHERE chain_id = {_CHAIN_ID} AND source = '{source_tag}'
    """
    try:
        rows = _ch_query(q, use_docker)
        n = int(rows[0].get("n", 0)) if rows else 0
        return n > 0, n
    except Exception:
        return False, 0


# ---------------------------------------------------------------------------
# Build raw_evm_logs row from eth_getLogs entry
# ---------------------------------------------------------------------------
def _swap_log_to_raw_row(log: dict[str, Any], source_tag: str, now_ms: str) -> dict[str, Any]:
    bn = int(log.get("blockNumber", "0x0"), 16)
    li = int(log.get("logIndex", "0x0"), 16)
    # block_time: linear interpolation (historically correct)
    bt_sec = _ts_for_block(bn)
    bt_str = datetime.fromtimestamp(bt_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")
    topics = log.get("topics", [])
    return {
        "chain_id": _CHAIN_ID,
        "source": source_tag,
        "subscription_id": "",
        "block_time": bt_str,
        # received_at_ms = NOW so TTL (180d from received_at_ms) doesn't expire historical data
        "received_at_ms": now_ms,
        "block_number": bn,
        "tx_hash": log.get("transactionHash", ""),
        "log_index": li,
        "address": (log.get("address") or "").lower(),
        "topic0": topics[0] if len(topics) > 0 else "",
        "topic1": topics[1] if len(topics) > 1 else "",
        "topic2": topics[2] if len(topics) > 2 else "",
        "topic3": topics[3] if len(topics) > 3 else "",
        "data": log.get("data", ""),
        "removed": 1 if log.get("removed") else 0,
        "value_usd": None,
        "payload_json": json.dumps({"backfill": True, "chunk": source_tag}),
        "ingested_at": now_ms,
    }


# ---------------------------------------------------------------------------
# Build discovered_pools row
# ---------------------------------------------------------------------------
def _build_pool_row(
    parsed: dict[str, Any],
    sym_cache: dict[str, dict[str, Any]],
    discovered_at: str,
) -> dict[str, Any]:
    t0_meta = sym_cache.get(parsed["token0"], {"symbol": "", "decimals": 18})
    t1_meta = sym_cache.get(parsed["token1"], {"symbol": "", "decimals": 18})
    weth = _WETH.lower()
    has_weth = 1 if (parsed["token0"] == weth or parsed["token1"] == weth) else 0
    return {
        "chain_id": _CHAIN_ID,
        "pool_address": parsed["pool"],
        "factory_address": _V3_FACTORY,
        "pool_type": "v3",
        "token0_address": parsed["token0"],
        "token0_symbol": t0_meta["symbol"],
        "token0_decimals": t0_meta["decimals"],
        "token1_address": parsed["token1"],
        "token1_symbol": t1_meta["symbol"],
        "token1_decimals": t1_meta["decimals"],
        "fee_bps": parsed["fee"],
        "has_weth_leg": has_weth,
        "created_block": parsed["block_number"],
        "created_tx_hash": parsed.get("tx_hash", ""),
        "discovered_at": discovered_at,
    }


# ---------------------------------------------------------------------------
# Process one chunk
# ---------------------------------------------------------------------------
def process_chunk(
    chunk_id: str,
    start_date: str,
    end_date: str,
    rpc_url: str,
    do_insert: bool,
    use_docker: bool,
    state: dict[str, Any],
    max_pools: int = 500,
) -> dict[str, Any]:
    source_tag = f"bf180_{chunk_id.replace('-', '_')}"
    _log(f"=== Chunk {chunk_id}: {start_date} → {end_date} [source={source_tag}] ===")

    # Resumability check
    done, existing_rows = _chunk_already_done(source_tag, use_docker)
    if done:
        _log(f"  SKIP: already inserted ({existing_rows} rows in raw_evm_logs)")
        return {"status": "skipped", "existing_rows": existing_rows, "source_tag": source_tag}

    now_ms = _now_dt_str()
    start_ts = _ts_for_date(start_date)
    end_ts = _ts_for_date(end_date) - 1  # last second of the day before end_date
    start_block = _block_for_ts(start_ts)
    end_block = _block_for_ts(end_ts)
    _log(f"  blocks: {start_block:,} → {end_block:,} ({end_block-start_block:,} blocks)")

    # Phase 1: Factory scan
    _log(f"  scanning factory for PoolCreated …")
    factory_logs = _get_logs_range(
        rpc_url, start_block, end_block,
        address=_V3_FACTORY,
        topics=[_POOL_CREATED_TOPIC],
    )
    _log(f"  factory logs: {len(factory_logs)}")

    pools_parsed: list[dict[str, Any]] = []
    for log in factory_logs:
        parsed = _parse_pool_created(log)
        if parsed and _is_weth_meme_pool(parsed):
            pools_parsed.append(parsed)

    _log(f"  WETH meme pools in chunk: {len(pools_parsed)}")
    if not pools_parsed:
        summary = {
            "status": "no_pools", "chunk_id": chunk_id,
            "factory_logs": len(factory_logs), "pools_found": 0,
            "swap_logs": 0, "rows_inserted": 0,
            "source_tag": source_tag,
        }
        return summary

    # Phase 2: Swap scan (batch by address)
    pool_addrs = [p["pool"] for p in pools_parsed[:max_pools]]
    _log(f"  scanning Swap events for {len(pool_addrs)} pools …")
    swap_logs: list[dict[str, Any]] = []
    for i in range(0, len(pool_addrs), _MAX_ADDR_PER_CALL):
        batch = pool_addrs[i: i + _MAX_ADDR_PER_CALL]
        blogs = _get_logs_range(
            rpc_url, start_block, end_block,
            address=batch,
            topics=[_SWAP_TOPIC],
        )
        swap_logs.extend(blogs)
        _log(f"    addr batch {i//50+1}: {len(blogs)} logs")

    # Filter to valid swap structure (matches MV criteria)
    valid_logs = [
        lg for lg in swap_logs
        if len((lg.get("topics") or ["", "", ""])[1] if len(lg.get("topics", [])) > 1 else "") == 66
        and len((lg.get("topics") or ["", "", ""])[2] if len(lg.get("topics", [])) > 2 else "") == 66
        and len(lg.get("data", "")) >= 322
    ]
    _log(f"  valid swap logs: {len(valid_logs)} / {len(swap_logs)}")

    rows_to_insert = [
        _swap_log_to_raw_row(lg, source_tag, now_ms) for lg in valid_logs
    ]

    # Phase 3: ERC20 metadata — only for pools we actually scanned swap events for.
    # (We don't sync metadata for the full factory universe; that's a separate task.)
    sym_cache: dict[str, dict[str, Any]] = state.get("token_symbol_cache", {})
    scanned_pool_set = set(pool_addrs)
    unique_tokens = {
        tok
        for p in pools_parsed
        if p["pool"] in scanned_pool_set
        for tok in [p["token0"], p["token1"]]
        if tok != _WETH.lower()
    }
    new_tokens = [t for t in unique_tokens if t not in sym_cache]
    if new_tokens:
        _log(f"  fetching ERC20 metadata for {len(new_tokens)} tokens …")
        _fetch_erc20_metadata(rpc_url, new_tokens, sym_cache)
        # WETH entry
        sym_cache[_WETH.lower()] = {"symbol": "WETH", "decimals": 18}
        state["token_symbol_cache"] = sym_cache
        _save_state(state)
        _log(f"  metadata cache now covers {len(sym_cache)} tokens")

    # Phase 4: Check which SCANNED pools are already in discovered_pools
    pool_addr_list = "', '".join(pool_addrs)
    existing_pool_q = f"""
    SELECT lower(pool_address) AS pa
    FROM eatlq.discovered_pools
    WHERE chain_id = {_CHAIN_ID}
      AND lower(pool_address) IN ('{pool_addr_list}')
    """
    try:
        existing_pools_rows = _ch_query(existing_pool_q, use_docker)
        existing_pool_set = {r["pa"] for r in existing_pools_rows}
    except Exception:
        existing_pool_set = set()
    new_pools = [p for p in pools_parsed if p["pool"] in scanned_pool_set and p["pool"] not in existing_pool_set]
    _log(f"  new pools for discovered_pools: {len(new_pools)} (already: {len(pools_parsed)-len(new_pools)})")

    pool_rows = [
        _build_pool_row(p, sym_cache, now_ms[:19])  # DateTime (no ms)
        for p in new_pools
    ]

    # Phase 5: Insert (or dry-run)
    rows_inserted = 0
    pools_synced = 0
    if do_insert:
        if rows_to_insert:
            _log(f"  inserting {len(rows_to_insert)} rows into raw_evm_logs …")
            rows_inserted = _ch_insert_batched(
                "eatlq.raw_evm_logs", rows_to_insert, use_docker
            )
        if pool_rows:
            _log(f"  syncing {len(pool_rows)} pools into discovered_pools …")
            pools_synced = _ch_insert_batched(
                "eatlq.discovered_pools", pool_rows, use_docker
            )
        time.sleep(2)  # let MV settle
    else:
        _log(f"  DRY RUN: would insert {len(rows_to_insert)} swap rows, {len(pool_rows)} pool rows")

    # Phase 6: Validation
    validation: dict[str, Any] = {}
    if do_insert and pool_addrs:
        vq = f"""
        SELECT
          count()                                   AS swap_rows,
          uniqExact(lower(pool_address))            AS pools,
          countIf(source = '{source_tag}')          AS pilot_rows
        FROM eatlq.uniswap_v3_swaps
        WHERE chain_id = {_CHAIN_ID}
          AND source = '{source_tag}'
        """
        try:
            vrows = _ch_query(vq, use_docker)
            validation = vrows[0] if vrows else {}
            _log(f"  validation → {validation}")
        except Exception as exc:
            validation = {"error": str(exc)}

    fee_dist: dict[str, int] = {}
    for p in pools_parsed:
        k = f"fee_{p['fee']}"
        fee_dist[k] = fee_dist.get(k, 0) + 1

    summary: dict[str, Any] = {
        "status": "inserted" if do_insert else "dry_run",
        "chunk_id": chunk_id,
        "start_date": start_date,
        "end_date": end_date,
        "source_tag": source_tag,
        "generated_at": now_ms,
        "block_range": {"start": start_block, "end": end_block},
        "factory_logs": len(factory_logs),
        "pools_found_in_factory": len(pools_parsed),
        "pools_scanned_for_swaps": len(pool_addrs),
        "pools_found": len(pools_parsed),
        "pool_fee_distribution": fee_dist,
        "swap_logs_fetched": len(swap_logs),
        "valid_swap_logs": len(valid_logs),
        "existing_ch_rows_overlap": 0,
        "rows_inserted": rows_inserted,
        "rows_dry_run": len(rows_to_insert) if not do_insert else 0,
        "pools_synced_to_discovered": pools_synced,
        "pools_already_in_discovered": len(pools_parsed) - len(new_pools),
        "validation": validation,
    }
    return summary


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------
def print_status(state: dict[str, Any], use_docker: bool) -> None:
    print("\n=== Sprint037B-BF180 Chunk Status ===\n")
    chunks = state.get("chunks", {})
    for chunk_id, start_date, end_date in _CHUNK_CALENDAR:
        cs = chunks.get(chunk_id, {})
        status = cs.get("status", "pending")
        rows = cs.get("rows_inserted", 0)
        pools = cs.get("pools_found", 0)
        synced = cs.get("pools_synced_to_discovered", 0)
        icon = {"done": "✓", "skipped": "↩", "dry_run": "~", "pending": " ", "error": "✗"}.get(status, "?")
        print(f"  [{icon}] {chunk_id}  {start_date}→{end_date}  "
              f"status={status:<8} swap_rows={rows:>7,} pools={pools:>4} meta_synced={synced:>4}")
    print()
    cache_size = len(state.get("token_symbol_cache", {}))
    print(f"  Token symbol cache: {cache_size} entries")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint037B-BF180 chunked historical backfill")
    ap.add_argument("--status", action="store_true", help="show chunk status and exit")
    ap.add_argument("--chunk", help="run a specific chunk (e.g. 2025-11)")
    ap.add_argument("--run-all", action="store_true", help="run all pending chunks")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="scan but do NOT write to CH (default)")
    ap.add_argument("--insert", action="store_true",
                    help="actually INSERT into raw_evm_logs and discovered_pools")
    ap.add_argument("--docker-exec", action="store_true",
                    help="use docker exec for CH access")
    ap.add_argument("--max-pools", type=int, default=500,
                    help="max pools per chunk for swap scan (default 500)")
    ap.add_argument("--rpc-url", default=_RPC_URL)
    args = ap.parse_args()

    rpc_url = args.rpc_url
    do_insert = args.insert
    use_docker = args.docker_exec

    _CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()

    if args.status:
        print_status(state, use_docker)
        return

    if not (args.chunk or args.run_all):
        print_status(state, use_docker)
        print("Pass --chunk YYYY-MM (e.g. --chunk 2025-11) or --run-all to proceed.")
        print("Add --insert to actually write to CH (default: dry-run).")
        return

    if not rpc_url:
        print("ERROR: LABEL_RPC_URL env or --rpc-url required", file=sys.stderr)
        sys.exit(1)

    _log(f"mode: {'LIVE INSERT' if do_insert else 'DRY RUN'}, docker={use_docker}")

    # Determine which chunks to run
    chunks_to_run: list[tuple[str, str, str]] = []
    if args.chunk:
        matches = [(cid, sd, ed) for cid, sd, ed in _CHUNK_CALENDAR if cid == args.chunk]
        if not matches:
            print(f"ERROR: chunk '{args.chunk}' not in calendar. Options: "
                  + ", ".join(cid for cid, _, _ in _CHUNK_CALENDAR), file=sys.stderr)
            sys.exit(1)
        chunks_to_run = matches
    elif args.run_all:
        chunks_to_run = [
            (cid, sd, ed) for cid, sd, ed in _CHUNK_CALENDAR
            if state.get("chunks", {}).get(cid, {}).get("status") not in ("done", "skipped")
        ]
        _log(f"pending chunks: {len(chunks_to_run)}")

    for chunk_id, start_date, end_date in chunks_to_run:
        summary = process_chunk(
            chunk_id=chunk_id,
            start_date=start_date,
            end_date=end_date,
            rpc_url=rpc_url,
            do_insert=do_insert,
            use_docker=use_docker,
            state=state,
            max_pools=args.max_pools,
        )

        # Save chunk summary
        chunk_file = _CHUNK_DIR / f"chunk_{chunk_id}.json"
        chunk_file.write_text(json.dumps(summary, indent=2, default=str))
        _log(f"  chunk summary → {chunk_file.relative_to(_REPO)}")

        # Update state
        status = "done" if do_insert and summary.get("rows_inserted", 0) > 0 else summary.get("status", "")
        state.setdefault("chunks", {})[chunk_id] = {
            "status": status,
            "rows_inserted": summary.get("rows_inserted", 0),
            "pools_found": summary.get("pools_found", 0),
            "pools_synced_to_discovered": summary.get("pools_synced_to_discovered", 0),
            "source_tag": summary.get("source_tag", ""),
            "completed_at": _now_dt_str(),
        }
        _save_state(state)

    # Final status
    print_status(state, use_docker)
    _log("done")


if __name__ == "__main__":
    main()
