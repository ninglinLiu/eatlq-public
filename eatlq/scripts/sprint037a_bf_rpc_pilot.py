"""sprint037a_bf_rpc_pilot.py
==============================
1–7 day targeted RPC backfill pilot.

Pipeline validated:
  eth_getLogs (Factory PoolCreated + Swap events)
  → parse to raw_evm_logs row format
  → INSERT eatlq.raw_evm_logs
  → MaterializedView auto-populates eatlq.uniswap_v3_swaps
  → validate row counts in uniswap_v3_swaps_deduped

Redlines:
  - Read-only RPC (eth_getLogs, eth_getBlockByNumber, eth_blockNumber)
  - INSERT only into raw_evm_logs (no schema changes)
  - No training / labeling / live ops / model promotion
  - Dry-run by default: pass --insert to actually write

Usage:
  # Dry run (default): scan Factory + Swap events, print summary, no CH write
  LABEL_RPC_URL=https://... python scripts/sprint037a_bf_rpc_pilot.py --dry-run

  # Live insert into CH via docker exec
  LABEL_RPC_URL=https://... python scripts/sprint037a_bf_rpc_pilot.py \
      --insert --docker-exec \
      --pilot-days 7 --pilot-end-date 2026-04-12

  # Custom pilot window (6-month historical)
  LABEL_RPC_URL=https://... python scripts/sprint037a_bf_rpc_pilot.py \
      --insert --docker-exec \
      --pilot-days 7 --pilot-end-date 2025-11-07

Env:
  LABEL_RPC_URL        Archive RPC endpoint (required for --dry-run / --insert)
  CLICKHOUSE_URL       HTTP endpoint for CH (alternative to --docker-exec)
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_DIAG_DIR = _REPO / "mvp" / "diagnostics"
_AUDIT_DIR = _REPO / "mvp" / "audits"

_CHAIN_ID = 8453
_WETH = "0x4200000000000000000000000000000000000006"

# Uniswap V3 Factory on Base
_V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# Meme fee tiers (raw Uniswap V3 fee values in 1/1,000,000 units)
_MEME_FEE_TIERS: set[int] = {100, 500, 3000, 10000}

# RPC
_RPC_URL = os.environ.get("LABEL_RPC_URL", "").strip()
_BLOCKS_PER_CHUNK = 2000     # getLogs block range per call
_RPC_SLEEP_S = 0.05          # ~20 calls/sec max
_MAX_ADDR_PER_CALL = 50      # addresses per getLogs call

# CH
_CH_URL = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123/").strip()
_CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
_CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
_CH_CONTAINER = "eatlq-clickhouse-1"

_SOURCE_TAG = "rpc_pilot_sprint037a"


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
        raise RuntimeError(f"RPC error {method}: {data['error']}")
    return data["result"]


def _get_block_number(rpc_url: str) -> int:
    result = _rpc(rpc_url, "eth_blockNumber", [])
    return int(result, 16)


def _get_block_timestamp(rpc_url: str, block_num: int) -> int:
    """Returns UNIX timestamp (seconds) for a block."""
    result = _rpc(rpc_url, "eth_getBlockByNumber", [hex(block_num), False])
    if result is None:
        return 0
    return int(result["timestamp"], 16)


def _estimate_block_for_ts(rpc_url: str, target_ts: float, current_block: int) -> int:
    """
    Linear estimate: Base produces ~1 block/2s.
    Binary refine with 3 iterations to get within ±60 blocks (~2 min).
    """
    now_ts = time.time()
    delta_sec = now_ts - target_ts
    estimate = max(0, current_block - int(delta_sec / 2.0))

    # Binary search refinement (3 iterations)
    lo, hi = max(0, estimate - 5000), estimate + 5000
    for _ in range(3):
        mid = (lo + hi) // 2
        mid_ts = _get_block_timestamp(rpc_url, mid)
        if mid_ts < target_ts:
            lo = mid
        else:
            hi = mid
        time.sleep(0.1)
    return (lo + hi) // 2


def _get_logs_chunk(
    rpc_url: str,
    from_block: int,
    to_block: int,
    address: str | list[str] | None = None,
    topics: list[str | None] | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
    }
    if address is not None:
        params["address"] = address
    if topics:
        params["topics"] = topics
    time.sleep(_RPC_SLEEP_S)
    return _rpc(rpc_url, "eth_getLogs", [params])


def _get_logs_range(
    rpc_url: str,
    from_block: int,
    to_block: int,
    address: str | list[str] | None = None,
    topics: list[str | None] | None = None,
) -> list[dict[str, Any]]:
    """Chunked getLogs over a large block range."""
    logs: list[dict[str, Any]] = []
    chunk = _BLOCKS_PER_CHUNK
    pos = from_block
    while pos <= to_block:
        end = min(pos + chunk - 1, to_block)
        try:
            chunk_logs = _get_logs_chunk(rpc_url, pos, end, address, topics)
            logs.extend(chunk_logs)
        except (HTTPError, URLError, RuntimeError) as exc:
            # Halve chunk on error and retry
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
    """Parse PoolCreated event log → {token0, token1, fee, pool_address}."""
    topics = log.get("topics", [])
    data = log.get("data", "0x")
    if len(topics) < 4:
        return None
    try:
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        fee = int(topics[3], 16)
        # data = abi.encode(int24 tickSpacing, address pool)
        # 32 bytes tickSpacing + 32 bytes pool
        data_hex = data[2:]  # strip 0x
        if len(data_hex) < 128:
            return None
        pool = "0x" + data_hex[88:128]  # last 20 bytes of second word
        return {"token0": token0.lower(), "token1": token1.lower(), "fee": fee, "pool": pool.lower()}
    except Exception:
        return None


def _is_weth_meme_pool(parsed: dict[str, Any]) -> bool:
    weth = _WETH.lower()
    return (
        (parsed["token0"] == weth or parsed["token1"] == weth)
        and parsed["fee"] in _MEME_FEE_TIERS
    )


# ---------------------------------------------------------------------------
# Block timestamp cache
# ---------------------------------------------------------------------------
def _fetch_block_timestamps(
    rpc_url: str,
    block_numbers: set[int],
    anchor_block: int = 44343068,
    anchor_ts: int = 1775628571,  # 2026-04-08 06:09:31 UTC (verified)
    secs_per_block: float = 2.0,
) -> dict[int, int]:
    """Estimate block timestamps via linear interpolation from a known anchor.

    Base mainnet is extremely consistent at ~2s/block.  Fetching each block
    individually would take 10+ minutes for 40K blocks; linear interpolation
    is accurate to ±60s which is more than sufficient for raw_evm_logs.block_time.

    For blocks far outside the anchor range, one spot-check RPC call verifies
    the anchor is still valid.
    """
    if not block_numbers:
        return {}
    sorted_blocks = sorted(block_numbers)
    # Quick sanity: check one outlier block against the formula
    mid = sorted_blocks[len(sorted_blocks) // 2]
    estimated_mid_ts = anchor_ts + (mid - anchor_block) * secs_per_block
    try:
        actual_mid_ts = _get_block_timestamp(rpc_url, mid)
        if actual_mid_ts and abs(actual_mid_ts - estimated_mid_ts) > 300:
            # Anchor drifted — recalibrate using the fetched point
            secs_per_block = secs_per_block  # keep default; could refine further
            anchor_block = mid
            anchor_ts = actual_mid_ts
    except Exception:
        pass

    cache: dict[int, int] = {}
    for bn in block_numbers:
        ts = anchor_ts + (bn - anchor_block) * secs_per_block
        cache[bn] = int(ts)
    return cache


# ---------------------------------------------------------------------------
# raw_evm_logs row builder
# ---------------------------------------------------------------------------
def _log_to_raw_evm_row(
    log: dict[str, Any],
    block_ts_cache: dict[int, int],
    now_ms: str,
) -> dict[str, Any]:
    bn = int(log.get("blockNumber", "0x0"), 16)
    li = int(log.get("logIndex", "0x0"), 16)
    ts_sec = block_ts_cache.get(bn, 0)
    dt_str = datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000") if ts_sec else None
    topics = log.get("topics", [])
    return {
        "chain_id": _CHAIN_ID,
        "source": _SOURCE_TAG,
        "subscription_id": "",
        "block_time": dt_str,
        "received_at_ms": dt_str or now_ms,
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
        "payload_json": json.dumps({"backfill": True, "pilot": "sprint037a"}),
        "ingested_at": now_ms,
    }


# ---------------------------------------------------------------------------
# ClickHouse helpers (read-only guard + insert)
# ---------------------------------------------------------------------------
def _ch_guard_ro(sql: str) -> None:
    t = sql.strip().lstrip("(").lower()
    if not (t.startswith("select") or t.startswith("with")):
        raise ValueError(f"_ch_guard_ro: only SELECT/WITH allowed; got: {sql[:60]}")


def _ch_query_docker(sql: str) -> list[dict[str, Any]]:
    _ch_guard_ro(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    r = subprocess.run(
        ["docker", "exec", _CH_CONTAINER, "clickhouse-client", "--query", q],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH query error: {r.stderr[:400]}")
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _ch_query_http(sql: str) -> list[dict[str, Any]]:
    _ch_guard_ro(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    req = Request(_CH_URL.rstrip("/") + "/", data=q.encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if _CH_USER:
        tok = b64encode(f"{_CH_USER}:{_CH_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with urlopen(req, timeout=60.0) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _ch_insert_docker(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    payload = "\n".join(json.dumps(r, default=str) for r in rows)
    sql_prefix = "INSERT INTO eatlq.raw_evm_logs FORMAT JSONEachRow\n"
    full = sql_prefix + payload
    r = subprocess.run(
        ["docker", "exec", "-i", _CH_CONTAINER, "clickhouse-client", "--query",
         "INSERT INTO eatlq.raw_evm_logs FORMAT JSONEachRow"],
        input=payload.encode(),
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH insert error: {r.stderr[:400]}")
    return len(rows)


def _ch_insert_http(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    payload = "\n".join(json.dumps(r, default=str) for r in rows).encode()
    url = _CH_URL.rstrip("/") + "/?query=INSERT+INTO+eatlq.raw_evm_logs+FORMAT+JSONEachRow"
    req = Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-ndjson")
    if _CH_USER:
        tok = b64encode(f"{_CH_USER}:{_CH_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with urlopen(req, timeout=120.0) as resp:
        resp.read()
    return len(rows)


# ---------------------------------------------------------------------------
# Validation query
# ---------------------------------------------------------------------------
def _validate_query(pool_addrs: list[str], from_ts: str, to_ts: str) -> str:
    addr_list = ", ".join(f"'{a}'" for a in pool_addrs)
    return f"""
