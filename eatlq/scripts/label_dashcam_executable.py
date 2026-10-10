"""Executable-quote labeler (author's Phase 2 mainline).

Improvements over label_dashcam.py:
  1. Dedup dashcam to unique (pool, 5-min bucket). Typical reduction 5×.
  2. Pre-build ts→block interpolation table in ONE batched scan of the chain
     instead of per-row binary search.
  3. Load pool metadata (v2 / v3 / aero + fee tier + token0) from
     eatlq.discovered_pools once; cache token0 for v2 pools.
  4. ThreadPoolExecutor with N workers for concurrent quote RPC.
  5. Streaming CSV output + periodic checkpoint so crashes are resumable.

Output schema matches label_dashcam.py so retrain_model.py is a drop-in
consumer.

Usage:
    LABEL_RPC_URL=<archive>  python scripts/label_dashcam_executable.py

Env knobs:
    DASHCAM_PATH (default logs/dashcam.csv)
    LABELED_OUTPUT (default logs/labeled_dataset_v2_executable.csv)
    CHAIN_ID (8453)
    LABEL_WORKERS (20)
    LABEL_BUCKET_SEC (300) -- dedup bucket size
    LABEL_EXIT_HORIZON_SEC (1800) -- matches author's 30-min exit
    LABEL_NOTIONAL_WEI (0.0005 ETH worth)
    LABEL_LIMIT (0 = unlimited; for smoke tests)
    LABEL_MAX_AGE_DAYS (0 = unlimited) -- drop samples older than N days
                                          (pools likely dead → quote_failed)
    LABEL_POOL_TYPES ("v2,v3")         -- comma-separated pool_type whitelist
                                          (e.g. "v2" for V2-only clean baseline)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("label_exec")

ROUND_TRIP_COST = 0.006
WETH_BASE = "0x4200000000000000000000000000000000000006".lower()
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"  # Uniswap V3 QuoterV2 on Base


# ─────────────────────────────────────────────────────────────────────
# JSON-RPC client (raw, supports batch)
# ─────────────────────────────────────────────────────────────────────

class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 6):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._lock = Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _post(self, body: bytes):
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                status = getattr(e, "code", None)
                # back off on 429 / 5xx / network blips
                if status == 429 or (status and 500 <= status < 600) or status is None:
                    time.sleep(min(0.5 * (2 ** attempt), 10.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc retries exhausted")

    def call(self, method: str, params: list):
        rid = self._next_id()
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid}).encode()
        return self._post(body)

    def batch(self, calls: list) -> list:
        """calls: list of (method, params). Returns results in order."""
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i} for i, (m, p) in enumerate(calls)]
        body = json.dumps(payload).encode()
        out = self._post(body)
        idx = {r.get("id"): r for r in out}
        return [idx.get(i) for i in range(len(calls))]


# ─────────────────────────────────────────────────────────────────────
# ts → block interpolation grid
# ─────────────────────────────────────────────────────────────────────

def build_ts_grid(rpc: RPC, ts_min_sec: int, ts_max_sec: int) -> tuple[list, list]:
    """Scan chain in [ts_min, ts_max] every ~50 blocks (~100 sec) and return
    two parallel sorted arrays (block_nums, timestamps) for interpolation.
    """
    r = rpc.call("eth_getBlockByNumber", ["latest", False])
    latest_num = int(r["result"]["number"], 16)
    latest_ts = int(r["result"]["timestamp"], 16)
    # Base ~2 sec/block. Over-estimate by 2x for safety margin.
    blocks_back = max((latest_ts - ts_min_sec) // 2 + 2000, 0)
    lo_block = max(0, latest_num - blocks_back)
    step = 50  # every 50 blocks
    block_nums = list(range(lo_block, latest_num + 1, step))
    batch_size = 500
    results_map: dict[int, int] = {}
    t0 = time.time()
    for i in range(0, len(block_nums), batch_size):
        sub = block_nums[i:i + batch_size]
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in sub]
        results = rpc.batch(calls)
        for bn, res in zip(sub, results):
            if res and res.get("result"):
                results_map[bn] = int(res["result"]["timestamp"], 16)
        log.info("ts grid: %d/%d blocks sampled (elapsed %.1fs)",
                 min(i + batch_size, len(block_nums)), len(block_nums),
                 time.time() - t0)
    sorted_bn = sorted(results_map.keys())
    sorted_ts = [results_map[b] for b in sorted_bn]
    log.info("ts grid built: %d samples, span [%d ... %d]",
             len(sorted_bn), sorted_ts[0] if sorted_ts else -1,
             sorted_ts[-1] if sorted_ts else -1)
    return sorted_bn, sorted_ts


def ts_to_block(bn_arr: list, ts_arr: list, target_ts: int) -> int:
    idx = bisect_left(ts_arr, target_ts)
    if idx == 0:
        return bn_arr[0]
    if idx >= len(bn_arr):
        return bn_arr[-1]
    b_lo, t_lo = bn_arr[idx - 1], ts_arr[idx - 1]
    b_hi, t_hi = bn_arr[idx], ts_arr[idx]
    if t_hi == t_lo:
        return b_hi
    return b_lo + int((target_ts - t_lo) / (t_hi - t_lo) * (b_hi - b_lo))


# ─────────────────────────────────────────────────────────────────────
# ClickHouse: load pool metadata for all dashcam pools
# ─────────────────────────────────────────────────────────────────────

def load_pool_meta(ch_url: str, ch_db: str, ch_user: str, ch_pw: str,
                   pools: set[str]) -> dict[str, dict]:
    if not pools:
        return {}
    pool_list = ",".join(f"'{p}'" for p in sorted(pools))
    sql = f"""
        SELECT
            lower(pool_address) AS pool,
            pool_type,
            lower(token0_address) AS t0,
            lower(token1_address) AS t1,
            fee_bps
        FROM eatlq.discovered_pools
        WHERE chain_id = 8453
          AND lower(pool_address) IN ({pool_list})
    """
    params = urlencode({
        "database": ch_db, "user": ch_user, "password": ch_pw,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{ch_url}/?{params}", data=sql.encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8").strip()
    meta: dict[str, dict] = {}
    for line in body.split("\n") if body else []:
        if not line.strip():
            continue
        r = json.loads(line)
        meta[r["pool"]] = {
            "pool_type": r.get("pool_type", ""),
            "t0": r.get("t0", ""),
            "t1": r.get("t1", ""),
            "fee_bps": int(r.get("fee_bps", 30)),
        }
    return meta


# ─────────────────────────────────────────────────────────────────────
# On-chain quoting (minimal ABI encoding, no web3.py dependency)
# ─────────────────────────────────────────────────────────────────────

# selectors
_SEL_TOKEN0 = "0x0dfe1681"       # token0()
_SEL_GET_RESERVES = "0x0902f1ac" # getReserves()
# QuoterV2.quoteExactInputSingle(struct params)
# selector is derived from quoteExactInputSingle((address,address,uint256,uint24,uint160))
_SEL_QUOTE_V3 = "0xc6a5026a"


def _pad32(hex_: str) -> str:
    s = hex_.lstrip("0x").lower()
    return s.rjust(64, "0")


def _strip_0x(hex_: str) -> str:
    """Remove only the literal 0x prefix; ABI word leading zeros are significant."""
    return hex_[2:] if hex_.startswith("0x") else hex_


def _addr_pad(addr: str) -> str:
    return _pad32(addr.lower().lstrip("0x"))


def _uint_pad(n: int) -> str:
    return _pad32(hex(n)[2:])


def _eth_call(rpc: RPC, to: str, data: str, block_num: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block_num)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_token0(rpc: RPC, pool: str, block: int) -> str:
    res = _eth_call(rpc, pool, _SEL_TOKEN0, block)
    return "0x" + res[-40:]


def call_get_reserves(rpc: RPC, pool: str, block: int) -> tuple[int, int]:
    res = _eth_call(rpc, pool, _SEL_GET_RESERVES, block)
    raw = res.lstrip("0x")
    r0 = int(raw[0:64], 16)
    r1 = int(raw[64:128], 16)
    return r0, r1


def quote_v2_out(rin: int, rout: int, amount_in: int) -> int:
    if rin <= 0 or rout <= 0 or amount_in <= 0:
        return 0
    af = amount_in * 997
    return (af * rout) // (rin * 1000 + af)


def call_quoter_v3(rpc: RPC, token_in: str, token_out: str,
                   amount_in: int, fee: int, block: int) -> int:
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in)
        + _addr_pad(token_out)
        + _uint_pad(amount_in)
        + _uint_pad(fee)
        + _uint_pad(0)  # sqrtPriceLimitX96
    )
    try:
        res = _eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception:
        return 0
    raw = _strip_0x(res)
    if len(raw) < 64:
        return 0
    return int(raw[0:64], 16)


# ─────────────────────────────────────────────────────────────────────
# Per-sample labeling
# ─────────────────────────────────────────────────────────────────────

TOKEN0_CACHE: dict[str, str] = {}
TOKEN0_LOCK = Lock()


def get_token0_cached(rpc: RPC, pool: str, any_block: int) -> str:
    with TOKEN0_LOCK:
        t0 = TOKEN0_CACHE.get(pool)
        if t0 is not None:
            return t0
    t0 = call_token0(rpc, pool, any_block).lower()
    with TOKEN0_LOCK:
        TOKEN0_CACHE[pool] = t0
    return t0


_MIN_WETH_RESERVE_WEI = int(os.environ.get("LABEL_MIN_WETH_WEI", str(int(0.1 * 1e18))))  # 0.1 ETH ≈ $200
_MAX_ABS_NET = float(os.environ.get("LABEL_MAX_ABS_NET", "10.0"))  # clip |r| > 10× as bogus


def _sanity_clip(net: float) -> tuple[float, bool]:
    """Clip net_return to [-1, _MAX_ABS_NET]. Return (clipped, is_bogus)."""
    if not (net == net):  # NaN
        return 0.0, True
    if net > _MAX_ABS_NET or net < -1.0:
        return max(-1.0, min(_MAX_ABS_NET, net)), True
    return net, False


def label_v2(rpc: RPC, pool: str, weth: str, notional_wei: int,
             entry_block: int, exit_block: int) -> tuple[int, float, str]:
    try:
        t0 = get_token0_cached(rpc, pool, entry_block)
        r0, r1 = call_get_reserves(rpc, pool, entry_block)
        if t0 == weth:
            r_weth_e, r_tok_e = r0, r1
        else:
            r_weth_e, r_tok_e = r1, r0
        if r_weth_e < _MIN_WETH_RESERVE_WEI:
            return -1, 0.0, "fail_low_liquidity"
        if r_tok_e <= 0:
            return -1, 0.0, "fail_zero_tok_reserve"
        if notional_wei * 100 > r_weth_e:
            return -1, 0.0, "fail_notional_gt_1pct"
        tokens = quote_v2_out(r_weth_e, r_tok_e, notional_wei)
        if tokens <= 0:
            return -1, 0.0, "fail_entry_quote_zero"
        r0x, r1x = call_get_reserves(rpc, pool, exit_block)
        if t0 == weth:
            r_weth_x, r_tok_x = r0x, r1x
        else:
            r_weth_x, r_tok_x = r1x, r0x
        if r_weth_x <= 0 or r_tok_x <= 0:
            return 0, -1.0, "executable_exit_drained"
        weth_back = quote_v2_out(r_tok_x, r_weth_x, tokens)
        if weth_back <= 0:
            return 0, -1.0, "executable_exit_drained"
        net = weth_back / notional_wei - 1.0 - ROUND_TRIP_COST
        net, bogus = _sanity_clip(net)
        if bogus:
            return -1, 0.0, "fail_sanity_clip"
        return (1 if net > 0 else 0), net, "executable"
    except Exception as e:
        log.debug("v2 label err %s: %s", pool, e)
        return -1, 0.0, "fail_rpc_exception"


def label_v3(
    rpc: RPC,
    pool: str,
    t0: str,
    t1: str,
    fee_bps: int,
    weth: str,
    notional_wei: int,
    entry_block: int,
    exit_block: int,
    fee_raw: int | None = None,
) -> tuple[int, float, str]:
    """Quoter fee parameter is the on-chain Uniswap V3 `fee` uint24 (e.g. 500/3000/10000),
    not necessarily fee_bps*100 when factories emit non-canonical tiers."""
    fee_q = int(fee_raw) if fee_raw is not None else fee_bps * 100
    token = t1 if t0 == weth else t0
    try:
        tokens = call_quoter_v3(rpc, weth, token, notional_wei, fee_q, entry_block)
        if tokens <= 0:
            return -1, 0.0, "fail_v3_entry_zero"
        weth_back = call_quoter_v3(rpc, token, weth, tokens, fee_q, exit_block)
        if weth_back <= 0:
            return 0, -1.0, "executable_exit_drained"
        net = weth_back / notional_wei - 1.0 - ROUND_TRIP_COST
        net, bogus = _sanity_clip(net)
        if bogus:
            return -1, 0.0, "fail_sanity_clip"
        return (1 if net > 0 else 0), net, "executable"
    except Exception as e:
        log.debug("v3 label err %s: %s", pool, e)
        return -1, 0.0, "fail_rpc_exception"


def label_one(rpc: RPC, pool: str, meta: dict, weth: str,
              notional_wei: int, entry_block: int, exit_block: int
              ) -> tuple[int, float, str]:
    pt = meta.get("pool_type", "")
    fr = meta.get("fee_raw")
    fee_raw_i: int | None
    if isinstance(fr, int):
        fee_raw_i = fr
    elif isinstance(fr, str) and fr.strip().isdigit():
        fee_raw_i = int(fr.strip())
    else:
        fee_raw_i = None
    if pt in ("v2", "aero_vamm"):
        return label_v2(rpc, pool, weth, notional_wei, entry_block, exit_block)
    if pt in ("v3", "aero_cl"):
        return label_v3(
            rpc,
            pool,
            meta.get("t0", ""),
            meta.get("t1", ""),
            meta.get("fee_bps", 30),
            weth,
            notional_wei,
            entry_block,
            exit_block,
            fee_raw=fee_raw_i,
        )
    # unknown → try v2 first, then v3 (fee 30bps)
    r = label_v2(rpc, pool, weth, notional_wei, entry_block, exit_block)
    if r[0] >= 0:
        return r
    # fall back v3 @ 30bps
    return label_v3(
        rpc,
        pool,
        meta.get("t0", weth),
        meta.get("t1", pool),
        meta.get("fee_bps", 30) or 30,
        weth,
        notional_wei,
        entry_block,
        exit_block,
        fee_raw=fee_raw_i,
    )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    dashcam_path = Path(os.environ.get("DASHCAM_PATH", "logs/dashcam.csv"))
    output_path = Path(os.environ.get("LABELED_OUTPUT", "logs/labeled_dataset_v2_executable.csv"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    rpc_url = os.environ.get("LABEL_RPC_URL", "") or os.environ.get("EVM_HTTP_URL", "")
    workers = int(os.environ.get("LABEL_WORKERS", "20"))
    bucket_sec = int(os.environ.get("LABEL_BUCKET_SEC", "300"))
    exit_horizon_sec = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    limit = int(os.environ.get("LABEL_LIMIT", "0"))
    max_age_days = int(os.environ.get("LABEL_MAX_AGE_DAYS", "0"))
    pool_types_env = os.environ.get("LABEL_POOL_TYPES", "v2,v3").strip()
    supported_types = {
        t.strip().lower() for t in pool_types_env.split(",") if t.strip()
    }

    if not rpc_url:
        log.error("LABEL_RPC_URL or EVM_HTTP_URL required (archive RPC)")
        sys.exit(1)
    if not dashcam_path.exists():
        log.error("%s missing", dashcam_path)
        sys.exit(1)

    rpc = RPC(rpc_url)
    r = rpc.call("eth_chainId", [])
    chid = int(r["result"], 16)
    if chid != chain_id:
        log.error("RPC chain_id=%d != expected %d", chid, chain_id)
        sys.exit(1)
    log.info("RPC ok, chain_id=%d", chid)

    # 1) read + dedup
    log.info("reading %s", dashcam_path)
    by_bucket: dict[tuple[str, int], dict] = {}
    with dashcam_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row.get("timestamp", 0))
            except ValueError:
                continue
            if ts <= 0:
                continue
            pool = (row.get("token_address") or "").lower()
            if not pool:
                continue
            bucket = (ts // 1000 // bucket_sec) * bucket_sec
            key = (pool, bucket)
            cur = by_bucket.get(key)
            if cur is None or ts < cur["_ts"]:
                row["_ts"] = ts
                row["_bucket"] = bucket
                by_bucket[key] = row
    samples = list(by_bucket.values())
    log.info("unique (pool, %ds bucket) samples: %d  (pools=%d)",
             bucket_sec, len(samples), len({k[0] for k in by_bucket}))

    # drop samples that are too young for exit horizon
    now_ms = int(time.time() * 1000)
    min_age_ms = (exit_horizon_sec + 120) * 1000
    samples = [s for s in samples if now_ms - s["_ts"] >= min_age_ms]
    log.info("after age filter (>= %ds old): %d", exit_horizon_sec, len(samples))

    # drop samples that are too old (pools likely dead → quote_failed)
    if max_age_days > 0:
        max_age_ms = max_age_days * 86400 * 1000
        before = len(samples)
        samples = [s for s in samples if now_ms - s["_ts"] <= max_age_ms]
        log.info("after max-age filter (<= %dd old): %d (dropped %d)",
                 max_age_days, len(samples), before - len(samples))

    if limit and len(samples) > limit:
        samples = samples[:limit]
        log.info("LIMIT applied → %d samples", len(samples))

    if not samples:
        log.error("no samples to label")
        sys.exit(1)

    # 2) pool meta
    pools = {s["token_address"].lower() for s in samples}
    log.info("loading pool meta for %d pools ...", len(pools))
    meta = load_pool_meta(ch_url, ch_db, ch_user, ch_pw, pools)
    log.info("meta resolved: %d/%d", len(meta), len(pools))

    # 2b) keep only whitelisted pool_types (default v2,v3).
    # Aerodrome (aero_cl / aero_vamm) and Uniswap V4 pools use
    # different contracts and would always yield quote_failed here.
    before_type_filter = len(samples)
    samples = [s for s in samples
               if meta.get(s["token_address"].lower(), {}).get("pool_type") in supported_types]
    dropped_by_type = before_type_filter - len(samples)
    log.info("after pool_type filter (%s only): %d (dropped %d others)",
             ",".join(sorted(supported_types)), len(samples), dropped_by_type)
    if not samples:
        log.error("no samples to label after pool_type filter")
        sys.exit(1)

    # 3) ts→block grid
    ts_min = min(s["_bucket"] for s in samples)
    ts_max = max(s["_bucket"] for s in samples) + exit_horizon_sec
    log.info("building ts→block grid for [%d … %d]", ts_min, ts_max)
    bn_arr, ts_arr = build_ts_grid(rpc, ts_min, ts_max)

    # 4) resume logic
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_keys: set[tuple[str, int]] = set()
    if output_path.exists():
        try:
            with output_path.open("r", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    done_keys.add((r["token_address"].lower(), int(r["timestamp"]) // 1000 // bucket_sec * bucket_sec))
            log.info("resuming: %d rows already labeled", len(done_keys))
        except Exception:
            log.warning("existing output unreadable, starting fresh")
            output_path.unlink(missing_ok=True)
            done_keys = set()

    todo = [s for s in samples if (s["token_address"].lower(), s["_bucket"]) not in done_keys]
    log.info("todo: %d samples", len(todo))

    # 5) worker pool
    append_mode = output_path.exists()
    f_out = output_path.open("a" if append_mode else "w", newline="", encoding="utf-8")
    w = csv.writer(f_out)
    if not append_mode:
        w.writerow([
            "timestamp", "token_address", "symbol",
            "price_velocity_5m", "buy_pressure_ratio",
            "token_volatility_5m", "activity_heat",
            "triggered_signal",
            "label", "net_return", "label_type",
        ])
        f_out.flush()
    write_lock = Lock()

    t_start = time.time()
    stats = {"labeled": 0, "pos": 0, "skipped": 0, "quote_failed": 0}
    reason_counts: dict[str, int] = {}
    stats_lock = Lock()

    def do_one(sample: dict):
        ts = sample["_ts"]
        pool = sample["token_address"].lower()
        signal_ts_sec = ts // 1000
        exit_ts_sec = signal_ts_sec + exit_horizon_sec
        entry_block = ts_to_block(bn_arr, ts_arr, signal_ts_sec)
        exit_block = ts_to_block(bn_arr, ts_arr, exit_ts_sec)
        m = meta.get(pool, {"pool_type": "", "t0": "", "t1": "", "fee_bps": 30})
        label, net, lt = label_one(rpc, pool, m, WETH_BASE, notional_wei, entry_block, exit_block)
        with stats_lock:
            reason_counts[lt] = reason_counts.get(lt, 0) + 1
        if label < 0:
            with stats_lock:
                stats["skipped"] += 1
                stats["quote_failed"] += 1
            return
        with write_lock:
            w.writerow([
                ts, pool, sample.get("symbol", ""),
                sample.get("momentum_val", "0"),
                sample.get("whale_val", "0"),
                sample.get("risk_val", "0"),
                sample.get("activity_val", "0"),
                sample.get("triggered_signal", "0"),
                label, round(net, 6), lt,
            ])
            f_out.flush()
        with stats_lock:
            stats["labeled"] += 1
            if label == 1:
                stats["pos"] += 1

    log.info("launching %d workers over %d samples", workers, len(todo))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(do_one, s) for s in todo]
        done = 0
        last_log = time.time()
        for _ in as_completed(futures):
            done += 1
            if time.time() - last_log > 20:
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(todo) - done) / rate if rate > 0 else -1
                with stats_lock:
                    log.info(
                        "progress %d/%d (%.1f%%) labeled=%d pos=%d quote_failed=%d "
                        "rate=%.1f/s eta=%.1fmin",
                        done, len(todo), 100.0 * done / len(todo),
                        stats["labeled"], stats["pos"], stats["quote_failed"],
                        rate, eta / 60,
                    )
                last_log = time.time()

    f_out.close()
    with stats_lock:
        pos_rate = stats["pos"] / max(stats["labeled"], 1) * 100
        log.info(
            "DONE labeled=%d pos=%d (%.1f%%) skipped=%d quote_failed=%d elapsed=%.1fs",
            stats["labeled"], stats["pos"], pos_rate,
            stats["skipped"], stats["quote_failed"],
            time.time() - t_start,
        )
        total = sum(reason_counts.values())
        if total:
            log.info("REASON BREAKDOWN (n=%d):", total)
            for reason, n in sorted(reason_counts.items(), key=lambda x: -x[1]):
                log.info("  %-32s %6d (%5.1f%%)",
                         reason, n, 100.0 * n / total)
    log.info("output=%s", output_path)


if __name__ == "__main__":
    main()
