"""
Run-004 Patch F1 Task C/D: targeted relabel of 823 duplicate-conflict keys.

DO NOT EXECUTE without explicit user confirmation — this consumes RPC.

Inputs:
  logs/run004_patch_f1_conflict_relabel_input.csv  (one row per conflict key)

Outputs:
  mvp/diagnostics/run004_patch_f1_canonical_ts_block_map.json   (persisted ts→block map)
  logs/run004_patch_f1_canonical_ts_block_map.csv
  logs/run004_patch_f1_conflict_relabel_output.csv               (streaming, one row per key)
  mvp/diagnostics/run004_patch_f1_conflict_relabel_report.json
  mvp/runs/run-004-patch-f1-conflict-relabel.md

Critical design:
  - ts→block map is built ONCE, written to disk, and reused on resume.
  - Resume reads the persisted map; refuses to rebuild unless RUN004_F1_REBUILD_TS_GRID=1.
  - One canonical ts-grid shared across all keys and all workers.
  - LABEL_WORKERS default 2, hard cap 4.
  - RPC calls counted per method (eth_getBlockByNumber, eth_call, eth_chainId, batch).
  - 429 → abort immediately, no infinite retry.
  - Every (tx_hash, log_index) processed at most once (deterministic, sorted order).
  - Streaming CSV write + flush after each row.
  - Only V2 pools relabeled (pool_type = v2 or aero_vamm).

Env:
  LABEL_RPC_URL or EVM_HTTP_URL   archive RPC (required unless DRY_RUN_ONLY)
  LABEL_WORKERS                   parallel workers (default 2, max 4)
  LABEL_EXIT_HORIZON_SEC          default 1800
  LABEL_NOTIONAL_WEI              default 500000000000000 (0.0005 ETH)
  CHAIN_ID                        default 8453
  CLICKHOUSE_URL, CLICKHOUSE_DB, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
  REDIS_URL
  RUN004_F1_RELABEL_INPUT         path to input CSV
  RUN004_F1_RELABEL_OUTPUT        path to output CSV
  RUN004_F1_RELABEL_JSON          path to JSON report
  RUN004_F1_RELABEL_MD            path to markdown report
  RUN004_F1_TS_MAP_JSON           path to persisted ts→block map JSON
  RUN004_F1_TS_MAP_CSV            path to persisted ts→block map CSV
  RUN004_F1_RELABEL_RESUME        1 = resume from existing output (default 1)
  RUN004_F1_REBUILD_TS_GRID       1 = force rebuild ts-grid even if map exists (default 0)
  RUN004_F1_RELABEL_DRY_RUN_ONLY  1 = validate input + load map, no RPC labels (default 0)
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import label_dashcam_executable as le
import validate_run004_executable_labels as vr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("relabel_f1")

OUT_FIELDS = [
    "tx_hash", "log_index", "pool_address", "token_address",
    "pool_type", "timestamp", "event_kind",
    "entry_block", "exit_block",
    "label", "net_return", "label_reason",
    "relabel_status",
    "rpc_calls_this_key",
]


# ─────────────────────────────────────────────────────────────────────
# RPC wrapper with per-method call counting and 429 abort
# ─────────────────────────────────────────────────────────────────────

class _CountingRPC(le.RPC):
    """Counts RPC calls per method; aborts on 429 with no infinite retry."""

    def __init__(self, url: str, timeout: int = 60):
        # max_retries=1: one attempt only — no silent infinite retry
        super().__init__(url, timeout=timeout, max_retries=1)
        self._method_lock = threading.Lock()
        self.method_counts: Counter[str] = Counter()
        self._aborted = threading.Event()

    def call(self, method: str, params: list) -> Any:
        if self._aborted.is_set():
            raise RuntimeError("HTTP_429_ABORT")
        with self._method_lock:
            self.method_counts[method] += 1
        try:
            return super().call(method, params)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                log.error("429 rate-limit on method=%s — aborting all workers", method)
                self._aborted.set()
                raise RuntimeError("HTTP_429_ABORT") from exc
            raise

    def batch(self, calls: list) -> list:
        if self._aborted.is_set():
            raise RuntimeError("HTTP_429_ABORT")
        with self._method_lock:
            self.method_counts["batch"] += 1
            for m, _ in calls:
                self.method_counts[f"batch:{m}"] += 1
        try:
            return super().batch(calls)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                log.error("429 rate-limit on batch — aborting all workers")
                self._aborted.set()
                raise RuntimeError("HTTP_429_ABORT") from exc
            raise

    @property
    def total_calls(self) -> int:
        with self._method_lock:
            return sum(v for k, v in self.method_counts.items() if not k.startswith("batch:"))


# ─────────────────────────────────────────────────────────────────────
# Canonical ts→block map: build once, persist, reuse on resume
# ─────────────────────────────────────────────────────────────────────

def _build_ts_grid_paced(
    rpc: _CountingRPC,
    ts_lo_sec: int,
    ts_hi_sec: int,
    step: int = 200,
    batch_size: int = 100,
    inter_batch_pause: float = 0.5,
    max_retries_per_batch: int = 3,
) -> tuple[list[int], list[int]]:
    """
    Rate-friendly ts-grid builder:
      - step=200 blocks (~400 s resolution; sufficient for 1800 s exit horizon)
      - batch_size=100 sub-requests per JSON-RPC batch (avoids 429 on paid endpoints)
      - pause between batches to respect rate limits
      - bounded retries (max_retries_per_batch) on 429 — NOT infinite retry
    Uses the RPC .call() for single requests and raw HTTP for batches to bypass
    the counting wrapper's abort-on-429 for batch; instead we handle 429 here
    with back-off, then abort if we exhaust retries.
    """
    from urllib.request import Request, urlopen
    import json as _json

    tip_resp = rpc.call("eth_getBlockByNumber", ["latest", False])
    chain_tip = int(tip_resp["result"]["number"], 16)
    chain_tip_ts_s = int(tip_resp["result"]["timestamp"], 16)

    blocks_back = max((chain_tip_ts_s - ts_lo_sec) // 2 + 2000, 0)
    lo_block = max(0, chain_tip - blocks_back)
    block_nums = list(range(lo_block, chain_tip + 1, step))

    log.info("ts-grid: scanning %d block numbers in batches of %d (step=%d)",
             len(block_nums), batch_size, step)

    results_map: dict[int, int] = {}
    t0 = time.time()

    for i in range(0, len(block_nums), batch_size):
        sub = block_nums[i: i + batch_size]
        payload = [
            {"jsonrpc": "2.0", "method": "eth_getBlockByNumber",
             "params": [hex(b), False], "id": j}
            for j, b in enumerate(sub)
        ]
        body = _json.dumps(payload).encode()

        attempt = 0
        while attempt < max_retries_per_batch:
            req = Request(rpc.url, data=body, method="POST")
            req.add_header("content-type", "application/json")
            with rpc._method_lock:
                rpc.method_counts["batch"] += 1
                rpc.method_counts["batch:eth_getBlockByNumber"] += len(sub)
            try:
                with urlopen(req, timeout=60) as resp:
                    out = _json.loads(resp.read())
                break
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == 429:
                    if attempt + 1 >= max_retries_per_batch:
                        log.error("429 on ts-grid batch after %d retries — aborting", max_retries_per_batch)
                        rpc._aborted.set()
                        raise RuntimeError("HTTP_429_ABORT") from exc
                    wait = min(2.0 * (2 ** attempt), 15.0)
                    log.warning("429 on ts-grid batch %d/%d — retry in %.1fs (attempt %d/%d)",
                                i // batch_size + 1, math.ceil(len(block_nums) / batch_size),
                                wait, attempt + 1, max_retries_per_batch)
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise
            attempt += 1

        idx = {r.get("id"): r for r in out}
        for j, bn in enumerate(sub):
            res = idx.get(j)
            if res and res.get("result"):
                results_map[bn] = int(res["result"]["timestamp"], 16)

        done = min(i + batch_size, len(block_nums))
        log.info("ts-grid: %d/%d sampled (elapsed %.1fs)", done, len(block_nums), time.time() - t0)
        if i + batch_size < len(block_nums):
            time.sleep(inter_batch_pause)

    sorted_bn = sorted(results_map.keys())
    sorted_ts = [results_map[b] for b in sorted_bn]
    log.info("ts-grid built: %d samples, span [%d … %d] (elapsed %.1fs)",
             len(sorted_bn), sorted_ts[0] if sorted_ts else -1,
             sorted_ts[-1] if sorted_ts else -1, time.time() - t0)
    return sorted_bn, sorted_ts


def _build_and_persist_ts_map(
    rpc: _CountingRPC,
    ts_lo_sec: int,
    ts_hi_sec: int,
    map_json_path: Path,
    map_csv_path: Path,
) -> tuple[list[int], list[int], dict[str, Any]]:
    """
    Build ts→block grid (paced, rate-limit-friendly), write to disk.
    Returns (bn_arr, ts_arr, metadata).
    """
    log.info("building canonical ts-grid: lo=%d hi=%d", ts_lo_sec, ts_hi_sec)

    bn_arr, ts_arr = _build_ts_grid_paced(rpc, ts_lo_sec, ts_hi_sec)

    # Read tip from the map itself (paced builder captures it internally)
    chain_tip_block = bn_arr[-1] if bn_arr else 0
    chain_tip_ts = ts_arr[-1] if ts_arr else 0

    metadata: dict[str, Any] = {
        "built_at_unix": int(time.time()),
        "chain_tip_block": chain_tip_block,
        "chain_tip_timestamp_sec": chain_tip_ts,
        "ts_lo_sec": ts_lo_sec,
        "ts_hi_sec": ts_hi_sec,
        "sample_count": len(bn_arr),
        "step_blocks": 200,
        "mapping_method": "bisect_linear_interpolation",
        "exit_horizon_sec": ts_hi_sec - max(ts_arr, default=0) if ts_arr else 0,
        "rpc_url_redacted": "<REDACTED_LABEL_RPC_URL>",
    }

    # Persist JSON
    map_json_path.parent.mkdir(parents=True, exist_ok=True)
    with map_json_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "metadata": metadata,
                "bn_arr": bn_arr,
                "ts_arr": ts_arr,
            },
            f,
            indent=2,
        )

    # Persist CSV (for inspection)
    map_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with map_csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["block_number", "timestamp_sec"])
        w.writeheader()
        for bn, ts in zip(bn_arr, ts_arr):
            w.writerow({"block_number": bn, "timestamp_sec": ts})

    log.info(
        "ts-grid persisted: %d samples, chain_tip_block=%d",
        len(bn_arr), chain_tip_block,
    )
    return bn_arr, ts_arr, metadata


def _load_ts_map(map_json_path: Path) -> tuple[list[int], list[int], dict[str, Any]]:
    """Load persisted canonical ts-grid from disk."""
    with map_json_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    bn_arr = [int(x) for x in payload["bn_arr"]]
    ts_arr = [int(x) for x in payload["ts_arr"]]
    metadata = payload.get("metadata", {})
    log.info(
        "loaded canonical ts-grid from %s: %d samples, chain_tip_block=%s",
        map_json_path.name,
        len(bn_arr),
        metadata.get("chain_tip_block", "?"),
    )
    return bn_arr, ts_arr, metadata


# ─────────────────────────────────────────────────────────────────────
# Per-key labeling
# ─────────────────────────────────────────────────────────────────────

def _label_one_key(
    row: dict[str, str],
    rpc: _CountingRPC,
    meta: dict[str, dict],
    bn_arr: list[int],
    ts_arr: list[int],
    exit_horizon_sec: int,
    notional_wei: int,
) -> dict[str, Any]:
    pool = (row.get("pool_address") or "").lower()
    ts_ms = int(row.get("timestamp", 0) or 0)
    ts_sec = ts_ms // 1000

    pm = meta.get(pool, {})
    if not pm:
        return {
            "tx_hash": row.get("tx_hash", ""),
            "log_index": row.get("log_index", ""),
            "pool_address": pool,
            "token_address": (row.get("token_address") or "").lower(),
            "pool_type": row.get("pool_type", ""),
            "timestamp": row.get("timestamp", ""),
            "event_kind": row.get("event_kind", ""),
            "entry_block": "",
            "exit_block": "",
            "label": "",
            "net_return": "",
            "label_reason": "fail_no_meta",
            "relabel_status": "fail_no_meta",
            "rpc_calls_this_key": 0,
        }

    before = rpc.total_calls

    entry_block = le.ts_to_block(bn_arr, ts_arr, ts_sec)
    exit_block = le.ts_to_block(bn_arr, ts_arr, ts_sec + exit_horizon_sec)

    pool_type = pm.get("pool_type", "v2")
    if pool_type in ("v2", "aero_vamm"):
        label, net, reason = le.label_v2(
            rpc, pool, le.WETH_BASE, notional_wei, entry_block, exit_block
        )
    else:
        label, net, reason = -1, 0.0, "fail_not_v2"

    calls_this = rpc.total_calls - before

    return {
        "tx_hash": row.get("tx_hash", ""),
        "log_index": row.get("log_index", ""),
        "pool_address": pool,
        "token_address": (row.get("token_address") or "").lower(),
        "pool_type": pool_type,
        "timestamp": row.get("timestamp", ""),
        "event_kind": row.get("event_kind", ""),
        "entry_block": entry_block,
        "exit_block": exit_block,
        "label": "" if label < 0 else str(label),
        "net_return": "" if label < 0 else str(round(net, 12)),
        "label_reason": reason,
        "relabel_status": "new",
        "rpc_calls_this_key": calls_this,
    }


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    input_csv = Path(os.environ.get(
        "RUN004_F1_RELABEL_INPUT",
        str(REPO / "logs" / "run004_patch_f1_conflict_relabel_input.csv"),
    ))
    output_csv = Path(os.environ.get(
        "RUN004_F1_RELABEL_OUTPUT",
        str(REPO / "logs" / "run004_patch_f1_conflict_relabel_output.csv"),
    ))
    out_json = Path(os.environ.get(
        "RUN004_F1_RELABEL_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f1_conflict_relabel_report.json"),
    ))
    out_md = Path(os.environ.get(
        "RUN004_F1_RELABEL_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-f1-conflict-relabel.md"),
    ))
    map_json = Path(os.environ.get(
        "RUN004_F1_TS_MAP_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f1_canonical_ts_block_map.json"),
    ))
    map_csv = Path(os.environ.get(
        "RUN004_F1_TS_MAP_CSV",
        str(REPO / "logs" / "run004_patch_f1_canonical_ts_block_map.csv"),
    ))

    resume = (os.environ.get("RUN004_F1_RELABEL_RESUME", "1") or "1").lower() in ("1", "true", "yes", "on")
    force_rebuild_grid = (os.environ.get("RUN004_F1_REBUILD_TS_GRID") or "").lower() in ("1", "true", "yes", "on")
    dry_only = (os.environ.get("RUN004_F1_RELABEL_DRY_RUN_ONLY") or "").lower() in ("1", "true", "yes", "on")
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    workers = min(int(os.environ.get("LABEL_WORKERS", "2") or 2), 4)
    exit_horizon = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_u = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    redis_url = (os.environ.get("REDIS_URL") or "").strip()

    if not input_csv.is_file():
        log.error("missing input: %s", input_csv)
        return 1

    with input_csv.open("r", encoding="utf-8") as f:
        input_rows = list(csv.DictReader(f))

    if not input_rows:
        log.error("empty input CSV")
        return 1

    # Deterministic sort order
    input_rows.sort(key=lambda r: (r.get("tx_hash", ""), r.get("log_index", "")))
    log.info("input_keys=%d", len(input_rows))

    # Resume: load already-written output keys
    done_keys: set[tuple[str, str]] = set()
    if resume and output_csv.is_file() and output_csv.stat().st_size > 0:
        with output_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_keys.add((row.get("tx_hash", ""), row.get("log_index", "")))
        log.info("resume: %d keys already done", len(done_keys))

    pending = [r for r in input_rows if (r.get("tx_hash", ""), r.get("log_index", "")) not in done_keys]
    log.info("pending_keys=%d", len(pending))

    if dry_only:
        log.info("DRY_RUN_ONLY=1: skipping all RPC")
        return 0

    if not rpc_url:
        log.error("LABEL_RPC_URL or EVM_HTTP_URL required")
        return 1

    if not pending:
        log.info("all keys already done, resume skipped all — writing report only")
        _write_report_from_existing(output_csv, out_json, out_md, map_json, done_keys)
        return 0

    rpc = _CountingRPC(rpc_url, timeout=60)

    # Verify chain ID
    try:
        r = rpc.call("eth_chainId", [])
        chid = int(r["result"], 16)
        if chid != chain_id:
            log.error("chain_id mismatch: got %d expected %d", chid, chain_id)
            return 1
        log.info("RPC ok chain_id=%d", chid)
    except RuntimeError as exc:
        if "HTTP_429_ABORT" in str(exc):
            log.error("429 on chain_id check — aborting")
            return 1
        raise

    # ── Canonical ts→block map: load or build ──
    ts_map_meta: dict[str, Any] = {}
    if not force_rebuild_grid and map_json.is_file():
        log.info("loading existing canonical ts-grid from %s", map_json)
        bn_arr, ts_arr, ts_map_meta = _load_ts_map(map_json)
    else:
        if map_json.is_file() and not force_rebuild_grid:
            log.warning("ts-grid exists but force_rebuild_grid=False — using existing map")
            bn_arr, ts_arr, ts_map_meta = _load_ts_map(map_json)
        else:
            tss_sec = [int(r.get("timestamp", 0) or 0) // 1000 for r in pending
                       if int(r.get("timestamp", 0) or 0) > 0]
            if not tss_sec:
                log.error("no valid timestamps in pending input")
                return 1
            ts_lo = min(tss_sec)
            ts_hi = max(tss_sec) + exit_horizon
            try:
                bn_arr, ts_arr, ts_map_meta = _build_and_persist_ts_map(
                    rpc, ts_lo, ts_hi, map_json, map_csv,
                )
            except RuntimeError as exc:
                if "HTTP_429_ABORT" in str(exc):
                    log.error("429 on ts-grid build — aborting")
                    return 1
                raise

    log.info("ts-grid ready: %d samples, rpc_calls_so_far=%d", len(bn_arr), rpc.total_calls)

    # Load pool metadata (ClickHouse + Redis, cached per pool)
    pools = {(r.get("pool_address") or "").lower() for r in pending if r.get("pool_address")}
    le.TOKEN0_CACHE.clear()
    try:
        chm = vr.load_pool_meta_full(ch_url, ch_db, ch_u, ch_pw, chain_id, pools)
        rm = vr.load_pool_meta_redis(redis_url, pools)
        meta = vr.merge_meta(chm, rm)
    except Exception as exc:
        log.warning("pool meta load failed (%s) — continuing with empty meta", exc)
        meta = {}
    log.info("pool_meta: %d/%d pools found", len(meta), len(pools))

    # ── Streaming output ──
    append_mode = bool(done_keys)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    aborted = False
    success_count = 0
    fail_counter: Counter[str] = Counter()

    t0 = time.time()

    with output_csv.open("a" if append_mode else "w", encoding="utf-8", newline="") as csv_f:
        csv_w = csv.DictWriter(csv_f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        if not append_mode:
            csv_w.writeheader()

        def _run_key(row: dict[str, str]) -> dict[str, Any]:
            if rpc._aborted.is_set():
                raise RuntimeError("HTTP_429_ABORT")
            return _label_one_key(row, rpc, meta, bn_arr, ts_arr, exit_horizon, notional_wei)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            fs: dict[Future, dict[str, str]] = {
                pool.submit(_run_key, row): row for row in pending
            }
            processed = 0
            for fut in as_completed(fs):
                try:
                    result = fut.result()
                except RuntimeError as exc:
                    if "HTTP_429_ABORT" in str(exc):
                        log.error("429 abort received — shutting down workers")
                        pool.shutdown(wait=False, cancel_futures=True)
                        aborted = True
                        break
                    row = fs[fut]
                    result = {
                        "tx_hash": row.get("tx_hash", ""),
                        "log_index": row.get("log_index", ""),
                        "pool_address": (row.get("pool_address") or "").lower(),
                        "label": "",
                        "net_return": "",
                        "label_reason": f"fail_exception:{str(exc)[:60]}",
                        "relabel_status": "new",
                        "rpc_calls_this_key": 0,
                    }
                    fail_counter["exception"] += 1

                reason = result.get("label_reason", "")
                lbl = result.get("label", "")
                if lbl in ("0", "1"):
                    success_count += 1
                else:
                    fail_counter[reason or "unknown"] += 1

                csv_w.writerow(result)
                csv_f.flush()
                results.append(result)
                processed += 1

                if processed % 50 == 0 or processed == len(pending):
                    elapsed = time.time() - t0
                    log.info(
                        "progress: %d/%d done, success=%d, rpc_calls=%d, elapsed=%.0fs",
                        processed, len(pending), success_count, rpc.total_calls, elapsed,
                    )

    elapsed_total = time.time() - t0
    _write_report(
        output_csv=output_csv,
        out_json=out_json,
        out_md=out_md,
        ts_map_meta=ts_map_meta,
        results=results,
        pending_count=len(pending),
        done_keys_count=len(done_keys),
        success_count=success_count,
        fail_counter=fail_counter,
        rpc=rpc,
        aborted=aborted,
        elapsed_sec=elapsed_total,
    )

    log.info(
        "DONE: pending=%d success=%d rpc_calls=%d aborted=%s elapsed=%.0fs",
        len(pending), success_count, rpc.total_calls, aborted, elapsed_total,
    )
    return 0 if not aborted else 2


def _write_report_from_existing(
    output_csv: Path,
    out_json: Path,
    out_md: Path,
    map_json: Path,
    done_keys: set,
) -> None:
    ts_map_meta: dict[str, Any] = {}
    if map_json.is_file():
        _, _, ts_map_meta = _load_ts_map(map_json)

    results: list[dict[str, str]] = []
    if output_csv.is_file():
        with output_csv.open("r", encoding="utf-8") as f:
            results = list(csv.DictReader(f))

    success_count = sum(1 for r in results if r.get("label") in ("0", "1"))
    fail_counter: Counter[str] = Counter(
        r.get("label_reason", "unknown")
        for r in results if r.get("label", "") not in ("0", "1")
    )
    _write_report(
        output_csv=output_csv,
        out_json=out_json,
        out_md=out_md,
        ts_map_meta=ts_map_meta,
        results=results,
        pending_count=0,
        done_keys_count=len(done_keys),
        success_count=success_count,
        fail_counter=fail_counter,
        rpc=None,
        aborted=False,
        elapsed_sec=0.0,
    )


def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    k = (n - 1) * q
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


def _write_report(
    output_csv: Path,
    out_json: Path,
    out_md: Path,
    ts_map_meta: dict[str, Any],
    results: list[dict],
    pending_count: int,
    done_keys_count: int,
    success_count: int,
    fail_counter: Counter[str],
    rpc: "_CountingRPC | None",
    aborted: bool,
    elapsed_sec: float,
) -> None:
    pos = sum(1 for r in results if r.get("label") == "1")
    neg = sum(1 for r in results if r.get("label") == "0")
    pos_rate = pos / max(1, success_count)
    nets = sorted(
        float(r["net_return"]) for r in results
        if r.get("label") in ("0", "1") and r.get("net_return")
    )
    total_rpc = rpc.total_calls if rpc else 0
    method_counts = dict(rpc.method_counts) if rpc else {}

    report: dict[str, Any] = {
        "status": "ABORTED_429" if aborted else "COMPLETE",
        "keys_input": pending_count + done_keys_count,
        "keys_resumed_skipped": done_keys_count,
        "keys_attempted": pending_count,
        "keys_success": success_count,
        "keys_fail": pending_count - success_count,
        "success_rate": round(success_count / max(1, pending_count), 6),
        "positive_count": pos,
        "negative_count": neg,
        "positive_rate": round(pos_rate, 6),
        "fail_by_reason": dict(fail_counter),
        "rpc_call_counts": {
            "total": total_rpc,
            "by_method": method_counts,
        },
        "estimated_rpc_calls_per_success": round(total_rpc / max(1, success_count), 2),
        "elapsed_sec": round(elapsed_sec, 1),
        "net_return_distribution": {
            "min": round(nets[0], 10) if nets else None,
            "p50": round(_pct(nets, 0.5), 10) if nets else None,
            "p95": round(_pct(nets, 0.95), 10) if nets else None,
            "max": round(nets[-1], 10) if nets else None,
        } if nets else None,
        "aborted": aborted,
        "canonical_ts_block_map": {
            **ts_map_meta,
            "rpc_url_redacted": "<REDACTED_LABEL_RPC_URL>",
        },
        "paths": {
            "input_csv": "logs/run004_patch_f1_conflict_relabel_input.csv",
            "output_csv": str(output_csv.relative_to(REPO)) if output_csv.is_relative_to(REPO) else str(output_csv),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    by_method_rows = "\n".join(
        f"| `{k}` | {v} |" for k, v in sorted(method_counts.items()) if not k.startswith("batch:")
    )
    md = f"""# Run-004 Patch F1 — Targeted Conflict Relabel

