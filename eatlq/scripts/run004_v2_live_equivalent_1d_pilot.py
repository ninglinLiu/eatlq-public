"""Run-004 V2 live-equivalent 1-day pilot.

Research-only diagnostics. This script uses the completed UniswapV2Factory
PairCreated universe, scans global V2 Swap logs by topic over one historical
day, filters to canonical factory WETH pairs, resolves timestamp horizons, and
labels fixed-notional roundtrip returns with archive reserves.

It does not train, score, write production models, emit Redis signals, enter
H19/shadow/live, or fall back to the H1611 747-pool universe.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from bisect import bisect_left
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v2_live_equivalent_backtest import (  # noqa: E402
    DEFAULT_CONFIG,
    V2_SWAP_TOPIC,
    WETH_BASE,
    RPC,
    _decode_uint_words,
    _lower,
    block_timestamp,
    decode_v2_swap_log,
    first_block_at_or_after_timestamp,
    get_reserves,
    load_config,
    quote_v2_out,
    rpc_from_config,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

UNIVERSE_CSV = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe.csv"
UNIVERSE_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe_summary.json"
OUT_ROWS = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_1d_pilot_rows.csv"
OUT_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_1d_pilot_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v2-live-equivalent-1d-pilot.md"

MAX_UNCOMPRESSED_CSV_BYTES = 200 * 1024 * 1024


class PilotRPC(RPC):
    def get_logs_global(self, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        return self.call(
            "eth_getLogs",
            [{
                "topics": topics,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }],
        ) or []

    def batch_call(self, method: str, params_list: list[list[Any]]) -> list[Any]:
        if not params_list:
            return []
        batch = []
        for params in params_list:
            self._id += 1
            batch.append({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id})
        payload = json.dumps(batch).encode()
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self.calls[method] += len(params_list)
            time.sleep(self.sleep)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                if not isinstance(data, list):
                    self.errors[method] += len(params_list)
                    raise RuntimeError(str(data)[:240])
                by_id = {item.get("id"): item for item in data}
                out = []
                for request in batch:
                    item = by_id.get(request["id"], {})
                    if item.get("error"):
                        self.errors[method] += 1
                        out.append(None)
                    else:
                        out.append(item.get("result"))
                return out
            except Exception as exc:  # noqa: BLE001 - batch RPC retry
                last_err = exc
                self.errors[method] += len(params_list)
                time.sleep(min(self.backoff * (2**attempt), 8.0))
        raise RuntimeError(f"RPC batch {method} failed after {self.max_retries} retries: {last_err}")


def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def pilot_rpc_from_config(cfg: dict[str, Any]) -> PilotRPC:
    _load_env_file()
    url = ""
    for env_name in cfg["rpc"].get("env_vars", ["LABEL_RPC_URL", "EVM_HTTP_URL"]):
        url = os.environ.get(env_name, "").strip()
        if url:
            break
    if not url:
        raise RuntimeError("LABEL_RPC_URL or EVM_HTTP_URL is required")
    return PilotRPC(
        url,
        timeout=int(cfg["rpc"].get("timeout_sec", 60)),
        max_retries=int(cfg["rpc"].get("max_retries", 4)),
        backoff=float(cfg["rpc"].get("retry_backoff_sec", 0.5)),
        sleep=float(cfg["rpc"].get("rate_limit_sleep_sec", 0.03)),
    )


def load_universe() -> tuple[dict[str, dict[str, Any]], set[str], dict[str, Any]]:
    summary = json.loads(UNIVERSE_SUMMARY.read_text(encoding="utf-8"))
    if not summary.get("point_in_time_universe_pass"):
        raise RuntimeError("Factory universe summary point_in_time_universe_pass is not true")

    all_pairs: set[str] = set()
    weth_pairs: dict[str, dict[str, Any]] = {}
    with UNIVERSE_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pair = _lower(row["pair_address"])
            token0 = _lower(row["token0"])
            token1 = _lower(row["token1"])
            all_pairs.add(pair)
            if str(row.get("contains_weth", "")).strip().lower() == "true":
                weth_pairs[pair] = {
                    "pair_address": pair,
                    "token0": token0,
                    "token1": token1,
                    "creation_block": int(row["creation_block"]),
                    "creation_tx_hash": row["creation_tx_hash"],
                    "log_index": int(row["log_index"]),
                    "is_weth_pair": True,
                    "token_address": token1 if token0 == WETH_BASE else token0,
                }
    return weth_pairs, all_pairs, summary


def block_number(rpc: PilotRPC) -> int:
    return int(rpc.call("eth_blockNumber", []), 16)


def resolve_window(rpc: PilotRPC, universe_to_block: int) -> tuple[int, int, int, int]:
    from_env = os.environ.get("V2_1D_FROM_BLOCK", "").strip()
    to_env = os.environ.get("V2_1D_TO_BLOCK", "").strip()
    latest = block_number(rpc)
    to_block = int(to_env, 0) if to_env else min(latest, universe_to_block)
    from_block = int(from_env, 0) if from_env else max(0, to_block - 43_200)
    from_ts = block_timestamp(rpc, from_block)
    to_ts = block_timestamp(rpc, to_block)
    return from_block, to_block, from_ts, to_ts


def safe_get_logs(rpc: PilotRPC, from_block: int, to_block: int, min_chunk: int = 50) -> list[dict[str, Any]]:
    try:
        return rpc.get_logs_global([V2_SWAP_TOPIC], from_block, to_block)
    except Exception:
        if to_block - from_block + 1 <= min_chunk:
            raise
        mid = (from_block + to_block) // 2
        return safe_get_logs(rpc, from_block, mid, min_chunk) + safe_get_logs(rpc, mid + 1, to_block, min_chunk)


def scan_swap_logs(
    rpc: PilotRPC,
    from_block: int,
    to_block: int,
    all_pairs: set[str],
    weth_pairs: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter]:
    chunk_size = int(os.environ.get("V2_1D_LOG_CHUNK", "1000"))
    counters: Counter = Counter()
    rows: list[dict[str, Any]] = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk_size - 1, to_block)
        logs = safe_get_logs(rpc, cur, end)
        counters["raw_swap_logs"] += len(logs)
        for log in logs:
            addr = _lower(log.get("address"))
            if addr not in all_pairs:
                counters["non_factory_swap_logs"] += 1
                continue
            counters["canonical_v2_swap_logs"] += 1
            if addr not in weth_pairs:
                counters["non_weth_pair_logs"] += 1
                continue
            counters["weth_pair_logs"] += 1
            meta = weth_pairs[addr]
            rows.append({
                "pool_address": addr,
                "token0": meta["token0"],
                "token1": meta["token1"],
                "token_address": meta["token_address"],
                "creation_block": meta["creation_block"],
                "tx_hash": log.get("transactionHash", ""),
                "log_index": int(str(log.get("logIndex", "0x0")), 16),
                "entry_block": int(str(log.get("blockNumber", "0x0")), 16),
                "data": log.get("data", "0x"),
            })
        print(f"[1d_pilot] logs {cur:,}-{end:,}: raw={len(logs)} canonical_weth_rows={len(rows)}")
        cur = end + 1
    return rows, counters


def quote_no_fee(reserve_in: int, reserve_out: int, amount_in: int) -> int:
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0
    return (amount_in * reserve_out) // (reserve_in + amount_in)


def reserve_orientation(row: dict[str, Any], reserves: tuple[int, int]) -> tuple[int, int]:
    r0, r1 = reserves
    if _lower(row["token0"]) == WETH_BASE:
        return r0, r1
    return r1, r0


def batch_block_timestamps(rpc: PilotRPC, from_block: int, to_block: int, chunk_size: int = 50) -> dict[int, int]:
    out: dict[int, int] = {}
    blocks = list(range(from_block, to_block + 1))
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i : i + chunk_size]
        results = rpc.batch_call("eth_getBlockByNumber", [[hex(b), False] for b in chunk])
        for b, block in zip(chunk, results, strict=True):
            if not block:
                continue
            out[b] = int(block["timestamp"], 16)
        print(f"[1d_pilot] block timestamps {chunk[0]:,}-{chunk[-1]:,}")
    return out


def threaded_single_reserves(
    rpc: PilotRPC,
    requests_list: list[tuple[str, int]],
    workers: int = 16,
) -> dict[tuple[str, int], tuple[int, int] | None]:
    """Issue one eth_call per (pool, block) via a thread pool with HTTP keep-alive.

    The configured endpoint silently drops items inside JSON-RPC batches at
    batch_size>=5; rpc_probe shows batch_size=1 is the only reliable path. This
    helper uses requests.Session + HTTPAdapter with a generous connection pool
    so worker threads reuse persistent HTTPS connections (avoids the FD /
    CLOSE-WAIT exhaustion seen with raw urllib.request).

    None means RPC failure (transport error, item-level RPC error, empty hex,
    decode failure); (0, 0) means the contract returned a zero pair literally.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    import requests
    from requests.adapters import HTTPAdapter

    unique = sorted(set(requests_list), key=lambda x: (x[1], x[0]))
    out: dict[tuple[str, int], tuple[int, int] | None] = {}
    if not unique:
        return out

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=workers, pool_maxsize=workers, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    id_lock = threading.Lock()

    def next_id() -> int:
        nonlocal_holder = next_id
        with id_lock:
            rpc._id += 1
            return rpc._id
        return nonlocal_holder  # unreachable, satisfies type checkers

    def fetch(args: tuple[str, int]) -> tuple[tuple[str, int], tuple[int, int] | None]:
        pool, block = args
        rpc_id = next_id()
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "eth_call",
            "params": [{"to": pool, "data": "0x0902f1ac"}, hex(block)],
        }
        last_err: Exception | None = None
        for attempt in range(rpc.max_retries):
            rpc.calls["eth_call"] += 1
            try:
                resp = session.post(rpc.url, json=body, timeout=rpc.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    rpc.errors["eth_call"] += 1
                    return args, None
                raw = data.get("result")
                if raw is None or raw == "0x":
                    return args, None
                vals = _decode_uint_words(raw, 2)
                return args, (vals[0], vals[1])
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                last_err = exc
                rpc.errors["eth_call"] += 1
                time.sleep(min(rpc.backoff * (2**attempt), 4.0))
        return args, None

    completed = 0
    total = len(unique)
    last_print_time = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(fetch, key) for key in unique]
            for fut in as_completed(futures):
                key, val = fut.result()
                out[key] = val
                completed += 1
                now = time.monotonic()
                if completed % 2_000 == 0 or completed == total or (now - last_print_time) > 30.0:
                    print(f"[reserves_threaded] {completed:,}/{total:,}", flush=True)
                    last_print_time = now
    finally:
        session.close()
    return out


