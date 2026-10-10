"""
Dynamic Swap Event Poller — polls eth_getLogs for Swap events on recently
discovered WETH-paired pools and publishes decoded ticks to market_ticks stream.

Bridge until the Rust ingestor supports dynamic address lists.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import msgpack
import redis
from web3 import Web3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("swap_poller")

TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_AERO_VAMM_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

ALL_SWAP_TOPICS = [TOPIC_V3_SWAP, TOPIC_V2_SWAP, TOPIC_AERO_VAMM_SWAP]

# Always poll blue-chip pools regardless of discovery age (executor registry).
BLUE_CHIP_POOLS = [
    a.strip().lower()
    for a in os.environ.get(
        "SWAP_POLLER_BLUE_CHIP_POOLS",
        "0xd0b53d9277642d899df5c87a3966a349a798f224,"
        "0x4c36388be6f416a29c8d8eee81c771ce6be14b18,"
        "0x10648ba41b8565907cfa1496765fa4d95390aa0d,"
        "0xb4cb800910b228ed3d0834cf79d697127bbb00e5,"
        "0xfcd08d68b2313cd5e1d9e0e3f3183ad7cfdbf66f,"
        "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d,"
        "0x82321f3beb69f503380d6b233857d5c43562e2d0,"
        "0xdfe5f275020def30993f042174fc2d335678b626,"
        "0xbe00ff35af70e8415d0eb605a286d8a45466a4c1,"
        "0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6",
    ).split(",")
    if a.strip()
]


def _get_discovered_pools(r: redis.Redis, max_age_h: int = 48) -> List[Dict[str, str]]:
    """Scan Redis for pool:discovered:* hashes with has_weth_leg=1 and age < max_age_h.
    Uses on-chain created_at_sec when available, falls back to discovered_at_ms."""
    pools = []
    now = time.time()
    cutoff_sec = now - max_age_h * 3600
    cutoff_ms = int(cutoff_sec * 1000)

    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=b"pool:discovered:*", count=200)
        for key in keys:
            data = r.hgetall(key)
            if not data:
                continue
            has_weth = (data.get(b"has_weth_leg") or b"0").decode()
            if has_weth != "1":
                continue

            created_sec = int((data.get(b"created_at_sec") or b"0").decode())
            disc_at = int((data.get(b"discovered_at_ms") or b"0").decode())

            if created_sec > 0:
                if created_sec < cutoff_sec:
                    continue
            elif disc_at > 0:
                if disc_at < cutoff_ms:
                    continue
            else:
                continue

            pools.append({
                "pool_address": (data.get(b"pool_address") or b"").decode(),
                "pool_type": (data.get(b"pool_type") or b"").decode(),
            })
        if cursor == 0:
            break
    return pools


def _resolve_tx_from(w3: Web3, tx_hash: str) -> str:
    """Fetch the actual user wallet address (tx.from) for a transaction."""
    try:
        tx = w3.eth.get_transaction(tx_hash)
        return str(tx["from"]).lower()
    except Exception:
        return ""


def _hex0x(v) -> str:
    """Normalize any bytes/HexBytes/str to lowercase 0x-prefixed hex.

    Rust ingestor writes hex with `0x` prefix; downstream ClickHouse MVs filter
    on `length(topic0)=66` and `topic0='0x...'`, so poller output must match.
    """
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        s = v.hex()
    else:
        s = str(v)
        if s.startswith("0x") or s.startswith("0X"):
            s = s[2:]
    s = s.lower()
    return ("0x" + s) if s else ""


def _topic_or_none(topics, idx) -> str:
    if idx >= len(topics):
        return None
    return _hex0x(topics[idx])


def _resolve_block_timestamp_hex(w3: Web3, block_number: int) -> str:
    """Fetch block timestamp as 0x hex string."""
    try:
        block_obj = w3.eth.get_block(block_number)
        ts = int(block_obj["timestamp"])
        return hex(ts)
    except Exception:
        return ""


def _build_ingest_record(
    log_entry: Dict,
    chain_id: int,
    tx_from: str = "",
    block_timestamp_hex: str = "",
) -> bytes:
    """Build IngestRecordV1-compatible msgpack payload from an eth_getLogs entry."""
    topics = log_entry.get("topics", [])
    payload_json = "{}"
    if block_timestamp_hex:
        payload_json = json.dumps(
            {
                "params": {
                    "result": {
                        "blockTimestamp": block_timestamp_hex,
                    }
                }
            },
            separators=(",", ":"),
        )

    record = {
        "version": 1,
        "chain_id": chain_id,
        "source": "discovery_poller",
        "subscription_id": None,
        "method": None,
        "received_at_ms": int(time.time() * 1000),
        "block_number": log_entry.get("blockNumber"),
        "block_hash": _hex0x(log_entry.get("blockHash")),
        "tx_hash": _hex0x(log_entry.get("transactionHash")),
        "tx_index": log_entry.get("transactionIndex"),
        "log_index": log_entry.get("logIndex"),
        "address": _hex0x(log_entry.get("address")),
        "topic0": _topic_or_none(topics, 0),
        "topic1": _topic_or_none(topics, 1),
        "topic2": _topic_or_none(topics, 2),
        "topic3": _topic_or_none(topics, 3),
        "data": _hex0x(log_entry.get("data")),
        "removed": log_entry.get("removed", False),
        "payload_json": payload_json,
        "tx_from": tx_from,
    }
    return msgpack.packb(record, use_bin_type=True)


_TRANSIENT_RPC = (
    "remotely disconnected",
    "remote end closed",
    "connection aborted",
    "connection reset",
    "broken pipe",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "503",
    "502",
    "429",
    "eof occurred",
    "reset by peer",
    "429 too many",
)


def _is_transient_rpc_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(n in text for n in _TRANSIENT_RPC)


def _rpc_urls() -> List[str]:
    primary = (
        os.environ.get("SWAP_POLLER_RPC_URL")
        or os.environ.get("EVM_HTTP_URL")
        or os.environ.get("EXECUTOR_RPC_URL")
        or "https://mainnet.base.org"
    )
    extras = os.environ.get("SWAP_POLLER_RPC_URL_FALLBACK", "")
    urls: List[str] = []
    for item in [primary, *extras.replace(";", ",").split(",")]:
        url = item.strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _make_w3(url: str) -> Web3:
    return Web3(
        Web3.HTTPProvider(
            url,
            request_kwargs={
                "timeout": 30,
                "headers": {
                    "User-Agent": "Mozilla/5.0 (compatible; EATLQ/1.0)",
                    "Content-Type": "application/json",
                },
            },
        )
    )


def _reset_w3_session(w3: Web3) -> None:
    provider = getattr(w3, "provider", None)
    if provider is None:
        return
    for attr in ("_request_session", "session"):
        session = getattr(provider, attr, None)
        if session is None:
            continue
        try:
            session.close()
        except Exception:
            pass
        try:
            import requests

            setattr(provider, attr, requests.Session())
        except Exception:
            pass


class ResilientRpc:
    """Rotate public HTTP endpoints after transient disconnects; keep a catch-up cursor."""

    def __init__(self, urls: List[str], retries: int = 3):
        if not urls:
            raise ValueError("no RPC urls")
        self.urls = urls
        self.retries = max(1, retries)
        self.idx = 0
        self.w3 = _make_w3(urls[0])

    def _rotate(self, reason: str) -> None:
        prev = self.urls[self.idx]
        self.idx = (self.idx + 1) % len(self.urls)
        nxt = self.urls[self.idx]
        log.warning("swap-poller RPC rotate %s -> %s (%s)", urlparse(prev).netloc, urlparse(nxt).netloc, reason)
        self.w3 = _make_w3(nxt)

    def call(self, fn: Callable[[Web3], Any]) -> Any:
        last_err: Optional[Exception] = None
        attempts = self.retries * max(1, len(self.urls))
        for attempt in range(1, attempts + 1):
            try:
                return fn(self.w3)
            except Exception as e:
                last_err = e
                transient = _is_transient_rpc_error(e)
                log.warning(
                    "swap-poller RPC fail attempt %d/%d transient=%s: %s",
                    attempt,
                    attempts,
                    transient,
                    e,
                )
                if transient:
                    _reset_w3_session(self.w3)
                    time.sleep(0.2 * min(attempt, 5))
                if attempt % self.retries == 0 or not transient:
                    self._rotate(str(e)[:80])
        raise last_err if last_err is not None else RuntimeError("swap-poller RPC failed")


def catchup_from(last: Optional[int], current: int, max_lookback: int) -> int:
    """Resume from the last completed block, capped so a long outage still backfills."""
    if current <= 0:
        return 0
    if last is None:
        return max(1, current - 5)
    return max(last, current - max_lookback)


def run() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    rpc_urls = _rpc_urls()
    rpc_url = rpc_urls[0]
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    stream = os.environ.get(
        "MARKET_TICKS_DISCOVERY_STREAM",
        os.environ.get("MARKET_TICKS_STREAM", "market_ticks_discovery"),
    )
    poll_interval = int(os.environ.get("SWAP_POLLER_INTERVAL_SEC", "300"))
    max_age_h = int(os.environ.get("SWAP_POLLER_POOL_MAX_AGE_H",
                                    os.environ.get("SMART_MONEY_POOL_MAX_AGE_H", "72")))
    maxlen = int(os.environ.get("MARKET_TICKS_DISCOVERY_MAXLEN",
                                os.environ.get("MARKET_TICKS_MAXLEN", "1000000")))

    r = redis.from_url(redis_url, decode_responses=False)
    retries = int(os.environ.get("SWAP_POLLER_RPC_RETRY", "3") or "3")
    catchup_blocks = int(os.environ.get("SWAP_POLLER_CATCHUP_BLOCKS", "400") or "400")
    rpc = ResilientRpc(rpc_urls, retries=retries)

    try:
        connected = rpc.call(lambda w3: w3.is_connected())
    except Exception as e:
        log.error("cannot connect RPC %s (%s)", rpc_url, e)
        sys.exit(1)
    if not connected:
        log.error("cannot connect RPC %s", rpc_url)
        sys.exit(1)

    log.info(json.dumps({
        "event": "swap_poller_started",
        "chain_id": chain_id,
        "poll_interval_sec": poll_interval,
        "max_pool_age_h": max_age_h,
        "stream": stream,
        "rpc_url_host": urlparse(rpc_url).netloc,
        "rpc_pool": [urlparse(u).netloc for u in rpc_urls],
        "catchup_blocks": catchup_blocks,
        "blue_chip_pools": len(BLUE_CHIP_POOLS),
    }, ensure_ascii=True))

    last_block: Dict[str, int] = {}
    last_pool_count_log = 0.0

    while True:
        cycle_start = time.time()
        try:
            pools = _get_discovered_pools(r, max_age_h=max_age_h)
            seen = {p["pool_address"].lower() for p in pools if p.get("pool_address")}
            for addr in BLUE_CHIP_POOLS:
                if addr not in seen:
                    pools.append({"pool_address": addr, "pool_type": "blue_chip"})
                    seen.add(addr)
            now_t = time.time()
            if now_t - last_pool_count_log > 300:
                log.info("monitoring %d discovered pools (max_age=%dh)", len(pools), max_age_h)
                last_pool_count_log = now_t
            if not pools:
                time.sleep(poll_interval)
                continue

            current_block = int(rpc.call(lambda w3: w3.eth.block_number))
            pool_addrs = [p["pool_address"] for p in pools if p["pool_address"]]

            batch_size = 20
            for i in range(0, len(pool_addrs), batch_size):
                batch = pool_addrs[i:i + batch_size]
                from_block = min(
                    catchup_from(last_block.get(a), current_block, catchup_blocks)
                    for a in batch
                )

                try:
                    cs_addrs = [Web3.to_checksum_address(a) for a in batch]

                    def _get_logs(w3, addrs=cs_addrs, start=from_block, end=current_block):
                        return w3.eth.get_logs({
                            "fromBlock": start,
                            "toBlock": end,
                            "address": addrs,
                            "topics": [ALL_SWAP_TOPICS],
                        })

                    logs = rpc.call(_get_logs)
                except Exception as e:
                    log.warning("eth_getLogs failed for batch after reconnect/rotate: %s", e)
                    time.sleep(2)
                    continue

                # Throttle between batches to avoid 429 on public RPCs
                if i + batch_size < len(pool_addrs):
                    time.sleep(0.5)

                published = 0
                tx_from_cache: Dict[str, str] = {}
                block_ts_cache: Dict[int, str] = {}
                for entry in logs:
                    try:
                        entry_dict = dict(entry)
                        raw_hash = entry_dict.get("transactionHash", b"")
                        tx_hash_str = raw_hash.hex() if isinstance(raw_hash, bytes) else str(raw_hash)
                        skip_tx_from = os.environ.get("SWAP_POLLER_SKIP_TX_FROM", "0") == "1"
                        if skip_tx_from:
                            tx_from = ""
                        else:
                            if tx_hash_str not in tx_from_cache:
                                tx_from_cache[tx_hash_str] = rpc.call(
                                    lambda w3, h=tx_hash_str: _resolve_tx_from(w3, h)
                                )
                            tx_from = tx_from_cache[tx_hash_str]
                        block_number = entry_dict.get("blockNumber")
                        block_ts_hex = ""
                        if isinstance(block_number, int) and block_number > 0:
                            if block_number not in block_ts_cache:
                                block_ts_cache[block_number] = rpc.call(
                                    lambda w3, n=block_number: _resolve_block_timestamp_hex(w3, n)
                                )
                            block_ts_hex = block_ts_cache[block_number]
                        if not block_ts_hex:
                            # Guard: do not silently publish rows that will land as block_time NULL.
                            log.warning("skip log due to missing block timestamp (tx=%s block=%s)", tx_hash_str, block_number)
                            continue
                        packed = _build_ingest_record(
                            entry_dict,
                            chain_id,
                            tx_from=tx_from,
                            block_timestamp_hex=block_ts_hex,
                        )
                        r.xadd(
                            stream,
                            {"data": packed, "tx_from": tx_from.encode()},
                            maxlen=maxlen,
                            approximate=True,
                        )
                        published += 1
                    except Exception as e:
                        log.debug("failed to publish log entry: %s", e)

                for a in batch:
                    last_block[a] = current_block + 1

                if published > 0:
                    log.info("polled %d swap events from %d pools (block %d-%d)",
                             published, len(batch), from_block, current_block)

        except Exception as e:
            log.error("poller cycle error: %s", e)

        # Sleep only the *remaining* interval so cycle cadence stays ~poll_interval
        # instead of poll_interval + cycle_work. This directly caps how far
        # max(block_time) can fall behind wall clock (the ch_swap_lag metric): with
        # ~1200 pools a full sweep already takes 15-30s, and adding a fixed 30s idle
        # on top pushed steady-state lag toward the watchdog threshold. If a cycle
        # runs long (RPC slow), we start the next one immediately (no negative sleep).
        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, poll_interval - elapsed))


if __name__ == "__main__":
    run()
