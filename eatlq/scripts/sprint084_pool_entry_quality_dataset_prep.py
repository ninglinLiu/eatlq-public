#!/usr/bin/env python3
"""Sprint084 — pool-entry quality dataset prep (research-only, no training).

Builds pool-entry / onset-event rows (not wallet-copy primary grain), joins
wallet diagnostics as pool-level guards only, emits gates and manifests.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sprint083_expanded_v2_sidecar_merge_refresh import (  # noqa: E402
    fetch_pool_features_5m_range,
    latest_feature,
)
from v3_smart_wallet_roundtrip_dataset_builder import (  # noqa: E402
    fetch_pool_births,
    floor_5m,
    parse_ch_dt,
)

DEFAULT_PAPER = "logs/sprint055_paper_shadow.csv"
DEFAULT_WALLET_DIAG = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_prep_expanded_v2/"
    "copyability_safe_dataset_sample.csv"
)
DEFAULT_POOL_FEATURE_SUPPLEMENT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv"
)
DEFAULT_OUT = "mvp/audits/sprint084_pool_entry_quality/prep_v1"
DEFAULT_UNIVERSE_CSV = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv"
)
DEFAULT_PREP_V1_FLAGS = "mvp/audits/sprint084_pool_entry_quality/prep_v1/pool_entry_quality_prep_flags.json"

INVENTORY_SOURCES: list[tuple[str, str, str, str, str | None]] = [
    ("logs/sprint055_paper_shadow.csv", "paper_shadow", "signal_time", "pool", "fee_bps"),
    (
        "mvp/audits/sprint083_fee500_paper_shadow/"
        "v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_dataset_sample.csv",
        "wallet_copyability_safe",
        "entry_time",
        "pool",
        "fee_tier",
    ),
    (
        "mvp/audits/sprint083_fee500_paper_shadow/"
        "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv",
        "wallet_builder_expanded_v2",
        "entry_time",
        "pool",
        "fee_tier",
    ),
    ("mvp/audits/sprint038a/young_pool_onset_floor_audit.csv", "young_pool_onset_audit", "", "pool", None),
]

TIER1_FEATURES = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "activity_heat",
    "token_volatility_5m",
    "pool_age_sec",
    "net_flow_liquidity_proxy",
    "onset_lag_sec",
]

TIER2_GUARDS = [
    "amount_weth",
    "amount_usd_notional",
    "pool_copy_delay_120s_median",
    "pool_copy_delay_120s_p90",
    "pool_closed_loop_rate",
    "pool_wallet_buyer_count",
    "pool_past_7d_roi_median",
    "pool_profit_factor_median",
    "fee_tier",
    "fee_bps",
]

TARGET_COLS = [
    "T_POOL_FWD_30M",
    "T_POOL_FWD_4H",
    "T_ONSET_PASS",
    "T_ENTRY_QUOTE_OK",
]

FORBIDDEN_AS_FEATURES = [
    "wallet",
    "token",
    "token_other",
    "future_return_5m",
    "future_return_30m",
    "future_return_2h",
    "future_return_6h",
    "realized_wallet_pnl",
    "realized_wallet_pnl_weth",
    "copy_delay_30s",
    "copy_delay_60s",
    "copy_delay_120s",
    "direct_wallet_rank",
    "trade_size_bucket",
    "block_number",
]

# Sprint072 A3-style onset band (research default)
ONSET_POOL_AGE_MIN = 3600
ONSET_POOL_AGE_MAX = 100000
ONSET_ACTIVITY_HEAT_MIN = 10
ONSET_BUY_PRESSURE_MIN = 0.5

GATE_TOP_POOL_SHARE_LT = 0.20
GATE_TOP10_SHARE_LT = 0.25
GATE_MIN_UNIQUE_POOLS = 50
GATE_MIN_TIER1_COVERAGE = 0.70
GATE_MIN_TARGET_COVERAGE = 0.50
GATE_MIN_TARGET_COVERAGE_V2 = 0.70
# Optional tier1 — gate failure does not block review if only these fail
TIER1_OPTIONAL_FOR_GATE = frozenset({"onset_lag_sec"})
CH_FORWARD_WINDOWS_30M = 6
CH_FORWARD_WINDOWS_4H = 48


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084 pool-entry quality dataset prep")
    p.add_argument("--paper-csv", default=DEFAULT_PAPER)
    p.add_argument(
        "--wallet-diagnostic-csv",
        default=DEFAULT_WALLET_DIAG,
        help="Wallet round-trip CSV for pool-level diagnostic aggregates only",
    )
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument(
        "--fee-tier",
        type=int,
        default=None,
        help="Optional fee filter applied to BOTH paper (fee_bps) and wallet rows",
    )
    p.add_argument(
        "--wallet-window-fee-tier",
        type=int,
        default=500,
        help="Fee filter for wallet pool-window rows only (default 500 Base V3)",
    )
    p.add_argument(
        "--include-wallet-pool-windows",
        action="store_true",
        default=True,
        help="Add fee-filtered wallet rows collapsed to pool+5m windows",
    )
    p.add_argument(
        "--no-include-wallet-pool-windows",
        action="store_false",
        dest="include_wallet_pool_windows",
    )
    p.add_argument("--max-buy-weth", type=float, default=500.0)
    p.add_argument("--per-pool-cap", type=int, default=200)
    p.add_argument(
        "--pool-feature-supplement-csv",
        default=DEFAULT_POOL_FEATURE_SUPPLEMENT,
        help="Optional pool-level tier1 backfill from wallet builder sample (pool medians)",
    )
    p.add_argument("--emit-gates-only", action="store_true")
    p.add_argument(
        "--source-mode",
        choices=("paper_only", "hybrid", "inventory_only"),
        default="paper_only",
        help="hybrid = paper + balanced wallet pool-onset universe + CH tier1/labels",
    )
    p.add_argument("--universe-csv", default=DEFAULT_UNIVERSE_CSV)
    p.add_argument(
        "--balanced-per-pool-cap",
        type=int,
        default=35,
        help="Max pool-entry events per pool (concentration guard)",
    )
    p.add_argument("--min-unique-pools-target", type=int, default=50)
    p.add_argument(
        "--ch-tier1-backfill",
        action="store_true",
        default=False,
        help="Per-pool bounded pool_features_5m fetch + forward-return labels",
    )
    p.add_argument(
        "--inventory-only",
        action="store_true",
        help="Write source_inventory.csv and exit",
    )
    p.add_argument("--prep-variant", choices=("v1", "v2", "v3"), default="v1")
    p.add_argument(
        "--global-per-pool-cap",
        type=int,
        default=None,
        help="Post-merge cap across all sources (v3 default: balanced-per-pool-cap)",
    )
    p.add_argument(
        "--onset-activity-min",
        type=int,
        default=None,
        help="Min median activity_heat for wallet pool-onset universe (v2 default 5)",
    )
    p.add_argument(
        "--ch-onset-candidates",
        action="store_true",
        default=False,
        help="Add per-pool CH pool_features_5m onset windows (bounded)",
    )
    p.add_argument("--ch-onset-window-days", type=int, default=60)
    p.add_argument(
        "--paper-per-pool-cap",
        type=int,
        default=None,
        help="Cap paper-shadow rows per pool (v2 default: balanced-per-pool-cap)",
    )
    return p.parse_args()


def apply_v2_defaults(args: argparse.Namespace) -> None:
    if args.prep_variant not in ("v2", "v3"):
        return
    if args.out_dir == DEFAULT_OUT:
        args.out_dir = (
            "mvp/audits/sprint084_pool_entry_quality/prep_v3"
            if args.prep_variant == "v3"
            else "mvp/audits/sprint084_pool_entry_quality/prep_v2"
        )
    args.source_mode = "hybrid"
    args.ch_tier1_backfill = True
    args.ch_onset_candidates = True
    if args.onset_activity_min is None:
        args.onset_activity_min = 5
    if args.paper_per_pool_cap is None:
        args.paper_per_pool_cap = args.balanced_per_pool_cap
    global GATE_MIN_TARGET_COVERAGE
    GATE_MIN_TARGET_COVERAGE = GATE_MIN_TARGET_COVERAGE_V2


def apply_v3_defaults(args: argparse.Namespace) -> None:
    if args.prep_variant != "v3":
        return
    apply_v2_defaults(args)
    # Concentration repair: one cap across merged sources; drop 3-pool wallet windows.
    args.include_wallet_pool_windows = False
    if args.balanced_per_pool_cap == 35:
        args.balanced_per_pool_cap = 24
    if args.paper_per_pool_cap is None or args.paper_per_pool_cap == 35:
        args.paper_per_pool_cap = args.balanced_per_pool_cap
    if args.global_per_pool_cap is None:
        args.global_per_pool_cap = args.balanced_per_pool_cap


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def as_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def floor_5m_iso(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    m = (dt.minute // 5) * 5
    dt = dt.replace(minute=m, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def stable_entry_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"pe_{h}"


def top_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return max(c.values()) / len(values)


def coverage(rows: list[dict], col: str) -> float:
    if not rows:
        return 0.0
    n = sum(1 for r in rows if as_float(r.get(col)) is not None or r.get(col) not in (None, ""))
    return n / len(rows)


def median_safe(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def pctile(vals: list[float], q: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    i = min(len(vals) - 1, max(0, int(math.ceil(q * len(vals)) - 1)))
    return vals[i]


def net_flow_proxy_from_pressure_heat(
    buy_pressure: float | None, activity: float | None
) -> float | None:
    """Signed flow-intensity proxy when volume-weighted net_flow is unavailable in CH."""
    if buy_pressure is None or activity is None:
        return None
    return (2.0 * buy_pressure - 1.0) * activity


def inventory_row(
    repo: Path,
    rel: str,
    kind: str,
    time_col: str,
    pool_col: str,
    fee_col: str | None,
) -> dict[str, Any]:
    path = repo / rel
    row: dict[str, Any] = {
        "source_id": kind,
        "path": rel,
        "exists": path.is_file(),
        "rows": 0,
        "unique_pools": 0,
        "date_min": "",
        "date_max": "",
        "fee_distribution": "",
        "target_t30_rows": 0,
        "target_t4h_rows": 0,
        "usable_for_pool_entry": "no",
        "notes": "",
    }
    if not path.is_file():
        row["notes"] = "missing"
        return row
    pools: set[str] = set()
    times: list[datetime] = []
    fees: Counter[str] = Counter()
    t30 = t4h = 0
    n = 0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            n += 1
            p = (raw.get(pool_col) or raw.get("pool_address") or "").lower()
            if p:
                pools.add(p)
            if fee_col and raw.get(fee_col):
                fees[str(raw[fee_col])] += 1
            if time_col and raw.get(time_col):
                try:
                    times.append(
                        datetime.fromisoformat(raw[time_col].replace("Z", "+00:00"))
                    )
                except ValueError:
                    pass
            if as_bool(raw.get("exit_30m_ok")) and raw.get("net_return_30m"):
                t30 += 1
            if as_bool(raw.get("exit_4h_ok")) and raw.get("net_return_4h"):
                t4h += 1
    row["rows"] = n
    row["unique_pools"] = len(pools)
    if times:
        row["date_min"] = min(times).isoformat()
        row["date_max"] = max(times).isoformat()
    if fees:
        row["fee_distribution"] = json.dumps(dict(fees.most_common(8)))
    row["target_t30_rows"] = t30
    row["target_t4h_rows"] = t4h
    if kind == "paper_shadow":
        row["usable_for_pool_entry"] = "yes_primary_onset"
        row["notes"] = (
            "onset grain; only 10 pools in current file — narrow for universe"
            if len(pools) <= 15
            else "onset grain"
        )
    elif kind.startswith("wallet"):
        row["usable_for_pool_entry"] = "yes_pool_window_aggregate"
        row["notes"] = "collapse to pool+5m; wallet fields diagnostic only"
    else:
        row["usable_for_pool_entry"] = "reference"
    return row


def scan_source_inventory(repo: Path) -> list[dict[str, Any]]:
    rows = [
        inventory_row(repo, rel, kind, tcol, pcol, fcol)
        for rel, kind, tcol, pcol, fcol in INVENTORY_SOURCES
    ]
    for p in sorted((repo / "logs" / "sprint068").glob("alpha_*_paper_shadow.csv")):
        rel = str(p.relative_to(repo))
        rows.append(inventory_row(repo, rel, p.name, "signal_time", "pool", "fee_bps"))
    return rows


def parse_entry_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def balanced_take(sorted_items: list[Any], cap: int) -> list[Any]:
    if len(sorted_items) <= cap:
        return sorted_items
    if cap <= 1:
        return sorted_items[:cap]
    out: list[Any] = []
    for i in range(cap):
        idx = int(i * (len(sorted_items) - 1) / (cap - 1))
        if not out or sorted_items[idx] != out[-1]:
            out.append(sorted_items[idx])
    return out


def load_ch_onset_candidates(
    pools: list[str],
    per_pool_cap: int,
    window_days: int,
    activity_min: int,
) -> tuple[list[dict], str]:
    """Per-pool bounded CH fetch — onset windows from pool_features_5m (not wallet-copy)."""
    from v3_smart_wallet_roundtrip_dataset_builder import q, run_ch, sql_dt

    if not pools:
        return [], "no_pools"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(window_days, 1))
    out: list[dict] = []
    fail = 0
    for pool in sorted(set(pools)):
        try:
            raw = run_ch(
                f"""
                SELECT
                  window_start,
                  price_velocity_5m,
                  buy_pressure_ratio,
                  token_volatility_5m,
                  activity_heat
                FROM pool_features_5m
                WHERE chain_id = 8453
                  AND lower(pool_address) = {q(pool.lower())}
                  AND window_start >= toDateTime({q(sql_dt(start))}, 'UTC')
                  AND window_start <= toDateTime({q(sql_dt(end))}, 'UTC')
                  AND activity_heat >= {int(activity_min)}
                ORDER BY window_start
                FORMAT TSVWithNames
                """,
                timeout=120,
            )
        except Exception:
            fail += 1
            continue
        windows = [_normalize_ch_feature_row(r) for r in raw]
        windows.sort(key=lambda x: x["window_start"])
        idxs = balanced_take(list(range(len(windows))), per_pool_cap)
        for i in idxs:
            w = windows[i]
            t5 = w["window_start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            bp = w.get("buy_pressure_ratio")
            heat = w.get("activity_heat")
            out.append(
                {
                    "entry_id": stable_entry_id("ch_onset", pool.lower(), t5),
                    "source": "ch_pool_onset_candidate",
                    "entry_time": t5,
                    "pool": pool.lower(),
                    "token_other": "",
                    "fee_bps": None,
                    "fee_tier": None,
                    "price_velocity_5m": w.get("price_velocity_5m"),
                    "buy_pressure_ratio": bp,
                    "activity_heat": heat,
                    "token_volatility_5m": w.get("token_volatility_5m"),
                    "pool_age_sec": None,
                    "net_flow_liquidity_proxy": net_flow_proxy_from_pressure_heat(
                        as_float(bp), as_float(heat)
                    ),
                    "net_flow_liquidity_proxy_source": "pressure_heat_proxy_v1",
                    "onset_lag_sec": None,
                    "T_POOL_FWD_30M": None,
                    "T_POOL_FWD_4H": None,
                    "T_POOL_FWD_30M_source": None,
                    "T_POOL_FWD_4H_source": None,
                    "T_ONSET_PASS": "true",
                    "T_ENTRY_QUOTE_OK": None,
                    "wallet_diagnostic_joined": "false",
                }
            )
    status = f"ok:pools={len(set(pools))},rows={len(out)},ch_fail_pools={fail}"
    return out, status


def list_pools_from_csv(path: Path) -> list[str]:
    if not path.is_file():
        return []
    pools: set[str] = set()
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            p = (raw.get("pool") or raw.get("pool_address") or "").lower()
            if p:
                pools.add(p)
    return sorted(pools)


def load_wallet_pool_onset_universe(
    path: Path,
    per_pool_cap: int,
    max_buy_weth: float,
    activity_min: int = ONSET_ACTIVITY_HEAT_MIN,
) -> tuple[list[dict], str]:
    """Collapse wallet builder rows to pool+5m onset events (not wallet-copy grain)."""
    if not path.is_file():
        return [], f"missing:{path}"
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            if as_bool(raw.get("router_flag")):
                continue
            aw = as_float(raw.get("amount_weth") or raw.get("amount_proxy"))
            if aw is not None and aw > max_buy_weth:
                continue
            pool = (raw.get("pool") or "").lower()
            et = raw.get("entry_time") or ""
            if not pool or not et:
                continue
            buckets[(pool, floor_5m_iso(et))].append(raw)

    by_pool: dict[str, list[tuple[str, list[dict]]]] = defaultdict(list)
    for (pool, t5), group in buckets.items():
        activity = median_safe([as_float(r.get("activity_heat")) for r in group])
        pressure = median_safe([as_float(r.get("buy_pressure_ratio")) for r in group])
        pool_age = median_safe([as_float(r.get("pool_age_sec")) for r in group])
        if activity is None or activity < activity_min:
            continue
        by_pool[pool].append((t5, group))

    out: list[dict] = []
    for pool, items in by_pool.items():
        items.sort(key=lambda x: x[0])
        for t5, group in balanced_take(items, per_pool_cap):
            def med(col: str) -> float | None:
                return median_safe([as_float(r.get(col)) for r in group])

            activity = med("activity_heat")
            pressure = med("buy_pressure_ratio")
            pool_age = med("pool_age_sec")
            onset_pass = (
                activity is not None
                and pressure is not None
                and pool_age is not None
                and activity >= ONSET_ACTIVITY_HEAT_MIN
                and pressure >= ONSET_BUY_PRESSURE_MIN
                and ONSET_POOL_AGE_MIN <= pool_age <= ONSET_POOL_AGE_MAX
            )
            bp = med("buy_pressure_ratio")
            heat = med("activity_heat")
            out.append(
                {
                    "entry_id": stable_entry_id("onset", pool, t5),
                    "source": "wallet_pool_onset_universe",
                    "entry_time": t5,
                    "pool": pool,
                    "token_other": (group[0].get("token") or "").lower(),
                    "fee_bps": None,
                    "fee_tier": group[0].get("fee_tier"),
                    "price_velocity_5m": med("price_velocity_5m"),
                    "buy_pressure_ratio": pressure,
                    "activity_heat": activity,
                    "token_volatility_5m": med("token_volatility_5m"),
                    "pool_age_sec": pool_age,
                    "net_flow_liquidity_proxy": net_flow_proxy_from_pressure_heat(bp, heat),
                    "net_flow_liquidity_proxy_source": "pressure_heat_proxy_v1",
                    "onset_lag_sec": None,
                    "T_POOL_FWD_30M": None,
                    "T_POOL_FWD_4H": None,
                    "T_POOL_FWD_30M_source": None,
                    "T_POOL_FWD_4H_source": None,
                    "T_ONSET_PASS": "true" if onset_pass else "false",
                    "T_ENTRY_QUOTE_OK": None,
                    "wallet_diagnostic_joined": "false",
                }
            )
    return out, f"ok:{path}"


def paper_label_index(paper_rows: list[dict]) -> dict[tuple[str, str], dict]:
    idx: dict[tuple[str, str], dict] = {}
    for r in paper_rows:
        key = (r["pool"], floor_5m_iso(r["entry_time"]))
        idx[key] = r
    return idx


def overlay_paper_labels(rows: list[dict], paper_idx: dict[tuple[str, str], dict]) -> int:
    n = 0
    for r in rows:
        key = (r["pool"], floor_5m_iso(r["entry_time"]))
        p = paper_idx.get(key)
        if not p:
            continue
        if p.get("T_POOL_FWD_30M") is not None:
            r["T_POOL_FWD_30M"] = p["T_POOL_FWD_30M"]
            r["T_POOL_FWD_30M_source"] = "paper_shadow_historical_backtest"
        if p.get("T_POOL_FWD_4H") is not None:
            r["T_POOL_FWD_4H"] = p["T_POOL_FWD_4H"]
            r["T_POOL_FWD_4H_source"] = "paper_shadow_historical_backtest"
        if p.get("T_ENTRY_QUOTE_OK") is not None:
            r["T_ENTRY_QUOTE_OK"] = p["T_ENTRY_QUOTE_OK"]
        if p.get("onset_lag_sec") is not None and r.get("onset_lag_sec") in (None, ""):
            r["onset_lag_sec"] = p["onset_lag_sec"]
        n += 1
    return n


def _normalize_ch_feature_row(r: dict) -> dict:
    ws = r.get("window_start")
    if isinstance(ws, datetime):
        window_start = ws
    else:
        window_start = parse_ch_dt(str(ws))
    return {
        "window_start": window_start,
        "price_velocity_5m": as_float(r.get("price_velocity_5m")),
        "buy_pressure_ratio": as_float(r.get("buy_pressure_ratio")),
        "token_volatility_5m": as_float(r.get("token_volatility_5m")),
        "activity_heat": as_float(r.get("activity_heat")),
    }


def sum_forward_velocity(
    series: list[dict], entry_time: datetime, n_windows: int
) -> float | None:
    if not series:
        return None
    cutoff = floor_5m(entry_time - timedelta(minutes=5))
    idx = -1
    for i, row in enumerate(series):
        if row["window_start"] <= cutoff:
            idx = i
        else:
            break
    if idx < 0:
        return None
    vals: list[float] = []
    for j in range(idx + 1, min(len(series), idx + 1 + n_windows)):
        v = as_float(series[j].get("price_velocity_5m"))
        if v is not None:
            vals.append(v)
    if len(vals) < max(1, n_windows // 2):
        return None
    return sum(vals)


def fetch_pool_series_cached(
    cache: dict[str, list[dict]], pool: str, entry_time: datetime
) -> list[dict]:
    pool = pool.lower()
    if pool in cache:
        return cache[pool]
    start = entry_time - timedelta(hours=2)
    end = entry_time + timedelta(hours=5)
    try:
        raw = fetch_pool_features_5m_range(pool, start, end)
    except Exception:
        raw = []
    series = [_normalize_ch_feature_row(r) for r in raw]
    series.sort(key=lambda x: x["window_start"])
    cache[pool] = series
    return series


def apply_ch_tier1_and_forward_labels(
    rows: list[dict], pool_series: dict[str, list[dict]]
) -> dict[str, int]:
  stats = {"ch_tier1_filled": 0, "ch_forward_labeled": 0, "ch_fetch_fail_pools": 0}
  pools_needed = {r["pool"] for r in rows}
  for pool in sorted(pools_needed):
    times = [parse_entry_dt(r["entry_time"]) for r in rows if r["pool"] == pool]
    if not times:
      continue
    start = min(times) - timedelta(hours=2)
    end = max(times) + timedelta(hours=5)
    try:
      raw = fetch_pool_features_5m_range(pool, start, end)
    except Exception:
      stats["ch_fetch_fail_pools"] += 1
      pool_series[pool] = []
      continue
    series = [_normalize_ch_feature_row(r) for r in raw]
    series.sort(key=lambda x: x["window_start"])
    pool_series[pool] = series

  for r in rows:
    et = parse_entry_dt(r["entry_time"])
    series = pool_series.get(r["pool"], [])
    if not series:
      continue
    feat = latest_feature(series, et)
    filled = False
    for col in (
      "price_velocity_5m",
      "buy_pressure_ratio",
      "activity_heat",
      "token_volatility_5m",
    ):
      if r.get(col) in (None, "") and feat.get(col) is not None:
        r[col] = feat[col]
        r[f"{col}_source"] = "ch_pool_features_5m"
        filled = True
    if r.get("net_flow_liquidity_proxy") in (None, ""):
      nf = net_flow_proxy_from_pressure_heat(
        as_float(r.get("buy_pressure_ratio")),
        as_float(r.get("activity_heat")),
      )
      if nf is not None:
        r["net_flow_liquidity_proxy"] = nf
        r["net_flow_liquidity_proxy_source"] = "pressure_heat_proxy_v1"
        filled = True
    if filled:
      stats["ch_tier1_filled"] += 1
    if r.get("T_POOL_FWD_30M") is None:
      v30 = sum_forward_velocity(series, et, CH_FORWARD_WINDOWS_30M)
      if v30 is not None:
        r["T_POOL_FWD_30M"] = v30
        r["T_POOL_FWD_30M_source"] = "ch_price_velocity_sum_30m"
        stats["ch_forward_labeled"] += 1
    if r.get("T_POOL_FWD_4H") is None:
      v4 = sum_forward_velocity(series, et, CH_FORWARD_WINDOWS_4H)
      if v4 is not None:
        r["T_POOL_FWD_4H"] = v4
        r["T_POOL_FWD_4H_source"] = "ch_price_velocity_sum_4h"
  return stats


def load_prep_v1_snapshot(repo: Path) -> dict[str, Any]:
    path = repo / DEFAULT_PREP_V1_FLAGS
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def cap_rows_per_pool(rows: list[dict], cap: int) -> list[dict]:
    if cap <= 0:
        return rows
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pool[r["pool"]].append(r)
    out: list[dict] = []
    for pool, items in by_pool.items():
        items.sort(key=lambda x: x.get("entry_time", ""))
        out.extend(balanced_take(items, cap))
    return out


def load_paper_rows(
    path: Path, fee_tier: int | None, per_pool_cap: int | None = None
) -> tuple[list[dict], str]:
    if not path.is_file():
        return [], f"missing:{path}"
    rows: list[dict] = []
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            if fee_tier is not None and str(raw.get("fee_bps", "")) != str(fee_tier):
                continue
            pool = (raw.get("pool") or "").lower()
            if not pool:
                continue
            signal_time = raw.get("signal_time") or ""
            entry_id = stable_entry_id("paper", signal_time, pool)
            pool_age = as_float(raw.get("pool_age_sec"))
            activity = as_float(raw.get("activity_heat"))
            pressure = as_float(raw.get("buy_pressure_ratio"))
            onset_pass = (
                activity is not None
                and pressure is not None
                and pool_age is not None
                and activity >= ONSET_ACTIVITY_HEAT_MIN
                and pressure >= ONSET_BUY_PRESSURE_MIN
                and ONSET_POOL_AGE_MIN <= pool_age <= ONSET_POOL_AGE_MAX
            )
            t30 = as_float(raw.get("net_return_30m")) if as_bool(raw.get("exit_30m_ok")) else None
            t4h = as_float(raw.get("net_return_4h")) if as_bool(raw.get("exit_4h_ok")) else None
            row = {
                "entry_id": entry_id,
                "source": "paper_shadow_onset",
                "entry_time": signal_time,
                "pool": pool,
                "token_other": (raw.get("token_other") or "").lower(),
                "fee_bps": raw.get("fee_bps"),
                "fee_tier": raw.get("fee_bps"),  # alias for stratification
                "price_velocity_5m": raw.get("price_velocity_5m"),
                "buy_pressure_ratio": raw.get("buy_pressure_ratio"),
                "activity_heat": raw.get("activity_heat"),
                "token_volatility_5m": None,
                "pool_age_sec": raw.get("pool_age_sec"),
                "net_flow_liquidity_proxy": None,
                "onset_lag_sec": raw.get("onset_lag_sec"),
                "T_POOL_FWD_30M": t30,
                "T_POOL_FWD_4H": t4h,
                "T_POOL_FWD_30M_source": "paper_shadow_historical_backtest" if t30 is not None else None,
                "T_POOL_FWD_4H_source": "paper_shadow_historical_backtest" if t4h is not None else None,
                "T_ONSET_PASS": "true" if onset_pass else "false",
                "T_ENTRY_QUOTE_OK": raw.get("entry_quote_ok"),
                "entry_quote_ok_raw": raw.get("entry_quote_ok"),
                "composite_flag": raw.get("composite_flag"),
                "dominant_sender": raw.get("dominant_sender"),
                "is_0x6ff5": raw.get("is_0x6ff5"),
                "dom_sender_share": raw.get("dom_sender_share"),
                "wallet_diagnostic_joined": "false",
            }
            rows.append(row)
    if per_pool_cap:
        rows = cap_rows_per_pool(rows, per_pool_cap)
    return rows, f"ok:{path}"


def build_pool_wallet_diagnostics(
    path: Path, fee_tier: int | None, max_buy_weth: float
) -> tuple[dict[str, dict], str]:
    if not path.is_file():
        return {}, f"missing:{path}"
    by_pool: dict[str, list[dict]] = defaultdict(list)
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            if as_bool(raw.get("router_flag")):
                continue
            ft = raw.get("fee_tier")
            if fee_tier is not None and str(ft) != str(fee_tier):
                continue
            aw = as_float(raw.get("amount_weth") or raw.get("amount_proxy"))
            if aw is not None and aw > max_buy_weth:
                continue
            pool = (raw.get("pool") or "").lower()
            if pool:
                by_pool[pool].append(raw)

    diag: dict[str, dict] = {}
    for pool, group in by_pool.items():
        cd120 = [as_float(r.get("copy_delay_120s")) for r in group]
        cd120 = [x for x in cd120 if x is not None]
        aw = [as_float(r.get("amount_weth")) for r in group]
        aw = [x for x in aw if x is not None]
        ausd = [as_float(r.get("amount_usd_notional")) for r in group]
        ausd = [x for x in ausd if x is not None]
        wallets = {r.get("wallet", "").lower() for r in group if r.get("wallet")}
        closed = [as_bool(r.get("closed_loop_eligible")) for r in group]
        roi7 = [as_float(r.get("past_7d_roi")) for r in group]
        roi7 = [x for x in roi7 if x is not None]
        pf = [as_float(r.get("profit_factor")) for r in group]
        pf = [x for x in pf if x is not None]
        tv = [as_float(r.get("token_volatility_5m")) for r in group]
        tv = [x for x in tv if x is not None]
        diag[pool] = {
            "pool_copy_delay_120s_median": median_safe(cd120),
            "pool_copy_delay_120s_p90": pctile(cd120, 0.9),
            "pool_amount_weth_median": median_safe(aw),
            "pool_amount_weth_p90": pctile(aw, 0.9),
            "pool_amount_usd_notional_median": median_safe(ausd),
            "pool_closed_loop_rate": sum(closed) / len(closed) if closed else None,
            "pool_wallet_buyer_count": len(wallets),
            "pool_past_7d_roi_median": median_safe(roi7),
            "pool_profit_factor_median": median_safe(pf),
            "pool_token_volatility_5m_median": median_safe(tv),
            "pool_wallet_row_count": len(group),
        }
    return diag, f"ok:{path}"


def load_wallet_pool_window_rows(
    path: Path,
    fee_tier: int | None,
    max_buy_weth: float,
    per_pool_cap: int,
) -> tuple[list[dict], str]:
    if not path.is_file():
        return [], f"missing:{path}"
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            if as_bool(raw.get("router_flag")):
                continue
            ft = raw.get("fee_tier")
            if fee_tier is not None and str(ft) != str(fee_tier):
                continue
            aw = as_float(raw.get("amount_weth") or raw.get("amount_proxy"))
            if aw is not None and aw > max_buy_weth:
                continue
            pool = (raw.get("pool") or "").lower()
            et = raw.get("entry_time") or ""
            if not pool or not et:
                continue
            buckets[(pool, floor_5m_iso(et))].append(raw)

    out: list[dict] = []
    pool_counts: Counter[str] = Counter()
    for (pool, t5), group in sorted(buckets.items()):
        if pool_counts[pool] >= per_pool_cap:
            continue
        pool_counts[pool] += 1

        def med(col: str) -> float | None:
            return median_safe([as_float(r.get(col)) for r in group])

        activity = med("activity_heat")
        pressure = med("buy_pressure_ratio")
        pool_age = med("pool_age_sec")
        onset_pass = (
            activity is not None
            and pressure is not None
            and pool_age is not None
            and activity >= ONSET_ACTIVITY_HEAT_MIN
            and pressure >= ONSET_BUY_PRESSURE_MIN
            and ONSET_POOL_AGE_MIN <= pool_age <= ONSET_POOL_AGE_MAX
        )
        entry_id = stable_entry_id("wallet_pool", pool, t5)
        out.append(
            {
                "entry_id": entry_id,
                "source": "wallet_pool_window_aggregate",
                "entry_time": t5,
                "pool": pool,
                "token_other": (group[0].get("token") or "").lower(),
                "fee_bps": None,
                "fee_tier": group[0].get("fee_tier"),
                "price_velocity_5m": med("price_velocity_5m"),
                "buy_pressure_ratio": pressure,
                "activity_heat": activity,
                "token_volatility_5m": med("token_volatility_5m"),
                "pool_age_sec": pool_age,
                "net_flow_liquidity_proxy": None,
                "onset_lag_sec": None,
                "T_POOL_FWD_30M": None,
                "T_POOL_FWD_4H": None,
                "T_ONSET_PASS": "true" if onset_pass else "false",
                "T_ENTRY_QUOTE_OK": None,
                "target_note": "pool_fwd_targets_require_paper_or_ch;wallet_window_guard_only",
                "wallet_diagnostic_joined": "true",
            }
        )
    return out, f"ok:{path}"


def build_pool_tier1_supplement(path: Path) -> tuple[dict[str, dict], str]:
    if not path.is_file():
        return {}, f"missing:{path}"
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            pool = (raw.get("pool") or "").lower()
            if not pool:
                continue
            for col in (
                "token_volatility_5m",
                "price_velocity_5m",
                "buy_pressure_ratio",
                "activity_heat",
                "pool_age_sec",
            ):
                v = as_float(raw.get(col))
                if v is not None:
                    acc[pool][col].append(v)
    out: dict[str, dict] = {}
    for pool, cols in acc.items():
        out[pool] = {f"pool_median_{k}": median_safe(v) for k, v in cols.items()}
    return out, f"ok:{path}"


def apply_pool_diagnostics(rows: list[dict], diag: dict[str, dict]) -> None:
    for r in rows:
        d = diag.get(r["pool"])
        if not d:
            continue
        r["wallet_diagnostic_joined"] = "true"
        r["pool_copy_delay_120s_median"] = d.get("pool_copy_delay_120s_median")
        r["pool_copy_delay_120s_p90"] = d.get("pool_copy_delay_120s_p90")
        r["amount_weth"] = d.get("pool_amount_weth_median")
        r["amount_usd_notional"] = d.get("pool_amount_usd_notional_median")
        r["pool_closed_loop_rate"] = d.get("pool_closed_loop_rate")
        r["pool_wallet_buyer_count"] = d.get("pool_wallet_buyer_count")
        r["pool_past_7d_roi_median"] = d.get("pool_past_7d_roi_median")
        r["pool_profit_factor_median"] = d.get("pool_profit_factor_median")
        if r.get("token_volatility_5m") in (None, "") and d.get("pool_token_volatility_5m_median") is not None:
            r["token_volatility_5m"] = d["pool_token_volatility_5m_median"]
            r["token_volatility_5m_source"] = "wallet_pool_median"


def apply_pool_age_from_births(rows: list[dict], births: dict[str, datetime]) -> int:
    n = 0
    for r in rows:
        if r.get("pool_age_sec") not in (None, ""):
            continue
        birth = births.get(r["pool"])
        if not birth:
            continue
        et = parse_entry_dt(r["entry_time"])
        r["pool_age_sec"] = (et - birth).total_seconds()
        r["pool_age_sec_source"] = "ch_pool_first_swap"
        n += 1
    return n


def apply_tier1_supplement(rows: list[dict], supplement: dict[str, dict]) -> None:
    for r in rows:
        s = supplement.get(r["pool"])
        if not s:
            continue
        for col in TIER1_FEATURES:
            if col == "net_flow_liquidity_proxy":
                continue
            if r.get(col) not in (None, ""):
                continue
            med = s.get(f"pool_median_{col}")
            if med is not None:
                r[col] = med
                r[f"{col}_source"] = "pool_feature_supplement_csv"


def apply_concentration_fields(rows: list[dict]) -> dict[str, float | None]:
    pools = [r["pool"] for r in rows]
    tokens = [r.get("token_other") or "" for r in rows]
    n = len(rows)
    pool_c = Counter(pools)
    token_c = Counter(tokens)
    top_pool = max(pool_c, key=pool_c.get) if pool_c else ""
    top_token = max(token_c, key=token_c.get) if token_c else ""
    sorted_pools = sorted(pool_c.items(), key=lambda x: -x[1])
    top10_n = max(1, int(math.ceil(0.10 * n)))
    top10_pool_share = sum(c for _, c in sorted_pools[: max(1, min(10, len(sorted_pools)))]) / n if n else None
    return {
        "top_pool_share": top_share(pools),
        "top_token_share": top_share([t for t in tokens if t]),
        "top10_pool_share_design": top10_pool_share,
        "dominant_pool": top_pool,
        "dominant_token": top_token,
    }


def leakage_guard_pass(manifest: dict) -> tuple[bool, list[str]]:
    violations: list[str] = []
    for feat in manifest.get("tier1_candidates", []):
        if feat in FORBIDDEN_AS_FEATURES:
            violations.append(f"tier1_contains_forbidden:{feat}")
    for feat in manifest.get("tier2_guards", []):
        if feat in ("wallet", "token", "future_return_30m"):
            violations.append(f"tier2_contains_forbidden:{feat}")
    labels = manifest.get("target_labels_only", [])
    for t in TARGET_COLS:
        if t not in labels:
            violations.append(f"missing_target_label:{t}")
    return len(violations) == 0, violations


def decide_next_step(
    paper_status: str,
    row_count: int,
    tier1_cov: dict[str, float],
    target_cov: dict[str, float],
    gates_failed: list[str],
    unique_pools: int,
    min_pools_target: int,
) -> str:
    if paper_status.startswith("missing:") and row_count == 0:
        return "SOURCE_ARTIFACT_MISSING"
    if row_count == 0:
        return "SOURCE_ARTIFACT_MISSING"
    if unique_pools < min_pools_target:
        return "SOURCE_SUPPLY_BLOCKER"
    if any("target" in g for g in gates_failed):
        return "TARGET_COVERAGE_REPAIR_REQUIRED"
    if any("tier1" in g for g in gates_failed):
        return "FEATURE_COVERAGE_REPAIR_REQUIRED"
    if gates_failed:
        return "POOL_ENTRY_QUALITY_REVIEW_READY"
    return "POOL_ENTRY_QUALITY_REVIEW_READY"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    seen = set()
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
    apply_v2_defaults(args)
    apply_v3_defaults(args)
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    inventory = scan_source_inventory(REPO)
    write_csv(out_dir / "source_inventory.csv", inventory)
    if args.inventory_only or args.source_mode == "inventory_only":
        print(json.dumps({"inventory_rows": len(inventory)}, indent=2))
        return 0

    prep_v1_snap = load_prep_v1_snapshot(REPO)
    paper_path = REPO / args.paper_csv
    wallet_path = REPO / args.wallet_diagnostic_csv
    universe_path = REPO / args.universe_csv

    paper_cap = args.paper_per_pool_cap if args.prep_variant == "v2" else None
    paper_rows, paper_status = load_paper_rows(paper_path, args.fee_tier, paper_cap)
    # Pool diagnostics: all fee tiers unless --fee-tier set (broader join for paper rows)
    supplement_path = REPO / args.pool_feature_supplement_csv
    supplement, supplement_status = build_pool_tier1_supplement(supplement_path)

    diag, wallet_status = build_pool_wallet_diagnostics(
        wallet_path, args.fee_tier, args.max_buy_weth
    )

    wallet_window_rows: list[dict] = []
    wallet_window_status = "skipped"
    wallet_window_fee = args.fee_tier if args.fee_tier is not None else args.wallet_window_fee_tier
    if args.include_wallet_pool_windows:
        wallet_window_rows, wallet_window_status = load_wallet_pool_window_rows(
            wallet_path, wallet_window_fee, args.max_buy_weth, args.per_pool_cap
        )

    onset_universe_rows: list[dict] = []
    onset_status = "skipped"
    ch_onset_rows: list[dict] = []
    ch_onset_status = "skipped"
    if args.source_mode == "hybrid":
        act_min = args.onset_activity_min if args.onset_activity_min is not None else ONSET_ACTIVITY_HEAT_MIN
        onset_universe_rows, onset_status = load_wallet_pool_onset_universe(
            universe_path, args.balanced_per_pool_cap, args.max_buy_weth, act_min
        )
        if args.ch_onset_candidates:
            pool_list = list_pools_from_csv(universe_path)
            ch_onset_rows, ch_onset_status = load_ch_onset_candidates(
                pool_list,
                args.balanced_per_pool_cap,
                args.ch_onset_window_days,
                ONSET_ACTIVITY_HEAT_MIN,
            )

    # Deduplicate: paper rows take precedence over wallet/onset same pool+time
    paper_keys = {(r["pool"], floor_5m_iso(r["entry_time"])) for r in paper_rows}
    wallet_only = [
        r
        for r in wallet_window_rows
        if (r["pool"], floor_5m_iso(r["entry_time"])) not in paper_keys
    ]
    onset_only = [
        r
        for r in onset_universe_rows
        if (r["pool"], floor_5m_iso(r["entry_time"])) not in paper_keys
    ]
    onset_keys_pre = {
        (r["pool"], floor_5m_iso(r["entry_time"])) for r in onset_only
    }
    ch_only = [
        r
        for r in ch_onset_rows
        if (r["pool"], floor_5m_iso(r["entry_time"])) not in paper_keys
        and (r["pool"], floor_5m_iso(r["entry_time"])) not in onset_keys_pre
    ]
    onset_keys = onset_keys_pre | {
        (r["pool"], floor_5m_iso(r["entry_time"])) for r in ch_only
    }
    wallet_only = [
        r
        for r in wallet_only
        if (r["pool"], floor_5m_iso(r["entry_time"])) not in onset_keys
    ]
    paper_idx = paper_label_index(paper_rows)

    if args.source_mode == "hybrid":
        rows = ch_only + onset_only + wallet_only + paper_rows
    else:
        rows = paper_rows + wallet_only

    overlay_paper_labels(ch_only + onset_only + wallet_only, paper_idx)

    if args.global_per_pool_cap and args.global_per_pool_cap > 0:
        before = len(rows)
        rows = cap_rows_per_pool(rows, args.global_per_pool_cap)
        global_cap_trimmed = before - len(rows)
    else:
        global_cap_trimmed = 0

    ch_stats: dict[str, int] = {}
    pool_series: dict[str, list[dict]] = {}
    if args.ch_tier1_backfill and rows:
        ch_stats = apply_ch_tier1_and_forward_labels(rows, pool_series)
    apply_pool_diagnostics(rows, diag)
    apply_tier1_supplement(rows, supplement)

    pools = [r["pool"] for r in rows]
    pool_births = fetch_pool_births(set(pools), timeout=180) if pools else {}
    pool_age_filled = apply_pool_age_from_births(rows, pool_births)

    conc = apply_concentration_fields(rows)

    tier1_cov = {c: coverage(rows, c) for c in TIER1_FEATURES}
    target_cov = {c: coverage(rows, c) for c in TARGET_COLS}
    wallet_diag_cov = sum(1 for r in rows if r.get("wallet_diagnostic_joined") == "true") / max(
        1, len(rows)
    )
    tokens = [r.get("token_other") or "" for r in rows if r.get("token_other")]

    gates_failed: list[str] = []
    if len(rows) == 0:
        gates_failed.append("row_count_zero")
    if len(set(pools)) < GATE_MIN_UNIQUE_POOLS:
        gates_failed.append(f"unique_pools_lt_{GATE_MIN_UNIQUE_POOLS}")
    if conc.get("top_pool_share") is not None and conc["top_pool_share"] >= GATE_TOP_POOL_SHARE_LT:
        gates_failed.append(f"top_pool_share_gte_{GATE_TOP_POOL_SHARE_LT}")
    if conc.get("top10_pool_share_design") is not None and conc["top10_pool_share_design"] >= GATE_TOP10_SHARE_LT:
        gates_failed.append(f"top10_pool_share_gte_{GATE_TOP10_SHARE_LT}")
    for c, cov in tier1_cov.items():
        if cov < GATE_MIN_TIER1_COVERAGE and c not in TIER1_OPTIONAL_FOR_GATE:
            gates_failed.append(f"tier1_{c}_coverage_lt_{GATE_MIN_TIER1_COVERAGE}")
    tier1_optional_gaps = [
        c
        for c, cov in tier1_cov.items()
        if c in TIER1_OPTIONAL_FOR_GATE and cov < GATE_MIN_TIER1_COVERAGE
    ]
    for c, cov in target_cov.items():
        if c in ("T_POOL_FWD_30M", "T_POOL_FWD_4H") and cov < GATE_MIN_TARGET_COVERAGE:
            gates_failed.append(f"target_{c}_coverage_lt_{GATE_MIN_TARGET_COVERAGE}")

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "grain": "pool_entry_onset_event",
        "wallet_copy_not_primary_grain": True,
        "tier1_candidates": TIER1_FEATURES,
        "tier2_guards": TIER2_GUARDS
        + [
            "pool_copy_delay_120s_median",
            "pool_copy_delay_120s_p90",
            "pool_closed_loop_rate",
            "pool_wallet_buyer_count",
        ],
        "tier3_diagnostic_only": [
            "pool",
            "token_other",
            "dominant_sender",
            "is_0x6ff5",
            "entry_id",
        ],
        "forbidden_as_primary_features": FORBIDDEN_AS_FEATURES,
        "target_labels_only": TARGET_COLS,
        "targets_not_features": True,
        "block_number_excluded_by_default": True,
    }
    leak_ok, leak_violations = leakage_guard_pass(manifest)

    next_step = decide_next_step(
        paper_status,
        len(rows),
        tier1_cov,
        target_cov,
        gates_failed,
        len(set(pools)),
        args.min_unique_pools_target,
    )

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "prep_variant": args.prep_variant,
        "global_per_pool_cap": args.global_per_pool_cap,
        "global_cap_rows_trimmed": global_cap_trimmed,
        "source_mode": args.source_mode,
        "POOL_ENTRY_QUALITY_PREP_DONE": True,
        "DIRECT_SMART_WALLET_COPY_CLOSED": True,
        "WALLET_FEATURES_DIAGNOSTIC_ONLY": True,
        "LEAKAGE_GUARD_PASS": leak_ok,
        "leakage_violations": leak_violations,
        "TRAINING_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "SMART_WALLET_COPY_SMOKE_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "NEXT_STEP": next_step,
        "input_artifacts": {
            "paper_csv": str(paper_path.relative_to(REPO)),
            "paper_status": paper_status,
            "wallet_diagnostic_csv": str(wallet_path.relative_to(REPO)),
            "wallet_status": wallet_status,
            "wallet_pool_windows_status": wallet_window_status,
            "wallet_window_fee_tier": wallet_window_fee,
            "pool_feature_supplement_csv": str(supplement_path.relative_to(REPO)),
            "pool_feature_supplement_status": supplement_status,
            "universe_csv": str(universe_path.relative_to(REPO)),
            "wallet_pool_onset_status": onset_status,
            "ch_onset_candidates_status": ch_onset_status,
            "onset_activity_min": args.onset_activity_min,
        },
        "source_supply_blocker": len(set(pools)) < args.min_unique_pools_target,
        "row_counts": {
            "paper_onset_rows": len(paper_rows),
            "wallet_pool_onset_universe_rows": len(onset_only),
            "ch_pool_onset_candidate_rows": len(ch_only),
            "wallet_pool_window_rows_added": len(wallet_only),
            "total_rows": len(rows),
        },
        "ch_backfill": ch_stats,
        "pool_age_filled_from_births": pool_age_filled,
        "tier1_optional_coverage_gaps": tier1_optional_gaps,
        "source_inventory_path": str((out_dir / "source_inventory.csv").relative_to(REPO)),
        "prep_v1_concentration_before": prep_v1_snap.get("concentration"),
        "prep_v1_unique_pools_before": prep_v1_snap.get("unique_pools"),
        "concentration": conc,
        "concentration_delta_vs_prep_v1": {
            "top_pool_share_before": (prep_v1_snap.get("concentration") or {}).get(
                "top_pool_share"
            ),
            "top_pool_share_after": conc.get("top_pool_share"),
            "top10_pool_share_before": (prep_v1_snap.get("concentration") or {}).get(
                "top10_pool_share_design"
            ),
            "top10_pool_share_after": conc.get("top10_pool_share_design"),
            "unique_pools_before": prep_v1_snap.get("unique_pools"),
            "unique_pools_after": len(set(pools)),
        },
        "gates_failed": gates_failed,
        "tier1_coverage": tier1_cov,
        "target_coverage": target_cov,
        "wallet_diagnostic_coverage": wallet_diag_cov,
        "unique_pools": len(set(pools)),
        "unique_tokens": len(set(tokens)),
    }

    gate_rows = [
        {"gate": "row_count", "value": len(rows), "pass": len(rows) > 0},
        {"gate": "unique_pools", "value": len(set(pools)), "pass": len(set(pools)) >= GATE_MIN_UNIQUE_POOLS},
        {"gate": "unique_tokens", "value": len(set(tokens)), "pass": len(set(tokens)) > 0},
        {"gate": "top_pool_share", "value": conc.get("top_pool_share"), "pass": (conc.get("top_pool_share") or 1) < GATE_TOP_POOL_SHARE_LT},
        {"gate": "top_token_share", "value": conc.get("top_token_share"), "pass": (conc.get("top_token_share") or 1) < GATE_TOP_POOL_SHARE_LT},
        {"gate": "top10_pool_share_design", "value": conc.get("top10_pool_share_design"), "pass": (conc.get("top10_pool_share_design") or 1) < GATE_TOP10_SHARE_LT},
        {"gate": "wallet_diagnostic_coverage", "value": wallet_diag_cov, "pass": wallet_diag_cov > 0},
        {"gate": "leakage_guard", "value": leak_ok, "pass": leak_ok},
    ]
    for c in TIER1_FEATURES:
        gate_rows.append(
            {
                "gate": f"tier1_coverage_{c}",
                "value": tier1_cov.get(c),
                "pass": tier1_cov.get(c, 0) >= GATE_MIN_TIER1_COVERAGE,
            }
        )
    for c in TARGET_COLS:
        gate_rows.append(
            {
                "gate": f"target_coverage_{c}",
                "value": target_cov.get(c),
                "pass": target_cov.get(c, 0) >= (GATE_MIN_TARGET_COVERAGE if c.startswith("T_POOL_FWD") else 0.5),
            }
        )

    if not args.emit_gates_only:
        write_csv(out_dir / "pool_entry_quality_candidate_dataset.csv", rows)
    write_csv(out_dir / "gate_summary.csv", gate_rows)
    (out_dir / "feature_candidate_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    (out_dir / "pool_entry_quality_prep_flags.json").write_text(
        json.dumps(flags, indent=2) + "\n"
    )

    md = build_readout_md(
        flags,
        manifest,
        paper_status,
        wallet_status,
        wallet_window_status,
        supplement_status,
        onset_status,
        gates_failed,
        inventory,
    )
    (out_dir / "pool_entry_quality_prep.md").write_text(md)

    print(json.dumps({"NEXT_STEP": next_step, "rows": len(rows), "gates_failed": gates_failed}, indent=2))
    return 0


def build_readout_md(
    flags: dict,
    manifest: dict,
    paper_status: str,
    wallet_status: str,
    wallet_window_status: str,
    supplement_status: str,
    onset_status: str,
    gates_failed: list[str],
    inventory: list[dict],
) -> str:
    rc = flags["row_counts"]
    delta = flags.get("concentration_delta_vs_prep_v1") or {}
    inv_lines = "\n".join(
        f"| {r['source_id']} | {r['rows']} | {r['unique_pools']} | {r['usable_for_pool_entry']} | {r['notes']} |"
        for r in inventory
        if r.get("exists")
    )
    variant = flags.get("prep_variant", "v1")
    return f"""# Sprint084{variant.upper()} — Pool-Entry Quality Dataset Prep

