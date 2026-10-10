"""Backfill block_time for historical blocks whose block_time is NULL.

Strategy (aligned with "fix in place, no extra entities"):
  1. Source of truth: a single helper table `eatlq.block_ts_map` that maps
     (chain_id, block_number) -> block_time.  ReplacingMergeTree makes
     re-runs idempotent.
  2. For every distinct block_number referenced by raw_evm_logs /
     uniswap_v2_swaps / uniswap_v3_swaps with block_time IS NULL that is
     NOT already in block_ts_map, fetch the real on-chain timestamp from
     the configured archive RPC (QuickNode) via JSON-RPC batch requests.
  3. INSERT the (block_number, block_time) pairs into block_ts_map.
  4. Finally run ALTER TABLE ... UPDATE mutations on the base tables so
     downstream views (uniswap_v*_swaps_decoded + sender_* views in 016-018)
     observe correct block_time without needing to be rewritten.

Only stdlib is used so this runs from host or container without extra deps.

Env:
    LABEL_RPC_URL       Archive RPC endpoint (required)
    CHAIN_ID            default 8453 (Base)
    BATCH_SIZE          RPC sub-calls per HTTP request, default 40
    CONCURRENCY         parallel HTTP workers, default 2
    MAX_CPS             hard cap on eth_getBlockByNumber calls per second
                        (each batch sub-call counts as 1 call); default 45
    START_BLOCK         inclusive lower bound of fix window, default 0
    END_BLOCK           inclusive upper bound, default 10**18 (no cap)
    MAX_BLOCKS          optional cap for canary runs, default unlimited
    TARGET_TABLES       comma list in {raw_evm_logs, uniswap_v2_swaps,
                        uniswap_v3_swaps}, default all three
    RUN_MUTATIONS       "1" to execute ALTER TABLE UPDATE after fetch, "0"
                        to skip and leave the block_ts_map filled for
                        manual wire-up later.  Default "1".
    WAIT_FOR_MUTATIONS  "1" (default) to block until mutations complete.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple


_FREE_RPC_DEFAULT = "https://mainnet.base.org"
RPC_URL = (
    os.environ.get("LABEL_RPC_URL", "").strip()
    or os.environ.get("FREE_RPC_URL", "").strip()
    or _FREE_RPC_DEFAULT
)
CHAIN_ID = int(os.environ.get("CHAIN_ID", "8453"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "40"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "2"))
MAX_CPS = float(os.environ.get("MAX_CPS", "45"))
START_BLOCK = int(os.environ.get("START_BLOCK", "0"))
END_BLOCK = int(os.environ.get("END_BLOCK", str(10**18)))
MAX_BLOCKS = os.environ.get("MAX_BLOCKS")
MAX_BLOCKS = int(MAX_BLOCKS) if MAX_BLOCKS else None

TARGET_TABLES = [t.strip() for t in os.environ.get(
    "TARGET_TABLES",
    # raw_evm_logs intentionally excluded from defaults: its NULL rows are
    # overwhelmingly pre-meme early-Base blocks (block 0 -> 2M) that the
    # meme pipeline does not read.  Opt-in via env if needed.
    "uniswap_v2_swaps,uniswap_v3_swaps"
).split(",") if t.strip()]

RUN_MUTATIONS = os.environ.get("RUN_MUTATIONS", "1") == "1"
WAIT_FOR_MUTATIONS = os.environ.get("WAIT_FOR_MUTATIONS", "1") == "1"

CH_CONTAINER = os.environ.get("CH_CONTAINER", "eatlq-clickhouse-1")

PROGRESS_EVERY_SECONDS = 10.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ClickHouse helpers  (shell-out to clickhouse-client via docker exec)
# ---------------------------------------------------------------------------

def ch_query(sql: str, input_data: bytes | None = None, fmt: str = "TSV") -> str:
    args = [
        "docker", "exec", "-i", CH_CONTAINER, "clickhouse-client",
        "--query_plan_max_optimizations_to_apply=0",
        "--format", fmt,
        "--query", sql,
    ]
    r = subprocess.run(args, input=input_data, capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ClickHouse query failed (rc={r.returncode}):\n{r.stderr.decode()}\nSQL:\n{sql}")
    return r.stdout.decode()


def ch_insert_tsv(table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> int:
    """Bulk INSERT rows (any iterables) as TSV."""
    buf: list[str] = []
    n = 0
    for row in rows:
        buf.append("\t".join(str(v) for v in row))
        n += 1
    if not buf:
        return 0
    payload = ("\n".join(buf) + "\n").encode()
    sql = f"INSERT INTO {table} ({', '.join(columns)}) FORMAT TSV"
    ch_query(sql, input_data=payload, fmt="TSV")
    return n


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

@dataclass
class RpcError(Exception):
    status: int
    body: str

    def __str__(self) -> str:  # pragma: no cover
        return f"RpcError status={self.status} body={self.body[:200]}"


class TokenBucket:
    """Simple token bucket shared across worker threads.  ``acquire(n)``
    blocks until ``n`` tokens are available; refills at ``rate`` tokens/sec
    up to ``capacity``.  Used to honour QuickNode per-second limits."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: int) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                deficit = n - self.tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 1.0))