def batch_reserves(
    rpc: PilotRPC,
    requests: list[tuple[str, int]],
    chunk_size: int = 50,
) -> dict[tuple[str, int], tuple[int, int] | None]:
    unique = sorted(set(requests), key=lambda x: (x[1], x[0]))
    out: dict[tuple[str, int], tuple[int, int] | None] = {}
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        if chunk_size == 1:
            pool, block = chunk[0]
            try:
                raw = rpc.eth_call(pool, "0x0902f1ac", block)
                if raw is None or raw == "0x":
                    out[(pool, block)] = None
                else:
                    vals = _decode_uint_words(raw, 2)
                    out[(pool, block)] = (vals[0], vals[1])
            except Exception:
                out[(pool, block)] = None
        else:
            params = [[{"to": pool, "data": "0x0902f1ac"}, hex(block)] for pool, block in chunk]
            results = rpc.batch_call("eth_call", params)
            for key, raw in zip(chunk, results, strict=True):
                if raw is None or raw == "0x":
                    out[key] = None
                    continue
                try:
                    vals = _decode_uint_words(raw, 2)
                    out[key] = (vals[0], vals[1])
                except Exception:
                    out[key] = None
        if (i + len(chunk)) % 500 == 0 or i + len(chunk) == len(unique):
            print(f"[1d_pilot] reserve calls {i + len(chunk):,}/{len(unique):,}")
    return out