Generated: {flags["generated_at_utc"]}  
Mode: **research-only** · no training · no paper-forward · no live  
Source mode: `{flags.get("source_mode", "paper_only")}`

## Executive conclusion

Pool-entry / onset-event dataset prep completed at grain **`pool_entry_onset_event`** (not wallet-copy primary).
**Direct smart-wallet copy remains closed.** Wallet CSV used only for **pool-level diagnostic aggregates**.

- **Paper onset rows:** {rc["paper_onset_rows"]} (`{flags["input_artifacts"]["paper_status"]}`)
- **CH pool-onset candidate rows:** {rc.get("ch_pool_onset_candidate_rows", 0)} (`{flags["input_artifacts"].get("ch_onset_candidates_status", "")}`)
- **Wallet pool-onset universe rows:** {rc.get("wallet_pool_onset_universe_rows", 0)} (`{onset_status}`)
- **Wallet pool-window rows added:** {rc["wallet_pool_window_rows_added"]} (`{wallet_window_status}`)
- **Total candidate rows:** {rc["total_rows"]}
- **Unique pools:** {flags["unique_pools"]}
- **Wallet diagnostic join coverage:** {flags["wallet_diagnostic_coverage"]:.1%}
- **CH backfill:** {json.dumps(flags.get("ch_backfill", {}))}
- **NEXT_STEP:** `{flags["NEXT_STEP"]}`

