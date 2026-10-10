"""
Label Dashcam -- Compute executable-quote-based profitability labels.

For each dashcam signal at time t0 on pool P:
  1. Entry quote: compute tokens_bought for a fixed WETH input.
  2. Exit quote at t0+30m: compute WETH received for selling tokens_bought.
  3. net_return = (weth_received / weth_input) - 1 - ROUND_TRIP_COST
  4. label = 1 if net_return > 0 else 0

Requires either:
  - EVM_HTTP_URL pointing to an archive node (can replay historical quotes)
  - Or ClickHouse pool_features_5m data (approximate fallback)

Approximate labels are tagged with label_type=approximate and must NOT be
used for production model training (see P0-3 firewall rule).

Usage:
    python scripts/label_dashcam.py

Env vars:
    DASHCAM_PATH, LABELED_OUTPUT, CLICKHOUSE_URL, CLICKHOUSE_DB,
    CLICKHOUSE_USER, CLICKHOUSE_PASSWORD, CHAIN_ID, EVM_HTTP_URL,
    LABEL_NOTIONAL_WEI (default: 0.0005 ETH in wei), REDIS_URL, LABEL_DRYRUN=1
    (writes logs/label_patch_b_dryrun_sample.csv and
    mvp/diagnostics/label_patch_b_dryrun.json, not LABELED_OUTPUT),
    LABEL_DRYRUN_LIMIT=200 (last N age-eligible rows only).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("label_dashcam")

ROUND_TRIP_COST = 0.006
WETH_BASE = "0x4200000000000000000000000000000000000006".lower()

MODEL_FEATURE_COLUMNS = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]
AUDIT_ONLY_COLUMNS = [
    "pool_address",
    "pool_type",
    "protocol",
    "tx_hash",
    "log_index",
    "block_number",
]
LABELED_OUTPUT_COLUMNS = [
    "timestamp",
    "token_address",
    "symbol",
    *MODEL_FEATURE_COLUMNS,
    "entropy_source",
    *AUDIT_ONLY_COLUMNS,
    "triggered_signal",
    "label",
    "net_return",
    "label_type",
]


def _parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    idx = int(round(q * (len(xs) - 1)))
    return xs[max(0, min(idx, len(xs) - 1))]


def ch_query(url: str, db: str, user: str, password: str, sql: str) -> list[dict]:
    params = urlencode({
        "database": db, "user": user, "password": password,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{url}/?{params}", data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8").strip()
    except Exception as e:
        log.warning("ClickHouse query failed: %s", e)
        return []
    if not body:
        return []
    rows = []
    for line in body.split("\n"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _non_weth_token(t0: str, t1: str, weth: str = WETH_BASE) -> Optional[str]:
    a, b, w = t0.lower(), t1.lower(), weth.lower()
    if not a or not b:
        return None
    if a == w and b != w:
        return b
    if b == w and a != w:
        return a
    return None


def load_pool_meta_ch(
    ch_url: str, ch_db: str, ch_user: str, ch_password: str,
    chain_id: int, pools: set[str],
) -> dict[str, dict[str, Any]]:
    if not pools:
        return {}
    pool_list = ",".join(f"'{p}'" for p in sorted(pools))
    sql = f"""
        SELECT
            lower(pool_address) AS pool,
            argMax(pool_type, discovered_at) AS pool_type,
            argMax(lower(token0_address), discovered_at) AS t0,
            argMax(lower(token1_address), discovered_at) AS t1,
            argMax(fee_bps, discovered_at) AS fee_bps
        FROM eatlq.discovered_pools
        WHERE chain_id = {int(chain_id)}
          AND lower(pool_address) IN ({pool_list})
        GROUP BY pool
    """
    rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = (r.get("pool") or "").lower()
        if not p:
            continue
        out[p] = {
            "pool_type": (r.get("pool_type") or "").lower(),
            "t0": (r.get("t0") or "").lower(),
            "t1": (r.get("t1") or "").lower(),
            "fee_bps": int(r.get("fee_bps", 0) or 0),
        }
    return out


def load_pool_meta_redis(redis_url: str, pools: set[str]) -> dict[str, dict[str, Any]]:
    if not redis_url:
        return {}
    try:
        import redis  # type: ignore
    except ImportError:
        return {}
    if not pools:
        return {}
    r = redis.from_url(redis_url, decode_responses=True)
    out: dict[str, dict[str, Any]] = {}
    for p in pools:
        pl = p.lower()
        h = r.hgetall(f"pool:discovered:{pl}")
        if not h:
            continue
        try:
            fee = int(h.get("fee_bps") or 0)
        except (TypeError, ValueError):
            fee = 0
        out[pl] = {
            "pool_type": (h.get("pool_type") or "").lower(),
            "t0": (h.get("token0_address") or "").lower(),
            "t1": (h.get("token1_address") or "").lower(),
            "fee_bps": fee,
        }
    return out


def merge_pool_meta(
    ch_m: dict[str, dict[str, Any]], redis_m: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """ClickHouse wins over Redis for the same pool key."""
    out: dict[str, dict[str, Any]] = {}
    for p in set(ch_m) | set(redis_m):
        if p in ch_m:
            out[p] = ch_m[p]
        else:
            out[p] = redis_m[p]
    return out


def _valid_hex_addr(s: str) -> bool:
    t = s.strip()
    if not t.startswith("0x") or len(t) != 42:
        return False
    try:
        int(t[2:], 16)
    except ValueError:
        return False
    return True


def _reject_reason(
    m: dict[str, Any] | None,
) -> Optional[tuple[str, str]]:
    """Return (stats_bucket, fail_reason) to record, or None if v2/v3 may proceed."""
    if not m:
        return ("unresolved", "missing_token_address_or_pool_address")
    raw = (m.get("pool_type") or "").strip().lower()
    if raw in ("aero_vamm", "aero_cl"):
        return (raw, "unsupported_pool_type")
    if not raw or raw == "unknown":
        return ("unknown", "unknown_pool_type")
    if raw not in ("v2", "v3"):
        return ("unknown", "unknown_pool_type")
    t0, t1 = m.get("t0", ""), m.get("t1", "")
    if not _non_weth_token(t0, t1):
        return (raw, "missing_token_address_or_pool_address")
    if raw == "v3" and int(m.get("fee_bps", 0) or 0) <= 0:
        return (raw, "missing_fee_tier")
    return None


def label_approximate(
    ch_url: str, ch_db: str, ch_user: str, ch_password: str,
    pool_address: str, chain_id: int, signal_ts_ms: int,
) -> tuple[int, float, str]:
    """Approximate label from ClickHouse price velocity.
    Returns (label, net_return_approx, label_type).
    label_type is always 'approximate' for this method.
    """
    signal_ts_sec = signal_ts_ms // 1000
    sql = f"""
        SELECT
            avg(fut.price_velocity_5m) AS future_velocity,
            avg(fut.buy_pressure_ratio) AS future_pressure,
            count() AS window_count
        FROM eatlq.pool_features_5m AS fut
        WHERE fut.pool_address = '{pool_address}'
          AND fut.chain_id = {chain_id}
          AND fut.window_start > fromUnixTimestamp({signal_ts_sec})
          AND fut.window_start <= fromUnixTimestamp({signal_ts_sec + 1800})
    """
    rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql)
    if not rows or int(rows[0].get("window_count", 0)) < 2:
        return -1, 0.0, "no_data"

    velocity = float(rows[0]["future_velocity"])
    net_return_approx = velocity - ROUND_TRIP_COST
    label = 1 if net_return_approx > 0 else 0
    return label, net_return_approx, "approximate"


def label_executable(
    w3, pool_address: str, pool_type: str, token_address: str,
    weth_address: str, fee_bps: int, notional_wei: int,
    entry_block: int, exit_block: int,
) -> tuple[int, float, str]:
    """Executable-quote label using archive RPC calls at historical blocks.
    Returns (label, net_return, label_type='executable').
    """
    try:
        if pool_type == "v2":
            tokens_bought = _quote_v2_buy(w3, pool_address, weth_address,
                                           notional_wei, entry_block)
            if tokens_bought <= 0:
                return -1, 0.0, "quote_failed"
            weth_received = _quote_v2_sell(w3, pool_address, weth_address,
                                            tokens_bought, exit_block)
        else:
            tokens_bought = _quote_v3_buy(w3, weth_address, token_address,
                                           notional_wei, fee_bps, entry_block)
            if tokens_bought <= 0:
                return -1, 0.0, "quote_failed"
            weth_received = _quote_v3_sell(w3, token_address, weth_address,
                                            tokens_bought, fee_bps, exit_block)

        if weth_received <= 0:
            return 0, -1.0, "executable"

        net_return = (weth_received / notional_wei) - 1.0 - ROUND_TRIP_COST
        label = 1 if net_return > 0 else 0
        return label, net_return, "executable"
    except Exception as e:
        log.debug("executable label failed: %s", e)
        return -1, 0.0, "quote_failed"


def _quote_v2_buy(w3, pool_addr: str, weth_addr: str, amount_in: int, block: int) -> int:
    """CPMM buy quote at a specific block."""
    from web3 import Web3
    pair_abi = json.loads(
        '[{"constant":true,"inputs":[],"name":"getReserves",'
        '"outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},'
        '{"name":"","type":"uint32"}],"stateMutability":"view","type":"function"},'
        '{"constant":true,"inputs":[],"name":"token0",'
        '"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]'
    )
    pair = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=pair_abi)
    t0 = pair.functions.token0().call(block_identifier=block).lower()
    r0, r1, _ = pair.functions.getReserves().call(block_identifier=block)
    if t0 == weth_addr.lower():
        ri, ro = r0, r1
    else:
        ri, ro = r1, r0
    if ri == 0 or ro == 0:
        return 0
    af = amount_in * 997
    return (af * ro) // (ri * 1000 + af)


def _quote_v2_sell(w3, pool_addr: str, weth_addr: str, amount_in: int, block: int) -> int:
    """CPMM sell quote at a specific block."""
    from web3 import Web3
    pair_abi = json.loads(
        '[{"constant":true,"inputs":[],"name":"getReserves",'
        '"outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},'
        '{"name":"","type":"uint32"}],"stateMutability":"view","type":"function"},'
        '{"constant":true,"inputs":[],"name":"token0",'
        '"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]'
    )
    pair = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=pair_abi)
    t0 = pair.functions.token0().call(block_identifier=block).lower()
    r0, r1, _ = pair.functions.getReserves().call(block_identifier=block)
    if t0 == weth_addr.lower():
        ri, ro = r1, r0
    else:
        ri, ro = r0, r1
    if ri == 0 or ro == 0:
        return 0
    af = amount_in * 997
    return (af * ro) // (ri * 1000 + af)


def _quote_v3_buy(w3, weth_addr: str, token_addr: str,
                   amount_in: int, fee_bps: int, block: int) -> int:
    from web3 import Web3
    quoter_addr = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    abi = json.loads(
        '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},'
        '{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},'
        '{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],'
        '"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",'
        '"outputs":[{"name":"amountOut","type":"uint256"},'
        '{"name":"sqrtPriceX96After","type":"uint160"},'
        '{"name":"initializedTicksCrossed","type":"uint32"},'
        '{"name":"gasEstimate","type":"uint256"}],'
        '"stateMutability":"nonpayable","type":"function"}]'
    )
    q = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=abi)
    weth_cs = Web3.to_checksum_address(weth_addr)
    token_cs = Web3.to_checksum_address(token_addr)
    fee = fee_bps * 100
    result = q.functions.quoteExactInputSingle(
        (weth_cs, token_cs, amount_in, fee, 0)
    ).call(block_identifier=block)
    return int(result[0])


def _quote_v3_sell(w3, token_addr: str, weth_addr: str,
                    amount_in: int, fee_bps: int, block: int) -> int:
    from web3 import Web3
    quoter_addr = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    abi = json.loads(
        '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},'
        '{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},'
        '{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],'
        '"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",'
        '"outputs":[{"name":"amountOut","type":"uint256"},'
        '{"name":"sqrtPriceX96After","type":"uint160"},'
        '{"name":"initializedTicksCrossed","type":"uint32"},'
        '{"name":"gasEstimate","type":"uint256"}],'
        '"stateMutability":"nonpayable","type":"function"}]'
    )
    q = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=abi)
    token_cs = Web3.to_checksum_address(token_addr)
    weth_cs = Web3.to_checksum_address(weth_addr)
    fee = fee_bps * 100
    result = q.functions.quoteExactInputSingle(
        (token_cs, weth_cs, amount_in, fee, 0)
    ).call(block_identifier=block)
    return int(result[0])


def _ts_to_block(w3, target_ts: int) -> int:
    """Approximate block number for a Unix timestamp using binary search."""
    latest = w3.eth.block_number
    latest_ts = int(w3.eth.get_block(latest)["timestamp"])
    if target_ts >= latest_ts:
        return latest
    blocks_back = (latest_ts - target_ts) // 2
    lo = max(0, latest - blocks_back - 5000)
    hi = latest
    for _ in range(20):
        mid = (lo + hi) // 2
        mid_ts = int(w3.eth.get_block(mid)["timestamp"])
        if mid_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return hi


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    dashcam_path = Path(os.environ.get("DASHCAM_PATH", "logs/dashcam.csv"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    rpc_url = os.environ.get("EVM_HTTP_URL", "")
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()

    dry = os.environ.get("LABEL_DRYRUN", "").strip().lower() in (
        "1", "true", "yes", "y", "on",
    )
    dry_limit = int(os.environ.get("LABEL_DRYRUN_LIMIT", "0") or 0)
    if dry:
        output_path = repo / "logs" / "label_patch_b_dryrun_sample.csv"
        stats_path = repo / "mvp" / "diagnostics" / "label_patch_b_dryrun.json"
        log.info("LABEL_DRYRUN: writing to %s (not LABELED_OUTPUT)", output_path)
    else:
        output_path = Path(os.environ.get("LABELED_OUTPUT", "logs/labeled_dataset.csv"))
        stats_path = None

    w3 = None
    archive_mode = False
    if rpc_url:
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if w3.is_connected():
                try:
                    w3.eth.get_block(w3.eth.block_number - 100_000)
                    archive_mode = True
                    log.info("Archive RPC detected -- using executable labels when v2/v3 + meta")
                except Exception:
                    log.info("Non-archive RPC -- falling back to approximate labels")
            else:
                w3 = None
        except ImportError:
            log.warning("web3 not installed, using approximate labels")

    if not dashcam_path.exists():
        log.error("%s not found", dashcam_path)
        sys.exit(1)

    rows: list[dict[str, str]] = []
    with dashcam_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    log.info("Found %d dashcam records", len(rows))

    now_ms = int(time.time() * 1000)
    min_age_ms = 35 * 60 * 1000
    max_age_days = int(os.environ.get("LABEL_MAX_AGE_DAYS", "30") or "30")
    max_age_ms = max_age_days * 86400 * 1000 if max_age_days > 0 else 0
    stale_skipped = 0

    eligible: list[dict[str, str]] = []
    for row in rows:
        ts = int(row.get("timestamp", 0))
        if now_ms - ts < min_age_ms:
            continue
        if max_age_ms and now_ms - ts > max_age_ms:
            stale_skipped += 1
            continue
        eligible.append(row)
    if dry_limit > 0 and len(eligible) > dry_limit:
        log.info("LABEL_DRYRUN_LIMIT=%d: using last %d of %d age-eligible rows",
                 dry_limit, dry_limit, len(eligible))
        eligible = eligible[-dry_limit:]

    pools = {
        a.strip().lower() for a in
        (e.get("token_address") or "" for e in eligible)
        if a.strip()
    }
    ch_meta = load_pool_meta_ch(
        ch_url, ch_db, ch_user, ch_password, chain_id, pools,
    )
    r_meta = load_pool_meta_redis(redis_url, pools)
    pool_meta = merge_pool_meta(ch_meta, r_meta)
    log.info("Pool meta: %d from ClickHouse, %d from Redis, %d merged (unique)",
             len(ch_meta), len(r_meta), len(pool_meta))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if dry:
        (repo / "mvp" / "diagnostics").mkdir(parents=True, exist_ok=True)

    labeled_count = 0
    positive_count = 0
    skipped = 0
    label_type_used = "approximate"

    total_by: Counter = Counter()
    attempted_executable: Counter = Counter()
    success_executable: Counter = Counter()
    written_by: Counter = Counter()
    failed_v2v3: Counter = Counter()
    fail_reason: Counter = Counter()
    weth = "0x4200000000000000000000000000000000000006"
    weth_c = weth
    entropy_present_rows = 0
    entropy_missing_rows = 0
    entropy_nonzero_rows = 0
    entropy_values: list[float] = []
    n_eligible = len(eligible)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(LABELED_OUTPUT_COLUMNS)

        for j, row in enumerate(eligible):
            ts = int(row.get("timestamp", 0))
            pool_raw = (row.get("token_address") or "").strip()
            if not pool_raw:
                fail_reason["empty_token_address"] += 1
                skipped += 1
                continue
            if not _valid_hex_addr(pool_raw):
                total_by["invalid"] += 1
                fail_reason["invalid_token_address"] += 1
                skipped += 1
                continue

            pl = pool_raw.lower()
            meta = pool_meta.get(pl)
            rj = _reject_reason(meta)
            if rj is not None:
                bucket, reason = rj
                total_by[bucket] += 1
                fail_reason[reason] += 1
                skipped += 1
                continue

            pt = (meta or {}).get("pool_type", "").lower()
            assert pt in ("v2", "v3")
            t0, t1 = (meta or {}).get("t0", ""), (meta or {}).get("t1", "")
            token_for_quote = _non_weth_token(t0, t1)
            assert token_for_quote
            total_by[pt] += 1
            fee_bps = int((meta or {}).get("fee_bps", 30) or 30) if pt == "v3" else 30

            label = -1
            net_return = 0.0
            lt = "no_data"

            if archive_mode and w3:
                signal_ts_sec = ts // 1000
                exit_ts_sec = signal_ts_sec + 1800
                try:
                    entry_block = _ts_to_block(w3, signal_ts_sec)
                    exit_block = _ts_to_block(w3, exit_ts_sec)
                    attempted_executable[pt] += 1
                    label, net_return, lt = label_executable(
                        w3, pl, pt, token_for_quote, weth_c, fee_bps,
                        notional_wei, entry_block, exit_block,
                    )
                    if label >= 0 and lt == "executable":
                        success_executable[pt] += 1
                except Exception as e:
                    log.debug("executable label error for %s: %s", pl, e)
                    label, net_return, lt = -1, 0.0, "quote_failed"

            if not (label >= 0 and lt == "executable"):
                label, net_return, lt = label_approximate(
                    ch_url, ch_db, ch_user, ch_password,
                    pl, chain_id, ts,
                )

            if label < 0:
                failed_v2v3[pt] += 1
                rfin = (lt or "no_data").lower()
                if rfin in ("no_data", "quote_failed"):
                    fail_reason[rfin] += 1
                else:
                    fail_reason["unlabeled"] += 1
                skipped += 1
                continue

            label_type_used = lt

            entropy_raw = row.get("unique_buyer_entropy_5m")
            entropy_val = _parse_optional_float(entropy_raw)
            if entropy_val is None:
                entropy_source = "missing"
                entropy_out = ""
                entropy_missing_rows += 1
            else:
                entropy_source = "dashcam"
                entropy_out = round(entropy_val, 6)
                entropy_present_rows += 1
                entropy_values.append(entropy_val)
                if entropy_val != 0.0:
                    entropy_nonzero_rows += 1

            protocol = row.get("protocol") or pt
            pool_address = row.get("pool_address") or pl
            writer.writerow([
                ts, pl, row.get("symbol", ""),
                row.get("momentum_val", "0"),
                row.get("whale_val", "0"),
                row.get("risk_val", "0"),
                row.get("activity_val", "0"),
                entropy_out,
                entropy_source,
                pool_address,
                pt,
                protocol,
                row.get("tx_hash", ""),
                row.get("log_index", ""),
                row.get("block_number", ""),
                row.get("triggered_signal", "0"),
                label, round(net_return, 6), lt,
            ])
            written_by[pt] += 1
            if lt == "executable":
                written_by[pt + "_executable"] += 1
            elif lt == "approximate":
                written_by[pt + "_approx"] += 1
            labeled_count += 1
            if label == 1:
                positive_count += 1

            if (j + 1) % 100 == 0:
                log.info("Processed %d/%d eligible, labeled %d (positive %d)",
                         j + 1, n_eligible, labeled_count, positive_count)

    rate = positive_count / labeled_count * 100 if labeled_count > 0 else 0.0
    log.info(
        "Done: %d labeled (%d positive = %.1f%%), %d skipped, %d stale (>%dd) dropped",
        labeled_count, positive_count, rate, skipped, stale_skipped, max_age_days,
    )
    log.info("Label type (last row): %s", label_type_used)
    entropy_total = entropy_present_rows + entropy_missing_rows
    entropy_nonzero_rate = (entropy_nonzero_rows / entropy_present_rows) if entropy_present_rows else 0.0
    entropy_mean = (sum(entropy_values) / len(entropy_values)) if entropy_values else None
    entropy_stats = {
        "entropy_present_rows": entropy_present_rows,
        "entropy_missing_rows": entropy_missing_rows,
        "entropy_nonzero_rows": entropy_nonzero_rows,
        "entropy_nonzero_rate": round(entropy_nonzero_rate, 6),
        "entropy_mean": None if entropy_mean is None else round(entropy_mean, 6),
        "entropy_p50": None if not entropy_values else round(float(_percentile(entropy_values, 0.50)), 6),
        "entropy_p95": None if not entropy_values else round(float(_percentile(entropy_values, 0.95)), 6),
        "entropy_max": None if not entropy_values else round(max(entropy_values), 6),
        "entropy_total_checked_rows": entropy_total,
    }
    log.info("Output: %s", output_path)
    log.info("Entropy stats: %s", entropy_stats)
    log.info("Labelability by pool_type: total %s", dict(total_by))
    log.info("Executable attempted %s, success (executable) %s",
             dict(attempted_executable), dict(success_executable))
    log.info("Rows written by pool %s, failed (no label) for v2/v3 %s",
             dict(written_by), dict(failed_v2v3))
    log.info("fail_reason: %s", dict(fail_reason.most_common()))
    log.info(
        "v2 total=%d success_executable=%d | v3 total=%d success_executable=%d | "
        "aero_vamm_rejected=%d aero_cl_rejected=%d unknown_rejected=%d",
        total_by.get("v2", 0), success_executable.get("v2", 0),
        total_by.get("v3", 0), success_executable.get("v3", 0),
        total_by.get("aero_vamm", 0), total_by.get("aero_cl", 0), total_by.get("unknown", 0),
    )

    if dry and stats_path is not None:
        payload: dict[str, Any] = {
            "total_by_pool_type": dict(total_by),
            "attempted_executable": dict(attempted_executable),
            "success_executable": dict(success_executable),
            "written_by": dict(written_by),
            "failed_v2v3": dict(failed_v2v3),
            "fail_reason": dict(fail_reason),
            "entropy_stats": entropy_stats,
            "output_columns": LABELED_OUTPUT_COLUMNS,
            "audit_only_columns": AUDIT_ONLY_COLUMNS,
            "summary": {
                "v2_rows": total_by.get("v2", 0),
                "v2_success_executable": success_executable.get("v2", 0),
                "v3_rows": total_by.get("v3", 0),
                "v3_success_executable": success_executable.get("v3", 0),
                "aero_vamm_rejected": total_by.get("aero_vamm", 0),
                "aero_cl_rejected": total_by.get("aero_cl", 0),
                "unknown_rejected": total_by.get("unknown", 0),
            },
        }
        with stats_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        log.info("Dry-run stats JSON: %s", stats_path)

    if (not dry) and label_type_used == "approximate":
        log.warning(
            "FIREWALL: Labels are approximate (velocity proxy). "
            "Model trained on this data must NOT be deployed as production. "
            "P0-3 is only complete with executable-quote labels."
        )


if __name__ == "__main__":
    main()