def deterministic_timestamp_grid(
    rpc: PilotRPC,
    from_block: int,
    to_block: int,
    from_ts: int,
    to_ts: int,
    timestamp_from: int,
    timestamp_to: int,
) -> tuple[dict[int, int], dict[str, Any]]:
    block_span = to_block - from_block
    ts_span = to_ts - from_ts
    if block_span <= 0 or ts_span % block_span != 0:
        raise RuntimeError("Cannot use deterministic timestamp grid: non-integer seconds per block")
    seconds_per_block = ts_span // block_span
    sample_blocks = sorted(set(
        [from_block, to_block, max(1, from_block - 1), timestamp_to]
        + list(range(from_block, to_block + 1, 1000))
    ))
    sample_results = []
    for b in sample_blocks:
        actual = block_timestamp(rpc, b)
        expected = from_ts + (b - from_block) * seconds_per_block
        sample_results.append({"block": b, "expected": expected, "actual": actual, "match": expected == actual})
    if not all(item["match"] for item in sample_results):
        raise RuntimeError(f"Deterministic timestamp grid validation failed: {sample_results[:5]}")
    out = {b: from_ts + (b - from_block) * seconds_per_block for b in range(timestamp_from, timestamp_to + 1)}
    meta = {
        "method": "deterministic_base_timestamp_grid_validated_by_samples",
        "seconds_per_block": seconds_per_block,
        "sample_n": len(sample_results),
        "sample_failures": [x for x in sample_results if not x["match"]],
    }
    return out, meta


