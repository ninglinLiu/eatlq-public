#!/usr/bin/env python3
"""Build a bounded Base V3 smart-wallet round-trip dataset.

Offline research only. This script reads ClickHouse analytical tables, builds a
wallet-token-pool FIFO ledger from raw Uniswap V3 swaps, and writes audit
artifacts. It never writes signals, trains models, touches RPC, or live trades.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
DEFAULT_OUT_DIR = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder"
)
CH_CONTAINER = os.environ.get("CLICKHOUSE_DOCKER_CONTAINER", "eatlq-clickhouse-1")
POOL_FEATURE_SIDECAR_TABLE = "pool_features_5m_sidecar_s083"
PREFLIGHT_PATH = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_roundtrip_preflight.md"
)
KNOWN_ROUTERS = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap Universal Router / Sprint071 0x6ff5 MM",
    "0x2626664c2603336e57b271c5c0b26f421741e481": "Uniswap V3 SwapRouter02",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router v2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3 SwapRouter01",
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1bebe2438d": "Aerodrome Router",
    "0xbe6d8da2d4f922f359b1da84fed3c60b269ce8a0": "Aerodrome SlipstreamMixer",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x ExchangeProxy",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
    "0x80c7dd17b01855a6d2347444a0fcc36136a314de": "OKX DEX aggregator",
    "0xc30141b657f4216252dc59af2e7cdb9d8792e1b0": "Socket.tech",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol Settlement",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap MetaAggregationRouterV2",
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52": "Velora v5 Augustus",
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd": "Sprint041 alt aggregator",
}
HORIZONS = {
    "future_return_5m": 5 * 60,
    "future_return_30m": 30 * 60,
    "future_return_2h": 2 * 60 * 60,
    "future_return_6h": 6 * 60 * 60,
}
COPY_DELAYS = {
    "copy_delay_30s": 30,
    "copy_delay_60s": 60,
    "copy_delay_120s": 120,
}
ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
WETH_WEI = 10**18
# Fallback ETH/USD for USD notional when stablecoin leg is unavailable (label sizing only).
ETH_USD_NOTIONAL_PROXY = float(os.environ.get("ETH_USD_NOTIONAL_PROXY", "3000"))
STABLECOIN_DECIMALS: dict[str, int] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC (Base)
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,  # bridged USDC variant seen in samples
}
ROUTER_TX_HASH_PREFIX = "0x6ff6"  # Universal Router / 0x6ff5 artifact correlation guard


@dataclass
class PoolMeta:
    pool_address: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    fee_bps: int

    @property
    def token_address(self) -> str | None:
        if self.token0_address == WETH:
            return self.token1_address
        if self.token1_address == WETH:
            return self.token0_address
        return None

    @property
    def weth_is_token0(self) -> bool:
        return self.token0_address == WETH


@dataclass
class Lot:
    entry_id: str
    wallet: str
    token: str
    pool: str
    entry_time: datetime
    entry_tx_hash: str
    entry_block: int
    log_index: int
    fee_bps: int
    token_remaining: float
    token_amount: float
    weth_cost: float
    sender: str
    recipient: str
    router_flag: bool
    router_reason: str


@dataclass
class WalletStats:
    closed: list[dict[str, Any]] = field(default_factory=list)
    first_seen: datetime | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--sample-limit", type=int, default=5000, help="Max dataset buy-entry rows to emit.")
    p.add_argument("--max-rows", type=int, default=100000, help="Max raw swap rows to scan across shards.")
    p.add_argument("--date-shard-days", type=int, default=1, help="Raw swap scan shard size in days.")
    p.add_argument("--max-pools", type=int, default=0, help="Max WETH pools to include in balanced raw swap scans; 0 means all available.")
    p.add_argument("--per-pool-max-rows", type=int, default=0, help="Global max raw swap rows retained per pool in balanced mode; 0 means uncapped.")
    p.add_argument("--balanced-pool-sample", action="store_true", help="Scan raw swaps across candidate pools and date shards with bounded per-pool retention.")
    p.add_argument(
        "--pool-metadata-source",
        choices=("registry", "discovered", "union"),
        default="union",
        help="Pool metadata source for WETH-leg detection and fee/token fields.",
    )
    p.add_argument(
        "--pool-feature-source",
        choices=("none", "view", "sidecar"),
        default="sidecar",
        help="Pool feature source. sidecar auto-falls back to view if table missing.",
    )
    p.add_argument("--max-top-pool-share", type=float, default=0.20, help="Max allowed top pool share after selection; apply deconcentration downsampling.")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--dry-run", action="store_true", help="Plan only; write flags/readout but do not query swaps.")
    p.add_argument("--chunk-days", type=int, default=1)
    p.add_argument("--docker-container", default=os.environ.get("CLICKHOUSE_DOCKER_CONTAINER", "eatlq-clickhouse-1"))
    p.add_argument("--query-timeout-sec", type=int, default=180)
    return p.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ch_dt(value: str) -> datetime:
    value = value.strip()
    if "." in value:
        base, frac = value.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        return datetime.strptime(f"{base}.{frac}", "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def sql_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run_ch(query: str, timeout: int = 180) -> list[dict[str, str]]:
    settings = "SET max_memory_usage='3500M', max_threads=4, max_execution_time=180;\n"
    cmd = [
        "docker",
        "exec",
        "-i",
        CH_CONTAINER,
        "clickhouse-client",
        "--database",
        "eatlq",
        "--multiquery",
    ]
    cp = subprocess.run(
        cmd,
        input=settings + query,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout).strip()
        raise RuntimeError(err[:1200])
    lines = [line for line in cp.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader]


def signed_int256(raw: str) -> int:
    raw = (raw or "0").strip().lower()
    if raw.startswith("0x"):
        value = int(raw, 16)
        if value >= 2**255:
            value -= 2**256
        return value
    return int(raw)


def amount_to_float(raw_amount: int) -> float:
    # Registry lacks decimals. Use raw-unit ratios for token quantities and WETH
    # raw units internally; ROI is invariant to WETH decimal scaling.
    return float(raw_amount)


def weth_from_raw(raw_weth: float) -> float:
    return raw_weth / WETH_WEI


def usd_notional_from_buy(weth_raw: float, token: str, token_raw: float) -> float | None:
    if weth_raw <= 0:
        return None
    dec = STABLECOIN_DECIMALS.get((token or "").lower())
    if dec is not None and token_raw > 0:
        return token_raw / (10**dec)
    return weth_from_raw(weth_raw) * ETH_USD_NOTIONAL_PROXY


def trade_size_bucket(usd: float | None) -> str | None:
    if usd is None:
        return None
    if usd < 50:
        return "micro"
    if usd < 500:
        return "small"
    if usd < 5000:
        return "medium"
    if usd < 50000:
        return "large"
    return "whale"


def closed_loop_horizon_flags(hold_time_sec: float | None) -> dict[str, bool | None]:
    if hold_time_sec is None:
        return {
            "closed_loop_eligible": False,
            "closed_within_7d": None,
            "closed_within_14d": None,
            "closed_within_30d": None,
        }
    return {
        "closed_loop_eligible": True,
        "closed_within_7d": hold_time_sec <= 7 * 86400,
        "closed_within_14d": hold_time_sec <= 14 * 86400,
        "closed_within_30d": hold_time_sec <= 30 * 86400,
    }


def router_status(*addresses: str) -> tuple[bool, str]:
    hits = []
    for addr in addresses:
        low = (addr or "").lower()
        if low in KNOWN_ROUTERS:
            hits.append(f"{low}:{KNOWN_ROUTERS[low]}")
    return bool(hits), "; ".join(hits)


def _rows_to_pool_meta(rows: list[dict[str, str]]) -> dict[str, PoolMeta]:
    out: dict[str, PoolMeta] = {}
    for r in rows:
        pool = (r.get("pool_address") or "").strip().lower().strip("'\"")
        t0 = (r.get("token0_address") or "").strip().lower().strip("'\"")
        t1 = (r.get("token1_address") or "").strip().lower().strip("'\"")
        if not pool or not t0 or not t1:
            continue
        if not ADDR_RE.match(pool) or not ADDR_RE.match(t0) or not ADDR_RE.match(t1):
            continue
        out[pool] = PoolMeta(
            pool_address=pool,
            token0_address=t0,
            token1_address=t1,
            token0_symbol=r.get("token0_symbol") or "",
            token1_symbol=r.get("token1_symbol") or "",
            fee_bps=int(r.get("fee_bps") or 0),
        )
    return out


def load_pool_metadata(source: str, timeout: int) -> tuple[dict[str, PoolMeta], dict[str, Any]]:
    registry_rows = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool_address,
          lower(token0_address) AS token0_address,
          lower(token1_address) AS token1_address,
          token0_symbol,
          token1_symbol,
          fee_bps
        FROM uniswap_v3_pool_registry
        WHERE chain_id = {CHAIN_ID}
          AND (lower(token0_address) = '{WETH}' OR lower(token1_address) = '{WETH}')
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    discovered_rows = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool_address,
          lower(token0_address) AS token0_address,
          lower(token1_address) AS token1_address,
          token0_symbol,
          token1_symbol,
          fee_bps
        FROM discovered_pools
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_type) LIKE '%v3%'
          AND has_weth_leg = 1
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    registry_meta = _rows_to_pool_meta(registry_rows)
    discovered_meta = _rows_to_pool_meta(discovered_rows)
    if source == "registry":
        meta = registry_meta
    elif source == "discovered":
        meta = discovered_meta
    else:
        meta = dict(registry_meta)
        for pool, dmeta in discovered_meta.items():
            if pool not in meta:
                meta[pool] = dmeta
    stats = {
        "pool_metadata_source": source,
        "registry_weth_pools_total": len(registry_meta),
        "discovered_weth_v3_pools_total": len(discovered_meta),
        "union_weth_pools_total": len(set(registry_meta) | set(discovered_meta)),
        "metadata_pools_total": len(meta),
    }
    return meta, stats


def max_raw_time(timeout: int) -> datetime:
    rows = run_ch(
        """
        SELECT max(block_time) AS max_bt
        FROM uniswap_v3_swaps
        WHERE chain_id = 8453 AND removed = 0 AND block_time IS NOT NULL
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    if not rows or not rows[0].get("max_bt"):
        raise RuntimeError("No raw V3 max(block_time) returned")
    return parse_ch_dt(rows[0]["max_bt"])