**Status**: {report['status']}  
RPC endpoint: `<REDACTED_LABEL_RPC_URL>`

## Summary

| Metric | Value |
|--------|------:|
| Keys input | {report['keys_input']} |
| Keys resumed/skipped | {report['keys_resumed_skipped']} |
| Keys attempted | {report['keys_attempted']} |
| Keys success | {report['keys_success']} |
| Keys fail | {report['keys_fail']} |
| Success rate | {report['success_rate']} |
| Positive rate | {report['positive_rate']} |
| Total RPC calls | {report['rpc_call_counts']['total']} |
| RPC calls / success | {report['estimated_rpc_calls_per_success']} |
| Elapsed (sec) | {report['elapsed_sec']} |

## RPC calls by method

| Method | Calls |
|--------|------:|
{by_method_rows}

## Fail reasons

| Reason | Count |
|--------|------:|
{''.join(f"| `{k}` | {v} |{chr(10)}" for k, v in report['fail_by_reason'].items())}

## Canonical ts→block map

| Key | Value |
|-----|-------|
| chain_tip_block | {ts_map_meta.get('chain_tip_block', '—')} |
| chain_tip_timestamp_sec | {ts_map_meta.get('chain_tip_timestamp_sec', '—')} |
| sample_count | {ts_map_meta.get('sample_count', '—')} |
| step_blocks | {ts_map_meta.get('step_blocks', '—')} |
| mapping_method | {ts_map_meta.get('mapping_method', '—')} |
| ts_lo_sec | {ts_map_meta.get('ts_lo_sec', '—')} |
| ts_hi_sec | {ts_map_meta.get('ts_hi_sec', '—')} |
| rpc_url | `<REDACTED_LABEL_RPC_URL>` |
"""
    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)


if __name__ == "__main__":
    raise SystemExit(main())