def compute_label(rpc: PilotRPC, cfg: dict[str, Any], row: dict[str, Any], block_ts_cache: dict[int, int]) -> dict[str, Any]:
    notional = int(cfg["execution"].get("notional_wei", 500000000000000))
    round_trip_cost = float(cfg["execution"].get("round_trip_cost", 0.006))
    slippage_bps = float(cfg["execution"].get("slippage_stress_bps", 100))
    gas_cfg = cfg["execution"].get("gas", {})
    gas_units = int(gas_cfg.get("buy_units", 180000)) + int(gas_cfg.get("sell_units", 160000)) + int(gas_cfg.get("approval_units", 50000))
    gas_price_wei = int(float(gas_cfg.get("gas_price_gwei_medium", 0.03)) * 1_000_000_000)
    gas_cost_return = (gas_units * gas_price_wei) / notional
    entry_block = int(row["entry_block"])
    entry_quote_block = max(1, entry_block - 1)

    if entry_block not in block_ts_cache:
        block_ts_cache[entry_block] = block_timestamp(rpc, entry_block)
    entry_ts = block_ts_cache[entry_block]
    exit_target_ts = entry_ts + int(cfg["horizon"].get("exit_horizon_sec", 1800))
    exit_block, exit_ts, exit_err = first_block_at_or_after_timestamp(rpc, exit_target_ts, entry_block, entry_block + 3600)

    base = {
        "entry_timestamp": entry_ts,
        "exit_target_timestamp": exit_target_ts,
        "exit_block": exit_block,
        "exit_timestamp": exit_ts,
        "exit_block_resolution_method": "binary_search_first_block_ts_gte_target",
        "exit_block_resolution_error_seconds": exit_err,
        "entry_quote_block": entry_quote_block,
        "entry_quote_success": False,
        "exit_quote_success": False,
        "failure_reason": "",
        "risk_flag": "",
        "sanity_flag": "",
    }
    try:
        entry_res = get_reserves(rpc, row["pool_address"], entry_quote_block)
        r_weth_e, r_tok_e = reserve_orientation(row, entry_res)
        if r_weth_e <= 0 or r_tok_e <= 0:
            return {**base, "failure_reason": "entry_zero_reserve"}
        tokens_no_fee = quote_no_fee(r_weth_e, r_tok_e, notional)
        tokens_fee = quote_v2_out(r_weth_e, r_tok_e, notional)
        if tokens_fee <= 0:
            return {**base, "failure_reason": "entry_quote_zero"}
        base["entry_quote_success"] = True

        exit_res = get_reserves(rpc, row["pool_address"], exit_block)
        r_weth_x, r_tok_x = reserve_orientation(row, exit_res)
        if r_weth_x <= 0 or r_tok_x <= 0:
            return {**base, "entry_quote_success": True, "failure_reason": "exit_zero_reserve_or_pool_drained"}
        weth_back_no_fee = quote_no_fee(r_tok_x, r_weth_x, tokens_no_fee)
        weth_back_fee = quote_v2_out(r_tok_x, r_weth_x, tokens_fee)
        if weth_back_fee <= 0:
            return {**base, "entry_quote_success": True, "failure_reason": "exit_quote_zero"}
        base["exit_quote_success"] = True

        gross_return = weth_back_no_fee / notional - 1.0
        fee_adjusted_return = weth_back_fee / notional - 1.0
        gas_adjusted_return = fee_adjusted_return - gas_cost_return - round_trip_cost
        net_return = gas_adjusted_return - (slippage_bps / 10000.0)
        risk_flag = ""
        sanity_flag = "ok"
        if net_return > float(cfg["execution"]["sanity"].get("max_abs_net_return", 10.0)) or net_return < -1.0:
            risk_flag = "extreme_return_quarantine"
            sanity_flag = "extreme_return"
        if abs(net_return) > float(cfg["execution"]["sanity"].get("quarantine_abs_net_return_gt", 1.0)):
            risk_flag = risk_flag or "abs_return_gt_quarantine_threshold"
        return {
            **base,
            "gross_return": gross_return,
            "fee_adjusted_return": fee_adjusted_return,
            "gas_adjusted_return": gas_adjusted_return,
            "net_return": net_return,
            "gas_cost_return": gas_cost_return,
            "slippage_stress_return_cost": slippage_bps / 10000.0,
            "entry_amount_out": tokens_fee,
            "exit_weth_back": weth_back_fee,
            "risk_flag": risk_flag,
            "sanity_flag": sanity_flag,
            "failure_reason": "",
        }
    except Exception as exc:
        return {**base, "failure_reason": f"rpc_or_label_exception:{str(exc)[:120]}"}


def output_paths_with_compression() -> tuple[Path, bool]:
    size = OUT_ROWS.stat().st_size if OUT_ROWS.exists() else 0
    if size <= MAX_UNCOMPRESSED_CSV_BYTES:
        return OUT_ROWS, False
    gz_path = OUT_ROWS.with_suffix(OUT_ROWS.suffix + ".gz")
    with OUT_ROWS.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        dst.writelines(src)
    OUT_ROWS.unlink()
    return gz_path, True