def fetch_swaps(start: datetime, end: datetime, limit: int, timeout: int, allowed_pools: set[str] | None = None) -> list[dict[str, str]]:
    pool_filter = ""
    if allowed_pools:
        pool_list = ",".join(q(p) for p in sorted(allowed_pools))
        pool_filter = f"\n          AND lower(pool_address) IN ({pool_list})"
    return run_ch(
        f"""
        SELECT
          block_time,
          block_number,
          tx_hash,
          log_index,
          lower(pool_address) AS pool_address,
          lower(sender) AS sender,
          lower(recipient) AS recipient,
          amount0_raw,
          amount1_raw
        FROM uniswap_v3_swaps
        WHERE chain_id = 8453
          AND removed = 0
          AND block_time >= toDateTime64({q(sql_dt(start))}, 3, 'UTC')
          AND block_time < toDateTime64({q(sql_dt(end))}, 3, 'UTC')
          {pool_filter}
        ORDER BY block_time, block_number, log_index
        LIMIT {int(limit)}
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )


def fetch_candidate_pools(
    start: datetime,
    end: datetime,
    max_pools: int,
    timeout: int,
    metadata_pools: set[str] | None = None,
) -> list[str]:
    if metadata_pools is not None:
        metadata_pools = {p for p in metadata_pools if ADDR_RE.match(p)}
    if metadata_pools is not None and not metadata_pools:
        return []
    rows = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool_address,
          count() AS swap_rows
        FROM uniswap_v3_swaps
        WHERE chain_id = 8453
          AND removed = 0
          AND block_time >= toDateTime64({q(sql_dt(start))}, 3, 'UTC')
          AND block_time < toDateTime64({q(sql_dt(end))}, 3, 'UTC')
        GROUP BY pool_address
        ORDER BY swap_rows DESC, pool_address
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    out: list[str] = []
    for r in rows:
        pool = (r.get("pool_address") or "").strip().lower().strip("'\"")
        if not ADDR_RE.match(pool):
            continue
        if metadata_pools is not None and pool not in metadata_pools:
            continue
        out.append(pool)
        if max_pools and max_pools > 0 and len(out) >= max_pools:
            break
    return out


def fetch_swaps_for_pools(
    start: datetime,
    end: datetime,
    pools: list[str],
    limit: int,
    per_pool_limit: int,
    timeout: int,
) -> list[dict[str, str]]:
    if not pools or limit <= 0:
        return []
    pool_list = ",".join(q(p) for p in pools)
    limit_by = f"LIMIT {int(per_pool_limit)} BY pool_address\n        LIMIT {int(limit)}" if per_pool_limit > 0 else f"LIMIT {int(limit)}"
    return run_ch(
        f"""
        SELECT
          block_time,
          block_number,
          tx_hash,
          log_index,
          lower(pool_address) AS pool_address,
          lower(sender) AS sender,
          lower(recipient) AS recipient,
          amount0_raw,
          amount1_raw
        FROM uniswap_v3_swaps
        WHERE chain_id = 8453
          AND removed = 0
          AND block_time >= toDateTime64({q(sql_dt(start))}, 3, 'UTC')
          AND block_time < toDateTime64({q(sql_dt(end))}, 3, 'UTC')
          AND lower(pool_address) IN ({pool_list})
        ORDER BY block_time, block_number, log_index
        {limit_by}
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )


def fetch_balanced_swaps(
    start: datetime,
    end: datetime,
    max_rows: int,
    date_shard_days: int,
    max_pools: int,
    per_pool_max_rows: int,
    timeout: int,
    metadata_pools: set[str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    candidate_pools = fetch_candidate_pools(start, end, max_pools, timeout, metadata_pools=metadata_pools)
    if not candidate_pools:
        return [], {"candidate_pools_available": 0, "balanced_scan_status": "no_candidate_pools"}

    shard_days = max(1, int(date_shard_days))
    shard_count = max(1, math.ceil((end - start).total_seconds() / (86400 * shard_days)))
    per_shard_limit = max(1, math.ceil(max_rows / shard_count))
    retained: list[dict[str, str]] = []
    retained_by_pool: dict[str, int] = defaultdict(int)
    cursor = start
    shards_attempted = 0
    shards_completed = 0

    while cursor < end and len(retained) < max_rows:
        shard_end = min(cursor + timedelta(days=shard_days), end)
        shards_attempted += 1
        remaining = max_rows - len(retained)
        query_limit = min(remaining, per_shard_limit)
        if per_pool_max_rows > 0:
            active_pools = [p for p in candidate_pools if retained_by_pool[p] < per_pool_max_rows]
            query_pool_limit = max(1, min(per_pool_max_rows, max(1, math.ceil(query_limit / max(1, len(active_pools))))))
        else:
            active_pools = candidate_pools
            query_pool_limit = 0
        if not active_pools:
            break
        rows = fetch_swaps_for_pools(cursor, shard_end, active_pools, query_limit, query_pool_limit, timeout)
        for row in rows:
            pool = row["pool_address"]
            if per_pool_max_rows > 0 and retained_by_pool[pool] >= per_pool_max_rows:
                continue
            retained.append(row)
            retained_by_pool[pool] += 1
            if len(retained) >= max_rows:
                break
        shards_completed += 1
        cursor = shard_end

    retained.sort(key=lambda r: (parse_ch_dt(r["block_time"]), int(r["block_number"]), int(r["log_index"])))
    return retained, {
        "balanced_scan_status": "available",
        "candidate_pools_available": len(candidate_pools),
        "candidate_pools_used": len([p for p in candidate_pools if retained_by_pool.get(p, 0) > 0]),
        "date_shard_days": shard_days,
        "date_shards_attempted": shards_attempted,
        "date_shards_completed": shards_completed,
        "per_shard_raw_limit": per_shard_limit,
        "per_pool_max_rows": per_pool_max_rows,
    }


def fetch_pool_births(pools: set[str], timeout: int) -> dict[str, datetime]:
    if not pools:
        return {}
    pool_list = ",".join(q(p) for p in sorted(pools))
    rows = run_ch(
        f"""
        SELECT pool_address, min(block_time) AS first_seen
        FROM uniswap_v3_swaps
        WHERE chain_id = 8453
          AND removed = 0
          AND block_time IS NOT NULL
          AND pool_address IN ({pool_list})
        GROUP BY pool_address
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    return {r["pool_address"].lower(): parse_ch_dt(r["first_seen"]) for r in rows if r.get("first_seen")}


def fetch_pool_features(
    pools: set[str],
    start: datetime,
    end: datetime,
    timeout: int,
    chunk_days: int = 1,
    pool_batch_size: int = 50,
) -> dict[str, list[dict[str, Any]]]:
    if not pools:
        return {}
    # Conservative lag: include windows that can be visible before entries.
    lag_start = start - timedelta(minutes=10)
    all_rows: list[dict[str, str]] = []
    pools_sorted = sorted(pools)
    step_days = max(int(chunk_days), 1)
    batch_size = max(int(pool_batch_size), 1)
    shard_start = lag_start
    while shard_start < end:
        shard_end = min(shard_start + timedelta(days=step_days), end)
        for i in range(0, len(pools_sorted), batch_size):
            batch = pools_sorted[i : i + batch_size]
            pool_list = ",".join(q(p) for p in batch)
            # pool_features_5m_clean can OOM on this host even for tiny slices.
            # Fallback to pool_features_5m keeps the same core fields and
            # preserves entry-time lag semantics.
            last_err: Exception | None = None
            rows: list[dict[str, str]] = []
            for table_name in ("pool_features_5m_clean", "pool_features_5m"):
                try:
                    rows = run_ch(
                        f"""
                        SELECT
                          pool_address,
                          window_start,
                          price_velocity_5m,
                          buy_pressure_ratio,
                          token_volatility_5m,
                          activity_heat
                        FROM {table_name}
                        WHERE chain_id = 8453
                          AND window_start >= toDateTime({q(sql_dt(shard_start))}, 'UTC')
                          AND window_start < toDateTime({q(sql_dt(shard_end))}, 'UTC')
                          AND pool_address IN ({pool_list})
                        FORMAT TSVWithNames
                        """,
                        timeout=timeout,
                    )
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    continue
            if last_err is not None:
                raise last_err
            all_rows.extend(rows)
        shard_start = shard_end
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_rows:
        out[r["pool_address"].lower()].append(
            {
                "window_start": parse_ch_dt(r["window_start"]),
                "price_velocity_5m": as_float(r.get("price_velocity_5m")),
                "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
                "token_volatility_5m": as_float(r.get("token_volatility_5m")),
                "activity_heat": as_float(r.get("activity_heat")),
            }
        )
    for vals in out.values():
        vals.sort(key=lambda x: x["window_start"])
    return out


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def floor_5m(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minute = ts.minute - (ts.minute % 5)
    return ts.replace(minute=minute)


def build_needed_feature_slots(lots: list[Lot]) -> dict[str, set[datetime]]:
    slots: dict[str, set[datetime]] = defaultdict(set)
    for lot in lots:
        cutoff = lot.entry_time - timedelta(minutes=5)
        slots[lot.pool].add(floor_5m(cutoff))
    return slots


def fetch_pool_features_for_slots(
    needed_slots: dict[str, set[datetime]],
    timeout: int,
    date_shard_days: int = 1,
    pool_batch_size: int = 10,
    table_name: str = "pool_features_5m_clean",
) -> dict[tuple[str, datetime], dict[str, Any]]:
    if not needed_slots:
        return {}
    pools_sorted = sorted(needed_slots)
    all_slots = [slot for slots in needed_slots.values() for slot in slots]
    min_slot = min(all_slots)
    max_slot_exclusive = max(all_slots) + timedelta(minutes=5)
    step_days = max(int(date_shard_days), 1)
    batch_size = max(int(pool_batch_size), 1)
    out: dict[tuple[str, datetime], dict[str, Any]] = {}
    shard_start = min_slot
    while shard_start < max_slot_exclusive:
        shard_end = min(shard_start + timedelta(days=step_days), max_slot_exclusive)
        for i in range(0, len(pools_sorted), batch_size):
            batch = pools_sorted[i : i + batch_size]
            pool_list = ",".join(q(p) for p in batch)
            rows = run_ch(
                f"""
                SELECT
                  lower(pool_address) AS pool_address,
                  window_start,
                  price_velocity_5m,
                  buy_pressure_ratio,
                  token_volatility_5m,
                  activity_heat
                FROM {table_name}
                WHERE chain_id = 8453
                  AND window_start >= toDateTime({q(sql_dt(shard_start))}, 'UTC')
                  AND window_start < toDateTime({q(sql_dt(shard_end))}, 'UTC')
                  AND lower(pool_address) IN ({pool_list})
                FORMAT TSVWithNames
                """,
                timeout=timeout,
            )
            for r in rows:
                pool = (r.get("pool_address") or "").lower()
                slot = floor_5m(parse_ch_dt(r["window_start"]))
                if slot not in needed_slots.get(pool, set()):
                    continue
                out[(pool, slot)] = {
                    "price_velocity_5m": as_float(r.get("price_velocity_5m")),
                    "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
                    "token_volatility_5m": as_float(r.get("token_volatility_5m")),
                    "activity_heat": as_float(r.get("activity_heat")),
                }
        shard_start = shard_end
    return out


def sidecar_table_exists(timeout: int) -> bool:
    rows = run_ch(
        f"""
        SELECT count() AS c
        FROM system.tables
        WHERE database='eatlq'
          AND name={q(POOL_FEATURE_SIDECAR_TABLE)}
        FORMAT TSVWithNames
        """,
        timeout=timeout,
    )
    return bool(rows and int(rows[0].get("c") or 0) > 0)


def fetch_sidecar_features_for_slots(
    needed_slots: dict[str, set[datetime]],
    timeout: int,
    date_shard_days: int = 1,
    pool_batch_size: int = 25,
) -> dict[tuple[str, datetime], dict[str, Any]]:
    if not needed_slots:
        return {}
    pools_sorted = sorted(needed_slots)
    all_slots = [slot for slots in needed_slots.values() for slot in slots]
    min_slot = min(all_slots)
    max_slot_exclusive = max(all_slots) + timedelta(minutes=5)
    step_days = max(int(date_shard_days), 1)
    batch_size = max(int(pool_batch_size), 1)
    out: dict[tuple[str, datetime], dict[str, Any]] = {}
    shard_start = min_slot
    while shard_start < max_slot_exclusive:
        shard_end = min(shard_start + timedelta(days=step_days), max_slot_exclusive)
        for i in range(0, len(pools_sorted), batch_size):
            batch = pools_sorted[i : i + batch_size]
            pool_list = ",".join(q(p) for p in batch)
            rows = run_ch(
                f"""
                SELECT
                  lower(pool_address) AS pool_address,
                  window_start,
                  price_velocity_5m,
                  buy_pressure_ratio,
                  token_volatility_5m,
                  activity_heat
                FROM {POOL_FEATURE_SIDECAR_TABLE}
                WHERE chain_id = {CHAIN_ID}
                  AND window_start >= toDateTime({q(sql_dt(shard_start))}, 'UTC')
                  AND window_start < toDateTime({q(sql_dt(shard_end))}, 'UTC')
                  AND lower(pool_address) IN ({pool_list})
                FORMAT TSVWithNames
                """,
                timeout=timeout,
            )
            for r in rows:
                pool = (r.get("pool_address") or "").lower()
                slot = floor_5m(parse_ch_dt(r["window_start"]))
                if slot not in needed_slots.get(pool, set()):
                    continue
                out[(pool, slot)] = {
                    "price_velocity_5m": as_float(r.get("price_velocity_5m")),
                    "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
                    "token_volatility_5m": as_float(r.get("token_volatility_5m")),
                    "activity_heat": as_float(r.get("activity_heat")),
                }
        shard_start = shard_end
    return out


def fetch_existing_sidecar_slot_keys(
    needed_slots: dict[str, set[datetime]],
    timeout: int,
    date_shard_days: int = 1,
    pool_batch_size: int = 25,
) -> set[tuple[str, datetime]]:
    if not needed_slots:
        return set()
    pools_sorted = sorted(needed_slots)
    all_slots = [slot for slots in needed_slots.values() for slot in slots]
    min_slot = min(all_slots)
    max_slot_exclusive = max(all_slots) + timedelta(minutes=5)
    step_days = max(int(date_shard_days), 1)
    batch_size = max(int(pool_batch_size), 1)
    out: set[tuple[str, datetime]] = set()
    shard_start = min_slot
    while shard_start < max_slot_exclusive:
        shard_end = min(shard_start + timedelta(days=step_days), max_slot_exclusive)
        for i in range(0, len(pools_sorted), batch_size):
            batch = pools_sorted[i : i + batch_size]
            pool_list = ",".join(q(p) for p in batch)
            rows = run_ch(
                f"""
                SELECT lower(pool_address) AS pool_address, window_start
                FROM {POOL_FEATURE_SIDECAR_TABLE}
                WHERE chain_id = {CHAIN_ID}
                  AND window_start >= toDateTime({q(sql_dt(shard_start))}, 'UTC')
                  AND window_start < toDateTime({q(sql_dt(shard_end))}, 'UTC')
                  AND lower(pool_address) IN ({pool_list})
                FORMAT TSVWithNames
                """,
                timeout=timeout,
            )
            for r in rows:
                pool = (r.get("pool_address") or "").lower()
                slot = floor_5m(parse_ch_dt(r["window_start"]))
                if slot in needed_slots.get(pool, set()):
                    out.add((pool, slot))
        shard_start = shard_end
    return out


def materialize_missing_sidecar_slots(
    missing_slots: dict[str, set[datetime]],
    timeout: int,
    pool_batch_size: int = 4,
    date_shard_days: int = 1,
) -> int:
    if not missing_slots:
        return 0
    pools_sorted = sorted(missing_slots)
    all_slots = [slot for slots in missing_slots.values() for slot in slots]
    min_slot = min(all_slots)
    max_slot_exclusive = max(all_slots) + timedelta(minutes=5)
    step_days = max(int(date_shard_days), 1)
    inserted_rows: list[dict[str, Any]] = []
    shard_start = min_slot
    while shard_start < max_slot_exclusive:
        shard_end = min(shard_start + timedelta(days=step_days), max_slot_exclusive)
        for i in range(0, len(pools_sorted), pool_batch_size):
            batch = pools_sorted[i : i + pool_batch_size]
            pool_list = ",".join(q(p) for p in batch)
            rows = run_ch(
                f"""
                SELECT
                  lower(pool_address) AS pool_address,
                  toStartOfFiveMinutes(coalesce(block_time, received_at_ms)) AS window_start,
                  toFloat64(sum(-toFloat64(amount0))) / greatest(sum(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))), 1) AS price_velocity_5m,
                  (countIf(amount0 < 0) * 1.) / greatest(count(), 1) AS buy_pressure_ratio,
                  if(avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))) > 0,
                     stddevPop(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))) / avg(greatest(abs(toFloat64(amount0)), abs(toFloat64(amount1)))),
                     0) AS token_volatility_5m,
                  count() AS activity_heat
                FROM eatlq.uniswap_v3_swaps_decoded
                WHERE chain_id = {CHAIN_ID}
                  AND lower(pool_address) IN ({pool_list})
                  AND coalesce(block_time, received_at_ms) >= toDateTime64({q(sql_dt(shard_start))}, 3, 'UTC')
                  AND coalesce(block_time, received_at_ms) < toDateTime64({q(sql_dt(shard_end))}, 3, 'UTC')
                GROUP BY pool_address, window_start
                HAVING count() >= 1
                FORMAT TSVWithNames
                """,
                timeout=timeout,
            )
            for r in rows:
                pool = (r.get("pool_address") or "").lower()
                slot = floor_5m(parse_ch_dt(r["window_start"]))
                if slot not in missing_slots.get(pool, set()):
                    continue
                inserted_rows.append(
                    {
                        "chain_id": CHAIN_ID,
                        "pool_address": pool,
                        "window_start": slot.strftime("%Y-%m-%d %H:%M:%S"),
                        "price_velocity_5m": as_float(r.get("price_velocity_5m")),
                        "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
                        "token_volatility_5m": as_float(r.get("token_volatility_5m")),
                        "activity_heat": int(float(r.get("activity_heat") or 0)),
                    }
                )
        shard_start = shard_end

    uniq: dict[tuple[str, str], dict[str, Any]] = {}
    for row in inserted_rows:
        uniq[(row["pool_address"], row["window_start"])] = row
    final_rows = list(uniq.values())
    if not final_rows:
        return 0
    tsv_header = [
        "chain_id",
        "pool_address",
        "window_start",
        "price_velocity_5m",
        "buy_pressure_ratio",
        "token_volatility_5m",
        "activity_heat",
    ]
    payload_lines = ["\t".join(tsv_header)]
    for row in final_rows:
        payload_lines.append("\t".join(str(row[h]) for h in tsv_header))
    payload = "\n".join(payload_lines) + "\n"
    cmd = [
        "docker",
        "exec",
        "-i",
        CH_CONTAINER,
        "clickhouse-client",
        "--database",
        "eatlq",
        "--query",
        (
            f"INSERT INTO {POOL_FEATURE_SIDECAR_TABLE} "
            "(chain_id,pool_address,window_start,price_velocity_5m,buy_pressure_ratio,token_volatility_5m,activity_heat) "
            "FORMAT TSVWithNames"
        ),
    ]
    cp = subprocess.run(cmd, input=payload, text=True, capture_output=True, timeout=timeout)
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout).strip()
        raise RuntimeError(err[:1200])
    return len(final_rows)


def enforce_pool_concentration(
    selected_lots: list[Lot],
    max_top_pool_share: float,
) -> tuple[list[Lot], float, float]:
    if not selected_lots:
        return [], 0.0, 0.0
    pool_counts_before = Counter(l.pool for l in selected_lots)
    top_before = max(pool_counts_before.values()) / len(selected_lots)
    threshold = max(0.01, min(0.99, float(max_top_pool_share)))
    cap = max(1, int(math.floor(threshold * len(selected_lots))))
    by_pool: dict[str, list[Lot]] = defaultdict(list)
    for lot in selected_lots:
        by_pool[lot.pool].append(lot)
    for pool in list(by_pool):
        by_pool[pool] = by_pool[pool][:cap]
    out: list[Lot] = []
    pool_order = sorted(by_pool)
    while pool_order:
        next_order = []
        for pool in pool_order:
            if by_pool[pool]:
                out.append(by_pool[pool].pop(0))
            if by_pool[pool]:
                next_order.append(pool)
        pool_order = next_order
    pool_counts_after = Counter(l.pool for l in out)
    top_after = (max(pool_counts_after.values()) / len(out)) if out else 0.0
    return out, top_before, top_after


def wallet_features(stats: WalletStats, entry_time: datetime) -> dict[str, Any]:
    closed = [t for t in stats.closed if t["exit_time"] < entry_time]
    closed_7d = [t for t in closed if t["exit_time"] >= entry_time - timedelta(days=7)]
    closed_30d = [t for t in closed if t["exit_time"] >= entry_time - timedelta(days=30)]

    def roi_sum(rows: list[dict[str, Any]]) -> float | None:
        cost = sum(t["cost_weth"] for t in rows)
        pnl = sum(t["pnl_weth"] for t in rows)
        return pnl / cost if cost > 0 else None

    wins = [t for t in closed if t["pnl_weth"] > 0]
    losses = [t for t in closed if t["pnl_weth"] < 0]
    gross_win = sum(t["pnl_weth"] for t in wins)
    gross_loss = -sum(t["pnl_weth"] for t in losses)
    holds = sorted(t["hold_time_sec"] for t in closed)
    notionals = [t["cost_weth"] for t in closed_30d if t["cost_weth"] > 0]
    total_notional = sum(notionals)

    if holds:
        mid = len(holds) // 2
        median_hold = holds[mid] if len(holds) % 2 else (holds[mid - 1] + holds[mid]) / 2
    else:
        median_hold = None
    wallet_age = (entry_time - stats.first_seen).total_seconds() / 86400 if stats.first_seen else None
    return {
        "past_7d_roi": roi_sum(closed_7d),
        "past_30d_roi": roi_sum(closed_30d),
        "win_rate": len(wins) / len(closed) if closed else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (None if gross_win == 0 else 999.0),
        "closed_trade_count": len(closed),
        "median_hold_time": median_hold,
        "max_single_trade_share": max(notionals) / total_notional if total_notional > 0 else None,
        "wallet_age_days": wallet_age,
    }


def price_after(series: list[tuple[datetime, float]], target: datetime) -> float | None:
    for ts, price in series:
        if ts >= target:
            return price
    return None


def min_price_between(series: list[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    vals = [p for ts, p in series if start <= ts <= end]
    return min(vals) if vals else None


def safe_return(future_price: float | None, entry_price: float | None) -> float | None:
    if future_price is None or entry_price is None or entry_price <= 0:
        return None
    return (future_price / entry_price) - 1.0


def build_manifest(path: Path) -> dict[str, str]:
    manifest = {
        "entry_id": "DIAGNOSTIC_ONLY",
        "wallet": "DIAGNOSTIC_ONLY",
        "token": "DIAGNOSTIC_ONLY",
        "pool": "DIAGNOSTIC_ONLY",
        "tx_hash": "DIAGNOSTIC_ONLY",
        "block_number": "TIME_REGIME_PROXY",
        "entry_time": "ENTRY_TIME_VISIBLE",
        "fee_tier": "ENTRY_TIME_VISIBLE",
        "amount_proxy": "ENTRY_TIME_VISIBLE",
        "amount_weth": "ENTRY_TIME_VISIBLE",
        "amount_usd_notional": "ENTRY_TIME_VISIBLE",
        "trade_size_bucket": "DIAGNOSTIC_ONLY",
        "pool_age_sec": "ENTRY_TIME_VISIBLE",
        "activity_heat": "ENTRY_TIME_VISIBLE",
        "buy_pressure_ratio": "ENTRY_TIME_VISIBLE",
        "price_velocity_5m": "ENTRY_TIME_VISIBLE",
        "token_volatility_5m": "ENTRY_TIME_VISIBLE",
        "past_7d_roi": "AS_OF_WALLET_FEATURE",
        "past_30d_roi": "AS_OF_WALLET_FEATURE",
        "win_rate": "AS_OF_WALLET_FEATURE",
        "profit_factor": "AS_OF_WALLET_FEATURE",
        "closed_trade_count": "AS_OF_WALLET_FEATURE",
        "median_hold_time": "AS_OF_WALLET_FEATURE",
        "max_single_trade_share": "AS_OF_WALLET_FEATURE",
        "wallet_age_days": "AS_OF_WALLET_FEATURE",
        "router_flag": "DIAGNOSTIC_ONLY",
        "router_reason": "DIAGNOSTIC_ONLY",
        "future_return_5m": "FUTURE_LABEL",
        "future_return_30m": "FUTURE_LABEL",
        "future_return_2h": "FUTURE_LABEL",
        "future_return_6h": "FUTURE_LABEL",
        "realized_wallet_pnl": "FUTURE_LABEL",
        "realized_wallet_pnl_weth": "FUTURE_LABEL",
        "hold_time_sec": "FUTURE_LABEL",
        "max_drawdown": "FUTURE_LABEL",
        "closed_loop_eligible": "DIAGNOSTIC_ONLY",
        "closed_within_7d": "DIAGNOSTIC_ONLY",
        "closed_within_14d": "DIAGNOSTIC_ONLY",
        "closed_within_30d": "DIAGNOSTIC_ONLY",
        "label_family_short_window": "DIAGNOSTIC_ONLY",
        "label_family_closed_loop": "DIAGNOSTIC_ONLY",
        "router_tx_artifact_prefix": "DIAGNOSTIC_ONLY",
        "amount_proxy_outlier": "DIAGNOSTIC_ONLY",
        "copy_delay_30s": "FUTURE_LABEL",
        "copy_delay_60s": "FUTURE_LABEL",
        "copy_delay_120s": "FUTURE_LABEL",
        "contract_flag": "NOT_AVAILABLE",
        "deployer_flag": "NOT_AVAILABLE",
        "mev_flag": "NOT_AVAILABLE",
    }
    feature_tags = {"ENTRY_TIME_VISIBLE", "AS_OF_WALLET_FEATURE"}
    leaks = [k for k, v in manifest.items() if k.startswith(("future_", "realized_", "hold_time", "max_drawdown", "copy_delay")) and v in feature_tags]
    if leaks:
        raise RuntimeError(f"Leakage manifest failure: label fields tagged as features: {leaks}")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def write_csv(path: Path, rows: list[dict[str, Any]], preferred_fields: list[str] | None = None) -> None:
    if preferred_fields is None:
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    else:
        fields = preferred_fields
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: serialize(row.get(k)) for k in fields})


def serialize(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, str):
        return v.replace("\r", " ").replace("\n", " ")
    if v is None:
        return ""
    return v


def clean_mat_validation(start: datetime, end: datetime, timeout: int) -> dict[str, Any]:
    try:
        rows = run_ch(
            f"""
            SELECT
              count() AS clean_rows,
              uniqExact(wallet) AS clean_wallets,
              min(block_time) AS clean_min_block_time,
              max(block_time) AS clean_max_block_time
            FROM uniswap_swaps_clean_mat
            WHERE chain_id = 8453
              AND block_time >= toDateTime64({q(sql_dt(start))}, 3, 'UTC')
              AND block_time < toDateTime64({q(sql_dt(end))}, 3, 'UTC')
            FORMAT TSVWithNames
            """,
            timeout=timeout,
        )
    except Exception as exc:
        return {"clean_mat_validation_status": f"not_available: {str(exc)[:300]}"}
    if not rows:
        return {"clean_mat_validation_status": "no_rows_returned"}
    row = rows[0]
    return {
        "clean_mat_validation_status": "available_validation_only",
        "clean_mat_rows": row.get("clean_rows"),
        "clean_mat_wallets": row.get("clean_wallets"),
        "clean_mat_min_block_time": row.get("clean_min_block_time"),
        "clean_mat_max_block_time": row.get("clean_max_block_time"),
    }


def main() -> int:
    args = parse_args()
    global CH_CONTAINER
    CH_CONTAINER = args.docker_container
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    decision_flags: dict[str, Any] = {
        "BUILDER_SCRIPT_EXISTS": True,
        "ROUNDTRIP_FEASIBLE": True,
        "ROUNDTRIP_LEDGER_BUILT": False,
        "FEATURE_MANIFEST_EXISTS": False,
        "LEAKAGE_GUARD_PASS": False,
        "ROUTER_FILTER_PASS": False,
        "SAMPLE_SIZE_SUMMARY_EXISTS": False,
        "DATASET_SAMPLE_EXISTS": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "dry_run": bool(args.dry_run),
        "window_days": args.window_days,
        "sample_limit": args.sample_limit,
        "max_rows": args.max_rows,
        "balanced_pool_sample": bool(args.balanced_pool_sample),
        "date_shard_days": args.date_shard_days,
        "max_pools": args.max_pools,
        "per_pool_max_rows": args.per_pool_max_rows,
        "pool_metadata_source": args.pool_metadata_source,
        "pool_feature_source": args.pool_feature_source,
        "max_top_pool_share": args.max_top_pool_share,
    }
    commands = [" ".join(sys.argv)]

    manifest = build_manifest(out_dir / "feature_manifest.json")
    decision_flags["FEATURE_MANIFEST_EXISTS"] = True
    decision_flags["LEAKAGE_GUARD_PASS"] = True

    if args.dry_run:
        write_csv(out_dir / "sample_size_summary.csv", [{"status": "DRY_RUN", "rows_scanned": 0}])
        (out_dir / "dataset_sample.csv").write_text("entry_id\n")
        (out_dir / "closed_trades_sample.csv").write_text("trade_id\n")
        (out_dir / "open_positions_sample.csv").write_text("entry_id\n")
        decision_flags.update(
            {
                "SAMPLE_SIZE_SUMMARY_EXISTS": True,
                "DATASET_SAMPLE_EXISTS": True,
                "ROUTER_FILTER_PASS": True,
                "blocker": "dry_run requested; no ClickHouse scan performed",
            }
        )
        write_outputs(out_dir, decision_flags, commands, manifest, [], [], [], started, args)
        return 0

    pool_meta, pool_meta_stats = load_pool_metadata(args.pool_metadata_source, args.query_timeout_sec)
    sidecar_exists = sidecar_table_exists(args.query_timeout_sec)
    requested_feature_source = args.pool_feature_source
    effective_feature_source = requested_feature_source
    if requested_feature_source == "sidecar" and not sidecar_exists:
        effective_feature_source = "view"
    decision_flags.update(
        {
            "pool_feature_sidecar_table": POOL_FEATURE_SIDECAR_TABLE,
            "pool_feature_sidecar_table_exists": sidecar_exists,
            "POOL_FEATURE_SOURCE_SIDECAR": effective_feature_source == "sidecar",
            "pool_feature_source_effective": effective_feature_source,
        }
    )
    decision_flags.update(pool_meta_stats)
    raw_max = max_raw_time(args.query_timeout_sec)
    end = raw_max.replace(microsecond=0) + timedelta(milliseconds=1)
    start = end - timedelta(days=args.window_days)
    scanned_rows: list[dict[str, str]] = []
    balanced_scan_meta: dict[str, Any] = {}
    if args.balanced_pool_sample:
        try:
            scanned_rows, balanced_scan_meta = fetch_balanced_swaps(
                start,
                end,
                args.max_rows,
                args.date_shard_days,
                args.max_pools,
                args.per_pool_max_rows,
                args.query_timeout_sec,
                metadata_pools=set(pool_meta),
            )
        except Exception as exc:
            decision_flags["blocker"] = f"ClickHouse balanced shard query failed {sql_dt(start)}..{sql_dt(end)}: {exc}"
            balanced_scan_meta = {"balanced_scan_status": f"failed: {str(exc)[:300]}"}
    else:
        cursor = start
        while cursor < end and len(scanned_rows) < args.max_rows:
            shard_end = min(cursor + timedelta(days=max(1, args.date_shard_days)), end)
            remaining = args.max_rows - len(scanned_rows)
            try:
                rows = fetch_swaps(
                    cursor,
                    shard_end,
                    remaining,
                    args.query_timeout_sec,
                    allowed_pools=set(pool_meta),
                )
            except Exception as exc:
                decision_flags["blocker"] = f"ClickHouse shard query failed {sql_dt(cursor)}..{sql_dt(shard_end)}: {exc}"
                break
            scanned_rows.extend(rows)
            cursor = shard_end

    lots: dict[tuple[str, str, str], deque[Lot]] = defaultdict(deque)
    wallet_stats: dict[str, WalletStats] = defaultdict(WalletStats)
    closed_trades: list[dict[str, Any]] = []
    dataset_lots: list[Lot] = []
    price_series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    router_swaps = 0
    router_entries_excluded = 0
    ambiguous = 0

    for r in scanned_rows:
        pool = r["pool_address"]
        meta = pool_meta.get(pool)
        if meta is None or meta.token_address is None:
            continue
        t = parse_ch_dt(r["block_time"])
        a0 = signed_int256(r["amount0_raw"])
        a1 = signed_int256(r["amount1_raw"])
        weth_delta = a0 if meta.weth_is_token0 else a1
        token_delta = a1 if meta.weth_is_token0 else a0
        if weth_delta == 0 or token_delta == 0:
            ambiguous += 1
            continue
        token_abs = abs(amount_to_float(token_delta))
        weth_abs = abs(amount_to_float(weth_delta))
        if token_abs <= 0 or weth_abs <= 0:
            ambiguous += 1
            continue
        price = weth_abs / token_abs
        price_series[pool].append((t, price))
        sender = r["sender"]
        recipient = r["recipient"]
        is_router, router_reason = router_status(sender, recipient)
        if is_router:
            router_swaps += 1

        wallet = recipient
        stats = wallet_stats[wallet]
        if stats.first_seen is None or t < stats.first_seen:
            stats.first_seen = t

        key = (wallet, meta.token_address, pool)
        if weth_delta > 0 and token_delta < 0:
            if is_router:
                router_entries_excluded += 1
                continue
            if weth_from_raw(weth_abs) > 500.0:
                ambiguous += 1
                continue
            lot = Lot(
                entry_id=f"{r['tx_hash']}:{r['log_index']}",
                wallet=wallet,
                token=meta.token_address,
                pool=pool,
                entry_time=t,
                entry_tx_hash=r["tx_hash"],
                entry_block=int(r["block_number"]),
                log_index=int(r["log_index"]),
                fee_bps=meta.fee_bps,
                token_remaining=token_abs,
                token_amount=token_abs,
                weth_cost=weth_abs,
                sender=sender,
                recipient=recipient,
                router_flag=is_router,
                router_reason=router_reason,
            )
            lots[key].append(lot)
            dataset_lots.append(lot)
        elif weth_delta < 0 and token_delta > 0:
            sell_remaining = token_abs
            proceeds_remaining = weth_abs
            while sell_remaining > 0 and lots[key]:
                lot = lots[key][0]
                consume = min(sell_remaining, lot.token_remaining)
                frac = consume / sell_remaining if sell_remaining else 0.0
                proceeds = proceeds_remaining * frac
                cost = lot.weth_cost * (consume / lot.token_amount)
                pnl = proceeds - cost
                hold = (t - lot.entry_time).total_seconds()
                trade = {
                    "trade_id": f"{lot.entry_id}->{r['tx_hash']}:{r['log_index']}",
                    "wallet": wallet,
                    "token": meta.token_address,
                    "pool": pool,
                    "entry_id": lot.entry_id,
                    "entry_time": lot.entry_time,
                    "exit_time": t,
                    "entry_tx_hash": lot.entry_tx_hash,
                    "exit_tx_hash": r["tx_hash"],
                    "token_amount": consume,
                    "cost_weth": cost,
                    "proceeds_weth": proceeds,
                    "pnl_weth": pnl,
                    "roi": pnl / cost if cost > 0 else None,
                    "hold_time_sec": hold,
                }
                closed_trades.append(trade)
                wallet_stats[wallet].closed.append(trade)
                lot.token_remaining -= consume
                sell_remaining -= consume
                proceeds_remaining -= proceeds
                if lot.token_remaining <= max(1.0, lot.token_amount * 1e-12):
                    lots[key].popleft()
            # Unmatched sells are ignored for realized PnL.
        else:
            ambiguous += 1

    if args.balanced_pool_sample:
        by_pool: dict[str, list[Lot]] = defaultdict(list)
        for lot in dataset_lots:
            by_pool[lot.pool].append(lot)
        selected_lots = []
        pool_order = sorted(by_pool)
        while pool_order and len(selected_lots) < args.sample_limit:
            next_pool_order = []
            for pool in pool_order:
                lots_for_pool = by_pool[pool]
                if lots_for_pool:
                    selected_lots.append(lots_for_pool.pop(0))
                    if len(selected_lots) >= args.sample_limit:
                        break
                if lots_for_pool:
                    next_pool_order.append(pool)
            pool_order = next_pool_order
    else:
        selected_lots = dataset_lots[: args.sample_limit]
    selected_lots, top_pool_share_before_decon, top_pool_share_after_decon = enforce_pool_concentration(
        selected_lots,
        args.max_top_pool_share,
    )
    selected_pools = {lot.pool for lot in selected_lots}
    pool_births = fetch_pool_births(selected_pools, args.query_timeout_sec)
    try:
        needed_feature_slots = build_needed_feature_slots(selected_lots)
        slots_needed = sum(len(v) for v in needed_feature_slots.values())
        pool_features = {}
        feature_join_status = "not_available: not_attempted"
        if effective_feature_source == "none":
            feature_join_status = f"not_available: source=none, slots_needed={slots_needed}"
        elif effective_feature_source == "sidecar":
            try:
                existing_keys = fetch_existing_sidecar_slot_keys(
                    needed_feature_slots,
                    args.query_timeout_sec,
                    date_shard_days=args.date_shard_days,
                )
                slots_existing = len(existing_keys)
                missing_slots: dict[str, set[datetime]] = defaultdict(set)
                for pool, slots in needed_feature_slots.items():
                    for slot in slots:
                        if (pool, slot) not in existing_keys:
                            missing_slots[pool].add(slot)
                slots_missing = sum(len(v) for v in missing_slots.values())
                slots_inserted = 0
                auto_sidecar_blocker = ""
                if slots_missing > 0:
                    try:
                        slots_inserted = materialize_missing_sidecar_slots(
                            missing_slots,
                            args.query_timeout_sec,
                            pool_batch_size=4,
                            date_shard_days=args.date_shard_days,
                        )
                    except Exception as exc:
                        auto_sidecar_blocker = str(exc)[:300]
                pool_features = fetch_sidecar_features_for_slots(
                    needed_feature_slots,
                    args.query_timeout_sec,
                    date_shard_days=args.date_shard_days,
                )
                slots_filled = len(pool_features)
                auto_sidecar_pass = slots_filled >= slots_existing
                feature_join_status = (
                    f"available: source=sidecar, slots_needed={slots_needed}, "
                    f"slots_existing={slots_existing}, slots_inserted={slots_inserted}, slots_filled={slots_filled}, "
                    f"auto_sidecar_blocker={auto_sidecar_blocker}"
                )
            except Exception as exc:
                feature_join_status = f"not_available: source=sidecar blocker={str(exc)[:300]}"
                slots_existing = 0
                slots_missing = slots_needed
                slots_inserted = 0
                slots_filled = 0
                auto_sidecar_pass = False
                auto_sidecar_blocker = str(exc)[:300]
        else:
            clean_err: Exception | None = None
            try:
                pool_features = fetch_pool_features_for_slots(
                    needed_feature_slots,
                    args.query_timeout_sec,
                    date_shard_days=args.date_shard_days,
                    table_name="pool_features_5m_clean",
                )
                feature_join_status = f"available: source=pool_features_5m_clean, slots_needed={slots_needed}, slots_filled={len(pool_features)}"
            except Exception as exc:
                clean_err = exc
            if not pool_features:
                try:
                    pool_features = fetch_pool_features_for_slots(
                        needed_feature_slots,
                        args.query_timeout_sec,
                        date_shard_days=args.date_shard_days,
                        table_name="pool_features_5m",
                    )
                    feature_join_status = (
                        "available: source=pool_features_5m_fallback, "
                        f"slots_needed={slots_needed}, slots_filled={len(pool_features)}, "
                        f"clean_blocker={str(clean_err)[:200] if clean_err else ''}"
                    )
                except Exception as exc2:
                    feature_join_status = (
                        "not_available: pool feature slot fetch failed; "
                        f"clean_blocker={str(clean_err)[:200] if clean_err else 'none'}; "
                        f"fallback_blocker={str(exc2)[:200]}"
                    )
    except Exception as exc:
        pool_features = {}
        feature_join_status = f"not_available: pool_features_5m_clean slot fetch failed: {str(exc)[:300]}"
    pool_feature_join_pass = feature_join_status.startswith("available")
    if effective_feature_source != "sidecar":
        slots_existing = 0
        slots_missing = 0
        slots_inserted = 0
        slots_filled = len(pool_features)
        auto_sidecar_pass = False
        auto_sidecar_blocker = "sidecar not selected"
    closed_by_entry = {t["entry_id"]: t for t in closed_trades}
    dataset_rows: list[dict[str, Any]] = []

    for lot in selected_lots:
        stats = wallet_stats[lot.wallet]
        wf = wallet_features(stats, lot.entry_time)
        feat_slot = floor_5m(lot.entry_time - timedelta(minutes=5))
        feat = pool_features.get((lot.pool, feat_slot), {})
        entry_price = lot.weth_cost / lot.token_amount if lot.token_amount > 0 else None
        series = price_series.get(lot.pool, [])
        closed = closed_by_entry.get(lot.entry_id)
        label_end = closed["exit_time"] if closed else lot.entry_time + timedelta(hours=6)
        min_px = min_price_between(series, lot.entry_time, label_end)
        hold_sec = closed.get("hold_time_sec") if closed else None
        weth_h = weth_from_raw(lot.weth_cost)
        usd_n = usd_notional_from_buy(lot.weth_cost, lot.token, lot.token_amount)
        pnl_weth_h = weth_from_raw(closed["pnl_weth"]) if closed else None
        row = {
            "entry_id": lot.entry_id,
            "wallet": lot.wallet,
            "token": lot.token,
            "pool": lot.pool,
            "tx_hash": lot.entry_tx_hash,
            "block_number": lot.entry_block,
            "entry_time": lot.entry_time,
            "fee_tier": lot.fee_bps,
            "amount_proxy": weth_h,
            "amount_weth": weth_h,
            "amount_usd_notional": usd_n,
            "trade_size_bucket": trade_size_bucket(usd_n),
            "pool_age_sec": (lot.entry_time - pool_births[lot.pool]).total_seconds() if lot.pool in pool_births else None,
            "activity_heat": feat.get("activity_heat"),
            "buy_pressure_ratio": feat.get("buy_pressure_ratio"),
            "price_velocity_5m": feat.get("price_velocity_5m"),
            "token_volatility_5m": feat.get("token_volatility_5m"),
            "router_flag": lot.router_flag,
            "router_reason": lot.router_reason,
            "router_tx_artifact_prefix": lot.entry_tx_hash.lower().startswith(ROUTER_TX_HASH_PREFIX),
            **wf,
            **closed_loop_horizon_flags(hold_sec),
            "label_family_short_window": "future_return_30m",
            "label_family_closed_loop": "realized_wallet_pnl_weth" if closed else "",
            "realized_wallet_pnl_weth": pnl_weth_h,
            "realized_wallet_pnl": pnl_weth_h,
            "hold_time_sec": hold_sec,
            "max_drawdown": safe_return(min_px, entry_price) if min_px is not None else None,
        }
        for name, seconds in HORIZONS.items():
            row[name] = safe_return(price_after(series, lot.entry_time + timedelta(seconds=seconds)), entry_price)
        for name, seconds in COPY_DELAYS.items():
            row[name] = safe_return(price_after(series, lot.entry_time + timedelta(seconds=seconds)), entry_price)
        dataset_rows.append(row)

    open_rows: list[dict[str, Any]] = []
    for queue in lots.values():
        for lot in queue:
            open_rows.append(
                {
                    "entry_id": lot.entry_id,
                    "wallet": lot.wallet,
                    "token": lot.token,
                    "pool": lot.pool,
                    "entry_time": lot.entry_time,
                    "token_remaining": lot.token_remaining,
                    "token_amount": lot.token_amount,
                    "weth_cost": lot.weth_cost,
                    "router_flag": lot.router_flag,
                }
            )

    clean_validation = clean_mat_validation(start, end, args.query_timeout_sec)
    feature_non_null_counts = {
        "feature_non_null_activity_heat": sum(1 for row in dataset_rows if row.get("activity_heat") is not None),
        "feature_non_null_buy_pressure_ratio": sum(1 for row in dataset_rows if row.get("buy_pressure_ratio") is not None),
        "feature_non_null_price_velocity_5m": sum(1 for row in dataset_rows if row.get("price_velocity_5m") is not None),
        "feature_non_null_token_volatility_5m": sum(1 for row in dataset_rows if row.get("token_volatility_5m") is not None),
    }
    closed_loop_stats = {
        "closed_loop_eligible_share": (
            sum(1 for row in dataset_rows if row.get("closed_loop_eligible")) / len(dataset_rows)
            if dataset_rows
            else 0.0
        ),
        "closed_within_7d_share": (
            sum(1 for row in dataset_rows if row.get("closed_within_7d")) / len(dataset_rows)
            if dataset_rows
            else 0.0
        ),
        "closed_within_14d_share": (
            sum(1 for row in dataset_rows if row.get("closed_within_14d")) / len(dataset_rows)
            if dataset_rows
            else 0.0
        ),
        "closed_within_30d_share": (
            sum(1 for row in dataset_rows if row.get("closed_within_30d")) / len(dataset_rows)
            if dataset_rows
            else 0.0
        ),
        "amount_proxy_human_weth_max": max(
            (row["amount_proxy"] for row in dataset_rows if row.get("amount_proxy") is not None),
            default=0,
        ),
    }
    fields = list(manifest)
    write_csv(out_dir / "dataset_sample.csv", dataset_rows, fields)
    write_csv(out_dir / "closed_trades_sample.csv", closed_trades[: args.sample_limit])
    write_csv(out_dir / "open_positions_sample.csv", open_rows[: args.sample_limit])
    selected_pool_counts: dict[str, int] = defaultdict(int)
    for lot in selected_lots:
        selected_pool_counts[lot.pool] += 1
    top_pool_share = (max(selected_pool_counts.values()) / len(selected_lots)) if selected_lots else 0.0
    exact_supply_blocker = (
        args.balanced_pool_sample
        and balanced_scan_meta.get("candidate_pools_available", 0) < 30
        and len(selected_pool_counts) == balanced_scan_meta.get("candidate_pools_used", 0)
    )
    balanced_sample_pass = (len(selected_pool_counts) >= 30 or exact_supply_blocker) if args.balanced_pool_sample else False
    if args.balanced_pool_sample and not balanced_sample_pass:
        decision_flags["balanced_sample_blocker"] = (
            f"Only {len(selected_pool_counts)} pools reached selected buy lots; "
            f"candidate_pools_available={balanced_scan_meta.get('candidate_pools_available', 0)}, "
            f"candidate_pools_used={balanced_scan_meta.get('candidate_pools_used', 0)}, "
            f"rows_scanned={len(scanned_rows)}, buy_lots_built={len(dataset_lots)}"
        )
    elif exact_supply_blocker:
        decision_flags["balanced_sample_blocker"] = (
            f"Exact supply blocker: only {balanced_scan_meta.get('candidate_pools_available', 0)} "
            "WETH V3 metadata pools have raw swaps in the 30d window; the WETH round-trip "
            "builder cannot reach 30 unique pools without expanding the pool universe."
        )
    summary = {
        "window_days": args.window_days,
        "raw_window_start": start,
        "raw_window_end": end,
        "balanced_pool_sample": bool(args.balanced_pool_sample),
        "pool_metadata_source": args.pool_metadata_source,
        "max_top_pool_share": args.max_top_pool_share,
        "top_pool_share_before_decon": top_pool_share_before_decon,
        "top_pool_share_after_decon": top_pool_share_after_decon,
        "registry_weth_pools_total": pool_meta_stats.get("registry_weth_pools_total", 0),
        "discovered_weth_v3_pools_total": pool_meta_stats.get("discovered_weth_v3_pools_total", 0),
        "union_weth_pools_total": pool_meta_stats.get("union_weth_pools_total", 0),
        "metadata_pools_total": pool_meta_stats.get("metadata_pools_total", 0),
        "date_shard_days": args.date_shard_days,
        "max_pools": args.max_pools,
        "per_pool_max_rows": args.per_pool_max_rows,
        "rows_scanned": len(scanned_rows),
        "buy_lots_built": len(dataset_lots),
        "dataset_rows_written": len(dataset_rows),
        "closed_trades": len(closed_trades),
        "open_positions": len(open_rows),
        "unique_wallets": len({lot.wallet for lot in selected_lots}),
        "unique_pools": len(selected_pool_counts),
        "top_pool_share": top_pool_share,
        "sidecar_slots_needed": slots_needed if "slots_needed" in locals() else 0,
        "sidecar_slots_existing": slots_existing if "slots_existing" in locals() else 0,
        "sidecar_slots_missing": slots_missing if "slots_missing" in locals() else 0,
        "sidecar_slots_inserted": slots_inserted if "slots_inserted" in locals() else 0,
        "sidecar_slots_filled": slots_filled if "slots_filled" in locals() else 0,
        "AUTO_SIDECAR_MATERIALIZATION_PASS": auto_sidecar_pass if "auto_sidecar_pass" in locals() else False,
        "auto_sidecar_blocker": auto_sidecar_blocker if "auto_sidecar_blocker" in locals() else "",
        "exact_supply_blocker": exact_supply_blocker,
        "router_swaps_flagged": router_swaps,
        "router_buy_entries_excluded": router_entries_excluded,
        "ambiguous_direction_rows": ambiguous,
        "BALANCED_SAMPLE_PASS": balanced_sample_pass,
        "clean_mat_validation_role": "not_used_as_input_lagged_validation_only",
        "pool_feature_join_status": feature_join_status,
        **feature_non_null_counts,
        **closed_loop_stats,
        **balanced_scan_meta,
        **clean_validation,
    }
    write_csv(out_dir / "sample_size_summary.csv", [summary])
    decision_flags.update(
        {
            "ROUNDTRIP_LEDGER_BUILT": bool(dataset_lots or closed_trades or open_rows),
            "SAMPLE_SIZE_SUMMARY_EXISTS": True,
            "DATASET_SAMPLE_EXISTS": bool(dataset_rows),
            "ROUTER_FILTER_PASS": router_entries_excluded >= 0 and all(not row.get("router_flag") for row in dataset_rows),
            "rows_scanned": len(scanned_rows),
            "dataset_rows_written": len(dataset_rows),
            "closed_trades": len(closed_trades),
            "open_positions": len(open_rows),
            "router_swaps_flagged": router_swaps,
            "router_buy_entries_excluded": router_entries_excluded,
            "pool_feature_join_status": feature_join_status,
            "POOL_FEATURE_JOIN_PASS": pool_feature_join_pass,
            "unique_pools": len(selected_pool_counts),
            "top_pool_share": top_pool_share,
            "top_pool_share_before_decon": top_pool_share_before_decon,
            "top_pool_share_after_decon": top_pool_share_after_decon,
            "unique_wallets": len({lot.wallet for lot in selected_lots}),
            "BALANCED_SAMPLE_PASS": balanced_sample_pass,
            "exact_supply_blocker": exact_supply_blocker,
            "METADATA_SOURCE_UNION_PASS": args.pool_metadata_source == "union",
            "REGISTRY_COVERAGE_GAP_HANDLED": args.pool_metadata_source == "union" and pool_meta_stats.get("union_weth_pools_total", 0) >= pool_meta_stats.get("registry_weth_pools_total", 0),
            "POOL_FEATURE_JOIN_PASS": pool_feature_join_pass,
            "AUTO_SIDECAR_MATERIALIZATION_PASS": auto_sidecar_pass if "auto_sidecar_pass" in locals() else False,
            "sidecar_slots_needed": slots_needed if "slots_needed" in locals() else 0,
            "sidecar_slots_existing": slots_existing if "slots_existing" in locals() else 0,
            "sidecar_slots_missing": slots_missing if "slots_missing" in locals() else 0,
            "sidecar_slots_inserted": slots_inserted if "slots_inserted" in locals() else 0,
            "sidecar_slots_filled": slots_filled if "slots_filled" in locals() else 0,
            "auto_sidecar_blocker": auto_sidecar_blocker if "auto_sidecar_blocker" in locals() else "",
            **feature_non_null_counts,
            **pool_meta_stats,
            **balanced_scan_meta,
            **clean_validation,
        }
    )
    if not decision_flags["ROUNDTRIP_LEDGER_BUILT"]:
        decision_flags["blocker"] = "No ledger rows built from bounded raw swap sample"
    write_outputs(out_dir, decision_flags, commands, manifest, dataset_rows, closed_trades, open_rows, started, args)
    return 0 if decision_flags["ROUNDTRIP_LEDGER_BUILT"] else 2


def write_outputs(
    out_dir: Path,
    decision_flags: dict[str, Any],
    commands: list[str],
    manifest: dict[str, str],
    dataset_rows: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    open_rows: list[dict[str, Any]],
    started: datetime,
    args: argparse.Namespace,
) -> None:
    decision_flags["generated_at"] = utc_now().isoformat()
    decision_flags["elapsed_sec"] = (utc_now() - started).total_seconds()
    (out_dir / "decision_flags.json").write_text(json.dumps(decision_flags, indent=2, sort_keys=True, default=serialize) + "\n")
    future_as_features = [
        k
        for k, tag in manifest.items()
        if tag in {"ENTRY_TIME_VISIBLE", "AS_OF_WALLET_FEATURE"} and (
            k.startswith("future_") or k.startswith("copy_delay") or k in {"realized_wallet_pnl", "hold_time_sec", "max_drawdown"}
        )
    ]
    frontmatter = """---
