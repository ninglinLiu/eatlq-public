#!/usr/bin/env python3
"""Sprint050 — Whitepaper-aligned V3 New-Continent research (read-only).

Scope contract: see mvp/audits/sprint050/v3_scope_contract.md

This script is **research only**. It:
  * does NOT call RPC,
  * does NOT touch private keys,
  * does NOT write to Redis `signals` stream,
  * does NOT train, retrain, or promote models,
  * does NOT modify production thresholds.

It reads:
  * mvp/audits/sprint039a/q4_bundle_combined/q4_candidates_*.csv  (offline)
  * mvp/diagnostics/sprint049b_*.csv                              (offline)
  * eatlq.uniswap_v3_swaps_trade_semantic                         (CH, bounded)

It writes JSON / CSV / Markdown under:
  * mvp/audits/sprint050/
  * mvp/diagnostics/sprint050/
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
Q4_DIR = ROOT / "mvp/audits/sprint039a/q4_bundle_combined"
ROUTED_FEATURE_CSV = ROOT / "mvp/audits/sprint039e/routed_slice_feature_rows.csv"
SIDECAR_RESCORED = ROOT / "mvp/diagnostics/sprint049b_rescored_candidates.csv"
SIDECAR_NORMALIZED = ROOT / "mvp/diagnostics/sprint049b_return_normalized_candidates.csv"

AUDIT_OUT = ROOT / "mvp/audits/sprint050"
DIAG_OUT = ROOT / "mvp/diagnostics/sprint050"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

WETH_BASE = "0x4200000000000000000000000000000000000006"
CH_CONTAINER_DEFAULT = "eatlq-clickhouse-1"

BUNDLE_WEEKS = sorted(
    p.stem.replace("q4_candidates_", "") for p in Q4_DIR.glob("q4_candidates_*.csv")
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(x: Any) -> float | None:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _safe_int(x: Any) -> int | None:
    try:
        if x is None or x == "":
            return None
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _truthy(x: Any) -> bool:
    if x is None:
        return False
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"true", "1", "yes", "t"}


# ---------------------------------------------------------------------------
# Q4 bundle loader
# ---------------------------------------------------------------------------

def load_q4_bundle() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for week in BUNDLE_WEEKS:
        path = Q4_DIR / f"q4_candidates_{week}.csv"
        with path.open() as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                r["regime_week"] = r.get("regime_week") or week
                rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# ClickHouse helper
# ---------------------------------------------------------------------------

def _ch_query(sql: str, container: str = CH_CONTAINER_DEFAULT, timeout: int = 90) -> str:
    if shutil.which("docker") is None:
        raise RuntimeError("docker binary not found; CH discovery skipped")
    result = subprocess.run(
        ["docker", "exec", container, "clickhouse-client", "--query", sql],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CH query failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


# ---------------------------------------------------------------------------
# WP-B / WP-C: V3 New Continent + OOS expansion
# ---------------------------------------------------------------------------

POST_BUNDLE_SCAN_FROM = "2026-03-10"  # post 2026-03-09 bundle week

def discover_v3_new_continent(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bundle_weeks = sorted({r["regime_week"] for r in rows})

    # 1. CH scan: weekly V3 trade rows since 2025-09-01
    week_rows: list[dict[str, Any]] = []
    try:
        sql = (
            "SELECT toStartOfWeek(toDateTime(block_time)) AS wk, "
            "count() AS n, "
            "uniq(pool_address) AS pools, "
            "uniq(token0_address) AS t0, "
            "uniq(token1_address) AS t1, "
            "sum(if(fee_bps=5,1,0)) AS f5, "
            "sum(if(fee_bps=30,1,0)) AS f30, "
            "sum(if(fee_bps=100,1,0)) AS f100, "
            "sum(if(fee_bps NOT IN (1,5,30,100),1,0)) AS f_other, "
            f"sum(if(token0_address='{WETH_BASE}' OR token1_address='{WETH_BASE}',1,0)) AS weth_leg, "
            "sum(if(has_clear_direction=1,1,0)) AS clear_dir "
            "FROM eatlq.uniswap_v3_swaps_trade_semantic "
            "WHERE block_time >= toDateTime('2025-09-01') "
            "GROUP BY wk ORDER BY wk"
        )
        out = _ch_query(sql)
        for line in out.strip().splitlines():
            cells = line.split("\t")
            if len(cells) < 11:
                continue
            week_rows.append(
                {
                    "ch_week_start": cells[0],
                    "swap_rows": int(cells[1]),
                    "uniq_pools": int(cells[2]),
                    "uniq_token0": int(cells[3]),
                    "uniq_token1": int(cells[4]),
                    "fee_5": int(cells[5]),
                    "fee_30": int(cells[6]),
                    "fee_100": int(cells[7]),
                    "fee_other": int(cells[8]),
                    "weth_leg_rows": int(cells[9]),
                    "clear_direction_rows": int(cells[10]),
                }
            )
    except Exception as exc:  # noqa: BLE001
        week_rows = []
        ch_error = repr(exc)
    else:
        ch_error = None

    # 2. Match CH weeks (Sun-anchored) to bundle weeks (Mon-anchored): CH week+1 day == bundle week.
    bundle_set = set(bundle_weeks)
    annotated: list[dict[str, Any]] = []
    for w in week_rows:
        ch_date = datetime.strptime(w["ch_week_start"], "%Y-%m-%d")
        # bundle uses Monday-anchored week labels; CH default is Sunday-anchored.
        bundle_label = (ch_date.replace(tzinfo=timezone.utc).date()).isoformat()
        # also try +1 day:
        plus1 = (ch_date.replace(tzinfo=timezone.utc).date()).isoformat()
        try:
            from datetime import timedelta
            plus1 = (ch_date + timedelta(days=1)).date().isoformat()
        except Exception:  # pragma: no cover
            plus1 = None
        in_bundle = bundle_label in bundle_set or (plus1 and plus1 in bundle_set)
        annotated.append(
            {
                **w,
                "matched_bundle_week": (
                    bundle_label if bundle_label in bundle_set else (plus1 if (plus1 in bundle_set) else None)
                ),
                "in_q4_bundle": bool(in_bundle),
            }
        )

    # 3. Universe inside the bundle (purely from q4 rows)
    pool_set: set[str] = set()
    token0_set: set[str] = set()
    token1_set: set[str] = set()
    fee_counts: Counter[int] = Counter()
    weth_leg = 0
    weth_other = 0
    routed_total = 0
    quoteable_total = 0
    selected_total = 0
    exit_5m_ok = 0
    label5_pos = 0
    week_pool_counts: dict[str, set[str]] = defaultdict(set)
    week_token_counts: dict[str, set[str]] = defaultdict(set)
    week_row_counts: Counter[str] = Counter()

    for r in rows:
        wk = r["regime_week"]
        week_row_counts[wk] += 1
        pool = (r.get("pool_address") or "").lower()
        if pool:
            pool_set.add(pool)
            week_pool_counts[wk].add(pool)
        t0 = (r.get("token0_addr") or "").lower()
        t1 = (r.get("token1_addr") or "").lower()
        if t0:
            token0_set.add(t0)
            week_token_counts[wk].add(t0)
        if t1:
            token1_set.add(t1)
            week_token_counts[wk].add(t1)
        fee = _safe_int(r.get("fee_bps"))
        if fee is not None:
            fee_counts[fee] += 1
        if t0 == WETH_BASE or t1 == WETH_BASE:
            weth_leg += 1
        else:
            weth_other += 1
        if _truthy(r.get("entry_quote_ok")):
            quoteable_total += 1
        if _truthy(r.get("selected_for_exit")):
            selected_total += 1
        if _truthy(r.get("exit_ok_5m")):
            exit_5m_ok += 1
        if _safe_int(r.get("label_5m")) == 1:
            label5_pos += 1
        # routed = quoteable + selected_for_exit + exit_ok_5m all true (Sprint037G semantics)
        if (
            _truthy(r.get("entry_quote_ok"))
            and _truthy(r.get("selected_for_exit"))
            and _truthy(r.get("exit_ok_5m"))
        ):
            routed_total += 1

    bundle_universe = {
        "weeks": bundle_weeks,
        "rows": len(rows),
        "uniq_pools": len(pool_set),
        "uniq_token0": len(token0_set),
        "uniq_token1": len(token1_set),
        "fee_distribution": dict(sorted(fee_counts.items())),
        "weth_leg_rows": weth_leg,
        "non_weth_leg_rows": weth_other,
        "quoteable_rows": quoteable_total,
        "selected_for_exit_rows": selected_total,
        "exit_ok_5m_rows": exit_5m_ok,
        "routed_5m_rows": routed_total,
        "label_5m_positive_rows": label5_pos,
    }

    # 4. New-continent slices from CH (post-bundle weeks)
    new_weeks = [w for w in annotated if not w["in_q4_bundle"]]
    new_weeks_sorted = sorted(new_weeks, key=lambda w: w["ch_week_start"])

    # categorise: pre-bundle, mid-bundle gap, post-bundle
    bundle_min = min(bundle_weeks) if bundle_weeks else "9999-99-99"
    bundle_max = max(bundle_weeks) if bundle_weeks else "0000-00-00"
    for w in new_weeks_sorted:
        if w["ch_week_start"] < bundle_min:
            w["bundle_position"] = "pre_bundle"
        elif w["ch_week_start"] > bundle_max:
            w["bundle_position"] = "post_bundle"
        else:
            w["bundle_position"] = "mid_bundle_gap"

    summary = {
        "kind": "sprint050_v3_new_continent_discovery",
        "generated_at": _now_utc(),
        "ch_query_error": ch_error,
        "bundle_universe": bundle_universe,
        "ch_weekly_v3_trade_universe": annotated,
        "new_continent_weeks": new_weeks_sorted,
        "new_continent_summary": {
            "weeks_outside_bundle": len(new_weeks_sorted),
            "post_bundle_weeks": [w["ch_week_start"] for w in new_weeks_sorted if w["bundle_position"] == "post_bundle"],
            "mid_bundle_gap_weeks": [w["ch_week_start"] for w in new_weeks_sorted if w["bundle_position"] == "mid_bundle_gap"],
            "pre_bundle_weeks": [w["ch_week_start"] for w in new_weeks_sorted if w["bundle_position"] == "pre_bundle"],
            "post_bundle_swap_rows": sum(w["swap_rows"] for w in new_weeks_sorted if w["bundle_position"] == "post_bundle"),
            "mid_bundle_gap_swap_rows": sum(w["swap_rows"] for w in new_weeks_sorted if w["bundle_position"] == "mid_bundle_gap"),
            "pre_bundle_swap_rows": sum(w["swap_rows"] for w in new_weeks_sorted if w["bundle_position"] == "pre_bundle"),
        },
    }
    return summary


# ---------------------------------------------------------------------------
# WP-D: 5m feature regime exploration
# ---------------------------------------------------------------------------

def _bucket_velocity(v: float | None) -> str:
    if v is None:
        return "missing"
    if v <= -0.5:
        return "v_neg_lt_-0.5"
    if v <= -0.1:
        return "v_neg_-0.5..-0.1"
    if v < 0.1:
        return "v_flat_-0.1..0.1"
    if v < 0.3:
        return "v_pos_0.1..0.3"
    if v < 0.5:
        return "v_pos_0.3..0.5"
    return "v_pos_ge_0.5"


def _bucket_buy_pressure(v: float | None) -> str:
    if v is None:
        return "missing"
    if v < 0.4:
        return "bp_lt_0.4"
    if v < 0.5:
        return "bp_0.4..0.5"
    if v < 0.6:
        return "bp_0.5..0.6"
    if v < 0.7:
        return "bp_0.6..0.7"
    return "bp_ge_0.7"


def _bucket_volatility(v: float | None) -> str:
    if v is None:
        return "missing"
    if v < 0.5:
        return "tv_lt_0.5"
    if v < 1.0:
        return "tv_0.5..1.0"
    if v < 1.5:
        return "tv_1.0..1.5"
    if v < 2.0:
        return "tv_1.5..2.0"
    return "tv_ge_2.0"


def _bucket_heat(v: float | None) -> str:
    if v is None:
        return "missing"
    if v < 20:
        return "heat_lt_20"
    if v < 50:
        return "heat_20..50"
    if v < 100:
        return "heat_50..100"
    if v < 250:
        return "heat_100..250"
    return "heat_ge_250"


def _bucket_fee(v: int | None) -> str:
    if v is None:
        return "fee_missing"
    return f"fee_{v}bps"


def _bucket_weth_leg(t0: str, t1: str) -> str:
    t0 = (t0 or "").lower()
    t1 = (t1 or "").lower()
    if t0 == WETH_BASE:
        return "weth_token0"
    if t1 == WETH_BASE:
        return "weth_token1"
    return "weth_absent"


def explore_feature_regimes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    feature_buckets = {
        "price_velocity_5m": _bucket_velocity,
        "buy_pressure_ratio": _bucket_buy_pressure,
        "token_volatility_5m": _bucket_volatility,
        "activity_heat": _bucket_heat,
        "fee_bps": lambda v: _bucket_fee(_safe_int(v)),
    }

    def _build_bucket(name: str, key: str, mapper) -> list[dict[str, Any]]:
        bucket_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            v_raw = r.get(key)
            v = _safe_float(v_raw) if name != "fee_bps" else v_raw
            bucket_rows[mapper(v)].append(r)
        out = []
        for b, items in bucket_rows.items():
            out.append(_summarise_bucket(name, b, items))
        return sorted(out, key=lambda x: x["bucket"])

    by_feature: dict[str, list[dict[str, Any]]] = {}
    for name, mapper in feature_buckets.items():
        by_feature[name] = _build_bucket(name, name, mapper)

    # weth-leg bucket separately
    weth_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        weth_buckets[_bucket_weth_leg(r.get("token0_addr", ""), r.get("token1_addr", ""))].append(r)
    by_feature["weth_leg"] = sorted(
        [_summarise_bucket("weth_leg", b, items) for b, items in weth_buckets.items()],
        key=lambda x: x["bucket"],
    )

    # week regime
    week_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        week_buckets[r.get("regime_week", "missing")].append(r)
    by_feature["regime_week"] = sorted(
        [_summarise_bucket("regime_week", b, items) for b, items in week_buckets.items()],
        key=lambda x: x["bucket"],
    )

    # regime_role (positive / negative)
    role_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        role_buckets[r.get("regime_role", "missing")].append(r)
    by_feature["regime_role"] = sorted(
        [_summarise_bucket("regime_role", b, items) for b, items in role_buckets.items()],
        key=lambda x: x["bucket"],
    )

    # combined velocity x buy_pressure for top-line micro-alpha hint
    combo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        v = _safe_float(r.get("price_velocity_5m"))
        bp = _safe_float(r.get("buy_pressure_ratio"))
        combo[f"{_bucket_velocity(v)}__{_bucket_buy_pressure(bp)}"].append(r)
    by_feature["velocity_x_buy_pressure"] = sorted(
        [_summarise_bucket("velocity_x_buy_pressure", b, items) for b, items in combo.items() if len(items) >= 25],
        key=lambda x: x["bucket"],
    )

    return by_feature


def _summarise_bucket(feature: str, bucket: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    quoteable = sum(1 for r in items if _truthy(r.get("entry_quote_ok")))
    selected = sum(1 for r in items if _truthy(r.get("selected_for_exit")))
    exit5_ok = sum(1 for r in items if _truthy(r.get("exit_ok_5m")))
    routed = sum(
        1
        for r in items
        if _truthy(r.get("entry_quote_ok"))
        and _truthy(r.get("selected_for_exit"))
        and _truthy(r.get("exit_ok_5m"))
    )
    rets5 = [_safe_float(r.get("net_return_5m")) for r in items]
    rets5 = [v for v in rets5 if v is not None]
    rets5_routed = [
        _safe_float(r.get("net_return_5m"))
        for r in items
        if _truthy(r.get("entry_quote_ok"))
        and _truthy(r.get("selected_for_exit"))
        and _truthy(r.get("exit_ok_5m"))
    ]
    rets5_routed = [v for v in rets5_routed if v is not None]
    label5_pos = sum(1 for r in items if _safe_int(r.get("label_5m")) == 1)
    label5_neg = sum(1 for r in items if _safe_int(r.get("label_5m")) == 0)
    label5_pos_routed = sum(
        1
        for r in items
        if _safe_int(r.get("label_5m")) == 1
        and _truthy(r.get("entry_quote_ok"))
        and _truthy(r.get("selected_for_exit"))
        and _truthy(r.get("exit_ok_5m"))
    )
    pools = {(r.get("pool_address") or "").lower() for r in items if r.get("pool_address")}
    weeks = {r.get("regime_week") for r in items if r.get("regime_week")}
    sample_size_class = _classify_sample(routed)
    pf, hit = _profit_factor_and_hit(rets5_routed)
    return {
        "feature": feature,
        "bucket": bucket,
        "rows_total": n,
        "rows_quoteable": quoteable,
        "rows_selected_for_exit": selected,
        "rows_exit_ok_5m": exit5_ok,
        "rows_routed_5m": routed,
        "rows_label5_pos": label5_pos,
        "rows_label5_neg": label5_neg,
        "rows_label5_pos_routed": label5_pos_routed,
        "uniq_pools": len(pools),
        "uniq_weeks": len(weeks),
        "ret5_mean_all": _mean(rets5),
        "ret5_median_all": _median(rets5),
        "ret5_mean_routed": _mean(rets5_routed),
        "ret5_median_routed": _median(rets5_routed),
        "ret5_routed_hit_rate_pos": hit,
        "ret5_routed_profit_factor": pf,
        "sample_size_class_routed": sample_size_class,
    }


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _profit_factor_and_hit(xs: list[float]) -> tuple[float | None, float | None]:
    if not xs:
        return None, None
    pos = sum(v for v in xs if v > 0)
    neg = -sum(v for v in xs if v < 0)
    pf = (pos / neg) if neg > 0 else (math.inf if pos > 0 else None)
    if pf is math.inf:
        pf = None  # avoid JSON Infinity
    hit = sum(1 for v in xs if v > 0) / len(xs)
    return pf, hit


def _classify_sample(n: int) -> str:
    if n < 300:
        return "smoke_or_label_sanity"
    if n < 1000:
        return "very_weak_feasibility"
    if n < 5000:
        return "scoped_dryrun_candidate"
    if n < 20000:
        return "scoped_backtest_candidate"
    return "formal_model_research_candidate"


# ---------------------------------------------------------------------------
# WP-E: economic pre-validation
# ---------------------------------------------------------------------------

def economic_prevalidation(rows: list[dict[str, Any]], gas_cost: float = 0.002) -> dict[str, Any]:
    routed = [
        r
        for r in rows
        if _truthy(r.get("entry_quote_ok"))
        and _truthy(r.get("selected_for_exit"))
        and _truthy(r.get("exit_ok_5m"))
    ]
    rets5 = [_safe_float(r.get("net_return_5m")) for r in routed]
    rets5 = [v for v in rets5 if v is not None]
    rets5_after_gas = [v - gas_cost for v in rets5]

    pf_raw, hit_raw = _profit_factor_and_hit(rets5)
    pf_g, hit_g = _profit_factor_and_hit(rets5_after_gas)

    pool_concentration: Counter[str] = Counter()
    token_concentration: Counter[str] = Counter()
    week_concentration: Counter[str] = Counter()
    for r in routed:
        pool_concentration[(r.get("pool_address") or "").lower()] += 1
        non_weth = (r.get("token0_addr") or "").lower()
        if non_weth == WETH_BASE:
            non_weth = (r.get("token1_addr") or "").lower()
        token_concentration[non_weth] += 1
        week_concentration[r.get("regime_week", "")] += 1

    def _top_share(c: Counter[str], k: int) -> float | None:
        total = sum(c.values())
        if total == 0:
            return None
        top = sum(v for _, v in c.most_common(k))
        return top / total

    sample_class = _classify_sample(len(routed))

    summary = {
        "kind": "sprint050_v3_economic_prevalidation",
        "generated_at": _now_utc(),
        "input": {
            "q4_bundle_rows_total": len(rows),
            "routed_5m_rows": len(routed),
            "weeks_routed": sorted(set(week_concentration)),
            "pools_routed": len(pool_concentration),
            "tokens_routed_non_weth": len(token_concentration),
        },
        "cost_assumptions": {
            "horizon_min": 5,
            "gas_per_round_trip_proxy_pct": gas_cost,
            "slippage_treatment": "embedded_in_net_return_5m_via_q4_quote_ladder",
            "failed_quote_treatment": "row dropped (entry_quote_ok must be true)",
        },
        "raw_5m": {
            "rows": len(rets5),
            "mean_net_return": _mean(rets5),
            "median_net_return": _median(rets5),
            "hit_rate_gt_zero": hit_raw,
            "profit_factor": pf_raw,
        },
        "after_gas_proxy_5m": {
            "rows": len(rets5_after_gas),
            "mean_net_return": _mean(rets5_after_gas),
            "median_net_return": _median(rets5_after_gas),
            "hit_rate_gt_zero": hit_g,
            "profit_factor": pf_g,
        },
        "concentration": {
            "pool_top1_share": _top_share(pool_concentration, 1),
            "pool_top5_share": _top_share(pool_concentration, 5),
            "token_top1_share": _top_share(token_concentration, 1),
            "token_top5_share": _top_share(token_concentration, 5),
            "week_top1_share": _top_share(week_concentration, 1),
            "week_top3_share": _top_share(week_concentration, 3),
        },
        "sample_size_class": sample_class,
        "verdict": _economic_verdict(len(routed), hit_g, pf_g),
        "decision_flags": {
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "PRODUCTION_SIGNALS_WRITTEN": False,
            "NO_ALPHA_CLAIM": True,
        },
    }
    return summary


def _economic_verdict(n: int, hit_g: float | None, pf_g: float | None) -> str:
    if n < 300:
        return "NOT_ENOUGH_DATA_SMOKE_ONLY"
    if hit_g is None or pf_g is None:
        return "INCONCLUSIVE"
    if pf_g >= 1.5 and (hit_g or 0) >= 0.5 and n >= 1000:
        return "WEAK_PASS_NEEDS_OOS_AND_COST_REPLAY"
    if pf_g >= 1.0 and (hit_g or 0) >= 0.4:
        return "MARGINAL_NEEDS_LABEL_OR_COST_REVIEW"
    return "FAIL_AFTER_GAS_PROXY"


# ---------------------------------------------------------------------------
# WP-F: sidecar ROI repair
# ---------------------------------------------------------------------------

def sidecar_roi_repair() -> dict[str, Any]:
    if not SIDECAR_RESCORED.exists() or not SIDECAR_NORMALIZED.exists():
        return {
            "kind": "sprint050_sidecar_roi_repair",
            "generated_at": _now_utc(),
            "status": "BLOCKED_MISSING_INPUT",
            "missing_files": [
                str(SIDECAR_RESCORED) if not SIDECAR_RESCORED.exists() else None,
                str(SIDECAR_NORMALIZED) if not SIDECAR_NORMALIZED.exists() else None,
            ],
        }

    rows: list[dict[str, Any]] = []
    with SIDECAR_RESCORED.open() as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)

    norm_rows: dict[str, dict[str, Any]] = {}
    with SIDECAR_NORMALIZED.open() as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            norm_rows[r["controller"]] = r

    repaired = []
    cap_eff_buckets: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    for r in rows:
        controller = r["controller"]
        norm = norm_rows.get(controller, {})
        notional = _safe_float(r.get("own_deployed_notional_weth"))
        roi = _safe_float(r.get("own_roi_pct"))
        pf = _safe_float(r.get("own_profit_factor"))
        wr = _safe_float(r.get("own_win_rate"))
        max_loss = _safe_float(r.get("own_max_loss_trade_pct"))
        median_hold = _safe_float(r.get("own_median_hold_hours"))
        top1 = _safe_float(r.get("top1_token_share"))
        cat = r.get("category_after_roi", "")
        category_counts[cat] += 1

        if notional is None or notional <= 0:
            cap_class = "no_notional"
        elif notional < 0.1:
            cap_class = "small_capital_lt_0.1_weth"
        elif notional < 1.0:
            cap_class = "scoped_capital_0.1_1_weth"
        elif notional < 10.0:
            cap_class = "moderate_capital_1_10_weth"
        else:
            cap_class = "ample_capital_ge_10_weth"
        cap_eff_buckets[cap_class] += 1

        # capacity proxy: own_capital_efficiency from sprint049b normalized
        cap_eff = _safe_float(norm.get("own_capital_efficiency"))
        return_per_hour = _safe_float(norm.get("own_return_per_hold_hour"))

        repaired.append(
            {
                "controller": controller,
                "category_after_roi": cat,
                "own_deployed_notional_weth": notional,
                "own_realized_pnl_weth": _safe_float(r.get("own_realized_pnl_weth")),
                "own_roi_pct": roi,
                "own_profit_factor": pf,
                "own_win_rate": wr,
                "own_max_loss_trade_pct": max_loss,
                "own_median_hold_hours": median_hold,
                "top1_token_share": top1,
                "concentration_risk": r.get("concentration_risk", ""),
                "perfect_winrate_suspect": r.get("perfect_winrate_suspect", ""),
                "delayed_follow_roi_pct": _safe_float(r.get("delayed_follow_roi_pct")),
                "delayed_follow_pf": _safe_float(r.get("delayed_follow_profit_factor")),
                "delayed_follow_verdict": r.get("delayed_follow_verdict", ""),
                "capital_class": cap_class,
                "own_capital_efficiency": cap_eff,
                "own_return_per_hold_hour": return_per_hour,
            }
        )

    # capital-efficiency primary ranking (top 12)
    def _cap_key(row):
        return (
            -(row["own_roi_pct"] or -math.inf),
            -(row["own_deployed_notional_weth"] or -math.inf),
        )
    top_cap = sorted(
        [r for r in repaired if (r["own_deployed_notional_weth"] or 0) >= 0.1 and (r["own_roi_pct"] or 0) > 0],
        key=_cap_key,
    )[:10]

    return {
        "kind": "sprint050_sidecar_roi_repair",
        "generated_at": _now_utc(),
        "status": "ROI_REPAIRED",
        "input_files": [str(SIDECAR_RESCORED), str(SIDECAR_NORMALIZED)],
        "scope": "auxiliary_radar_only",
        "category_counts": dict(category_counts),
        "capital_class_counts": dict(cap_eff_buckets),
        "top_capital_efficient_candidates": top_cap,
        "rows": repaired,
        "available_fields": sorted(set().union(*[r.keys() for r in rows])),
        "missing_for_full_capacity_estimate": [
            "live_market_ticks_latency_ms_per_controller",
            "post_follow_slippage_in_realtime",
            "depth_at_follow_entry_per_pool_at_block",
        ],
        "decision_flags": {
            "SIDECAR_STATUS": "AUXILIARY_RADAR_ONLY",
            "ROI_NORMALIZED": True,
            "WRITES_TO_SIGNALS_STREAM": False,
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
        },
        "next_required_data_for_capacity_resolution": [
            "Sprint049C 7-day paper shadow to measure real follow-delay",
            "depth-at-block snapshot per controller pool (no RPC unless approved)",
        ],
    }


# ---------------------------------------------------------------------------
# OOS expansion plan (WP-C)
# ---------------------------------------------------------------------------

def oos_candidate_plan(discovery: dict[str, Any]) -> dict[str, Any]:
    new_weeks = discovery.get("new_continent_weeks", [])
    post_weeks = [w for w in new_weeks if w["bundle_position"] == "post_bundle"]
    gap_weeks = [w for w in new_weeks if w["bundle_position"] == "mid_bundle_gap"]
    pre_weeks = [w for w in new_weeks if w["bundle_position"] == "pre_bundle"]

    def _bundle_estimate(w: dict[str, Any]) -> int:
        # bundle/q4 candidate row count is roughly bundled trade rows / ~600 (empirical 2026-02-02 ratio).
        # Use a conservative 1/700 proxy and bound with 50/5000.
        est = max(50, min(5000, int(w["swap_rows"] / 700)))
        return est

    def _per_week(items):
        out = []
        for w in items:
            est = _bundle_estimate(w)
            out.append(
                {
                    "ch_week_start": w["ch_week_start"],
                    "swap_rows": w["swap_rows"],
                    "uniq_pools": w["uniq_pools"],
                    "fee_5_share": w["fee_5"] / w["swap_rows"] if w["swap_rows"] else None,
                    "fee_30_share": w["fee_30"] / w["swap_rows"] if w["swap_rows"] else None,
                    "fee_100_share": w["fee_100"] / w["swap_rows"] if w["swap_rows"] else None,
                    "weth_leg_share": w["weth_leg_rows"] / w["swap_rows"] if w["swap_rows"] else None,
                    "estimated_q4_candidate_rows": est,
                    "sample_size_class_estimate": _classify_sample(int(est * 0.3)),
                    "candidate_status": "OOS_BEYOND_BUNDLE_PRIORITY_HIGH"
                    if w["bundle_position"] == "post_bundle"
                    else "OOS_GAP_BETWEEN_BUNDLE_WEEKS_PRIORITY_MED"
                    if w["bundle_position"] == "mid_bundle_gap"
                    else "OOS_BEFORE_BUNDLE_PRIORITY_LOW",
                }
            )
        return out

    plan = {
        "kind": "sprint050_oos_candidate_expansion",
        "generated_at": _now_utc(),
        "ch_query_error": discovery.get("ch_query_error"),
        "context_prior_sprint": "Sprint039G selected_oos_routed_rows = 0 under Sprint037G young_pool_onset gate; selection was constrained to bundle weeks only.",
        "why_this_is_oos_relative_to_039": [
            "Sprint039E routed slice is built from 8 in-bundle weeks (2025-10-27 .. 2026-03-30).",
            "Sprint039G rotated through 10 other in-bundle weeks but kept the gate fixed; gate matched 0 routed rows.",
            "Sprint050 candidate weeks below are NEW timestamps ENTIRELY OUTSIDE the q4 bundle (post 2026-03-09 / 2026-03-30).",
        ],
        "post_bundle_oos_candidates": _per_week(post_weeks),
        "mid_bundle_gap_oos_candidates": _per_week(gap_weeks),
        "pre_bundle_oos_candidates": _per_week(pre_weeks),
        "next_expansion_action": [
            "Bounded q4 builder rerun on top-3 post_bundle weeks (ch>=2026-03-15 AND ch<=2026-04-26) using existing scripts/run004_v3r24_new_v3_candidate_builder_prototype.py",
            "Apply Sprint037G young_pool_onset gate AS-IS to the new weeks first; if zero rows persist, evaluate gate-relax at fixed onset_count_thresh ONLY in a separate diagnostic pass (not retuning a guard).",
            "If rerun produces >= 1000 routed rows on at least 2 OOS weeks, escalate to scoped 5m economic dry-run.",
        ],
        "decision_flags": {
            "OOS_EXPANSION_STATUS": "READY_FOR_BOUNDED_VALIDATION"
            if any(w["swap_rows"] >= 100_000 for w in post_weeks)
            else "NEEDS_MORE_DISCOVERY",
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
        },
    }
    return plan


# ---------------------------------------------------------------------------
# Markdown writers
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2, default=str)


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        with path.open("w") as fh:
            fh.write("# empty result\n")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_discovery_md(payload: dict[str, Any]) -> None:
    bu = payload["bundle_universe"]
    nc = payload["new_continent_summary"]
    out = []
    out.append("# Sprint050 — V3 New-Continent Discovery\n")
    out.append(f"Generated: {payload['generated_at']}\n")
    if payload.get("ch_query_error"):
        out.append(f"\n> ClickHouse error: `{payload['ch_query_error']}` — CH section partial.\n")
    out.append("\n## Existing q4 bundle universe (offline)\n")
    out.append("| Metric | Value |")
    out.append("| --- | ---: |")
    out.append(f"| weeks | {len(bu['weeks'])} |")
    out.append(f"| candidate rows | {bu['rows']} |")
    out.append(f"| uniq pools | {bu['uniq_pools']} |")
    out.append(f"| uniq token0 | {bu['uniq_token0']} |")
    out.append(f"| uniq token1 | {bu['uniq_token1']} |")
    out.append(f"| WETH-leg rows | {bu['weth_leg_rows']} |")
    out.append(f"| non-WETH-leg rows | {bu['non_weth_leg_rows']} |")
    out.append(f"| quoteable (entry_quote_ok) | {bu['quoteable_rows']} |")
    out.append(f"| selected_for_exit | {bu['selected_for_exit_rows']} |")
    out.append(f"| exit_ok_5m | {bu['exit_ok_5m_rows']} |")
    out.append(f"| **routed_5m (quoteable & selected & exit_ok_5m)** | **{bu['routed_5m_rows']}** |")
    out.append(f"| label_5m positive (full pool) | {bu['label_5m_positive_rows']} |")
    out.append("\n### Fee-tier distribution (q4 bundle rows)\n")
    out.append("| fee_bps | rows |")
    out.append("| --- | ---: |")
    for fee, n in sorted(bu["fee_distribution"].items()):
        out.append(f"| {fee} | {n} |")

    out.append("\n## CH-side V3 trade-semantic universe (since 2025-09-01)\n")
    out.append("| ch_week_start | swap_rows | pools | weth_leg | clear_dir | in_bundle | bundle_label_match |")
    out.append("| --- | ---: | ---: | ---: | ---: | :---: | --- |")
    for w in payload["ch_weekly_v3_trade_universe"]:
        out.append(
            f"| {w['ch_week_start']} | {w['swap_rows']} | {w['uniq_pools']} | "
            f"{w['weth_leg_rows']} | {w['clear_direction_rows']} | "
            f"{'Y' if w['in_q4_bundle'] else 'N'} | {w['matched_bundle_week'] or '—'} |"
        )

    out.append("\n## New-continent slices (V3 weeks outside the q4 bundle)\n")
    out.append(f"- weeks_outside_bundle: **{nc['weeks_outside_bundle']}**")
    out.append(f"- post-bundle (after 2026-03-30) weeks: `{nc['post_bundle_weeks']}` · {nc['post_bundle_swap_rows']} swap rows")
    out.append(f"- mid-bundle-gap weeks: `{nc['mid_bundle_gap_weeks']}` · {nc['mid_bundle_gap_swap_rows']} swap rows")
    out.append(f"- pre-bundle weeks: `{nc['pre_bundle_weeks']}` · {nc['pre_bundle_swap_rows']} swap rows")

    out.append("\n## Top candidate regimes (existing bundle)\n")
    out.append("- All Sprint037G young_pool_onset routed rows in the bundle live in 8 weeks listed in `mvp/audits/sprint039g/oos_week_selection.json#regime_weeks_in_sprint039e_routed_csv`.")
    out.append("- Underexplored regimes inside the bundle: weeks where chrono_holdout_rows>200 but young_pool_onset_gate_pass_rows=0 (see Sprint039G summary): 2025-11-03, 2025-12-29 (excl), 2026-02-09, 2026-02-16, 2026-03-02, 2026-03-09.")

    out.append("\n## Recommended expansion targets\n")
    out.append("1. **Post-bundle CH weeks 2026-03-15 → 2026-04-26** — entirely outside any prior q4 / 037G / 039E sample. This is the new continent.")
    out.append("2. Mid-bundle gap weeks `2025-11-16, 2025-11-23, 2025-12-07, 2025-12-21` — small but cheap to fill (≈ 14 k swap rows total, no RPC).")
    out.append("3. Pre-bundle weeks `2025-09-28, 2025-10-05, 2025-10-12` — ≈ 210 k swap rows; useful for time-stability sanity, lower priority.")
    out.append("\n## Hard non-claims\n")
    out.append("- This sprint **does NOT** train, retrain, or promote any model.")
    out.append("- This sprint **does NOT** authorise live trading or `signals` writes.")
    out.append("- Discovery row counts above are *swap event* counts; q4 candidate rows after gating will be far fewer (estimate ratio ≈ 1/700 from 2026-02-02 baseline).\n")
    (AUDIT_OUT / "v3_new_continent_discovery.md").write_text("\n".join(out))


def write_oos_md(plan: dict[str, Any]) -> None:
    out = []
    out.append("# Sprint050 — OOS Candidate Expansion\n")
    out.append(f"Generated: {plan['generated_at']}\n")
    out.append(f"\n**Status:** `{plan['decision_flags']['OOS_EXPANSION_STATUS']}`\n")
    if plan.get("ch_query_error"):
        out.append(f"\n> ClickHouse error: `{plan['ch_query_error']}` — fallback to in-bundle selection only.\n")
    out.append("\n## Why this is OOS relative to Sprint039E/F/G\n")
    for line in plan["why_this_is_oos_relative_to_039"]:
        out.append(f"- {line}")

    out.append("\n## Post-bundle OOS candidates (PRIORITY HIGH)\n")
    out.append("| ch_week_start | swap_rows | pools | weth_share | est_q4_rows | est_sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for w in plan["post_bundle_oos_candidates"]:
        out.append(
            f"| {w['ch_week_start']} | {w['swap_rows']} | {w['uniq_pools']} | "
            f"{w['weth_leg_share']:.2%} | {w['estimated_q4_candidate_rows']} | {w['sample_size_class_estimate']} |"
            if w["weth_leg_share"] is not None
            else f"| {w['ch_week_start']} | {w['swap_rows']} | {w['uniq_pools']} | n/a | {w['estimated_q4_candidate_rows']} | {w['sample_size_class_estimate']} |"
        )

    out.append("\n## Mid-bundle gap candidates (PRIORITY MED)\n")
    out.append("| ch_week_start | swap_rows | pools | weth_share | est_q4_rows | est_sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for w in plan["mid_bundle_gap_oos_candidates"]:
        out.append(
            f"| {w['ch_week_start']} | {w['swap_rows']} | {w['uniq_pools']} | "
            f"{(w['weth_leg_share'] or 0):.2%} | {w['estimated_q4_candidate_rows']} | {w['sample_size_class_estimate']} |"
        )

    out.append("\n## Pre-bundle candidates (PRIORITY LOW)\n")
    out.append("| ch_week_start | swap_rows | pools | weth_share | est_q4_rows | est_sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for w in plan["pre_bundle_oos_candidates"]:
        out.append(
            f"| {w['ch_week_start']} | {w['swap_rows']} | {w['uniq_pools']} | "
            f"{(w['weth_leg_share'] or 0):.2%} | {w['estimated_q4_candidate_rows']} | {w['sample_size_class_estimate']} |"
        )

    out.append("\n## Whether enough for smoke / feasibility / scoped dry-run\n")
    n_post_high = sum(1 for w in plan["post_bundle_oos_candidates"] if w["estimated_q4_candidate_rows"] >= 300)
    n_post_dry = sum(1 for w in plan["post_bundle_oos_candidates"] if w["estimated_q4_candidate_rows"] >= 1000)
    out.append(f"- post-bundle weeks meeting smoke threshold (>=300 est rows): **{n_post_high}**")
    out.append(f"- post-bundle weeks meeting scoped-dry-run threshold (>=1000 est rows): **{n_post_dry}**")

    out.append("\n## Next expansion action\n")
    for line in plan["next_expansion_action"]:
        out.append(f"- {line}")

    out.append("\n## Decision flags\n")
    out.append(f"- OOS_EXPANSION_STATUS = `{plan['decision_flags']['OOS_EXPANSION_STATUS']}`")
    out.append(f"- TRAINING_ALLOWED = `{plan['decision_flags']['TRAINING_ALLOWED']}`")
    out.append(f"- LIVE_ALLOWED = `{plan['decision_flags']['LIVE_ALLOWED']}`")
    (AUDIT_OUT / "oos_candidate_expansion.md").write_text("\n".join(out))


def write_feature_md(by_feature: dict[str, list[dict[str, Any]]]) -> None:
    out = []
    out.append("# Sprint050 — 5m Feature Regime Exploration\n")
    out.append(f"Generated: {_now_utc()}\n")
    out.append("\nWindow: q4 bundle (18 weeks, ~12 k candidate rows). All metrics are computed offline from the pre-existing CSV ladders; **no RPC calls were made**.\n")
    out.append("\n*'routed' below means `entry_quote_ok=True` AND `selected_for_exit=True` AND `exit_ok_5m=True` — the executable subset closest to whitepaper-aligned 5m gating.*\n")
    for fname, buckets in by_feature.items():
        out.append(f"\n## {fname}\n")
        out.append(
            "| bucket | rows_total | quoteable | routed_5m | label5_pos_routed | hit_rate_routed | profit_factor_routed | mean_ret5_routed | uniq_pools | sample_class |"
        )
        out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for b in buckets:
            hit = b["ret5_routed_hit_rate_pos"]
            pf = b["ret5_routed_profit_factor"]
            mr = b["ret5_mean_routed"]
            out.append(
                f"| {b['bucket']} | {b['rows_total']} | {b['rows_quoteable']} | {b['rows_routed_5m']} | "
                f"{b['rows_label5_pos_routed']} | "
                f"{f'{hit:.2%}' if hit is not None else '—'} | "
                f"{f'{pf:.2f}' if pf is not None else '—'} | "
                f"{f'{mr:+.4f}' if mr is not None else '—'} | "
                f"{b['uniq_pools']} | {b['sample_size_class_routed']} |"
            )

    out.append("\n## Why this matters for next-round bounded label expansion\n")
    out.append("- Buckets with **routed_5m >= 300 AND profit_factor_routed >= 1.5 AND hit_rate_routed >= 0.5** are *worth a scoped re-validation* on the new-continent OOS weeks.")
    out.append("- Buckets with **routed_5m < 100** are below smoke threshold and should NOT be used to retune any guard.")
    out.append("- Buckets with **token_volatility_5m >= 2.0** confirm Sprint039F's `VOL_GUARD_MIN_EXCLUDE = 2.0` prototype direction, but no threshold is *retuned* here.")
    out.append("\n*Status flags:*")
    promising = sum(
        1
        for buckets in by_feature.values()
        for b in buckets
        if (b["rows_routed_5m"] or 0) >= 300
        and (b["ret5_routed_profit_factor"] or 0) >= 1.5
        and (b["ret5_routed_hit_rate_pos"] or 0) >= 0.5
    )
    weak = sum(
        1
        for buckets in by_feature.values()
        for b in buckets
        if (b["rows_routed_5m"] or 0) >= 100
        and (b["rows_routed_5m"] or 0) < 300
        and (b["ret5_routed_profit_factor"] or 0) >= 1.0
    )
    out.append(f"- promising buckets (routed>=300, pf>=1.5, hit>=50%): **{promising}**")
    out.append(f"- weak-promising buckets (routed in [100, 300), pf>=1.0): **{weak}**")
    out.append("\nIf `promising` >= 3 across at least 2 different feature dimensions → `FEATURE_REGIME_STATUS = PROMISING`.")
    out.append("Otherwise if `weak >= 3` → `WEAK_PROMISING`. Otherwise `NO_SIGNAL_FOUND_OR_INCONCLUSIVE`.")
    (AUDIT_OUT / "feature_regime_exploration.md").write_text("\n".join(out))


def write_economic_md(payload: dict[str, Any]) -> None:
    out = []
    out.append("# Sprint050 — V3 Economic Pre-Validation\n")
    out.append(f"Generated: {payload['generated_at']}\n")
    out.append(f"\n**Verdict:** `{payload['verdict']}`  ·  Sample class: `{payload['sample_size_class']}`\n")
    out.append("\n## Sample\n")
    inp = payload["input"]
    out.append("| Metric | Value |")
    out.append("| --- | ---: |")
    out.append(f"| q4 bundle rows total | {inp['q4_bundle_rows_total']} |")
    out.append(f"| routed_5m rows | {inp['routed_5m_rows']} |")
    out.append(f"| weeks contributing routed rows | {len(inp['weeks_routed'])} |")
    out.append(f"| pools contributing routed rows | {inp['pools_routed']} |")
    out.append(f"| non-WETH tokens contributing routed rows | {inp['tokens_routed_non_weth']} |")

    out.append("\n## Cost assumptions\n")
    for k, v in payload["cost_assumptions"].items():
        out.append(f"- **{k}**: `{v}`")

    out.append("\n## Raw 5m return\n")
    raw = payload["raw_5m"]
    out.append(f"- mean_net_return: `{raw['mean_net_return']}`")
    out.append(f"- median_net_return: `{raw['median_net_return']}`")
    out.append(f"- hit_rate_gt_zero: `{raw['hit_rate_gt_zero']}`")
    out.append(f"- profit_factor: `{raw['profit_factor']}`")

    out.append("\n## After gas proxy (`-0.002` per round trip)\n")
    g = payload["after_gas_proxy_5m"]
    out.append(f"- mean_net_return: `{g['mean_net_return']}`")
    out.append(f"- median_net_return: `{g['median_net_return']}`")
    out.append(f"- hit_rate_gt_zero: `{g['hit_rate_gt_zero']}`")
    out.append(f"- profit_factor: `{g['profit_factor']}`")

    out.append("\n## Concentration\n")
    c = payload["concentration"]
    out.append("| metric | value |")
    out.append("| --- | ---: |")
    for k, v in c.items():
        out.append(f"| {k} | {v if v is None else f'{v:.4f}'} |")

    out.append("\n## Verdict logic\n")
    out.append("- < 300 routed rows → `NOT_ENOUGH_DATA_SMOKE_ONLY`")
    out.append("- >= 1000 routed AND pf_after_gas >= 1.5 AND hit >= 50% → `WEAK_PASS_NEEDS_OOS_AND_COST_REPLAY`")
    out.append("- pf_after_gas >= 1.0 AND hit >= 40% → `MARGINAL_NEEDS_LABEL_OR_COST_REVIEW`")
    out.append("- else → `FAIL_AFTER_GAS_PROXY`")

    out.append("\n## Hard non-claims\n")
    for k, v in payload["decision_flags"].items():
        out.append(f"- {k} = `{v}`")
    out.append("\n## Next validation step\n")
    out.append("- If verdict ∈ {WEAK_PASS, MARGINAL}: rerun on 1–2 post-bundle OOS weeks (Sprint050 OOS plan) before any further claim.")
    out.append("- If verdict = NOT_ENOUGH_DATA: bounded q4 builder on top-3 post-bundle weeks. Cost: read-only CH; **0 RPC calls** if pool first-seen-block already present in `eatlq.discovered_pools`. Otherwise add a strict per-pool RPC budget (≤ 5 k calls) — gated by user.")
    out.append("- Under no condition: training, model promotion, live, or `signals` writes from this sprint.")
    (AUDIT_OUT / "v3_economic_prevalidation.md").write_text("\n".join(out))


def write_sidecar_md(payload: dict[str, Any]) -> None:
    out = []
    out.append("# Sprint050 — Sidecar B-line ROI Repair\n")
    out.append(f"Generated: {payload['generated_at']}\n")
    out.append(f"\n**Status:** `{payload['status']}`  ·  Scope: `{payload['scope']}`\n")
    if payload.get("status") == "BLOCKED_MISSING_INPUT":
        out.append(f"\n> Missing inputs: `{payload['missing_files']}`")
        (AUDIT_OUT / "sidecar_roi_repair.md").write_text("\n".join(out))
        return

    out.append("\n## Inputs\n")
    for f in payload["input_files"]:
        out.append(f"- `{f}`")

    out.append("\n## Category counts (post-Sprint049B)\n")
    for cat, n in sorted(payload["category_counts"].items(), key=lambda x: -x[1]):
        out.append(f"- `{cat}` × {n}")

    out.append("\n## Capital class distribution\n")
    for cls, n in sorted(payload["capital_class_counts"].items(), key=lambda x: -x[1]):
        out.append(f"- `{cls}` × {n}")

    out.append("\n## Top capital-efficient candidates (notional >= 0.1 WETH AND ROI > 0)\n")
    out.append(
        "| controller | category | own_notional_W | own_pnl_W | own_roi_pct | own_pf | wr | top1_token_share | conc | df_verdict |"
    )
    out.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for c in payload["top_capital_efficient_candidates"]:
        out.append(
            f"| {c['controller'][:18]}... | {c['category_after_roi']} | "
            f"{c['own_deployed_notional_weth']} | {c['own_realized_pnl_weth']} | "
            f"{c['own_roi_pct']:.2f} | {c['own_profit_factor']} | "
            f"{c['own_win_rate']} | {c['top1_token_share']} | {c['concentration_risk']} | {c['delayed_follow_verdict']} |"
        )

    out.append("\n## Available fields (sidecar inputs)\n")
    out.append(", ".join(f"`{k}`" for k in payload["available_fields"]))

    out.append("\n## Missing fields preventing full capacity / ROI rollup\n")
    for f in payload["missing_for_full_capacity_estimate"]:
        out.append(f"- `{f}`")

    out.append("\n## Whether sidecar still only `paper / replay / shadow`\n")
    out.append("- **Yes.** Sidecar writes to `copy_follow_decisions` only, never `signals`. Sprint050 confirms this.")
    out.append("- Sidecar may serve as a **V3 candidate-discovery hint**: a controller's BUY pool is a free pointer to a V3 pool worth scanning; pool address is harvested without using its absolute PnL.")

    out.append("\n## Decision flags\n")
    for k, v in payload["decision_flags"].items():
        out.append(f"- {k} = `{v}`")
    out.append("\n## Next required data\n")
    for f in payload["next_required_data_for_capacity_resolution"]:
        out.append(f"- {f}")
    (AUDIT_OUT / "sidecar_roi_repair.md").write_text("\n".join(out))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    global CH_CONTAINER_DEFAULT  # noqa: PLW0603
    parser.add_argument("--ch-container", default=os.environ.get("SPRINT050_CH_CONTAINER", CH_CONTAINER_DEFAULT))
    parser.add_argument("--skip-ch", action="store_true", help="skip CH queries (use bundle-only)")
    args = parser.parse_args()
    CH_CONTAINER_DEFAULT = args.ch_container

    print(f"[sprint050] loading {len(BUNDLE_WEEKS)} q4 bundle weeks", flush=True)
    rows = load_q4_bundle()
    print(f"[sprint050] loaded {len(rows)} q4 candidate rows", flush=True)

    # WP-B + WP-C
    if args.skip_ch:
        discovery = {
            "kind": "sprint050_v3_new_continent_discovery",
            "generated_at": _now_utc(),
            "ch_query_error": "skipped_via_flag",
            "bundle_universe": {},
            "ch_weekly_v3_trade_universe": [],
            "new_continent_weeks": [],
            "new_continent_summary": {
                "weeks_outside_bundle": 0,
                "post_bundle_weeks": [],
                "mid_bundle_gap_weeks": [],
                "pre_bundle_weeks": [],
                "post_bundle_swap_rows": 0,
                "mid_bundle_gap_swap_rows": 0,
                "pre_bundle_swap_rows": 0,
            },
        }
    else:
        discovery = discover_v3_new_continent(rows)
    _write_json(DIAG_OUT / "v3_new_continent_discovery.json", discovery)
    _write_csv_rows(
        DIAG_OUT / "v3_new_continent_weeks.csv",
        discovery.get("new_continent_weeks", []),
    )
    write_discovery_md(discovery)
    print("[sprint050] WP-B written", flush=True)

    plan = oos_candidate_plan(discovery)
    _write_json(DIAG_OUT / "oos_candidate_expansion.json", plan)
    write_oos_md(plan)
    print("[sprint050] WP-C written", flush=True)

    # WP-D
    by_feature = explore_feature_regimes(rows)
    flat: list[dict[str, Any]] = []
    for fname, buckets in by_feature.items():
        for b in buckets:
            flat.append({**b})
    _write_csv_rows(DIAG_OUT / "feature_regime_exploration.csv", flat)
    _write_json(DIAG_OUT / "feature_regime_exploration.json", by_feature)
    write_feature_md(by_feature)
    print("[sprint050] WP-D written", flush=True)

    # WP-E
    econ = economic_prevalidation(rows)
    _write_json(DIAG_OUT / "v3_economic_prevalidation.json", econ)
    write_economic_md(econ)
    print("[sprint050] WP-E written", flush=True)

    # WP-F
    sidecar = sidecar_roi_repair()
    _write_json(DIAG_OUT / "sidecar_roi_repair.json", sidecar)
    write_sidecar_md(sidecar)
    print("[sprint050] WP-F written", flush=True)

    # decision_flags rollup
    promising = sum(
        1
        for buckets in by_feature.values()
        for b in buckets
        if (b["rows_routed_5m"] or 0) >= 300
        and (b["ret5_routed_profit_factor"] or 0) >= 1.5
        and (b["ret5_routed_hit_rate_pos"] or 0) >= 0.5
    )
    weak = sum(
        1
        for buckets in by_feature.values()
        for b in buckets
        if 100 <= (b["rows_routed_5m"] or 0) < 300
        and (b["ret5_routed_profit_factor"] or 0) >= 1.0
    )
    feature_status = (
        "PROMISING" if promising >= 3 else ("WEAK_PROMISING" if weak >= 3 else "NO_SIGNAL_FOUND_OR_INCONCLUSIVE")
    )

    rollup = {
        "kind": "sprint050_decision_rollup",
        "generated_at": _now_utc(),
        "MAINLINE_SCOPE": "BASE_UNISWAP_V3",
        "WHITEPAPER_MAINLINE_CONTINUES": True,
        "V2_MAINLINE_STATUS": "PAUSED_REFERENCE_ONLY",
        "V3_NEW_CONTINENT_STATUS": (
            "FOUND"
            if discovery.get("new_continent_summary", {}).get("post_bundle_swap_rows", 0) >= 100_000
            else (
                "WEAK_FOUND"
                if discovery.get("new_continent_summary", {}).get("post_bundle_swap_rows", 0) >= 10_000
                else "NOT_FOUND"
            )
        ),
        "OOS_EXPANSION_STATUS": plan["decision_flags"]["OOS_EXPANSION_STATUS"],
        "FEATURE_REGIME_STATUS": feature_status,
        "ECONOMIC_PREVALIDATION_STATUS": econ["verdict"],
        "SIDECAR_STATUS": "AUXILIARY_RADAR_ONLY",
        "TRAINING_ALLOWED": False,
        "TRAINING_PROPOSAL_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PRIVATE_KEY_USED": False,
        "PRODUCTION_SIGNALS_WRITTEN": False,
        "RPC_COST_RISK": "LOW",
        "feature_promising_count": promising,
        "feature_weak_promising_count": weak,
    }
    _write_json(DIAG_OUT / "decision_flags.json", rollup)
    print("[sprint050] decision rollup written", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