**Not an alpha claim.** Paper targets = **historical backtest** (`net_return_*`). CH velocity-sum labels = **research proxy**, not live-forward.

### Concentration vs prep_v1

| Metric | prep_v1 | prep_v2 |
|--------|--------:|--------:|
| unique_pools | {delta.get("unique_pools_before", "n/a")} | {delta.get("unique_pools_after", "n/a")} |
| top_pool_share | {delta.get("top_pool_share_before", "n/a")} | {delta.get("top_pool_share_after", "n/a")} |
| top10_pool_share | {delta.get("top10_pool_share_before", "n/a")} | {delta.get("top10_pool_share_after", "n/a")} |

## Source inventory (summary)

| source_id | rows | pools | usable | notes |
|-----------|-----:|------:|--------|-------|
{inv_lines}

## Input artifacts

| Artifact | Status |
|----------|--------|
| `{flags["input_artifacts"]["paper_csv"]}` | {paper_status} |
| `{flags["input_artifacts"]["wallet_diagnostic_csv"]}` | {wallet_status} |
| `{flags["input_artifacts"].get("pool_feature_supplement_csv", "")}` | {supplement_status} |

## Dataset / gate summary

| Metric | Value |
|--------|------:|
| row_count | {rc["total_rows"]} |
| unique_pools | {flags["unique_pools"]} |
| unique_tokens | {flags["unique_tokens"]} |
| top_pool_share | {flags["concentration"].get("top_pool_share")} |
| top_token_share | {flags["concentration"].get("top_token_share")} |
| top10_pool_share (design) | {flags["concentration"].get("top10_pool_share_design")} |
| dominant_pool | `{flags["concentration"].get("dominant_pool")}` |