_bucket = TokenBucket(rate=MAX_CPS, capacity=MAX_CPS)


def rpc_batch(
    block_numbers: Sequence[int],
) -> tuple[list[tuple[int, int]], list[int]]:
    """Fetch block timestamps via one JSON-RPC batch.  Returns
    ``(fetched, rate_limited)`` where ``fetched`` is a list of
    ``(block_number, unix_timestamp_s)`` pairs and ``rate_limited`` is a
    list of block numbers whose sub-call returned ``-32007`` and should be
    retried later.  Raises :class:`RpcError` on HTTP-level failure.
    """
    _bucket.acquire(len(block_numbers))
    calls = [
        {
            "jsonrpc": "2.0",
            "id": blk,
            "method": "eth_getBlockByNumber",
            "params": [hex(blk), False],
        }
        for blk in block_numbers
    ]
    req = urllib.request.Request(
        RPC_URL,
        data=json.dumps(calls).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": os.environ.get("RPC_USER_AGENT", "eatlq-backfill/1.0"),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        raise RpcError(status=e.code, body=e.read().decode(errors="replace")) from None
    except urllib.error.URLError as e:
        raise RpcError(status=0, body=str(e.reason)) from None

    parsed = json.loads(body)
    if isinstance(parsed, dict):
        raise RpcError(status=200, body=body[:500])

    fetched: list[tuple[int, int]] = []
    rate_limited: list[int] = []
    for item in parsed:
        err = item.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            # QuickNode -32007; public Base RPC -32016 over rate limit
            if code in (-32007, -32016, -32005):
                rate_limited.append(int(item.get("id")))
            continue
        res = item.get("result")
        if not res:
            continue
        blk_hex = res.get("number")
        ts_hex = res.get("timestamp")
        if not blk_hex or not ts_hex:
            continue
        fetched.append((int(blk_hex, 16), int(ts_hex, 16)))
    return fetched, rate_limited


def rpc_batch_with_retry(
    block_numbers: Sequence[int],
) -> tuple[list[tuple[int, int]], list[int]]:
    delay = 1.0
    for attempt in range(6):
        try:
            return rpc_batch(block_numbers)
        except RpcError as e:
            if e.status in (429, 502, 503, 504) or e.status == 0:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            log(f"RPC fatal {e}; dropping batch of {len(block_numbers)} starting {block_numbers[0]}")
            return [], list(block_numbers)
    log(f"RPC gave up after 6 retries; requeueing batch of {len(block_numbers)} starting {block_numbers[0]}")
    return [], list(block_numbers)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def discover_candidates(max_blocks: int | None) -> List[int]:
    """Return the distinct block_numbers needing block_time fix."""
    union_parts: list[str] = []
    if "uniswap_v2_swaps" in TARGET_TABLES:
        union_parts.append(
            f"SELECT block_number FROM eatlq.uniswap_v2_swaps "
            f"WHERE chain_id={CHAIN_ID} AND block_time IS NULL "
            f"AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}"
        )
    if "uniswap_v3_swaps" in TARGET_TABLES:
        union_parts.append(
            f"SELECT block_number FROM eatlq.uniswap_v3_swaps "
            f"WHERE chain_id={CHAIN_ID} AND block_time IS NULL "
            f"AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}"
        )
    if "raw_evm_logs" in TARGET_TABLES:
        union_parts.append(
            f"SELECT block_number FROM eatlq.raw_evm_logs "
            f"WHERE block_time IS NULL "
            f"AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}"
        )

    union_sql = "\nUNION DISTINCT\n".join(union_parts)
    limit_clause = f"LIMIT {max_blocks}" if max_blocks else ""
    sql = f"""
    SELECT block_number FROM (
        SELECT DISTINCT block_number FROM (
            {union_sql}
        ) WHERE block_number > 0
    ) AS needed
    LEFT ANTI JOIN (
        SELECT block_number FROM eatlq.block_ts_map
        WHERE chain_id={CHAIN_ID}
    ) AS have USING (block_number)
    ORDER BY block_number
    {limit_clause}
    """
    log("Discovering candidate blocks (distinct block_number not yet in block_ts_map) ...")
    raw = ch_query(sql, fmt="TSV")
    blocks = [int(line) for line in raw.strip().split("\n") if line]
    log(f"-> {len(blocks):,} blocks to fetch (window [{START_BLOCK}, {END_BLOCK}])")
    return blocks


def chunked(iterable: Sequence[int], size: int) -> Iterable[list[int]]:
    buf: list[int] = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


_FLUSHER_SENTINEL = object()  # unique object so we can distinguish from None


class Flusher(threading.Thread):
    """Background inserter: drains result queue and bulk-inserts into
    block_ts_map every ~25k rows or 5s.  Only exits when the main thread
    puts :data:`_FLUSHER_SENTINEL` on the queue.  Flush errors are logged
    and retried; the buffer is never dropped on transient failure."""

    FLUSH_MAX_ROWS = 25_000
    FLUSH_MAX_SECONDS = 5.0

    def __init__(self, result_q: "queue.Queue[object]"):
        super().__init__(daemon=True, name="block_ts_flusher")
        self.q = result_q
        self.inserted = 0
        self.failed_flushes = 0
        self.buf: list[tuple[int, int, int]] = []  # chain_id, block_number, unix_ts
        self._stopping = False

    def run(self) -> None:
        last_flush = time.monotonic()
        while not self._stopping:
            try:
                item = self.q.get(timeout=2.0)
            except queue.Empty:
                if self.buf and (time.monotonic() - last_flush) >= self.FLUSH_MAX_SECONDS:
                    self._safe_flush()
                    last_flush = time.monotonic()
                continue
            if item is _FLUSHER_SENTINEL:
                break
            if item is None:  # legacy path; also treated as sentinel
                break
            try:
                blk, ts = item  # type: ignore[misc]
            except Exception as e:  # pragma: no cover - defensive
                log(f"Flusher: bad queue item {item!r}: {e}")
                continue
            self.buf.append((CHAIN_ID, int(blk), int(ts)))
            if len(self.buf) >= self.FLUSH_MAX_ROWS or \
                    (time.monotonic() - last_flush) >= self.FLUSH_MAX_SECONDS:
                self._safe_flush()
                last_flush = time.monotonic()
        # Drain on shutdown
        while True:
            if self.buf:
                self._safe_flush()
            if self.q.empty():
                break
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item is _FLUSHER_SENTINEL or item is None:
                continue
            try:
                blk, ts = item  # type: ignore[misc]
                self.buf.append((CHAIN_ID, int(blk), int(ts)))
            except Exception:
                pass
        if self.buf:
            self._safe_flush()

    def _safe_flush(self) -> None:
        backoff = 1.0
        for attempt in range(6):
            try:
                self._flush()
                return
            except Exception as e:  # pragma: no cover - defensive
                self.failed_flushes += 1
                log(f"Flusher flush failed (attempt {attempt + 1}/6, buf={len(self.buf)}): {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        log(f"Flusher: giving up on current buf of {len(self.buf)} rows; clearing.")
        self.buf.clear()

    def _flush(self) -> None:
        if not self.buf:
            return
        rows_iter = (
            (cid, blk, f"{ts}.000")  # unix seconds -> DateTime64(3)
            for (cid, blk, ts) in self.buf
        )
        n = ch_insert_tsv(
            "eatlq.block_ts_map",
            ("chain_id", "block_number", "block_time"),
            rows_iter,
        )
        self.inserted += n
        self.buf.clear()


def fetch_all(blocks: list[int]) -> int:
    if not blocks:
        return 0
    result_q: "queue.Queue[object]" = queue.Queue(maxsize=500_000)
    flusher = Flusher(result_q)
    flusher.start()

    t0 = time.monotonic()
    last_log = t0
    total = len(blocks)
    completed_blocks = 0
    retry_pool: list[int] = []
    retry_lock = threading.Lock()

    def drain(batches: list[list[int]], phase: str) -> int:
        nonlocal completed_blocks, last_log
        phase_total = sum(len(b) for b in batches)
        phase_done = 0
        log(f"[{phase}] Fetching {phase_total:,} blocks in {len(batches):,} HTTP batches "
            f"(batch_size={BATCH_SIZE}, concurrency={CONCURRENCY}, max_cps={MAX_CPS}) ...")
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(rpc_batch_with_retry, b): b for b in batches}
            for fut in as_completed(futures):
                batch_blocks = futures[fut]
                try:
                    fetched, rate_limited = fut.result()
                except Exception as e:  # pragma: no cover
                    log(f"Unexpected worker error: {e}; requeueing {len(batch_blocks)} blocks")
                    fetched, rate_limited = [], list(batch_blocks)
                for pair in fetched:
                    result_q.put(pair)
                if rate_limited:
                    with retry_lock:
                        retry_pool.extend(rate_limited)
                completed_blocks += len(fetched)
                phase_done += len(batch_blocks)
                now = time.monotonic()
                if now - last_log >= PROGRESS_EVERY_SECONDS:
                    elapsed = max(now - t0, 1e-6)
                    rate = completed_blocks / elapsed
                    remain = total - completed_blocks + len(retry_pool)
                    eta = remain / max(rate, 1e-6)
                    alive = "ok" if flusher.is_alive() else "DEAD"
                    log(f"  [{phase}] scanned {phase_done:,}/{phase_total:,}  "
                        f"got {completed_blocks:,}/{total:,} "
                        f"({completed_blocks * 100.0 / total:.1f}%) "
                        f"@ {rate:.1f} blk/s  eta {eta/60:.1f} min  "
                        f"inserted {flusher.inserted:,} (failed_flushes={flusher.failed_flushes}) "
                        f"flusher={alive} qsize={result_q.qsize()} retry_pool={len(retry_pool)}")
                    last_log = now
                    if not flusher.is_alive():
                        log("FATAL: Flusher died; aborting so we can restart cleanly")
                        raise SystemExit(4)
        return phase_done

    drain(list(chunked(blocks, BATCH_SIZE)), phase="initial")

    round_ix = 0
    while retry_pool:
        round_ix += 1
        with retry_lock:
            pending = sorted(set(retry_pool))
            retry_pool.clear()
        log(f"[retry#{round_ix}] {len(pending):,} rate-limited blocks to retry")
        if round_ix > 8:
            log(f"[retry#{round_ix}] too many retry rounds; abandoning "
                f"{len(pending)} blocks (will be picked up on a later run)")
            break
        time.sleep(min(2 + round_ix, 10))  # back off between rounds
        drain(list(chunked(pending, BATCH_SIZE)), phase=f"retry#{round_ix}")

    result_q.put(_FLUSHER_SENTINEL)
    flusher.join(timeout=300)
    if flusher.is_alive():
        log("WARN: Flusher did not exit cleanly within 300s")
    log(f"Fetch done: requested {total:,} blocks, wrote {flusher.inserted:,} rows "
        f"to block_ts_map in {(time.monotonic() - t0):.0f}s")
    return flusher.inserted


def run_mutation(table: str) -> str | None:
    """Launch ALTER TABLE UPDATE using block_ts_map, return mutation_id."""
    log(f"Launching mutation on eatlq.{table} ...")
    # Use dictionary lookup instead of correlated subquery; ALTER UPDATE does
    # not support referencing target-table columns inside subqueries.
    ch_query("SYSTEM RELOAD DICTIONARY eatlq.block_ts_dict", fmt="TSV")
    sql = f"""
    ALTER TABLE eatlq.{table}
    UPDATE block_time = dictGet(
        'eatlq.block_ts_dict',
        'block_time',
        (toUInt64(chain_id), toUInt64(block_number))
    )
    WHERE chain_id = {CHAIN_ID}
      AND block_time IS NULL
      AND dictHas(
          'eatlq.block_ts_dict',
          (toUInt64(chain_id), toUInt64(block_number))
      )
    """
    ch_query(sql, fmt="TSV")
    rows = ch_query(
        f"SELECT mutation_id FROM system.mutations "
        f"WHERE database='eatlq' AND table='{table}' AND is_done=0 "
        f"ORDER BY create_time DESC LIMIT 1",
        fmt="TSV",
    )
    mutation_id = rows.strip() or None
    log(f"  eatlq.{table} mutation submitted id={mutation_id}")
    return mutation_id


def wait_mutation(table: str, mutation_id: str | None) -> None:
    if not mutation_id:
        log(f"  eatlq.{table}: no mutation_id; skipping wait")
        return
    log(f"  waiting for mutation {mutation_id} on eatlq.{table} ...")
    while True:
        rows = ch_query(
            f"SELECT is_done, parts_to_do, latest_fail_reason "
            f"FROM system.mutations WHERE database='eatlq' AND table='{table}' "
            f"AND mutation_id='{mutation_id}' FORMAT TSV",
            fmt="TSV",
        ).strip()
        if not rows:
            log(f"  mutation {mutation_id} disappeared; assuming done")
            return
        parts = rows.split("\t")
        is_done = parts[0] == "1"
        parts_to_do = parts[1] if len(parts) > 1 else "?"
        fail = parts[2] if len(parts) > 2 else ""
        log(f"    is_done={is_done} parts_to_do={parts_to_do} fail={fail[:200]}")
        if is_done:
            return
        time.sleep(15)


def main() -> int:
    if not RPC_URL:
        log("ERROR: set LABEL_RPC_URL or FREE_RPC_URL")
        return 2

    log(f"RPC endpoint: {RPC_URL.split('//', 1)[-1].split('/')[0]}")
    log(f"cfg: chain_id={CHAIN_ID} batch={BATCH_SIZE} concurrency={CONCURRENCY} "
        f"window=[{START_BLOCK}, {END_BLOCK}] max_blocks={MAX_BLOCKS} "
        f"tables={TARGET_TABLES} mutate={RUN_MUTATIONS}")

    blocks = discover_candidates(MAX_BLOCKS)
    if not blocks:
        log("Nothing to do.")
        return 0

    fetch_all(blocks)

    if RUN_MUTATIONS:
        mut_ids: dict[str, str | None] = {}
        for table in TARGET_TABLES:
            if table == "raw_evm_logs":
                # raw_evm_logs is 9M rows; mutation window unbounded would be
                # very expensive and downstream meme pipeline doesn't use it.
                log(f"Skipping mutation on raw_evm_logs (not used by meme pipeline)")
                continue
            mut_ids[table] = run_mutation(table)
        if WAIT_FOR_MUTATIONS:
            for table, mid in mut_ids.items():
                wait_mutation(table, mid)

    log("\n=== Verification ===")
    for table in TARGET_TABLES:
        if table == "raw_evm_logs":
            continue
        row = ch_query(
            f"SELECT countDistinct(block_number) FROM eatlq.{table} "
            f"WHERE chain_id={CHAIN_ID} AND block_time IS NULL",
            fmt="TSV",
        ).strip()
        log(f"  eatlq.{table}: {row} distinct blocks still with NULL block_time")

    rows = ch_query(
        f"SELECT count(), countDistinct(block_number), "
        f"min(block_time), max(block_time) "
        f"FROM eatlq.block_ts_map WHERE chain_id={CHAIN_ID}",
        fmt="TSV",
    ).strip()
    log(f"  eatlq.block_ts_map: {rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