def main() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    rpc = pilot_rpc_from_config(cfg)
    universe_summary = json.loads(UNIVERSE_SUMMARY.read_text(encoding="utf-8"))
    weth_pairs, all_pairs, _ = load_universe()

    from_block, to_block, from_ts, to_ts = resolve_window(rpc, int(universe_summary["to_block"]))
    rows, scan_counts = scan_swap_logs(rpc, from_block, to_block, all_pairs, weth_pairs)

    OUT_ROWS.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "pool_address", "token_address", "token0", "token1", "tx_hash", "log_index",
        "entry_block", "entry_timestamp", "exit_target_timestamp", "exit_block", "exit_timestamp",
        "exit_block_resolution_method", "exit_block_resolution_error_seconds",
        "observed_direction", "label_action", "amount0_in", "amount1_in", "amount0_out", "amount1_out",
        "entry_quote_block", "entry_quote_success", "exit_quote_success",
        "entry_amount_out", "exit_weth_back",
        "gross_return", "fee_adjusted_return", "gas_adjusted_return", "net_return",
        "gas_cost_return", "slippage_stress_return_cost", "risk_flag", "sanity_flag", "failure_reason",
    ]
    counters: Counter = Counter()
    pool_counts: Counter = Counter()
    token_counts: Counter = Counter()
    failure_reasons: Counter = Counter()
    duplicate_keys: dict[tuple[str, int, str], dict[str, Any]] = {}
    duplicate_conflict_count = 0

    decoded_inputs: list[dict[str, Any]] = []
    unknown_outputs: list[dict[str, Any]] = []
    for row in rows:
        decoded = decode_v2_swap_log({"data": row["data"]}, row["token0"], row["token1"], WETH_BASE)
        base = {
            **{k: row.get(k, "") for k in ["pool_address", "token_address", "token0", "token1", "tx_hash", "log_index", "entry_block"]},
            "observed_direction": decoded.direction,
            "label_action": "FIXED_NOTIONAL_BUY_WETH_TO_TOKEN_ROUNDTRIP",
            "amount0_in": decoded.amount0_in,
            "amount1_in": decoded.amount1_in,
            "amount0_out": decoded.amount0_out,
            "amount1_out": decoded.amount1_out,
        }
        if decoded.certainty != "certain":
            counters["unknown_direction_rows"] += 1
            failure_reasons[decoded.reason or "unknown_direction"] += 1
            unknown_outputs.append({**base, "failure_reason": decoded.reason or "unknown_direction", "risk_flag": "semantic_uncertain"})
            continue
        counters["decoded_rows"] += 1
        counters["attempted_label_rows"] += 1
        decoded_inputs.append({**row, **base})

    timestamp_from = max(1, from_block - 1)
    timestamp_to = to_block + 2_000
    block_ts, timestamp_mapping_meta = deterministic_timestamp_grid(
        rpc, from_block, to_block, from_ts, to_ts, timestamp_from, timestamp_to
    )
    ordered_blocks = sorted(block_ts)
    ordered_ts = [block_ts[b] for b in ordered_blocks]
    reserve_requests: list[tuple[str, int]] = []
    prepared: list[dict[str, Any]] = []
    horizon_sec = int(cfg["horizon"].get("exit_horizon_sec", 1800))
    for row in decoded_inputs:
        entry_block = int(row["entry_block"])
        entry_ts = block_ts.get(entry_block)
        if entry_ts is None:
            failure_reasons["entry_timestamp_missing"] += 1
            counters["failed_rows"] += 1
            prepared.append({**row, "failure_reason": "entry_timestamp_missing", "risk_flag": "timestamp_resolution_failed"})
            continue
        exit_target_ts = entry_ts + horizon_sec
        pos = bisect_left(ordered_ts, exit_target_ts)
        if pos >= len(ordered_blocks):
            failure_reasons["exit_block_missing"] += 1
            counters["failed_rows"] += 1
            prepared.append({
                **row,
                "entry_timestamp": entry_ts,
                "exit_target_timestamp": exit_target_ts,
                "failure_reason": "exit_block_missing",
                "risk_flag": "timestamp_resolution_failed",
            })
            continue
        exit_block = ordered_blocks[pos]
        exit_ts = ordered_ts[pos]
        entry_quote_block = max(1, entry_block - 1)
        prepared_row = {
            **row,
            "entry_timestamp": entry_ts,
            "exit_target_timestamp": exit_target_ts,
            "exit_block": exit_block,
            "exit_timestamp": exit_ts,
            "exit_block_resolution_method": timestamp_mapping_meta["method"],
            "exit_block_resolution_error_seconds": exit_ts - exit_target_ts,
            "entry_quote_block": entry_quote_block,
        }
        prepared.append(prepared_row)
        reserve_requests.append((row["pool_address"], entry_quote_block))
        reserve_requests.append((row["pool_address"], exit_block))

    reserves = batch_reserves(rpc, reserve_requests)
    notional = int(cfg["execution"].get("notional_wei", 500000000000000))
    round_trip_cost = float(cfg["execution"].get("round_trip_cost", 0.006))
    slippage_bps = float(cfg["execution"].get("slippage_stress_bps", 100))
    gas_cfg = cfg["execution"].get("gas", {})
    gas_units = int(gas_cfg.get("buy_units", 180000)) + int(gas_cfg.get("sell_units", 160000)) + int(gas_cfg.get("approval_units", 50000))
    gas_price_wei = int(float(gas_cfg.get("gas_price_gwei_medium", 0.03)) * 1_000_000_000)
    gas_cost_return = (gas_units * gas_price_wei) / notional
    max_abs_net_return = float(cfg["execution"]["sanity"].get("max_abs_net_return", 10.0))
    quarantine_abs_return = float(cfg["execution"]["sanity"].get("quarantine_abs_net_return_gt", 1.0))

    with OUT_ROWS.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for out in unknown_outputs:
            writer.writerow({k: out.get(k, "") for k in fields})
        for row in prepared:
            if row.get("failure_reason"):
                writer.writerow({k: row.get(k, "") for k in fields})
                continue
            entry_res = reserves.get((row["pool_address"], int(row["entry_quote_block"])))
            exit_res = reserves.get((row["pool_address"], int(row["exit_block"])))
            out = {
                **row,
                "entry_quote_success": False,
                "exit_quote_success": False,
                "gross_return": "",
                "fee_adjusted_return": "",
                "gas_adjusted_return": "",
                "net_return": "",
                "gas_cost_return": gas_cost_return,
                "slippage_stress_return_cost": slippage_bps / 10000.0,
                "risk_flag": "",
                "sanity_flag": "",
                "failure_reason": "",
            }
            if not entry_res:
                out["failure_reason"] = "entry_reserve_rpc_failure"
            else:
                r_weth_e, r_tok_e = reserve_orientation(row, entry_res)
                if r_weth_e <= 0 or r_tok_e <= 0:
                    out["failure_reason"] = "entry_zero_reserve"
                else:
                    tokens_no_fee = quote_no_fee(r_weth_e, r_tok_e, notional)
                    tokens_fee = quote_v2_out(r_weth_e, r_tok_e, notional)
                    if tokens_fee <= 0:
                        out["failure_reason"] = "entry_quote_zero"
                    else:
                        out["entry_quote_success"] = True
                        out["entry_amount_out"] = tokens_fee
                        if not exit_res:
                            out["failure_reason"] = "exit_reserve_rpc_failure"
                        else:
                            r_weth_x, r_tok_x = reserve_orientation(row, exit_res)
                            if r_weth_x <= 0 or r_tok_x <= 0:
                                out["failure_reason"] = "exit_zero_reserve_or_pool_drained"
                            else:
                                weth_back_no_fee = quote_no_fee(r_tok_x, r_weth_x, tokens_no_fee)
                                weth_back_fee = quote_v2_out(r_tok_x, r_weth_x, tokens_fee)
                                if weth_back_fee <= 0:
                                    out["failure_reason"] = "exit_quote_zero"
                                else:
                                    out["exit_quote_success"] = True
                                    out["exit_weth_back"] = weth_back_fee
                                    gross_return = weth_back_no_fee / notional - 1.0
                                    fee_adjusted_return = weth_back_fee / notional - 1.0
                                    gas_adjusted_return = fee_adjusted_return - gas_cost_return - round_trip_cost
                                    net_return = gas_adjusted_return - (slippage_bps / 10000.0)
                                    out["gross_return"] = gross_return
                                    out["fee_adjusted_return"] = fee_adjusted_return
                                    out["gas_adjusted_return"] = gas_adjusted_return
                                    out["net_return"] = net_return
                                    out["sanity_flag"] = "ok"
                                    if net_return > max_abs_net_return or net_return < -1.0:
                                        out["risk_flag"] = "extreme_return_quarantine"
                                        out["sanity_flag"] = "extreme_return"
                                    if abs(net_return) > quarantine_abs_return:
                                        out["risk_flag"] = out["risk_flag"] or "abs_return_gt_quarantine_threshold"
            key = (row["tx_hash"], int(row["log_index"]), row["pool_address"])
            if key in duplicate_keys and duplicate_keys[key] != out:
                duplicate_conflict_count += 1
            duplicate_keys[key] = out
            if out.get("entry_quote_success"):
                counters["entry_quote_success"] += 1
            if out.get("exit_quote_success"):
                counters["exit_quote_success"] += 1
            if out.get("failure_reason") or out.get("risk_flag"):
                counters["failed_rows"] += 1
                failure_reasons[out.get("failure_reason") or out.get("risk_flag")] += 1
            else:
                counters["clean_rows"] += 1
                pool_counts[row["pool_address"]] += 1
                token_counts[row["token_address"]] += 1
            writer.writerow({k: out.get(k, "") for k in fields})

    output_rows_path, compressed = output_paths_with_compression()
    clean_rows = counters["clean_rows"]
    unique_pools = len(pool_counts)
    unique_tokens = len(token_counts)
    top1_pool_share = (pool_counts.most_common(1)[0][1] / clean_rows) if clean_rows and pool_counts else 0.0
    top5_pool_share = (sum(v for _, v in pool_counts.most_common(5)) / clean_rows) if clean_rows else 0.0
    top1_token_share = (token_counts.most_common(1)[0][1] / clean_rows) if clean_rows and token_counts else 0.0
    top5_token_share = (sum(v for _, v in token_counts.most_common(5)) / clean_rows) if clean_rows else 0.0
    entry_rate = counters["entry_quote_success"] / counters["attempted_label_rows"] if counters["attempted_label_rows"] else 0.0
    exit_rate = counters["exit_quote_success"] / counters["attempted_label_rows"] if counters["attempted_label_rows"] else 0.0
    entry_ts_rate = (
        (counters["attempted_label_rows"] - failure_reasons["entry_timestamp_missing"]) / counters["attempted_label_rows"]
        if counters["attempted_label_rows"]
        else 0.0
    )
    exit_block_rate = (
        (counters["attempted_label_rows"] - failure_reasons["entry_timestamp_missing"] - failure_reasons["exit_block_missing"])
        / counters["attempted_label_rows"]
        if counters["attempted_label_rows"]
        else 0.0
    )
    timestamp_passed = counters["attempted_label_rows"] > 0 and entry_ts_rate == 1.0 and exit_block_rate == 1.0
    pool_conc_risk = top5_pool_share > 0.50
    clean_gate = clean_rows >= 100

    sizes = {
        str(output_rows_path.relative_to(ROOT)): output_rows_path.stat().st_size,
    }
    decision = "PILOT_1D_COMPLETED"
    if not scan_counts["raw_swap_logs"]:
        decision = "FAILED_SWAP_LOG_SCAN"
    elif not timestamp_passed:
        decision = "FAILED_TIMESTAMP_HORIZON"
    elif not clean_gate:
        decision = "FAILED_DATA_INSUFFICIENT"
    elif duplicate_conflict_count:
        decision = "FAILED_SEMANTICS_UNCERTAIN"

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "patch": "run-004-v2-live-equivalent-1d-pilot",
        "sanity_only_not_deploy_evidence": True,
        "from_block": from_block,
        "to_block": to_block,
        "from_timestamp": from_ts,
        "to_timestamp": to_ts,
        "raw_swap_logs": scan_counts["raw_swap_logs"],
        "canonical_v2_swap_logs": scan_counts["canonical_v2_swap_logs"],
        "non_factory_swap_logs": scan_counts["non_factory_swap_logs"],
        "weth_pair_logs": scan_counts["weth_pair_logs"],
        "non_weth_pair_logs": scan_counts["non_weth_pair_logs"],
        "decoded_rows": counters["decoded_rows"],
        "unknown_direction_rows": counters["unknown_direction_rows"],
        "attempted_label_rows": counters["attempted_label_rows"],
        "clean_rows": clean_rows,
        "failed_rows": counters["failed_rows"] + counters["unknown_direction_rows"],
        "failure_reason_distribution": dict(failure_reasons.most_common()),
        "unique_pools": unique_pools,
        "unique_tokens": unique_tokens,
        "top1_pool_share": top1_pool_share,
        "top5_pool_share": top5_pool_share,
        "top1_token_share": top1_token_share,
        "top5_token_share": top5_token_share,
        "entry_quote_success_rate": entry_rate,
        "exit_quote_success_rate": exit_rate,
        "entry_timestamp_resolution_success_rate": entry_ts_rate,
        "exit_block_resolution_success_rate": exit_block_rate,
        "duplicate_conflict_count": duplicate_conflict_count,
        "same_block_primary_label_used": False,
        "fixed_block_primary_label_used": False,
        "rpc_calls": dict(rpc.calls),
        "rpc_errors": dict(rpc.errors),
        "output_file_sizes": sizes,
        "rows_output_compressed": compressed,
        "factory_universe": {
            "path": str(UNIVERSE_CSV.relative_to(ROOT)),
            "summary": str(UNIVERSE_SUMMARY.relative_to(ROOT)),
            "unique_pairs": universe_summary.get("unique_pairs"),
            "weth_pairs": universe_summary.get("weth_pairs"),
            "point_in_time_universe_pass": universe_summary.get("point_in_time_universe_pass"),
        },
        "decision_flags": {
            "factory_universe_loaded": True,
            "point_in_time_universe_passed": bool(universe_summary.get("point_in_time_universe_pass")),
            "pilot_1d_completed": decision == "PILOT_1D_COMPLETED",
            "timestamp_horizon_label_passed": timestamp_passed,
            "clean_rows_gate_passed": clean_gate,
            "pool_concentration_risk": pool_conc_risk,
            "candidate_selection_bias_risk": scan_counts["non_factory_swap_logs"] > 0 or scan_counts["non_weth_pair_logs"] > 0,
            "allow_7d_pilot": timestamp_passed and clean_gate and duplicate_conflict_count == 0,
            "allow_training": False,
            "allow_model_research": False,
            "allow_h19": False,
            "allow_shadow": False,
            "allow_live": False,
            "allow_promote": False,
        },
        "decision": decision,
        "timestamp_mapping": timestamp_mapping_meta,
    }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_markdown(summary, output_rows_path)
    print(json.dumps({
        "decision": decision,
        "clean_rows": clean_rows,
        "allow_7d_pilot": summary["decision_flags"]["allow_7d_pilot"],
        "summary": str(OUT_SUMMARY),
        "rows": str(output_rows_path),
        "markdown": str(OUT_MD),
    }, indent=2))