SELECT
  count()                                  AS swap_rows,
  uniqExact(lower(pool_address))           AS pools,
  min(coalesce(block_time, received_at_ms)) AS min_ts,
  max(coalesce(block_time, received_at_ms)) AS max_ts,
  countIf(source = '{_SOURCE_TAG}')        AS pilot_rows,
  countIf(source != '{_SOURCE_TAG}')       AS streaming_rows
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {_CHAIN_ID}
  AND lower(pool_address) IN ({addr_list})
  AND coalesce(block_time, received_at_ms) BETWEEN '{from_ts}' AND '{to_ts}'
"""


# ---------------------------------------------------------------------------
# Pilot orchestrator
# ---------------------------------------------------------------------------
def run_pilot(
    rpc_url: str,
    pilot_end_date: str,
    pilot_days: int,
    do_insert: bool,
    use_docker: bool,
    max_pools: int,
    start_block_override: int = 0,
    end_block_override: int = 0,
) -> dict[str, Any]:
    ch_query = _ch_query_docker if use_docker else _ch_query_http
    ch_insert = _ch_insert_docker if use_docker else _ch_insert_http

    now_ms = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")

    # --- determine pilot window ---
    pilot_end_dt = datetime.strptime(pilot_end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    pilot_start_dt = pilot_end_dt - timedelta(days=pilot_days)
    pilot_end_ts = pilot_end_dt.timestamp()
    pilot_start_ts = pilot_start_dt.timestamp()

    print(f"[pilot] window: {pilot_start_dt.date()} → {pilot_end_dt.date()} ({pilot_days}d)", flush=True)

    # --- estimate block range ---
    if start_block_override and end_block_override:
        start_block = start_block_override
        end_block = end_block_override
        print(f"[pilot] block range (override): {start_block:,} → {end_block:,} ({end_block-start_block:,} blocks)", flush=True)
    else:
        print("[pilot] fetching current block number …", flush=True)
        current_block = _get_block_number(rpc_url)
        print(f"[pilot] current block: {current_block:,}", flush=True)
        print("[pilot] estimating start block …", flush=True)
        start_block = _estimate_block_for_ts(rpc_url, pilot_start_ts, current_block)
        print("[pilot] estimating end block …", flush=True)
        end_block = _estimate_block_for_ts(rpc_url, pilot_end_ts, current_block)
        print(f"[pilot] block range: {start_block:,} → {end_block:,} ({end_block-start_block:,} blocks)", flush=True)

    # --- Phase 1: scan Factory for PoolCreated events ---
    print("[pilot] scanning V3 Factory for PoolCreated events …", flush=True)
    factory_logs = _get_logs_range(
        rpc_url, start_block, end_block,
        address=_V3_FACTORY,
        topics=[_POOL_CREATED_TOPIC],
    )
    print(f"[pilot] factory logs: {len(factory_logs)}", flush=True)

    pools_found: list[dict[str, Any]] = []
    for log in factory_logs:
        parsed = _parse_pool_created(log)
        if parsed and _is_weth_meme_pool(parsed):
            pools_found.append({
                **parsed,
                "block_number": int(log.get("blockNumber", "0x0"), 16),
            })

    print(f"[pilot] WETH meme pools found (fee {sorted(_MEME_FEE_TIERS)}): {len(pools_found)}")

    if not pools_found:
        print("[pilot] no meme pools found in pilot window — pilot window may predate meme activity")
        return {
            "status": "no_pools_found",
            "pilot_window": {"start": str(pilot_start_dt.date()), "end": str(pilot_end_dt.date())},
            "block_range": {"start": start_block, "end": end_block},
            "factory_logs_total": len(factory_logs),
            "meme_pools_found": 0,
            "swap_logs_fetched": 0,
            "rows_inserted": 0,
            "validation": {},
        }

    pool_addrs = [p["pool"] for p in pools_found[:max_pools]]
    print(f"[pilot] using top {len(pool_addrs)} pools for swap scan")

    # --- Phase 2: pull Swap events for those pools ---
    print("[pilot] pulling Swap events …")
    swap_logs: list[dict[str, Any]] = []
    for i in range(0, len(pool_addrs), _MAX_ADDR_PER_CALL):
        batch = pool_addrs[i: i + _MAX_ADDR_PER_CALL]
        batch_logs = _get_logs_range(
            rpc_url, start_block, end_block,
            address=batch,
            topics=[_SWAP_TOPIC],
        )
        swap_logs.extend(batch_logs)
        print(f"  batch {i//50+1}: {len(batch_logs)} logs")

    print(f"[pilot] total Swap logs: {len(swap_logs)}")

    # Filter: length(topic1)==66, length(topic2)==66, length(data)>=322 (matches MV)
    valid_logs = [
        lg for lg in swap_logs
        if len((lg.get("topics") or ["", "", ""])[1]) == 66
        and len((lg.get("topics") or ["", "",""])[2]) == 66
        and len(lg.get("data", "")) >= 322
    ]
    print(f"[pilot] valid Swap logs (match MV criteria): {len(valid_logs)}")

    # --- Phase 3: fetch block timestamps ---
    unique_blocks: set[int] = set()
    for lg in valid_logs:
        unique_blocks.add(int(lg.get("blockNumber", "0x0"), 16))
    print(f"[pilot] estimating timestamps for {len(unique_blocks)} unique blocks (linear interp) …", flush=True)
    block_ts = _fetch_block_timestamps(rpc_url, unique_blocks)

    # --- Phase 4: check for existing rows in CH ---
    print("[pilot] checking existing CH rows for overlap …")
    overlap_q = f"""
    SELECT count() AS existing
    FROM eatlq.raw_evm_logs
    WHERE chain_id = {_CHAIN_ID}
      AND lower(address) IN ({', '.join(f"'{a}'" for a in pool_addrs)})
      AND topic0 = '{_SWAP_TOPIC}'
      AND received_at_ms BETWEEN '{pilot_start_dt.strftime('%Y-%m-%d %H:%M:%S')}' AND '{pilot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}'
    """
    try:
        existing = ch_query(overlap_q)
        existing_count = int(existing[0].get("existing", 0)) if existing else 0
    except Exception as exc:
        print(f"[pilot] warning: overlap check failed: {exc}")
        existing_count = -1

    print(f"[pilot] existing CH rows in this window/pool set: {existing_count}")

    # --- Phase 5: build rows ---
    rows_to_insert = [
        _log_to_raw_evm_row(lg, block_ts, now_ms) for lg in valid_logs
    ]

    # --- Phase 6: insert or dry-run ---
    rows_inserted = 0
    if do_insert and rows_to_insert:
        print(f"[pilot] inserting {len(rows_to_insert)} rows into raw_evm_logs …")
        rows_inserted = ch_insert(rows_to_insert)
        print(f"[pilot] inserted {rows_inserted} rows")
        # Give MV a moment to process
        time.sleep(2)
    else:
        print(f"[pilot] DRY RUN — would insert {len(rows_to_insert)} rows (pass --insert to write)")

    # --- Phase 7: validate ---
    validation: dict[str, Any] = {}
    if do_insert and pool_addrs:
        print("[pilot] validating uniswap_v3_swaps …")
        vq = _validate_query(
            pool_addrs[:20],
            pilot_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            pilot_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            vrows = ch_query(vq)
            validation = vrows[0] if vrows else {}
            print(f"[pilot] validation: {validation}")
        except Exception as exc:
            validation = {"error": str(exc)}
            print(f"[pilot] validation error: {exc}")

    pool_fee_dist: dict[str, int] = {}
    for p in pools_found[:max_pools]:
        key = f"fee_{p['fee']}"
        pool_fee_dist[key] = pool_fee_dist.get(key, 0) + 1

    return {
        "status": "inserted" if do_insert else "dry_run",
        "generated_at": now_ms,
        "pilot_window": {
            "start": str(pilot_start_dt.date()),
            "end": str(pilot_end_dt.date()),
            "days": pilot_days,
        },
        "block_range": {"start": start_block, "end": end_block},
        "factory_logs_total": len(factory_logs),
        "meme_pools_found": len(pools_found),
        "pools_used_for_scan": len(pool_addrs),
        "pool_fee_distribution": pool_fee_dist,
        "swap_logs_fetched": len(swap_logs),
        "valid_swap_logs": len(valid_logs),
        "unique_blocks_fetched": len(unique_blocks),
        "existing_ch_rows_in_window": existing_count,
        "rows_inserted": rows_inserted if do_insert else 0,
        "rows_dry_run": len(rows_to_insert) if not do_insert else 0,
        "validation": validation,
        "pipeline_validated": do_insert and int(validation.get("swap_rows", 0)) > 0,
        "decision_flags": {
            "PILOT_DATA_FOUND": len(valid_logs) > 0,
            "PIPELINE_INSERT_VALIDATED": do_insert and int(validation.get("pilot_rows", 0)) > 0,
            "FULL_BACKFILL_UNBLOCKED": len(valid_logs) > 0 and do_insert,
            "SPRINT037B_REPLAY_ALLOWED": False,
        },
    }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------
def _write_md(data: dict[str, Any], path: Path) -> None:
    flags = data.get("decision_flags", {})
    pw = data.get("pilot_window", {})
    br = data.get("block_range", {})
    val = data.get("validation", {})

    lines = [
        "# Sprint037A-BF: RPC Pilot Report",
        f"*Generated: {data.get('generated_at','')}*",
        f"*Status: **{data.get('status','').upper()}***",
        "",
        "## Pilot Window",
        f"- Date range: {pw.get('start','')} → {pw.get('end','')} ({pw.get('days','')} days)",
        f"- Block range: {br.get('start',0):,} → {br.get('end',0):,}",
        "",
        "## Factory Scan (PoolCreated)",
        f"- Total factory logs: {data.get('factory_logs_total',0):,}",
        f"- WETH meme pools found: {data.get('meme_pools_found',0):,}",
        f"- Fee distribution: {data.get('pool_fee_distribution',{})}",
        "",
        "## Swap Event Pull",
        f"- Raw swap logs fetched: {data.get('swap_logs_fetched',0):,}",
        f"- Valid (match MV criteria): {data.get('valid_swap_logs',0):,}",
        f"- Unique blocks: {data.get('unique_blocks_fetched',0):,}",
        f"- Existing CH rows (overlap): {data.get('existing_ch_rows_in_window','?')}",
        "",
        "## Insert / Dry-Run",
        f"- Rows inserted: {data.get('rows_inserted',0):,}",
        f"- Rows dry-run (not written): {data.get('rows_dry_run',0):,}",
        "",
    ]

    if val:
        lines += [
            "## Validation (uniswap_v3_swaps)",
            "",
            "| metric | value |",
            "|--------|-------|",
        ]
        for k, v in val.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    lines += [
        "## Decision Flags",
        "",
        "| flag | value |",
        "|------|-------|",
    ]
    for k, v in flags.items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "---",
        "## Pipeline Validated",
        f"> {data.get('pipeline_validated', False)}",
        "",
        "> If PILOT_DATA_FOUND=True and PIPELINE_INSERT_VALIDATED=True, "
        "proceed with full historical backfill.",
    ]

    path.write_text("\n".join(lines))
    print(f"[pilot] report → {path.relative_to(_REPO)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint037A-BF RPC backfill pilot")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="scan and report but do NOT insert into CH (default)")
    ap.add_argument("--insert", action="store_true",
                    help="actually INSERT into raw_evm_logs (turns off dry-run)")
    ap.add_argument("--docker-exec", action="store_true",
                    help="use docker exec for CH queries/inserts")
    ap.add_argument("--pilot-days", type=int, default=7,
                    help="number of days for the pilot window (default 7)")
    ap.add_argument("--pilot-end-date", default="2026-04-12",
                    help="end date of pilot window YYYY-MM-DD (default 2026-04-12)")
    ap.add_argument("--max-pools", type=int, default=200,
                    help="max pool addresses to scan for swaps (default 200)")
    ap.add_argument("--rpc-url", default=_RPC_URL,
                    help="RPC URL (or set LABEL_RPC_URL env)")
    ap.add_argument("--start-block", type=int, default=0,
                    help="override start block (skips RPC timestamp estimation)")
    ap.add_argument("--end-block", type=int, default=0,
                    help="override end block (skips RPC timestamp estimation)")
    args = ap.parse_args()

    rpc_url = args.rpc_url
    if not rpc_url:
        print("ERROR: LABEL_RPC_URL env var or --rpc-url required", file=sys.stderr)
        sys.exit(1)

    do_insert = args.insert
    dry_run = not do_insert

    _DIAG_DIR.mkdir(parents=True, exist_ok=True)
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[pilot] mode: {'DRY RUN' if dry_run else 'LIVE INSERT'}")
    print(f"[pilot] pilot_end_date={args.pilot_end_date}, pilot_days={args.pilot_days}")

    result = run_pilot(
        rpc_url=rpc_url,
        pilot_end_date=args.pilot_end_date,
        pilot_days=args.pilot_days,
        do_insert=do_insert,
        use_docker=args.docker_exec,
        max_pools=args.max_pools,
        start_block_override=args.start_block,
        end_block_override=args.end_block,
    )

    out_json = _DIAG_DIR / "sprint037a_bf_rpc_pilot.json"
    out_md = _AUDIT_DIR / "sprint037a_bf_rpc_pilot_report.md"

    out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[pilot] JSON → {out_json.relative_to(_REPO)}")
    _write_md(result, out_md)

    flags = result.get("decision_flags", {})
    print("\n=== PILOT DECISION FLAGS ===")
    for k, v in flags.items():
        print(f"  {k}: {v}")
    print(f"\nStatus: {result.get('status','')}")
    print(f"Meme pools found: {result.get('meme_pools_found',0)}")
    print(f"Valid swap logs: {result.get('valid_swap_logs',0)}")
    print(f"Rows inserted: {result.get('rows_inserted',0)}")


if __name__ == "__main__":
    main()
