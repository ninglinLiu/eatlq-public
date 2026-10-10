"""Minimal JSON-RPC client for Base mainnet + Aerodrome ABI helpers.

The archive RPC URL is read from /opt/EATLQ/infra/compose/.env at import time and is
never written to stdout, logs or output files.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENV_PATH = "/opt/EATLQ/infra/compose/.env"


def _load_rpc_url() -> str:
    url = os.environ.get("BASE_ARCHIVE_RPC")
    if url:
        return url.strip()
    with open(ENV_PATH) as fh:
        for line in fh:
            if line.startswith("POOL_DISCOVERY_RPC_URL="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("archive RPC url not found")


RPC_URL = _load_rpc_url()

_req_id = [0]
_id_lock = threading.Lock()
STATS = {"calls": 0, "batches": 0, "retries": 0, "errors": 0, "ratelimited": 0}

# The provider enforces a hard 50 requests/second cap and counts each element of a
# JSON-RPC batch separately, so throttle on individual sub-requests.
RATE_LIMIT_PER_SEC = 30.0
MAX_BATCH = 20
_bucket = {"tokens": RATE_LIMIT_PER_SEC, "ts": time.monotonic()}
_bucket_lock = threading.Lock()


def _throttle(n):
    while True:
        with _bucket_lock:
            now = time.monotonic()
            _bucket["tokens"] = min(
                RATE_LIMIT_PER_SEC,
                _bucket["tokens"] + (now - _bucket["ts"]) * RATE_LIMIT_PER_SEC,
            )
            _bucket["ts"] = now
            if _bucket["tokens"] >= n:
                _bucket["tokens"] -= n
                return
            wait = (n - _bucket["tokens"]) / RATE_LIMIT_PER_SEC
        time.sleep(min(wait, 0.25))


def _post(payload, timeout=90):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        RPC_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _is_rate_limit(err):
    if not err:
        return False
    msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
    return err.get("code") == -32007 or "limit reached" in msg or "429" in msg


def _batch_once(reqs, timeout=90, max_retries=6):
    payload = []
    with _id_lock:
        for method, params in reqs:
            _req_id[0] += 1
            payload.append(
                {"jsonrpc": "2.0", "id": _req_id[0], "method": method, "params": params}
            )
    id_order = [p["id"] for p in payload]
    delay = 1.0
    for attempt in range(max_retries):
        _throttle(len(payload))
        try:
            out = _post(payload, timeout=timeout)
            STATS["batches"] += 1
            STATS["calls"] += len(payload)
            if isinstance(out, dict):
                raise RuntimeError(json.dumps(out)[:400])
            by_id = {o["id"]: o for o in out}
            res = []
            for i in id_order:
                o = by_id.get(i, {})
                if "error" in o:
                    res.append((None, o["error"]))
                else:
                    res.append((o.get("result"), None))
            return res
        except Exception as exc:  # noqa: BLE001 - transport/rate-limit, retry with backoff
            STATS["retries"] += 1
            if attempt == max_retries - 1:
                return [(None, {"message": f"transport: {exc}"}) for _ in id_order]
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    return [(None, {"message": "unreachable"}) for _ in id_order]


def rpc_batch(reqs, timeout=90, max_retries=6, rl_rounds=8):
    """reqs: list of (method, params). Returns list of (result, error) tuples.

    Splits into provider-safe sub-batches, throttles, and re-issues any sub-request
    that came back with a rate-limit error until it succeeds or rl_rounds is hit.
    """
    results = [None] * len(reqs)
    pending = list(range(len(reqs)))
    backoff = 0.5
    for rnd in range(rl_rounds):
        nxt = []
        for i in range(0, len(pending), MAX_BATCH):
            idxs = pending[i:i + MAX_BATCH]
            out = _batch_once([reqs[j] for j in idxs], timeout=timeout,
                              max_retries=max_retries)
            for j, (res, err) in zip(idxs, out):
                if err and _is_rate_limit(err):
                    STATS["ratelimited"] += 1
                    nxt.append(j)
                    results[j] = (res, err)
                else:
                    results[j] = (res, err)
        if not nxt:
            break
        pending = nxt
        time.sleep(backoff)
        backoff = min(backoff * 2, 10.0)
    for r in results:
        if r and r[1]:
            STATS["errors"] += 1
    return results


def rpc(method, params, **kw):
    (result, error), = rpc_batch([(method, params)], **kw)
    if error:
        raise RuntimeError(f"{method} failed: {error}")
    return result


# ---------------------------------------------------------------- ABI encoding

def sel(x):
    """4-byte selector, given as hex string constant (precomputed with keccak)."""
    return x


def enc_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n):
    return hex(n)[2:].rjust(64, "0")


def dec_addr(hexdata):
    if not hexdata or hexdata == "0x":
        return None
    return "0x" + hexdata[-40:].lower()


def dec_uint(hexdata, word=0):
    if not hexdata or hexdata == "0x":
        return None
    raw = hexdata[2:]
    chunk = raw[word * 64:(word + 1) * 64]
    if len(chunk) < 64:
        return None
    return int(chunk, 16)


def dec_int(hexdata, word=0, bits=256):
    v = dec_uint(hexdata, word)
    if v is None:
        return None
    if v >= (1 << (bits - 1)):
        v -= 1 << bits
    return v


def eth_call(to, data, block="latest"):
    blk = block if isinstance(block, str) else hex(block)
    return rpc("eth_call", [{"to": to, "data": data}, blk])


def eth_call_batch(items, block="latest"):
    """items: list of (to, data). Returns list of (result_hex, error)."""
    blk = block if isinstance(block, str) else hex(block)
    return rpc_batch([("eth_call", [{"to": to, "data": data}, blk]) for to, data in items])


# ---------------------------------------------------------------- epoch helpers

WEEK = 7 * 24 * 3600


def epoch_start(ts: int) -> int:
    """Aerodrome epoch = floor(ts / 1 week) in raw unix seconds.

    Unix epoch 0 (1970-01-01) was a Thursday, so week-floor lands on Thursday 00:00 UTC.
    """
    return (ts // WEEK) * WEEK


def epochs_between(ts_from: int, ts_to: int):
    e = epoch_start(ts_from)
    out = []
    while e <= ts_to:
        out.append(e)
        e += WEEK
    return out


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- block lookup

_block_ts_cache = {}


def block_timestamp(bn: int) -> int:
    if bn in _block_ts_cache:
        return _block_ts_cache[bn]
    b = rpc("eth_getBlockByNumber", [hex(bn), False])
    ts = int(b["timestamp"], 16)
    _block_ts_cache[bn] = ts
    return ts


MAX_LOG_SPAN = 10000  # provider hard cap: "eth_getLogs is limited to a 10,000 range"


def scan_logs(address, topics, from_block, to_block, workers=12, span=MAX_LOG_SPAN,
              progress=None):
    """Parallel chunked eth_getLogs. Returns (logs, failed_ranges)."""
    from concurrent.futures import ThreadPoolExecutor

    ranges = []
    b = from_block
    while b <= to_block:
        e = min(b + span - 1, to_block)
        ranges.append((b, e))
        b = e + 1

    logs, failed = [], []
    done = [0]
    lock = threading.Lock()

    def work(rng):
        s, e = rng
        flt = {"fromBlock": hex(s), "toBlock": hex(e)}
        if address:
            flt["address"] = address
        if topics:
            flt["topics"] = topics
        res, err = rpc_batch([("eth_getLogs", [flt])])[0]
        with lock:
            done[0] += 1
            if progress and done[0] % progress == 0:
                print(f"  ...{done[0]}/{len(ranges)} chunks, {len(logs)} logs",
                      flush=True)
            if err:
                failed.append((s, e, str(err)[:200]))
            else:
                logs.extend(res or [])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, ranges))
    return logs, failed


def block_at_timestamp(target_ts: int, lo: int = 1, hi: int = None) -> int:
    """Largest block with timestamp <= target_ts (binary search on the archive RPC)."""
    if hi is None:
        hi = int(rpc("eth_blockNumber", []), 16)
    if block_timestamp(hi) <= target_ts:
        return hi
    lo_ts = block_timestamp(lo)
    if lo_ts > target_ts:
        return lo
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if block_timestamp(mid) <= target_ts:
            lo = mid
        else:
            hi = mid
    return lo