def write_markdown(summary: dict[str, Any], rows_path: Path) -> None:
    flags = summary["decision_flags"]
    md = f"""# Run-004 V2 Live-Equivalent 1D Pilot

**Status:** {summary['decision']}  
**Scope:** research-only / diagnostics-only; no training, no model research, no H19, no shadow, no live, no promote.

## Window

| Field | Value |
|-------|-------|
| from_block | {summary['from_block']:,} |
| to_block | {summary['to_block']:,} |
| from_timestamp | {summary['from_timestamp']} |
| to_timestamp | {summary['to_timestamp']} |

## Counts

| Metric | Value |
|--------|-------|
| raw_swap_logs | {summary['raw_swap_logs']:,} |
| canonical_v2_swap_logs | {summary['canonical_v2_swap_logs']:,} |
| non_factory_swap_logs | {summary['non_factory_swap_logs']:,} |
| weth_pair_logs | {summary['weth_pair_logs']:,} |
| non_weth_pair_logs | {summary['non_weth_pair_logs']:,} |
| decoded_rows | {summary['decoded_rows']:,} |
| unknown_direction_rows | {summary['unknown_direction_rows']:,} |
| attempted_label_rows | {summary['attempted_label_rows']:,} |
| clean_rows | **{summary['clean_rows']:,}** |
| failed_rows | {summary['failed_rows']:,} |

## Concentration

| Metric | Value |
|--------|-------|
| unique_pools | {summary['unique_pools']} |
| unique_tokens | {summary['unique_tokens']} |
| top1_pool_share | {summary['top1_pool_share']:.4f} |
| top5_pool_share | {summary['top5_pool_share']:.4f} |
| top1_token_share | {summary['top1_token_share']:.4f} |
| top5_token_share | {summary['top5_token_share']:.4f} |

Pool concentration risk: **{flags['pool_concentration_risk']}**.

## Quote / Horizon

| Metric | Value |
|--------|-------|
| entry_timestamp_resolution_success_rate | {summary['entry_timestamp_resolution_success_rate']:.4f} |
| exit_block_resolution_success_rate | {summary['exit_block_resolution_success_rate']:.4f} |
| entry_quote_success_rate | {summary['entry_quote_success_rate']:.4f} |
| exit_quote_success_rate | {summary['exit_quote_success_rate']:.4f} |
| duplicate_conflict_count | {summary['duplicate_conflict_count']} |
| same_block_primary_label_used | {summary['same_block_primary_label_used']} |
| fixed_block_primary_label_used | {summary['fixed_block_primary_label_used']} |

Failure reasons: `{summary['failure_reason_distribution']}`

## Decision Flags

| Flag | Value |
|------|-------|
| factory_universe_loaded | {flags['factory_universe_loaded']} |
| point_in_time_universe_passed | {flags['point_in_time_universe_passed']} |
| pilot_1d_completed | {flags['pilot_1d_completed']} |
| timestamp_horizon_label_passed | {flags['timestamp_horizon_label_passed']} |
| clean_rows_gate_passed | {flags['clean_rows_gate_passed']} |
| candidate_selection_bias_risk | {flags['candidate_selection_bias_risk']} |
| allow_7d_pilot | **{flags['allow_7d_pilot']}** |
| allow_training | {flags['allow_training']} |
| allow_model_research | {flags['allow_model_research']} |
| allow_h19 / shadow / live / promote | {flags['allow_h19']} / {flags['allow_shadow']} / {flags['allow_live']} / {flags['allow_promote']} |

## Required Answers

1. 1d pilot 是否跑通：**{flags['pilot_1d_completed']}**.
2. 真实 canonical V2 活跃度：raw `{summary['raw_swap_logs']:,}`, canonical `{summary['canonical_v2_swap_logs']:,}`, WETH `{summary['weth_pair_logs']:,}`.
3. clean rows：**{summary['clean_rows']:,}**.
4. 是否集中：top5 pool share `{summary['top5_pool_share']:.4f}`, top5 token share `{summary['top5_token_share']:.4f}`.
5. timestamp horizon label 是否稳定：entry quote `{summary['entry_quote_success_rate']:.4f}`, exit quote `{summary['exit_quote_success_rate']:.4f}`, same-block/fixed-block primary label both false.
6. candidate-selection bias 风险：**{flags['candidate_selection_bias_risk']}**.
7. 是否允许进入 7d pilot：**{flags['allow_7d_pilot']}**.
8. 是否仍禁止 training/model research/shadow/live：**True**.

## Artifacts

- `{OUT_SUMMARY.relative_to(ROOT)}`
- `{rows_path.relative_to(ROOT)}` ({summary['output_file_sizes'][str(rows_path.relative_to(ROOT))]} bytes)
- `{OUT_MD.relative_to(ROOT)}`
"""
    OUT_MD.write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()