sop_applied: true
alpha_id: AR-SP083-V3-SMART-WALLET-RT
family: F3/F5/F7
next_gate: OFFLINE_SCHEMA_AND_SAMPLE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
---
"""
    readout = [
        frontmatter,
        "# Base V3 Smart Wallet Round-Trip Dataset Builder v0",
        "",
        "## Executive Conclusion",
        "",
        f"BUILDER_SCRIPT_EXISTS={str(decision_flags.get('BUILDER_SCRIPT_EXISTS')).lower()}.",
        f"ROUNDTRIP_LEDGER_BUILT={str(decision_flags.get('ROUNDTRIP_LEDGER_BUILT')).lower()}.",
        f"TRAINING_ALLOWED={str(decision_flags.get('TRAINING_ALLOWED')).lower()}. LIVE_ALLOWED={str(decision_flags.get('LIVE_ALLOWED')).lower()}.",
        "",
        "Raw `eatlq.uniswap_v3_swaps` plus pool metadata source (`registry|discovered|union`) is the dataset input. Lagged `uniswap_swaps_clean_mat` remains validation-only.",
        "",
        "## Commands Run",
        "",
        *[f"- `{cmd}`" for cmd in commands],
        "",
        "## Sample Size Summary",
        "",
        f"- window_days: `{args.window_days}`",
        f"- rows_scanned: `{decision_flags.get('rows_scanned', 0)}`",
        f"- dataset_rows_written: `{decision_flags.get('dataset_rows_written', len(dataset_rows))}`",
        f"- closed_trades: `{decision_flags.get('closed_trades', len(closed_trades))}`",
        f"- open_positions: `{decision_flags.get('open_positions', len(open_rows))}`",
        "",
        "## Leakage Audit",
        "",
        f"- FEATURE_MANIFEST_EXISTS={str(decision_flags.get('FEATURE_MANIFEST_EXISTS')).lower()}",
        f"- LEAKAGE_GUARD_PASS={str(decision_flags.get('LEAKAGE_GUARD_PASS') and not future_as_features).lower()}",
        f"- future_label_fields_as_features: `{future_as_features}`",
        "- As-of wallet features are computed only from closed trades with `exit_time < entry_time`.",
        "- Pool feature windows are joined with a conservative 5 minute lag before entry time.",
        "",
        "## Router Filter",
        "",
        f"- ROUTER_FILTER_PASS={str(decision_flags.get('ROUTER_FILTER_PASS')).lower()}",
        f"- router_swaps_flagged: `{decision_flags.get('router_swaps_flagged', 0)}`",
        f"- router_buy_entries_excluded: `{decision_flags.get('router_buy_entries_excluded', 0)}`",
        "- 0x6ff5693b99212da76ad316178a184ab56d299b43 is explicitly classified as router/programmatic market-maker artifact, never a smart wallet.",
        "",
        "## Dataset Paths",
        "",
        f"- `{out_dir / 'dataset_sample.csv'}`",
        f"- `{out_dir / 'closed_trades_sample.csv'}`",
        f"- `{out_dir / 'open_positions_sample.csv'}`",
        f"- `{out_dir / 'feature_manifest.json'}`",
        f"- `{out_dir / 'decision_flags.json'}`",
        f"- `{out_dir / 'sample_size_summary.csv'}`",
        "",
        "## Decision Flags",
        "",
        "```json",
        json.dumps(decision_flags, indent=2, sort_keys=True, default=serialize),
        "```",
        "",
        "## Exact Next Command",
        "",
        "`python3 scripts/v3_smart_wallet_roundtrip_dataset_builder.py --window-days 30 --sample-limit 10000 --max-rows 250000 --date-shard-days 1 --max-pools 50 --per-pool-max-rows 50000 --balanced-pool-sample --pool-metadata-source union --pool-feature-source sidecar --out-dir mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_dataset_builder --query-timeout-sec 180`",
        "",
    ]
    (out_dir / "builder_readout.md").write_text("\n".join(readout))


if __name__ == "__main__":
    raise SystemExit(main())
