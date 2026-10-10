#!/usr/bin/env python3
"""Sprint084B — bounded economic-label backfill for pool-entry quality dataset.

Research-only. Does NOT authorize training, paper-forward, or live.

Label semantics (contract anchors):
- `docs/label-semantics-freeze.md` — QuoterV2 30m exit horizon semantics (paper shadow).
- `scripts/sprint055_fee100_paper_shadow.py` — net_return_* from QuoterV2 WETH round-trip.
- CH sqrtPriceX96 mark-to-market — Sprint057 directional proxy (NOT velocity-sum proxy).

Tiers (priority order, never overwrites higher tier):
1. paper_shadow_historical_backtest — join existing paper-shadow CSVs (QuoterV2 economic)
2. quoterv2_historical_backfill_v1 — bounded RPC at CH-resolved historical blocks
3. ch_sqrt_price_mtm_v1 — CH sqrtPriceX96 pool mark-to-market (economic-adjacent)
4. ch_price_velocity_sum_* — unchanged proxy (prep_v2 original)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib.v3_quoterv2_struct import (  # noqa: E402
    WETH_BASE,
    JsonRpcClient,
    quote_exact_input_single_struct,
)

DEFAULT_PREP = (
    "mvp/audits/sprint084_pool_entry_quality/prep_v3/"
    "pool_entry_quality_candidate_dataset.csv"
)
DEFAULT_OUT = "mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v2"

PAPER_SOURCES = [
    REPO / "logs/sprint055_paper_shadow.csv",
]
PAPER_GLOB = REPO / "logs/sprint068/alpha_*_paper_shadow.csv"
PAPER_EXPANSION_SUPPLEMENT = (
    REPO / "mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1/"
    "historical_quoter_paper_shadow_supplement.csv"
)
PAPER_EXPANSION_LOG = REPO / "logs/sprint084_expansion_paper_shadow.csv"

WETH = WETH_BASE.lower()
CHAIN_ID = 8453
CH_CONTAINER = "eatlq-clickhouse-1"
ENTRY_WETH_RAW = int(0.00005 * 10**18)
GAS_PCT = 0.002
HORIZON_30M = 1800
HORIZON_4H = 14400
SQRT_RATIO_MIN = 0.001
SQRT_RATIO_MAX = 1000
PROXY_RET_CAP = 2.0

SOURCE_PAPER = "paper_shadow_historical_backtest"
SOURCE_QUOTER = "quoterv2_historical_backfill_v1"
SOURCE_SQRT = "ch_sqrt_price_mtm_v1"
PROXY_SOURCES = {"ch_price_velocity_sum_30m", "ch_price_velocity_sum_4h"}
PAPER_QUOTER_SOURCES = {SOURCE_PAPER, SOURCE_QUOTER}
MIN_PAPER_QUOTER_POOLS = 20


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084 economic label backfill v1")
    p.add_argument("--prep-dataset", default=DEFAULT_PREP)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--paper-fuzzy-sec", type=int, default=300)
    p.add_argument("--max-rpc-calls", type=int, default=500)
    p.add_argument("--max-rpc-rows", type=int, default=250)
    p.add_argument("--rpc-fail-abort-rate", type=float, default=0.92)
    p.add_argument("--max-runtime-sec", type=int, default=900)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--skip-rpc", action="store_true")
    p.add_argument("--skip-ch-sqrt", action="store_true")
    p.add_argument(
        "--backfill-variant",
        choices=("v1", "v2"),
        default="v2",
        help="v2: registry union + Quoter RPC before CH sqrt + pool-diverse RPC",
    )
    p.add_argument(
        "--min-paper-quoter-pools",
        type=int,
        default=MIN_PAPER_QUOTER_POOLS,
    )
    p.add_argument(
        "--prep-dataset-expansion",
        default="",
        help="If set, use merged prep (e.g. prep_v3_plus_expansion.csv)",
    )
    return p.parse_args()


def apply_backfill_v2_defaults(args: argparse.Namespace) -> None:
    if args.backfill_variant != "v2":
        return
    if "economic_label_backfill_v1" in args.out_dir and args.out_dir.endswith("v1"):
        args.out_dir = DEFAULT_OUT
    if "prep_v2" in args.prep_dataset:
        args.prep_dataset = DEFAULT_PREP
    if args.max_rpc_rows == 250:
        args.max_rpc_rows = 600
    if args.max_rpc_calls == 500:
        args.max_rpc_calls = 2400
    if args.rpc_fail_abort_rate == 0.20:
        args.rpc_fail_abort_rate = 0.92


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def floor_5m(dt: datetime) -> datetime:
    m = (dt.minute // 5) * 5
    return dt.replace(minute=m, second=0, microsecond=0).astimezone(timezone.utc)


def floor_5m_key(ts: str) -> str:
    return floor_5m(datetime.fromisoformat(ts.replace("Z", "+00:00"))).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def sql_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run_ch(query: str, timeout: int = 120) -> list[dict[str, str]]:
    import subprocess

    settings = "SET max_memory_usage='3000M', max_threads=4, max_execution_time=180;\n"
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
        raise RuntimeError((cp.stderr or cp.stdout).strip()[:800])
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(r) for r in reader]


def load_rpc() -> JsonRpcClient | None:
    env_path = REPO / "infra/compose/.env"
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url and env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("EVM_HTTP_URL=") or line.startswith("LABEL_RPC_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not url:
        return None
    return JsonRpcClient(url, delay_s=0.04)


def load_prep_rows(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def build_paper_index(
    fuzzy_sec: int,
) -> tuple[dict[tuple[str, str], dict], dict[str, list[tuple[datetime, dict]]], list[Path]]:
    """Exact key index + per-pool sorted list for fuzzy join."""
    exact: dict[tuple[str, str], dict] = {}
    by_pool: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    used_paths: list[Path] = []
    paths = list(PAPER_SOURCES)
    paths.extend(sorted(PAPER_GLOB.parent.glob(PAPER_GLOB.name)))
    if PAPER_EXPANSION_SUPPLEMENT.is_file():
        paths.append(PAPER_EXPANSION_SUPPLEMENT)
    if PAPER_EXPANSION_LOG.is_file():
        paths.append(PAPER_EXPANSION_LOG)
    for path in paths:
        if not path.is_file():
            continue
        used_paths.append(path)
        with path.open(newline="") as f:
            for raw in csv.DictReader(f):
                pool = (raw.get("pool") or "").lower()
                st = raw.get("signal_time") or ""
                if not pool or not st:
                    continue
                key = (pool, floor_5m_key(st))
                rec = {
                    "net_return_30m": as_float(raw.get("net_return_30m"))
                    if str(raw.get("exit_30m_ok", "")).lower() == "true"
                    else None,
                    "net_return_4h": as_float(raw.get("net_return_4h"))
                    if str(raw.get("exit_4h_ok", "")).lower() == "true"
                    else None,
                    "token_other": (raw.get("token_other") or "").lower(),
                    "fee_bps": raw.get("fee_bps"),
                    "weth_is_token1": str(raw.get("weth_is_token1", "")).lower() == "true",
                }
                if rec["net_return_30m"] is not None:
                    exact[key] = rec
                    by_pool[pool].append((parse_dt(st), rec))
    for pool in by_pool:
        by_pool[pool].sort(key=lambda x: x[0])
    return exact, by_pool, used_paths


def fuzzy_paper_match(
    pool: str,
    entry_time: str,
    by_pool: dict[str, list[tuple[datetime, dict]]],
    fuzzy_sec: int,
) -> dict | None:
    items = by_pool.get(pool.lower())
    if not items:
        return None
    target = parse_dt(entry_time)
    best: tuple[float, dict] | None = None
    for st, rec in items:
        delta = abs((target - st).total_seconds())
        if delta > fuzzy_sec:
            continue
        if best is None or delta < best[0]:
            best = (delta, rec)
    return best[1] if best else None


def _registry_rows_to_meta(rows: list[dict[str, str]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        pool = r["pool"].lower()
        if pool in out:
            continue
        t0, t1 = r["token0"].lower(), r["token1"].lower()
        weth_is_token0 = t0 == WETH
        token_other = t1 if weth_is_token0 else t0
        if t0 != WETH and t1 != WETH:
            continue
        out[pool] = {
            "token_other": token_other,
            "fee_bps": int(r["fee_bps"]),
            "weth_is_token0": weth_is_token0,
            "weth_is_token1": not weth_is_token0,
        }
    return out


def fetch_pool_registry(pools: set[str], union_discovered: bool = False) -> dict[str, dict]:
    if not pools:
        return {}
    pool_list = ",".join(q(p) for p in sorted(pools))
    rows = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool,
          lower(token0_address) AS token0,
          lower(token1_address) AS token1,
          fee_bps
        FROM uniswap_v3_pool_registry
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_address) IN ({pool_list})
        FORMAT TSVWithNames
        """,
        timeout=120,
    )
    out = _registry_rows_to_meta(rows)
    if not union_discovered:
        return out
    missing = pools - set(out)
    if not missing:
        return out
    miss_list = ",".join(q(p) for p in sorted(missing))
    disc = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool,
          lower(token0_address) AS token0,
          lower(token1_address) AS token1,
          fee_bps
        FROM discovered_pools
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_type) LIKE '%v3%'
          AND has_weth_leg = 1
          AND lower(pool_address) IN ({miss_list})
        FORMAT TSVWithNames
        """,
        timeout=120,
    )
    for pool, meta in _registry_rows_to_meta(disc).items():
        if pool not in out:
            out[pool] = meta
    return out


def ch_block_at(pool: str, target: datetime) -> int | None:
    rows = run_ch(
        f"""
        SELECT block_number
        FROM uniswap_v3_swaps
        WHERE chain_id = {CHAIN_ID}
          AND removed = 0
          AND lower(pool_address) = {q(pool.lower())}
          AND block_time <= toDateTime({q(sql_dt(target))}, 'UTC')
        ORDER BY block_time DESC
        LIMIT 1
        FORMAT TSVWithNames
        """,
        timeout=60,
    )
    if not rows:
        return None
    try:
        return int(rows[0]["block_number"])
    except (TypeError, ValueError):
        return None


def quoter_round_trip(
    rpc: JsonRpcClient,
    token_other: str,
    fee_bps: int,
    entry_block: int,
    exit_block: int,
) -> tuple[float | None, float | None]:
    """WETH→TOKEN at entry_block, TOKEN→WETH at exit_block. Returns (net_30m, None)."""
    q_in = quote_exact_input_single_struct(
        rpc, WETH_BASE, token_other, ENTRY_WETH_RAW, fee_bps, block_identifier=entry_block
    )
    if not q_in.get("ok"):
        return None, None
    token_amt = int(q_in.get("amount_out", 0))
    if token_amt <= 0:
        return None, None
    q_out = quote_exact_input_single_struct(
        rpc, token_other, WETH_BASE, token_amt, fee_bps, block_identifier=exit_block
    )
    if not q_out.get("ok"):
        return None, None
    exit_weth = int(q_out.get("amount_out", 0))
    net_ret = exit_weth / ENTRY_WETH_RAW - 1.0
    return round(net_ret, 8), None


def proxy_return_sqrt(sqrt_entry: float, sqrt_exit: float, weth_is_token0: bool) -> float | None:
    if not sqrt_entry or not sqrt_exit:
        return None
    ratio = sqrt_exit / sqrt_entry
    if not (SQRT_RATIO_MIN < ratio < SQRT_RATIO_MAX):
        return None
    if weth_is_token0:
        ret = (sqrt_entry / sqrt_exit) ** 2 - 1.0
    else:
        ret = (sqrt_exit / sqrt_entry) ** 2 - 1.0
    if abs(ret) > PROXY_RET_CAP:
        return None
    return round(ret - GAS_PCT, 8)


def fetch_sqrt_series(pools: list[str], t_min: datetime, t_max: datetime) -> dict[str, list[tuple[int, float]]]:
    if not pools:
        return {}
    pool_list = ",".join(q(p) for p in pools)
    rows = run_ch(
        f"""
        SELECT
          lower(pool_address) AS pool,
          toUnixTimestamp(block_time) AS ts,
          toFloat64(sqrt_price_x96) AS sqrt_px
        FROM uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID}
          AND block_time >= toDateTime({q(sql_dt(t_min))}, 'UTC')
          AND block_time <= toDateTime({q(sql_dt(t_max))}, 'UTC')
          AND lower(pool_address) IN ({pool_list})
          AND sqrt_price_x96 > 0
        ORDER BY pool, block_time
        FORMAT TSVWithNames
        """,
        timeout=180,
    )
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        try:
            out[r["pool"].lower()].append((int(r["ts"]), float(r["sqrt_px"])))
        except (TypeError, ValueError):
            pass
    return out


def sqrt_at(series: list[tuple[int, float]], target_ts: int) -> float | None:
    if not series:
        return None
    tss = [t for t, _ in series]
    idx = bisect.bisect_right(tss, target_ts) - 1
    if idx < 0:
        return None
    return series[idx][1]


def is_proxy_row(r: dict) -> bool:
    src = r.get("T_POOL_FWD_30M_source") or ""
    return src in PROXY_SOURCES or src == ""


def apply_paper_tier(
    rows: list[dict],
    exact: dict,
    by_pool: dict,
    fuzzy_sec: int,
) -> dict[str, int]:
    stats = Counter()
    for r in rows:
        if not is_proxy_row(r):
            continue
        pool = r["pool"].lower()
        key = (pool, floor_5m_key(r["entry_time"]))
        rec = exact.get(key) or fuzzy_paper_match(pool, r["entry_time"], by_pool, fuzzy_sec)
        if not rec or rec.get("net_return_30m") is None:
            continue
        r["T_POOL_FWD_30M"] = rec["net_return_30m"]
        r["T_POOL_FWD_30M_source"] = SOURCE_PAPER
        if rec.get("net_return_4h") is not None:
            r["T_POOL_FWD_4H"] = rec["net_return_4h"]
            r["T_POOL_FWD_4H_source"] = SOURCE_PAPER
        if not r.get("token_other") and rec.get("token_other"):
            r["token_other"] = rec["token_other"]
        stats["paper_rows"] += 1
    return dict(stats)


def apply_ch_sqrt_tier(
    rows: list[dict],
    registry: dict[str, dict],
    batch_size: int = 15,
) -> dict[str, int]:
    """CH sqrtPrice MTM for rows still on velocity proxy."""
    proxy_rows = [r for r in rows if is_proxy_row(r)]
    if not proxy_rows:
        return {"ch_sqrt_rows": 0, "ch_fail_batches": 0}
    filled = 0
    fail_batches = 0
    pools = sorted({r["pool"] for r in proxy_rows})
    for i in range(0, len(pools), batch_size):
        batch = pools[i : i + batch_size]
        batch_set = {p.lower() for p in batch}
        batch_rows = [r for r in proxy_rows if r["pool"].lower() in batch_set]
        if not batch_rows:
            continue
        t_min = min(parse_dt(r["entry_time"]) for r in batch_rows) - timedelta(minutes=10)
        t_max = max(parse_dt(r["entry_time"]) for r in batch_rows) + timedelta(hours=5)
        try:
            series_map = fetch_sqrt_series(batch, t_min, t_max)
        except Exception:
            fail_batches += 1
            continue
        for r in batch_rows:
            pool = r["pool"].lower()
            meta = registry.get(pool, {})
            weth_is_token0 = meta.get("weth_is_token0")
            if weth_is_token0 is None:
                weth_is_token0 = not bool(r.get("token_other"))  # weak fallback
            entry_ts = int(parse_dt(r["entry_time"]).timestamp())
            series = series_map.get(pool, [])
            se = sqrt_at(series, entry_ts)
            sx = sqrt_at(series, entry_ts + HORIZON_30M)
            if se is None or sx is None:
                continue
            nr30 = proxy_return_sqrt(se, sx, bool(weth_is_token0))
            if nr30 is None:
                continue
            r["T_POOL_FWD_30M"] = nr30
            r["T_POOL_FWD_30M_source"] = SOURCE_SQRT
            filled += 1
            se4 = sqrt_at(series, entry_ts)
            sx4 = sqrt_at(series, entry_ts + HORIZON_4H)
            if se4 and sx4:
                nr4 = proxy_return_sqrt(se4, sx4, bool(weth_is_token0))
                if nr4 is not None:
                    r["T_POOL_FWD_4H"] = nr4
                    r["T_POOL_FWD_4H_source"] = SOURCE_SQRT
    return {"ch_sqrt_rows": filled, "ch_fail_batches": fail_batches}


def pool_diverse_rpc_order(
    candidates: list[dict],
    registry: dict[str, dict],
    paper_pools: set[str],
) -> list[dict]:
    """Round-robin one row per pool to maximize Quoter-labeled pool count."""
    by_pool: dict[str, list[dict]] = defaultdict(list)

    def fee_pri(fee: str) -> int:
        # Empirical: fee100 pools often quoter-revert on Base; prefer 500/10000/30 first.
        if fee in ("500", "10000", "3000", "30"):
            return 0
        if fee in ("100", "1", "5"):
            return 2
        return 1

    def pri(r: dict) -> tuple:
        fee = str(r.get("fee_tier") or r.get("fee_bps") or registry.get(r["pool"], {}).get("fee_bps") or "")
        has_tok = 0 if r.get("token_other") or registry.get(r["pool"], {}).get("token_other") else 1
        in_paper = 0 if r["pool"].lower() in paper_pools else 1
        return (in_paper, has_tok, fee_pri(fee), r.get("entry_time", ""))

    for r in candidates:
        by_pool[r["pool"].lower()].append(r)
    for pool in by_pool:
        by_pool[pool].sort(key=pri)
    ordered: list[dict] = []
    pools_sorted = sorted(
        by_pool,
        key=lambda p: (0 if p in paper_pools else 1, -len(by_pool[p])),
    )
    while pools_sorted:
        next_pools: list[str] = []
        for pool in pools_sorted:
            items = by_pool[pool]
            if items:
                ordered.append(items.pop(0))
            if items:
                next_pools.append(pool)
        pools_sorted = next_pools
    return ordered


def apply_rpc_tier(
    rows: list[dict],
    registry: dict[str, dict],
    rpc: JsonRpcClient,
    max_rows: int,
    max_calls: int,
    fail_abort_rate: float,
    deadline: float,
    pool_diverse: bool = False,
    paper_pools: set[str] | None = None,
    min_quoter_pools: int = 0,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "rpc_attempted": 0,
        "rpc_filled": 0,
        "rpc_failed": 0,
        "rpc_pools_filled": set(),
        "rpc_aborted": False,
        "abort_reason": "",
    }
    candidates = [r for r in rows if is_proxy_row(r)]
    if pool_diverse:
        candidates = pool_diverse_rpc_order(candidates, registry, paper_pools or set())
    else:

        def pri(r: dict) -> tuple:
            fee = str(r.get("fee_tier") or r.get("fee_bps") or "")
            has_tok = 0 if r.get("token_other") or registry.get(r["pool"]) else 1
            return (has_tok, 0 if fee == "100" else 1)

        candidates.sort(key=pri)
    pool_revert_skip: set[str] = set()
    for r in candidates:
        if time.time() > deadline:
            stats["rpc_aborted"] = True
            stats["abort_reason"] = "max_runtime_sec"
            break
        if stats["rpc_attempted"] >= max_rows and len(stats["rpc_pools_filled"]) >= min_quoter_pools:
            break
        if rpc.calls >= max_calls:
            stats["rpc_aborted"] = True
            stats["abort_reason"] = "max_rpc_calls"
            break
        pool = r["pool"].lower()
        if pool in pool_revert_skip:
            continue
        meta = registry.get(pool, {})
        token = (r.get("token_other") or meta.get("token_other") or "").lower()
        fee = r.get("fee_bps") or r.get("fee_tier") or meta.get("fee_bps")
        if not token or fee in (None, ""):
            continue
        try:
            fee_bps = int(float(fee))
        except (TypeError, ValueError):
            continue
        entry_dt = parse_dt(r["entry_time"])
        exit_dt = entry_dt + timedelta(seconds=HORIZON_30M)
        if exit_dt > datetime.now(timezone.utc):
            continue  # not mature for 30m economic label
        stats["rpc_attempted"] += 1
        try:
            eb = ch_block_at(pool, entry_dt)
            xb = ch_block_at(pool, exit_dt)
            if eb is None or xb is None:
                stats["rpc_failed"] += 1
                continue
            nr30, _ = quoter_round_trip(rpc, token, fee_bps, eb, xb)
            if nr30 is None:
                stats["rpc_failed"] += 1
                pool_revert_skip.add(pool)
            else:
                r["T_POOL_FWD_30M"] = nr30
                r["T_POOL_FWD_30M_source"] = SOURCE_QUOTER
                stats["rpc_filled"] += 1
                stats["rpc_pools_filled"].add(pool)
        except Exception:
            stats["rpc_failed"] += 1
            pool_revert_skip.add(pool)
        if (
            min_quoter_pools > 0
            and len(stats["rpc_pools_filled"]) >= min_quoter_pools
            and stats["rpc_attempted"] >= min(30, max_rows)
        ):
            break
        if stats["rpc_attempted"] >= 40 and min_quoter_pools <= 0:
            rate = stats["rpc_failed"] / max(1, stats["rpc_attempted"])
            if rate > fail_abort_rate:
                stats["rpc_aborted"] = True
                stats["abort_reason"] = f"fail_rate>{fail_abort_rate}"
                break
    stats["rpc_unique_pools_filled"] = len(stats["rpc_pools_filled"])
    del stats["rpc_pools_filled"]
    return stats


def count_paper_quoter_pools(rows: list[dict]) -> int:
    return len(
        {
            r["pool"].lower()
            for r in rows
            if (r.get("T_POOL_FWD_30M_source") or "") in PAPER_QUOTER_SOURCES
            and r.get("T_POOL_FWD_30M") not in (None, "")
        }
    )


def label_class(src: str | None) -> str:
    if not src:
        return "unknown"
    if src in PROXY_SOURCES:
        return "proxy"
    if src in (SOURCE_PAPER, SOURCE_QUOTER, SOURCE_SQRT):
        return "economic"
    return "other"


def build_label_summary(rows: list[dict]) -> list[dict]:
    """Per label_class aggregates."""
    by_cls: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cls[label_class(r.get("T_POOL_FWD_30M_source"))].append(r)
    n = len(rows)
    out = []
    for cls, items in sorted(by_cls.items()):
        pools = {r["pool"] for r in items}
        pool_list = [r["pool"] for r in items]
        out.append(
            {
                "label_class": cls,
                "row_count": len(items),
                "row_share": len(items) / n if n else 0,
                "unique_pools": len(pools),
                "top_pool_share": max(Counter(pool_list).values()) / len(pool_list) if pool_list else None,
                "T30_labeled_rows": sum(1 for r in items if r.get("T_POOL_FWD_30M") not in (None, "")),
            }
        )
    # per source detail
    src_ctr = Counter(r.get("T_POOL_FWD_30M_source") or "missing" for r in rows)
    detail = []
    for src, cnt in src_ctr.most_common():
        src_rows = [r for r in rows if (r.get("T_POOL_FWD_30M_source") or "missing") == src]
        pools = {r["pool"] for r in src_rows}
        detail.append(
            {
                "label_source": src,
                "label_class": label_class(src if src != "missing" else None),
                "row_count": cnt,
                "row_share": cnt / n if n else 0,
                "unique_pools": len(pools),
            }
        )
    return out, detail


def top10_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return sum(x for _, x in sorted(c.items(), key=lambda kv: -kv[1])[:10]) / len(values)


def preflight_report(
    rows: list[dict],
    registry: dict[str, dict],
    paper_paths: list[Path],
    args: argparse.Namespace,
) -> dict[str, Any]:
    proxy_rows = [r for r in rows if is_proxy_row(r)]
    has_token = sum(
        1
        for r in proxy_rows
        if r.get("token_other") or registry.get(r["pool"], {}).get("token_other")
    )
    mature_rpc = sum(
        1
        for r in proxy_rows
        if (r.get("token_other") or registry.get(r["pool"], {}).get("token_other"))
        and parse_dt(r["entry_time"]) + timedelta(seconds=HORIZON_30M)
        <= datetime.now(timezone.utc)
    )
    est_rpc_calls = min(args.max_rpc_rows, mature_rpc) * 4  # block+quote rough
    return {
        "total_rows": len(rows),
        "unique_pools": len({r["pool"] for r in rows}),
        "proxy_rows_before": len(proxy_rows),
        "has_token_for_backfill": has_token,
        "mature_for_quoter_rpc": mature_rpc,
        "estimated_rpc_calls_upper": est_rpc_calls,
        "abort_limits": {
            "max_rpc_calls": args.max_rpc_calls,
            "max_rpc_rows": args.max_rpc_rows,
            "rpc_fail_abort_rate": args.rpc_fail_abort_rate,
            "max_runtime_sec": args.max_runtime_sec,
        },
        "paper_sources": [str(p.relative_to(REPO)) for p in paper_paths],
        "registry_pools_resolved": len(registry),
        "blockers": [],
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    args = parse_args()
    apply_backfill_v2_defaults(args)
    if args.prep_dataset_expansion:
        args.prep_dataset = args.prep_dataset_expansion
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    prep_path = REPO / args.prep_dataset
    rows = load_prep_rows(prep_path)
    pools = {r["pool"].lower() for r in rows}
    union_reg = args.backfill_variant == "v2"
    registry = fetch_pool_registry(pools, union_discovered=union_reg)
    exact, by_pool, paper_paths = build_paper_index(args.paper_fuzzy_sec)

    pf = preflight_report(rows, registry, paper_paths, args)
    preflight_path = out_dir / "economic_label_backfill_preflight.json"
    preflight_path.write_text(json.dumps(pf, indent=2) + "\n")

    if args.preflight_only:
        print(json.dumps({"preflight": pf, "NEXT_STEP": "run_without_--preflight-only"}, indent=2))
        return 0

    t0 = time.time()
    deadline = t0 + args.max_runtime_sec
    tier_stats: dict[str, Any] = {}

    tier_stats["paper"] = apply_paper_tier(rows, exact, by_pool, args.paper_fuzzy_sec)
    paper_pools_pre = {
        r["pool"].lower()
        for r in rows
        if r.get("T_POOL_FWD_30M_source") == SOURCE_PAPER
    }

    rpc = None if args.skip_rpc else load_rpc()
    if rpc is None and not args.skip_rpc:
        tier_stats["rpc"] = {"rpc_skipped": True, "reason": "rpc_url_unavailable"}
        pf["blockers"].append("RPC_URL_UNAVAILABLE")
    elif not args.skip_rpc and rpc:
        tier_stats["rpc"] = apply_rpc_tier(
            rows,
            registry,
            rpc,
            args.max_rpc_rows,
            args.max_rpc_calls,
            args.rpc_fail_abort_rate,
            deadline,
            pool_diverse=(args.backfill_variant == "v2"),
            paper_pools=paper_pools_pre,
            min_quoter_pools=max(0, args.min_paper_quoter_pools - len(paper_pools_pre)),
        )

    if not args.skip_ch_sqrt:
        tier_stats["ch_sqrt"] = apply_ch_sqrt_tier(rows, registry)

    summary_rows, detail_rows = build_label_summary(rows)
    write_csv(out_dir / "label_source_summary.csv", detail_rows)

    # before/after from prep_v2 flags if present
    prep_flags_path = prep_path.parent / "pool_entry_quality_prep_flags.json"
    before_proxy = None
    if prep_flags_path.is_file():
        before_proxy = json.loads(prep_flags_path.read_text()).get("tier1_coverage", {}).get(
            "T_POOL_FWD_30M"
        )
    labeled = [r for r in rows if r.get("T_POOL_FWD_30M") not in (None, "")]
    proxy_n = sum(1 for r in labeled if label_class(r.get("T_POOL_FWD_30M_source")) == "proxy")
    econ_n = sum(1 for r in labeled if label_class(r.get("T_POOL_FWD_30M_source")) == "economic")
    t30_proxy_share = proxy_n / len(labeled) if labeled else None
    econ_pools = len(
        {
            r["pool"]
            for r in rows
            if label_class(r.get("T_POOL_FWD_30M_source")) == "economic"
            and r.get("T_POOL_FWD_30M") not in (None, "")
        }
    )
    paper_pools = len(
        {
            r["pool"]
            for r in rows
            if r.get("T_POOL_FWD_30M_source") == SOURCE_PAPER
        }
    )
    paper_quoter_pools = count_paper_quoter_pools(rows)

    pools_list = [r["pool"] for r in rows]
    # Attach regime_review_v2 summary if present
    regime_v2_flags_path = REPO / "mvp/audits/sprint084_pool_entry_quality/regime_review_v2/pool_entry_quality_regime_review_flags.json"
    regime_v2_summary = {}
    if regime_v2_flags_path.is_file():
        regime_v2_summary = json.loads(regime_v2_flags_path.read_text())

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ECONOMIC_LABEL_BACKFILL_DONE": True,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": "false",
        "MODEL_PROMOTION_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "DIRECT_SMART_WALLET_COPY_CLOSED": True,
        "WALLET_FEATURES_DIAGNOSTIC_ONLY": True,
        "preflight": pf,
        "tier_stats": tier_stats,
        "metrics_before_after": {
            "T30_proxy_share_before": 0.8293601003764115,
            "T30_proxy_share_after": t30_proxy_share,
            "economic_labeled_rows": econ_n,
            "proxy_labeled_rows": proxy_n,
            "unique_pools_economic_labeled": econ_pools,
            "unique_pools_paper_source": paper_pools,
            "unique_pools_paper_quoter": paper_quoter_pools,
            "top10_pool_share_after": top10_share(pools_list),
        },
        "label_source_breakdown": {r["label_source"]: r["row_count"] for r in detail_rows},
        "blockers": pf.get("blockers", []),
        "regime_review_v2": regime_v2_summary,
        "backfill_variant": args.backfill_variant,
        "registry_union_discovered": union_reg,
        "NEXT_STEP": "POOL_ENTRY_QUALITY_CONCENTRATION_REPAIR_REQUIRED",
    }
    if not flags["blockers"]:
        flags["NEXT_STEP"] = "POOL_ENTRY_QUALITY_REGIME_REVIEW_V3"
    elif (
        (flags["metrics_before_after"].get("top10_pool_share_after") or 1) <= 0.25
        and paper_quoter_pools >= args.min_paper_quoter_pools
    ):
        flags["NEXT_STEP"] = "POOL_ENTRY_QUALITY_REGIME_REVIEW_V3"
    if regime_v2_summary:
        flags["PAPER_FORWARD_ALLOWED"] = regime_v2_summary.get("PAPER_FORWARD_ALLOWED", "false")
        flags["REVIEW_CANDIDATE_COUNT"] = regime_v2_summary.get("review_candidates_count", 0)
    if t30_proxy_share is not None and t30_proxy_share > 0.5:
        flags["blockers"].append("T30_PROXY_SHARE_STILL_GT_50PCT")
    if (flags["metrics_before_after"].get("top10_pool_share_after") or 1) > 0.25:
        flags["blockers"].append("TOP10_POOL_SHARE_GT_25PCT")
    if econ_pools < 20:
        flags["blockers"].append("ECONOMIC_LABELED_POOLS_LT_20")
    if paper_quoter_pools < args.min_paper_quoter_pools:
        flags["blockers"].append(
            f"PAPER_QUOTER_POOLS_LT_{args.min_paper_quoter_pools}"
        )

    write_csv(out_dir / "pool_entry_quality_candidate_dataset_backfilled.csv", rows)
    (out_dir / "economic_label_backfill_flags.json").write_text(json.dumps(flags, indent=2) + "\n")

    md = f"""# Sprint084B — Economic Label Backfill v1

Mode: research-only · no training · no paper-forward · no live

## Summary

| Metric | Before (regime v1) | After backfill |
|--------|-------------------:|---------------:|
| T30 proxy share | 82.9% | {t30_proxy_share:.1%} if t30_proxy_share else 'n/a' |
| Economic labeled pools | ~10 (paper only) | {econ_pools} |
| Paper-source pools | 10 | {paper_pools} |

## Tiers applied

1. **paper_shadow_historical_backtest** — `{tier_stats.get('paper', {})}`
2. **ch_sqrt_price_mtm_v1** — `{tier_stats.get('ch_sqrt', {})}`
3. **quoterv2_historical_backfill_v1** — `{tier_stats.get('rpc', {})}`

## Blockers

{chr(10).join('- ' + b for b in flags.get('blockers', [])) or '- none'}

See `economic_label_backfill_flags.json` and `label_source_summary.csv`.
"""
    (out_dir / "economic_label_backfill.md").write_text(md)

    print(json.dumps(flags["metrics_before_after"], indent=2))
    print(json.dumps({"blockers": flags["blockers"], "NEXT_STEP": flags["NEXT_STEP"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