### Tier-1 feature coverage

| Feature | Coverage |
|---------|----------:|
{chr(10).join(f'| `{k}` | {v:.1%} |' for k, v in flags["tier1_coverage"].items())}

### Target coverage (labels only)

| Target | Coverage |
|--------|----------:|
{chr(10).join(f'| `{k}` | {v:.1%} |' for k, v in flags["target_coverage"].items())}

### Gates failed

{chr(10).join(f'- {g}' for g in gates_failed) if gates_failed else '- (none — review concentration / coverage warnings anyway)'}

## Feature policy

- **Tier 1:** pool-entry observable (`{", ".join(manifest["tier1_candidates"])}`)
- **Tier 2:** diagnostic guards (copy-delay pool aggregates, amount, buyer count, fee)
- **Tier 3:** `pool`, `token_other`, sender flags — attribution only
- **Forbidden as features:** wallet identity, future_return_*, realized_wallet_pnl*, wallet rank, trade_size_bucket, block_number

## Outputs

- `pool_entry_quality_candidate_dataset.csv`
- `feature_candidate_manifest.json`
- `gate_summary.csv`
- `pool_entry_quality_prep_flags.json`

## Exact next command

```bash
cd /opt/EATLQ && python3 scripts/sprint084_pool_entry_quality_dataset_prep.py \\
  --prep-variant v2 \\
  --paper-csv logs/sprint055_paper_shadow.csv \\
  --universe-csv mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv \\
  --wallet-diagnostic-csv mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_dataset_sample.csv \\
  --out-dir mvp/audits/sprint084_pool_entry_quality/prep_v2
```

`--prep-variant v2` enables hybrid universe + per-pool CH tier1/forward labels. Not live-forward alpha.
"""


if __name__ == "__main__":
    raise SystemExit(main())
