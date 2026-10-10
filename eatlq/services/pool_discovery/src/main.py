"""
Pool Discovery Service — consumes factory PairCreated/PoolCreated events from
market_ticks, decodes new pools, fetches token metadata via RPC, and registers
them in Redis + ClickHouse.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import clickhouse_connect
import msgpack
import redis
from web3 import Web3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pool_discovery")

WETH_BASE = "0x4200000000000000000000000000000000000006"

TOPIC_PAIR_CREATED_V2 = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
TOPIC_POOL_CREATED_V3 = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
TOPIC_POOL_CREATED_AERO = "0x2128d88d14c80cb081c1252a5acff7a264671bf199ce226b53571b3a693f7ef2"

FACTORY_UNISWAP_V2 = "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"
FACTORY_UNISWAP_V3 = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
FACTORY_AERO_V2 = "0x420dd381b31aef6683db6b902084cb0ffece40da"
FACTORY_AERO_CL = "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a"

FACTORY_POOL_TYPE = {
    FACTORY_UNISWAP_V2: "v2",
    FACTORY_UNISWAP_V3: "v3",
    FACTORY_AERO_V2: "aero_vamm",
    FACTORY_AERO_CL: "aero_cl",
}

ERC20_META_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
)

DISCOVERY_TOPICS = frozenset({
    TOPIC_PAIR_CREATED_V2, TOPIC_POOL_CREATED_V3, TOPIC_POOL_CREATED_AERO,
})

_block_ts_cache: Dict[int, int] = {}


class DiscoveredPoolsSink:
    """Insert discovered pools into ClickHouse for durable analytics."""

    COLUMN_NAMES = [
        "chain_id",
        "pool_address",
        "factory_address",
        "pool_type",
        "token0_address",
        "token0_symbol",
        "token0_decimals",
        "token1_address",
        "token1_symbol",
        "token1_decimals",
        "fee_bps",
        "has_weth_leg",
        "created_block",
        "created_tx_hash",
    ]

    def __init__(self, client: Any, table_name: str, database: str) -> None:
        self.client = client
        self.table_name = table_name
        self.database = database

    @classmethod
    def from_env(cls) -> Optional["DiscoveredPoolsSink"]:
        enabled = os.environ.get("POOL_DISCOVERY_ENABLE_CLICKHOUSE", "1").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            log.warning("clickhouse sink disabled by POOL_DISCOVERY_ENABLE_CLICKHOUSE")
            return None

        ch_url = (
            os.environ.get("CLICKHOUSE_URL")
            or os.environ.get("CLICKHOUSE_HTTP_URL")
            or "http://clickhouse:8123"
        )
        ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
        ch_user = os.environ.get("CLICKHOUSE_USER", "default")
        ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
        ch_table = os.environ.get("POOL_DISCOVERY_CLICKHOUSE_TABLE", "discovered_pools")

        parsed = urlparse(ch_url)
        host = parsed.hostname or "clickhouse"
        port = parsed.port or (8443 if parsed.scheme == "https" else 8123)
        secure = parsed.scheme == "https"

        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=ch_user,
            password=ch_password,
            database=ch_db,
            interface="http",
            secure=secure,
        )
        log.info("clickhouse sink enabled: %s.%s via %s:%d", ch_db, ch_table, host, port)
        return cls(client=client, table_name=ch_table, database=ch_db)

    def insert_pool(self, pool_data: Dict[str, str]) -> None:
        row = [
            int(pool_data["chain_id"]),
            pool_data["pool_address"],
            pool_data["factory_address"],
            pool_data["pool_type"],
            pool_data["token0_address"],
            pool_data["token0_symbol"],
            int(pool_data["token0_decimals"]),
            pool_data["token1_address"],
            pool_data["token1_symbol"],
            int(pool_data["token1_decimals"]),
            int(pool_data["fee_bps"]),
            int(pool_data["has_weth_leg"]),
            int(pool_data["created_block"]),
            pool_data["created_tx_hash"],
        ]
        self.client.insert(
            table=self.table_name,
            data=[row],
            column_names=self.COLUMN_NAMES,
            database=self.database,
        )


def _get_block_timestamp(w3: Web3, block_num: int) -> int:
    """Get block timestamp (seconds). Cached since blocks are immutable."""
    if block_num <= 0:
        return 0
    if block_num in _block_ts_cache:
        return _block_ts_cache[block_num]
    try:
        blk = w3.eth.get_block(block_num)
        ts = int(blk["timestamp"])
        _block_ts_cache[block_num] = ts
        if len(_block_ts_cache) > 10000:
            oldest = min(_block_ts_cache.keys())
            del _block_ts_cache[oldest]
        return ts
    except Exception:
        return 0


def _strip_0x(s: str) -> str:
    return s[2:] if s.startswith("0x") or s.startswith("0X") else s


def _hex_to_addr(hex32: str) -> str:
    cleaned = _strip_0x(hex32).lower()
    if len(cleaned) < 40:
        return ""
    return Web3.to_checksum_address("0x" + cleaned[-40:])


def _normalize_payload(raw: bytes) -> Dict[str, Any]:
    # Ingestor now writes named msgpack (to_vec_named) so decoded payload is
    # always a dict. Legacy positional-list encoding is no longer produced.
    unpacked = msgpack.unpackb(raw, raw=False)
    if isinstance(unpacked, dict):
        return unpacked
    raise ValueError("unsupported msgpack format")


def _fetch_token_meta(w3: Web3, addr: str) -> Dict[str, Any]:
    """Query on-chain symbol(), decimals(), name() for a token address."""
    cs = Web3.to_checksum_address(addr)
    contract = w3.eth.contract(address=cs, abi=ERC20_META_ABI)
    meta: Dict[str, Any] = {"address": addr.lower()}
    try:
        meta["symbol"] = contract.functions.symbol().call()
    except Exception:
        meta["symbol"] = "UNKNOWN"
    try:
        meta["decimals"] = int(contract.functions.decimals().call())
    except Exception:
        meta["decimals"] = 18
    try:
        meta["name"] = contract.functions.name().call()
    except Exception:
        meta["name"] = ""
    return meta


def _decode_pair_created_v2(data_hex: str, topic1: str, topic2: str) -> Optional[Dict]:
    """PairCreated(address indexed token0, address indexed token1, address pair, uint)"""
    token0 = _hex_to_addr(topic1)
    token1 = _hex_to_addr(topic2)
    cleaned = _strip_0x(data_hex)
    if len(cleaned) < 128:
        return None
    pair_addr = _hex_to_addr(cleaned[0:64])
    return {"token0": token0, "token1": token1, "pool_address": pair_addr, "fee_bps": 30}


def _decode_pool_created_v3(data_hex: str, topic1: str, topic2: str, topic3: str) -> Optional[Dict]:
    """PoolCreated(address indexed token0, address indexed token1, uint24 indexed fee, int24 tickSpacing, address pool)"""
    token0 = _hex_to_addr(topic1)
    token1 = _hex_to_addr(topic2)
    fee_raw = int(_strip_0x(topic3), 16) if topic3 else 0
    cleaned = _strip_0x(data_hex)
    if len(cleaned) < 128:
        return None
    pool_addr = _hex_to_addr(cleaned[64:128])
    return {"token0": token0, "token1": token1, "pool_address": pool_addr, "fee_bps": fee_raw // 100}


def _decode_pool_created_aero(data_hex: str, topic1: str, topic2: str) -> Optional[Dict]:
    """Aerodrome PoolCreated(address indexed token0, address indexed token1, bool stable, address pool, uint)"""
    token0 = _hex_to_addr(topic1)
    token1 = _hex_to_addr(topic2)
    cleaned = _strip_0x(data_hex)
    if len(cleaned) < 192:
        return None
    is_stable = int(cleaned[0:64], 16) != 0
    pool_addr = _hex_to_addr(cleaned[64:128])
    return {"token0": token0, "token1": token1, "pool_address": pool_addr,
            "fee_bps": 5 if is_stable else 30, "is_stable": is_stable}


def process_event(
    payload: Dict[str, Any],
    w3: Web3,
    r: redis.Redis,
    chain_id: int,
    pool_sink: Optional[DiscoveredPoolsSink] = None,
) -> Optional[str]:
    topic0 = str(payload.get("topic0", "")).lower().strip()
    if topic0 not in DISCOVERY_TOPICS:
        return None

    factory = str(payload.get("address", "")).lower().strip()
    pool_type = FACTORY_POOL_TYPE.get(factory)
    if pool_type is None:
        return None

    topic1 = str(payload.get("topic1", ""))
    topic2 = str(payload.get("topic2", ""))
    topic3 = str(payload.get("topic3", ""))
    data_hex = str(payload.get("data", ""))

    if topic0 == TOPIC_PAIR_CREATED_V2:
        decoded = _decode_pair_created_v2(data_hex, topic1, topic2)
    elif topic0 == TOPIC_POOL_CREATED_V3:
        decoded = _decode_pool_created_v3(data_hex, topic1, topic2, topic3)
    elif topic0 == TOPIC_POOL_CREATED_AERO:
        decoded = _decode_pool_created_aero(data_hex, topic1, topic2)
        if decoded and factory == FACTORY_AERO_CL:
            pool_type = "aero_cl"
    else:
        return None

    if decoded is None:
        return None

    pool_addr = decoded["pool_address"].lower()
    existing = r.exists(f"pool:discovered:{pool_addr}")
    if existing:
        return None

    t0_addr = decoded["token0"].lower()
    t1_addr = decoded["token1"].lower()
    has_weth = t0_addr == WETH_BASE.lower() or t1_addr == WETH_BASE.lower()

    t0_meta = _fetch_token_meta(w3, t0_addr)
    t1_meta = _fetch_token_meta(w3, t1_addr)

    block_num = int(payload.get("block_number") or 0)
    tx_hash = str(payload.get("tx_hash", ""))
    created_at_sec = _get_block_timestamp(w3, block_num)

    pool_data = {
        "chain_id": str(chain_id),
        "pool_address": pool_addr,
        "factory_address": factory,
        "pool_type": pool_type,
        "token0_address": t0_addr,
        "token0_symbol": t0_meta["symbol"],
        "token0_decimals": str(t0_meta["decimals"]),
        "token1_address": t1_addr,
        "token1_symbol": t1_meta["symbol"],
        "token1_decimals": str(t1_meta["decimals"]),
        "fee_bps": str(decoded["fee_bps"]),
        "has_weth_leg": "1" if has_weth else "0",
        "created_block": str(block_num),
        "created_at_sec": str(created_at_sec),
        "created_tx_hash": tx_hash,
        "discovered_at_ms": str(int(time.time() * 1000)),
    }

    if pool_sink is not None:
        try:
            pool_sink.insert_pool(pool_data)
        except Exception as exc:
            # Do not mark pool as discovered in Redis if CH insert failed.
            # This keeps the event retryable via stream replay/re-bootstrap.
            log.error("clickhouse insert failed for pool %s: %s", pool_addr, exc)
            return None

    r.hset(f"pool:discovered:{pool_addr}", mapping=pool_data)
    r.expire(f"pool:discovered:{pool_addr}", 86400 * 7)

    r.xadd("pool_events", {"pool_json": json.dumps(pool_data, ensure_ascii=True)}, maxlen=10000)

    pair_label = f"{t0_meta['symbol']}/{t1_meta['symbol']}"
    weth_tag = " [WETH]" if has_weth else ""
    log.info("NEW POOL: %s %s %s fee=%dbps block=%d%s",
             pool_type, pair_label, pool_addr[:16], decoded["fee_bps"], block_num, weth_tag)

    return pool_addr


def _hex(val: Any) -> str:
    """Convert HexBytes or bytes to 0x-prefixed hex string."""
    if isinstance(val, bytes):
        return "0x" + val.hex()
    s = str(val)
    return s if s.startswith("0x") else "0x" + s


def _process_log_entry(
    entry: Any,
    w3: Web3,
    r: redis.Redis,
    chain_id: int,
    pool_sink: Optional[DiscoveredPoolsSink] = None,
) -> Optional[str]:
    """Convert a web3 log entry dict into a payload and run process_event."""
    topics = entry.get("topics", [])
    payload = {
        "topic0": _hex(topics[0]) if topics else "",
        "topic1": _hex(topics[1]) if len(topics) > 1 else "",
        "topic2": _hex(topics[2]) if len(topics) > 2 else "",
        "topic3": _hex(topics[3]) if len(topics) > 3 else "",
        "data": _hex(entry.get("data", b"")),
        "address": str(entry.get("address", "")).lower(),
        "block_number": entry.get("blockNumber", 0),
        "tx_hash": _hex(entry.get("transactionHash", b"")),
    }
    return process_event(payload, w3, r, chain_id, pool_sink=pool_sink)


FACTORY_TOPIC_MAP = {
    FACTORY_UNISWAP_V2: TOPIC_PAIR_CREATED_V2,
    FACTORY_UNISWAP_V3: TOPIC_POOL_CREATED_V3,
    FACTORY_AERO_V2: TOPIC_PAIR_CREATED_V2,
    FACTORY_AERO_CL: TOPIC_POOL_CREATED_AERO,
}


def _bootstrap_recent_pools(
    w3: Web3,
    r: redis.Redis,
    chain_id: int,
    lookback_blocks: int = 43200,
    pool_sink: Optional[DiscoveredPoolsSink] = None,
) -> int:
    """Directly query RPC for factory events in the last N blocks (~24h at 2s/block)."""
    current = w3.eth.block_number
    from_block = max(1, current - lookback_blocks)
    found = 0

    for factory_addr, topic0 in FACTORY_TOPIC_MAP.items():
        cs_addr = Web3.to_checksum_address(factory_addr)
        chunk = 2000
        blk = from_block
        factory_count = 0
        while blk <= current:
            end = min(blk + chunk - 1, current)
            try:
                logs = w3.eth.get_logs({
                    "fromBlock": blk,
                    "toBlock": end,
                    "address": [cs_addr],
                    "topics": [[topic0]],
                })
            except Exception as e:
                log.warning("bootstrap %s %d-%d failed: %s", factory_addr[:10], blk, end, e)
                chunk = max(chunk // 2, 200)
                continue
            for entry in logs:
                result = _process_log_entry(entry, w3, r, chain_id, pool_sink=pool_sink)
                if result:
                    factory_count += 1
            blk = end + 1
        log.info("bootstrap %s: %d pools from blocks %d-%d", factory_addr[:10], factory_count, from_block, current)
        found += factory_count

    log.info("bootstrap total: discovered %d new pools", found)
    return found


def run() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("EXECUTOR_RPC_URL", "https://mainnet.base.org")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    stream = os.environ.get("MARKET_TICKS_STREAM", "market_ticks")
    group = os.environ.get("POOL_DISCOVERY_GROUP", "pool_discovery_group")
    consumer = os.environ.get("POOL_DISCOVERY_CONSUMER", "discovery-1")
    # Re-bootstrap every N seconds (default 12h). Keeps pool list fresh even when
    # factory creation events are not flowing through market_ticks.
    rebootstrap_interval = int(os.environ.get("POOL_DISCOVERY_REBOOTSTRAP_SEC", str(12 * 3600)))
    # Lookback window for each bootstrap (default 24h = 43200 blocks at 2s/block)
    bootstrap_blocks = int(os.environ.get("POOL_DISCOVERY_BOOTSTRAP_BLOCKS", "43200"))

    r = redis.from_url(redis_url, decode_responses=False)
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    pool_sink = DiscoveredPoolsSink.from_env()

    if not w3.is_connected():
        log.error("cannot connect RPC %s", rpc_url)
        sys.exit(1)

    if bootstrap_blocks > 0:
        _bootstrap_recent_pools(
            w3,
            r,
            chain_id,
            lookback_blocks=bootstrap_blocks,
            pool_sink=pool_sink,
        )
    last_bootstrap_ts = time.time()

    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
        log.info("created consumer group %s on %s", group, stream)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    log.info(json.dumps({
        "event": "pool_discovery_started",
        "chain_id": chain_id,
        "rpc": rpc_url[:40] + "…",
        "stream": stream,
        "rebootstrap_interval_h": round(rebootstrap_interval / 3600, 1),
    }, ensure_ascii=True))

    total_discovered = 0
    while True:
        # Periodic re-bootstrap: re-scan recent blocks to pick up new pools even
        # when factory creation events are absent from the market_ticks stream.
        if rebootstrap_interval > 0 and (time.time() - last_bootstrap_ts) >= rebootstrap_interval:
            log.info("periodic re-bootstrap triggered (interval=%dh)", rebootstrap_interval // 3600)
            _bootstrap_recent_pools(
                w3,
                r,
                chain_id,
                lookback_blocks=bootstrap_blocks,
                pool_sink=pool_sink,
            )
            last_bootstrap_ts = time.time()

        try:
            resp = r.xreadgroup(group, consumer, {stream: ">"}, count=100, block=2000)
        except redis.ResponseError as e:
            log.error("xreadgroup error: %s", e)
            time.sleep(1)
            continue

        if not resp:
            continue

        ack_ids = []
        for stream_name, messages in resp:
            for msg_id, fields in messages:
                ack_ids.append(msg_id)
                raw = (
                    fields.get(b"data")
                    or fields.get(b"payload_msgpack")
                    or fields.get(b"payload")
                    or fields.get(b"tick")
                )
                if raw is None:
                    continue
                try:
                    payload = _normalize_payload(raw)
                    result = process_event(payload, w3, r, chain_id, pool_sink=pool_sink)
                    if result:
                        total_discovered += 1
                except Exception:
                    pass

        if ack_ids:
            r.xack(stream, group, *ack_ids)


if __name__ == "__main__":
    run()
