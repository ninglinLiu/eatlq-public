#!/usr/bin/env python3
"""
Sprint028B-R — Base V3 broader-universe feature-only sidecar (read-only ClickHouse).

Contract (charter: mvp/audits/sprint028_active_universe_mining_charter.md):
- Feature-only. No labels, no training, no Ethereum JSON-RPC, no Dune, no Redis writes.
- **Production runs require CLICKHOUSE_URL** (Sprint028B-R); local dashcam-only is insufficient
  per prior NEED_RESCOPING — use `--offline-fixture` only for CI/synthetic tests.
- Read-only ClickHouse HTTP SELECT on `discovered_pools` + `uniswap_v3_swaps_enriched`.
- Identity columns only as _*_AUDIT_ONLY; never tx_hash / log_index / block_number as features.

Usage:
    python3 scripts/sprint028b_v3_universe_mining.py [--dry-run] [--offline-fixture] [--rescope-grid] [--max-scan-lines N]

Env:
    CLICKHOUSE_URL            **required** unless --offline-fixture (read-only HTTP)
    CLICKHOUSE_USER / CLICKHOUSE_PASSWORD  optional basic auth
    H108_JOIN_SAMPLE          default mvp/diagnostics/sprint027d_h108_pool_quality_joined_sample.csv
    DASHCAM_PATH              used only with --offline-fixture
    SPRINT028B_CH_WINDOW_DAYS     default 21 — swap density lookback
    SPRINT028B_MIN_SWAPS_PER_POOL default 50 — HAVING threshold on swaps in window
    SPRINT028B_MAX_POOL_CANDIDATES default 120 — cap pools from discovery JOIN
    SPRINT028B_CH_SWAP_ROW_CAP    default 250000 — max swap rows pulled for aggregation
    SPRINT028B_PER_POOL_CAP   default 200
    SPRINT028B_MAX_OUTPUT_ROWS default 2500 (R2 grid may use 3000)
    SPRINT028B_OUT_CSV / _OUT_JSON / _OUT_MD  optional output paths
    Sprint028B-R2: --rescope-grid writes mvp/diagnostics/sprint028b_v3_universe_rescope_grid.json then final artifacts if a cell passes gates
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SEC = (60, 180, 300, 900, 1800)
BUCKET_MS = 300_000


def _repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


CHAIN_ID_BASE = 8453

DECISION_FLAGS = {
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "TRAINING_ALLOWED": False,
    "LABELING_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "PHASE6_ALLOWED": False,
    "H12_ABLATION_ALLOWED": False,
    "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED": False,
    "SMART_MONEY_GENERAL_LINE_OPEN": False,
    "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    "ACTIVE_UNIVERSE_MINING_ALLOWED": True,
    "SCOPED_MICRO_ALPHA_CHARTER_ALLOWED": True,
    "MICRO_LIVE_CALIBRATION_CHARTER_ALLOWED": True,
    "MICRO_LIVE_EXECUTION_ALLOWED": False,
    "IMPLEMENTATION_ALLOWED_sidecar_diagnostic": True,
    "redis_write": False,
    "rpc_called": False,
    "dune_called": False,
    "clickhouse_write": False,
    "feature_only_no_alpha_claim": True,
}


def _sprint028a_charter_in_head(repo: Path) -> bool:
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "cat-file",
                "-e",
                "HEAD:mvp/audits/sprint028_active_universe_mining_charter.md",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _load_top5_exclusion(join_csv: Path) -> Set[str]:
    if not join_csv.is_file():
        return set()
    c: Counter[str] = Counter()
    with join_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pa = (row.get("pool_address") or "").strip().lower()
            if pa:
                c[pa] += 1
    return {p for p, _ in c.most_common(5)}


def _optional_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except (TypeError, ValueError):
        return None


def _ch_http_get_body(
    ch_url: str,
    sql: str,
    ch_user: str,
    ch_password: str,
    timeout_sec: float = 120.0,
) -> Optional[str]:
    if not ch_url:
        return None
    params = urlencode({"query": sql})
    url = f"{ch_url.rstrip('/')}/?{params}"
    try:
        req = Request(url, method="GET")
        if ch_user:
            import base64

            token = base64.b64encode(f"{ch_user}:{ch_password}".encode()).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        with urlopen(req, timeout=timeout_sec) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, TimeoutError, ValueError):
        return None


def _parse_discovered_at_ts(s: str) -> Optional[float]:
    s = (s or "").strip().replace("Z", "")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            d = dt.datetime.strptime(s[:26], fmt).replace(tzinfo=dt.timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    try:
        d = dt.datetime.fromisoformat(s[:19]).replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def _pool_age_bucket_from_sec(age_sec: float) -> str:
    if age_sec < 3600:
        return "new_lt_1h"
    if age_sec < 86400:
        return "young_lt_24h"
    if age_sec < 604800:
        return "maturing_lt_7d"
    return "mature_gt_7d"


def _ch_discover_v3_weth_dense_pools(
    exclude: Set[str],
    ch_url: str,
    ch_user: str,
    ch_password: str,
    window_days: int,
    min_swaps: int,
    max_pools: int,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """
    Join discovered_pools (v3, WETH leg) with swap density in uniswap_v3_swaps_enriched.
    Returns ordered pool list + meta map (fee_bps, discovered_at_str, swap_count_window, has_weth_leg).
    """
    ex_list = "', '".join(sorted(exclude))
    not_in = (
        f"AND lower(dp.pool_address) NOT IN ('{ex_list}')"
        if exclude
        else ""
    )
    sql = f"""
SELECT
  lower(dp.pool_address) AS pa,
  toUInt8(dp.has_weth_leg) AS hw,
  toInt32(dp.fee_bps) AS fee_bps,
  toString(dp.discovered_at) AS disc_at,
  toUInt64(sc.swap_cnt) AS swap_cnt
FROM eatlq.discovered_pools AS dp
INNER JOIN (
  SELECT
    lower(pool_address) AS pa,
    count() AS swap_cnt
  FROM eatlq.uniswap_v3_swaps_enriched
  WHERE chain_id = {CHAIN_ID_BASE}
    AND removed = 0
    AND coalesce(block_time, received_at_ms) >= now() - INTERVAL {int(window_days)} DAY
  GROUP BY pa
  HAVING swap_cnt >= {int(min_swaps)}
) AS sc ON lower(dp.pool_address) = sc.pa
WHERE dp.chain_id = {CHAIN_ID_BASE}
  AND lower(dp.pool_type) = 'v3'
  AND dp.has_weth_leg = 1
  {not_in}
ORDER BY sc.swap_cnt DESC
LIMIT {int(max_pools)}
FORMAT TabSeparated
""".strip()
    body = _ch_http_get_body(ch_url, sql, ch_user, ch_password, timeout_sec=120.0)
    if not body:
        return [], {}
    meta: Dict[str, Dict[str, Any]] = {}
    order_list: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pa = parts[0].lower()
        try:
            hw = int(parts[1])
            fee = int(parts[2])
            scnt = int(parts[4])
        except ValueError:
            continue
        disc_at = parts[3]
        order_list.append(pa)
        meta[pa] = {
            "has_weth_leg": hw,
            "fee_bps": fee,
            "discovered_at_str": disc_at,
            "swap_count_window": scnt,
        }
    return order_list, meta


def _ch_fetch_swaps_sample(
    pool_addrs: List[str],
    ch_url: str,
    ch_user: str,
    ch_password: str,
    window_days: int,
    row_cap: int,
) -> Tuple[List[Tuple[str, int, str, float, float, float]], bool]:
    """Return (pa, ts_ms, sender, a0, a1, sqrt_price) rows; truncated flag if LIMIT hit."""
    if not pool_addrs or not ch_url:
        return [], False
    plist = "', '".join(sorted(set(pool_addrs))[:300])
    sql = f"""
SELECT
  lower(pool_address) AS pa,
  toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) AS ts_ms,
  lower(sender) AS sender,
  toFloat64OrZero(amount0_raw) AS a0,
  toFloat64OrZero(amount1_raw) AS a1,
  toFloat64OrZero(sqrt_price_x96_raw) AS sp
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID_BASE}
  AND removed = 0
  AND lower(pool_address) IN ('{plist}')
  AND coalesce(block_time, received_at_ms) >= now() - INTERVAL {int(window_days)} DAY
ORDER BY pool_address, ts_ms
LIMIT {int(row_cap)}
FORMAT TabSeparated
""".strip()
    body = _ch_http_get_body(ch_url, sql, ch_user, ch_password, timeout_sec=180.0)
    if not body:
        return [], False
    out: List[Tuple[str, int, str, float, float, float]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        pa = parts[0].lower()
        try:
            ts_ms = int(parts[1])
            a0 = float(parts[3])
            a1 = float(parts[4])
            sp = float(parts[5])
        except ValueError:
            continue
        sender = parts[2] or ""
        out.append((pa, ts_ms, sender, a0, a1, sp))
    truncated = len(out) >= row_cap
    return out, truncated


def _vol_proxy(a0: float, a1: float) -> float:
    return abs(a0) + abs(a1)


def _window_buyer_stats(
    swaps: List[Tuple[int, str, float, float, float]], end_ms: int, window_sec: int
) -> Tuple[int, Optional[float], float, int]:
    """unique senders, top1 share of volume, total vol, swap count in [end_ms - w, end_ms]."""
    start_ms = end_ms - window_sec * 1000
    vol_by: Counter[str] = Counter()
    n = 0
    for ts_ms, sender, a0, a1, _ in swaps:
        if ts_ms < start_ms or ts_ms > end_ms:
            continue
        n += 1
        if sender:
            vol_by[sender] += _vol_proxy(a0, a1)
    total = float(sum(vol_by.values()))
    uniq = len(vol_by)
    if total <= 0 or not vol_by:
        return uniq, None, total, n
    top = max(vol_by.values())
    return uniq, top / total, total, n


def _sidecar_rows_from_ch_swaps(
    pool_order: List[str],
    pool_meta: Dict[str, Dict[str, Any]],
    swap_rows: List[Tuple[str, int, str, float, float, float]],
    per_pool_cap: int,
    max_output_rows: int,
) -> List[Dict[str, Any]]:
    per_pool_swaps: DefaultDict[str, List[Tuple[int, str, float, float, float]]] = defaultdict(list)
    for pa, ts_ms, sender, a0, a1, sp in swap_rows:
        per_pool_swaps[pa].append((ts_ms, sender, a0, a1, sp))
    for pa in per_pool_swaps:
        per_pool_swaps[pa].sort(key=lambda x: x[0])

    def _new_bucket() -> Dict[str, Any]:
        return {"senders": Counter(), "n": 0, "vol": 0.0, "sps": []}

    bucket_agg: DefaultDict[Tuple[str, int], Dict[str, Any]] = defaultdict(_new_bucket)

    for pa, ts_ms, sender, a0, a1, sp in swap_rows:
        b_ms = (ts_ms // BUCKET_MS) * BUCKET_MS
        g = bucket_agg[(pa, b_ms)]
        g["n"] += 1
        g["vol"] += _vol_proxy(a0, a1)
        if sender:
            g["senders"][sender] += _vol_proxy(a0, a1)
        if sp and not math.isnan(sp) and sp > 0:
            g["sps"].append((ts_ms, sp))

    rows_out: List[Dict[str, Any]] = []
    prev_bucket_count: Dict[str, Optional[int]] = defaultdict(lambda: None)

    for pa in pool_order:
        if pa not in per_pool_swaps:
            continue
        buckets_sorted = sorted({b for (p, b) in bucket_agg.keys() if p == pa})
        meta = pool_meta.get(pa, {})
        disc_ts = _parse_discovered_at_ts(meta.get("discovered_at_str", ""))
        for b_ms in buckets_sorted[:per_pool_cap]:
            if len(rows_out) >= max_output_rows:
                break
            g = bucket_agg[(pa, b_ms)]
            senders: Counter[str] = g["senders"]
            uniq_b = len(senders)
            tot_v = float(sum(senders.values())) if senders else 0.0
            top1_b = None
            if tot_v > 0 and senders:
                top1_b = max(senders.values()) / tot_v
            sps: List[Tuple[int, float]] = g["sps"]
            sps.sort(key=lambda x: x[0])
            sp_vel = None
            if len(sps) >= 2:
                sp0, sp1 = sps[0][1], sps[-1][1]
                if sp0 > 0:
                    sp_vel = (sp1 - sp0) / sp0

            end_ms = b_ms + BUCKET_MS
            swaps_p = per_pool_swaps[pa]
            side: Dict[str, Any] = {
                "_pool_address_AUDIT_ONLY": pa,
                "_bucket_ts_ms_AUDIT_ONLY": b_ms,
                "_source_lane": "sprint028b_ch_v3_swaps_enriched",
                "swap_count_bucket": int(g["n"]),
                "volume_l1_proxy_bucket": round(g["vol"], 8),
                "unique_buyers_bucket": uniq_b,
                "top1_buyer_share_bucket": round(top1_b, 6) if top1_b is not None else None,
                "sqrt_price_velocity_bucket": round(sp_vel, 8) if sp_vel is not None else None,
                "has_weth_leg_proxy": meta.get("has_weth_leg"),
                "_fee_bps_meta": meta.get("fee_bps"),
                "_registry_swap_density_window": meta.get("swap_count_window"),
                "_buyer_concentration_status": "from_ch_sender_volume_windows",
                "_flow_quality_raw_status": "from_ch_swap_and_uniq_senders",
                "_temporal_dynamics_status": "from_ch_bucket_and_windows",
                "_pool_age_meta_status": "from_discovered_pools_discovered_at",
            }
            if disc_ts is not None:
                sig_ts = end_ms / 1000.0
                age = max(0.0, sig_ts - disc_ts)
                side["pool_age_sec"] = round(age, 3)
                side["pool_age_bucket"] = _pool_age_bucket_from_sec(age)
            else:
                side["pool_age_sec"] = None
                side["pool_age_bucket"] = ""

            prev_n = prev_bucket_count[pa]
            side["bucket_swap_count_delta_prev"] = (
                int(g["n"] - prev_n) if prev_n is not None else None
            )
            prev_bucket_count[pa] = int(g["n"])

            for w in WINDOWS_SEC:
                uq, t1, vol_w, ct = _window_buyer_stats(swaps_p, end_ms, w)
                side[f"unique_buyers_{w}s"] = uq
                side[f"top1_buyer_share_{w}s"] = round(t1, 6) if t1 is not None else None
                side[f"buy_count_{w}s"] = ct
                side[f"sell_count_{w}s"] = None
                side[f"buy_volume_raw_{w}s"] = round(vol_w, 8) if vol_w > 0 else None
                fq = uq / max(1, ct) if ct else None
                side[f"flow_uniq_ratio_{w}s"] = round(fq, 6) if fq is not None else None
                side[f"activity_acceleration_{w}s_stub"] = None

            rows_out.append(side)
        if len(rows_out) >= max_output_rows:
            break

    return rows_out


def _apply_cap_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {
            "total_rows": 0,
            "distinct_pools": 0,
            "pools_with_gte_30_rows": 0,
            "top1_share": 0.0,
            "top5_share": 0.0,
            "median_rows_per_pool": 0.0,
            "feature_coverage_rate": 0.0,
        }
    c = Counter(r["_pool_address_AUDIT_ONLY"] for r in rows)
    top5 = c.most_common(5)
    top1_share = top5[0][1] / n if top5 else 0.0
    top5_share = sum(x for _, x in top5) / n if top5 else 0.0
    ge30 = sum(1 for _, cnt in c.items() if cnt >= 30)
    counts_sorted = sorted(c.values())
    mid = len(counts_sorted) // 2
    med = (
        counts_sorted[mid]
        if len(counts_sorted) % 2
        else (counts_sorted[mid - 1] + counts_sorted[mid]) / 2
    )
    ok = 0
    for r in rows:
        if any(
            (r.get(f"unique_buyers_{w}s") or 0) > 0 or (r.get(f"buy_count_{w}s") or 0) > 0
            for w in WINDOWS_SEC
        ):
            ok += 1
        elif r.get("swap_count_bucket"):
            ok += 1
        elif r.get("price_velocity_5m") is not None:
            ok += 1
    fcov = ok / n
    return {
        "total_rows": n,
        "distinct_pools": len(c),
        "pools_with_gte_30_rows": ge30,
        "top1_share": round(top1_share, 4),
        "top5_share": round(top5_share, 4),
        "median_rows_per_pool": float(med),
        "feature_coverage_rate": round(fcov, 4),
    }


def _per_feature_nonnull_rates(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    keys: List[str] = [
        "swap_count_bucket",
        "unique_buyers_bucket",
        "top1_buyer_share_bucket",
        "sqrt_price_velocity_bucket",
        "has_weth_leg_proxy",
        "pool_age_sec",
    ]
    for w in WINDOWS_SEC:
        keys.extend(
            [
                f"unique_buyers_{w}s",
                f"top1_buyer_share_{w}s",
                f"buy_volume_raw_{w}s",
                f"flow_uniq_ratio_{w}s",
            ]
        )
    if not rows:
        return {k: 0.0 for k in keys}
    n = len(rows)
    out: Dict[str, float] = {}
    for k in keys:
        c = sum(
            1
            for r in rows
            if r.get(k) is not None and r.get(k) != ""
        )
        out[k] = round(c / n, 4)
    return out


def _gates_ok(m: Dict[str, Any]) -> bool:
    return (
        m["distinct_pools"] >= 20
        and m["pools_with_gte_30_rows"] >= 20
        and m["top1_share"] <= 0.30
        and m["top5_share"] <= 0.70
    )


def _r2_gates_ok(metrics: Dict[str, Any], swap_truncated: bool) -> bool:
    if swap_truncated or metrics.get("total_rows", 0) == 0:
        return False
    if metrics.get("feature_coverage_rate", 0) < 0.95:
        return False
    return (
        metrics["distinct_pools"] >= 20
        and metrics["pools_with_gte_30_rows"] >= 20
        and metrics["top1_share"] <= 0.30
        and metrics["top5_share"] <= 0.70
    )


def _fee_bps_histogram(pool_meta: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    c: Counter[str] = Counter()
    for m in pool_meta.values():
        c[str(m.get("fee_bps", ""))] += 1
    return dict(sorted(c.items(), key=lambda x: (-x[1], x[0])))


def _ch_ingest_source_uniq_pools(
    lookback_days: int,
    ch_url: str,
    ch_user: str,
    ch_password: str,
) -> Dict[str, int]:
    """Read-only: uniq pools per ingest `source` over lookback (global chain slice)."""
    sql = f"""
SELECT if(source = '', 'empty', toString(source)) AS src, uniqExact(lower(pool_address)) AS u
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID_BASE}
  AND removed = 0
  AND coalesce(block_time, received_at_ms) >= now() - INTERVAL {int(lookback_days)} DAY
GROUP BY src
ORDER BY u DESC
FORMAT TabSeparated
""".strip()
    body = _ch_http_get_body(ch_url, sql, ch_user, ch_password, timeout_sec=90.0)
    if not body:
        return {}
    out: Dict[str, int] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            out[str(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return out


def _run_ch_pipeline_once(
    exclude: Set[str],
    ch_url: str,
    ch_user: str,
    ch_password: str,
    window_days: int,
    min_swaps: int,
    max_pool_cand: int,
    swap_row_cap: int,
    per_cap: int,
    max_out: int,
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    bool,
    List[str],
    Dict[str, Dict[str, Any]],
    str,
]:
    pool_order, pool_meta = _ch_discover_v3_weth_dense_pools(
        exclude, ch_url, ch_user, ch_password, window_days, min_swaps, max_pool_cand
    )
    if not pool_order:
        return (
            [],
            {
                "total_rows": 0,
                "distinct_pools": 0,
                "pools_with_gte_30_rows": 0,
                "top1_share": 0.0,
                "top5_share": 0.0,
                "median_rows_per_pool": 0.0,
                "feature_coverage_rate": 0.0,
            },
            {"universe_discovery": "empty_discovery", "rows_from_clickhouse_swaps": 0},
            False,
            [],
            {},
            "clickhouse_discovered_pools_join_v3_swaps_enriched",
        )
    swaps, swap_truncated = _ch_fetch_swaps_sample(
        pool_order, ch_url, ch_user, ch_password, window_days, swap_row_cap
    )
    rows = _sidecar_rows_from_ch_swaps(pool_order, pool_meta, swaps, per_cap, max_out)
    metrics = _apply_cap_metrics(rows)
    source_note = "clickhouse_discovered_pools_join_v3_swaps_enriched"
    source_mix = {
        "rows_from_clickhouse_swaps": len(rows),
        "rows_from_dashcam_overlay": 0,
        "universe_discovery": source_note,
        "swap_fetch_truncated": swap_truncated,
    }
    return rows, metrics, source_mix, swap_truncated, pool_order, pool_meta, source_note


def _grid_combo_iter() -> List[Tuple[int, int, int]]:
    out: List[Tuple[int, int, int]] = []
    for lb in (21, 45, 60, 90):
        for ms in (50, 30, 20):
            for mpc in (120, 200):
                out.append((lb, ms, mpc))
    return out


def _pick_closest_r2_cell(grid_cells: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not grid_cells:
        return None
    best: Optional[Dict[str, Any]] = None
    best_tup: Optional[Tuple[int, int, float, float, float]] = None
    for cell in grid_cells:
        m = cell.get("concentration_post_cap") or {}
        d = int(m.get("distinct_pools") or 0)
        g30 = int(m.get("pools_with_gte_30_rows") or 0)
        t1 = float(m.get("top1_share") or 0)
        t5 = float(m.get("top5_share") or 0)
        fc = float(m.get("feature_coverage_rate") or 0)
        tup = (d, g30, fc, -max(0.0, t1 - 0.30), -max(0.0, t5 - 0.70))
        if best_tup is None or tup > best_tup:
            best_tup = tup
            best = cell
    return best


def _write_rescope_grid_file(
    path: Path,
    grid_cells: List[Dict[str, Any]],
    selected: Optional[Dict[str, Any]],
    verdict: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sprint": "028B-R2",
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "grid_cells": grid_cells,
                "selected_combo": selected,
                "universe_verdict": verdict,
                "decision_flags": DECISION_FLAGS,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_blocked_json_md(
    verdict: str,
    message: str,
    out_json: Path,
    out_md: Path,
    repo_root: Path,
    extra: Optional[Dict[str, Any]] = None,
    md_sections: Optional[List[str]] = None,
) -> None:
    summary: Dict[str, Any] = {
        "sprint": "028B-R",
        "universe_gate_verdict": verdict,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": message,
        "decision_flags": DECISION_FLAGS,
        "sprint028c_can_start": False,
    }
    if extra:
        summary.update(extra)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    if md_sections:
        out_md.write_text("\n".join(md_sections), encoding="utf-8")
    else:
        out_md.write_text(
            f"# Sprint028B — blocked\n\n**Verdict:** `{verdict}`\n\n{message}\n",
            encoding="utf-8",
        )


# --- offline fixture (dashcam) legacy path ---------------------------------

def _dashcam_v3_counts(
    dashcam: Path,
    exclude: Set[str],
    max_scan_lines: int,
) -> Counter:
    counts: Counter = Counter()
    lines = 0
    with dashcam.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "pool_address" not in reader.fieldnames:
            return counts
        for row in reader:
            lines += 1
            if lines > max_scan_lines:
                break
            pa = (row.get("pool_address") or "").strip().lower()
            if not pa or pa in exclude:
                continue
            if (row.get("pool_type") or "").strip().lower() != "v3":
                continue
            counts[pa] += 1
    return counts


def _try_ch_v3_pool_rank(
    exclude: Set[str],
    ch_url: str,
    ch_user: str,
    ch_password: str,
    limit_pools: int = 120,
) -> Optional[List[Tuple[str, int]]]:
    if not ch_url:
        return None
    ex_list = "', '".join(sorted(exclude))
    not_in_clause = (
        f"AND lower(pool_address) NOT IN ('{ex_list}')"
        if exclude
        else ""
    )
    sql = (
        f"SELECT lower(pool_address) AS pa, count() AS cnt "
        f"FROM eatlq.uniswap_v3_swaps_enriched "
        f"WHERE chain_id = {CHAIN_ID_BASE} "
        f"{not_in_clause} "
        f"GROUP BY pa ORDER BY cnt DESC LIMIT {limit_pools} "
        f"FORMAT TabSeparated"
    )
    body = _ch_http_get_body(ch_url, sql, ch_user, ch_password)
    if body is None:
        return None
    out: List[Tuple[str, int]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            out.append((parts[0].lower(), int(parts[1])))
        except ValueError:
            continue
    return out if out else None


def _try_ch_discovered_meta(
    pool_addrs: List[str],
    ch_url: str,
    ch_user: str,
    ch_password: str,
    timeout_sec: float = 45.0,
) -> Dict[str, Dict[str, Any]]:
    if not ch_url or not pool_addrs:
        return {}
    uniq = sorted({p.lower() for p in pool_addrs})[:220]
    plist = "', '".join(uniq)
    sql = (
        f"SELECT lower(pool_address), toString(discovered_at), toUInt8(has_weth_leg), "
        f"fee_bps, pool_type "
        f"FROM eatlq.discovered_pools "
        f"WHERE chain_id = {CHAIN_ID_BASE} AND lower(pool_address) IN ('{plist}') "
        f"FORMAT TabSeparated"
    )
    body = _ch_http_get_body(ch_url, sql, ch_user, ch_password, timeout_sec)
    if not body:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pa = parts[0].lower()
        try:
            hw = int(parts[2])
            fee = int(parts[3])
        except ValueError:
            continue
        out[pa] = {
            "discovered_at_str": parts[1],
            "has_weth_leg": hw,
            "fee_bps": fee,
            "pool_type_ch": str(parts[4]),
        }
    return out


def _merge_discovered_meta(
    rows: List[Dict[str, Any]],
    meta_by_pool: Dict[str, Dict[str, Any]],
) -> None:
    for r in rows:
        pa = r.get("_pool_address_AUDIT_ONLY") or ""
        m = meta_by_pool.get(pa)
        if not m:
            r["_pool_age_meta_status"] = "miss_in_discovered_pools_or_ch_unreachable"
            continue
        r["_pool_age_meta_status"] = "from_discovered_pools_read_only"
        r["has_weth_leg_proxy"] = m["has_weth_leg"]
        r["_fee_bps_meta"] = m["fee_bps"]
        r["_registry_pool_type_AUDIT_ONLY"] = m.get("pool_type_ch", "")
        disc_ts = _parse_discovered_at_ts(m["discovered_at_str"])
        b_ms = r.get("_bucket_ts_ms_AUDIT_ONLY")
        if disc_ts is None or not isinstance(b_ms, int):
            r["pool_age_sec"] = None
            r["pool_age_bucket"] = ""
            continue
        sig_ts = b_ms / 1000.0
        age = max(0.0, sig_ts - disc_ts)
        r["pool_age_sec"] = round(age, 3)
        r["pool_age_bucket"] = _pool_age_bucket_from_sec(age)


def _stream_build_sidecar_dashcam(
    dashcam: Path,
    exclude: Set[str],
    pool_order: List[str],
    per_pool_cap: int,
    max_output_rows: int,
    max_scan_lines: int,
) -> List[Dict[str, Any]]:
    per_pool_kept: Dict[str, int] = defaultdict(int)
    rows_out: List[Dict[str, Any]] = []
    pool_rank = {p: i for i, p in enumerate(pool_order)}
    lines = 0
    seen_bucket: Set[Tuple[str, int]] = set()
    prev_vel: Dict[str, float] = {}

    with dashcam.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lines += 1
            if lines > max_scan_lines:
                break
            if len(rows_out) >= max_output_rows:
                break
            pa = (row.get("pool_address") or "").strip().lower()
            if not pa or pa in exclude:
                continue
            if (row.get("pool_type") or "").strip().lower() != "v3":
                continue
            if pa not in pool_rank:
                continue
            if per_pool_kept[pa] >= per_pool_cap:
                continue
            try:
                ts = int(row.get("timestamp") or 0)
            except ValueError:
                continue
            if ts <= 0:
                continue
            b_ms = (ts // BUCKET_MS) * BUCKET_MS
            key = (pa, b_ms)
            if key in seen_bucket:
                continue
            seen_bucket.add(key)

            pv = _optional_float(row.get("momentum_val"))
            vel_delta: Optional[float] = None
            if pv is not None and pa in prev_vel:
                vel_delta = pv - prev_vel[pa]
            if pv is not None:
                prev_vel[pa] = pv
            temporal_status = (
                "dashcam_scan_order_delta_proxy"
                if vel_delta is not None
                else "dashcam_stub_no_prior_row_same_pool"
            )

            side: Dict[str, Any] = {
                "_pool_address_AUDIT_ONLY": pa,
                "_bucket_ts_ms_AUDIT_ONLY": b_ms,
                "_source_lane": "sprint028b_offline_fixture_dashcam",
                "price_velocity_5m": pv,
                "buy_pressure_ratio": _optional_float(row.get("whale_val")),
                "token_volatility_5m": _optional_float(row.get("risk_val")),
                "activity_heat": _optional_float(row.get("activity_val")),
                "unique_buyer_entropy_5m": _optional_float(row.get("unique_buyer_entropy_5m")),
                "temporal_velocity_delta_scan_order": vel_delta,
                "_pool_type_AUDIT_ONLY": row.get("pool_type", ""),
                "_dex_AUDIT_ONLY": row.get("dex", ""),
                "_buyer_concentration_status": "offline_fixture_no_ch",
                "_flow_quality_raw_status": "offline_fixture_no_ch",
                "_temporal_dynamics_status": temporal_status,
                "_pool_age_meta_status": "pending_discovered_pools_merge",
                "has_weth_leg_proxy": None,
                "pool_age_sec": None,
                "pool_age_bucket": "",
            }
            for w in WINDOWS_SEC:
                side[f"unique_buyers_{w}s"] = None
                side[f"top1_buyer_share_{w}s"] = None
                side[f"buy_count_{w}s"] = None
                side[f"sell_count_{w}s"] = None
                side[f"buy_volume_raw_{w}s"] = None
                side[f"flow_uniq_ratio_{w}s"] = None
                side[f"activity_acceleration_{w}s_stub"] = None
            rows_out.append(side)
            per_pool_kept[pa] += 1
    return rows_out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline-fixture", action="store_true", help="CI only: dashcam path, optional CH rank")
    parser.add_argument(
        "--rescope-grid",
        action="store_true",
        help="Sprint028B-R2: bounded CH param grid; writes rescope_grid.json; refreshes artifacts if a cell passes",
    )
    parser.add_argument("--max-scan-lines", type=int, default=None)
    args = parser.parse_args(argv)

    grid_wall_s = 0.0

    dashcam = Path(os.environ.get("DASHCAM_PATH", REPO_ROOT / "logs" / "dashcam.csv"))
    join_sample = Path(
        os.environ.get(
            "H108_JOIN_SAMPLE",
            REPO_ROOT / "mvp" / "diagnostics" / "sprint027d_h108_pool_quality_joined_sample.csv",
        )
    )
    out_csv = Path(
        os.environ.get(
            "SPRINT028B_OUT_CSV",
            str(REPO_ROOT / "mvp" / "diagnostics" / "sprint028b_v3_universe_sidecar_sample.csv"),
        )
    )
    out_json = Path(
        os.environ.get(
            "SPRINT028B_OUT_JSON",
            str(REPO_ROOT / "mvp" / "diagnostics" / "sprint028b_v3_universe_coverage_summary.json"),
        )
    )
    out_md = Path(
        os.environ.get(
            "SPRINT028B_OUT_MD",
            str(REPO_ROOT / "mvp" / "audits" / "sprint028b_v3_universe_mining_report.md"),
        )
    )

    max_scan = int(args.max_scan_lines or os.environ.get("SPRINT028B_MAX_SCAN_LINES", "1000000"))
    per_cap = int(os.environ.get("SPRINT028B_PER_POOL_CAP", "200"))
    if args.rescope_grid:
        max_out = int(os.environ.get("SPRINT028B_MAX_OUTPUT_ROWS", "3000"))
    else:
        max_out = int(os.environ.get("SPRINT028B_MAX_OUTPUT_ROWS", "2500"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "").strip()
    ch_user = os.environ.get("CLICKHOUSE_USER", "")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    window_days = int(os.environ.get("SPRINT028B_CH_WINDOW_DAYS", "21"))
    min_swaps = int(os.environ.get("SPRINT028B_MIN_SWAPS_PER_POOL", "50"))
    max_pool_cand = int(os.environ.get("SPRINT028B_MAX_POOL_CANDIDATES", "120"))
    swap_row_cap = int(os.environ.get("SPRINT028B_CH_SWAP_ROW_CAP", "250000"))

    exclude = _load_top5_exclusion(join_sample)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "sprint": "028B-R",
                    "clickhouse_url_set": bool(ch_url),
                    "clickhouse_required_for_run": not args.offline_fixture,
                    "ch_tables": [
                        "eatlq.discovered_pools",
                        "eatlq.uniswap_v3_swaps_enriched",
                    ],
                    "row_caps": {
                        "max_pool_candidates": max_pool_cand,
                        "ch_swap_fetch_limit": swap_row_cap,
                        "per_pool_cap": per_cap,
                        "max_output_rows": max_out,
                        "ch_window_days": window_days,
                        "min_swaps_per_pool": min_swaps,
                    },
                    "estimated_max_output_rows": min(
                        max_out, max_pool_cand * per_cap
                    ),
                    "read_only": True,
                    "rpc_dune_redis": False,
                    "offline_fixture": args.offline_fixture,
                    "rescope_grid": args.rescope_grid,
                    "current_top5_exclusion_count": len(exclude),
                    "decision_flags": DECISION_FLAGS,
                },
                indent=2,
            )
        )
        return 0

    if not _sprint028a_charter_in_head(REPO_ROOT):
        print("BLOCKED_BY_MISSING_028A_COMMIT", file=sys.stderr)
        _write_blocked_json_md(
            "BLOCKED_BY_MISSING_028A_COMMIT",
            "Sprint028A charter file is not present at `HEAD` in this repo clone.",
            out_json,
            out_md,
            REPO_ROOT,
        )
        return 6

    if not args.offline_fixture and not ch_url:
        print("BLOCKED_BY_MISSING_CLICKHOUSE_URL", file=sys.stderr)
        ch_block_md = [
            "# Sprint028B-R — blocked (ClickHouse required)",
            "",
            "**Verdict:** `BLOCKED_BY_MISSING_CLICKHOUSE_URL`",
            "",
            "Sprint028B-R does **not** repeat dashcam-only discovery (prior run: NEED_RESCOPING). Set `CLICKHOUSE_URL` "
            "to a read-only HTTP endpoint and re-run, or use `--offline-fixture` **only** in CI.",
            "",
            "## T1 Preflight",
            "",
            "- **CH tables (read-only):** `eatlq.discovered_pools`, `eatlq.uniswap_v3_swaps_enriched`.",
            f"- **Row caps (defaults):** pools ≤ {max_pool_cand}; swap fetch ≤ {swap_row_cap}; "
            f"CSV output ≤ {max_out}; per-pool ≤ {per_cap}; lookback {window_days}d; min swaps/pool {min_swaps}.",
            "- **Estimated max rows:** bounded by `min(max_output_rows_cap, max_pool_candidates × per_pool_cap)` for sidecar assembly.",
            "- **Read-only:** HTTP `SELECT` only; **no** Ethereum JSON-RPC, Dune, or Redis writes.",
            "",
            "**Note:** Sidecar CSV is **not** written on this blocked run; an older sample file may still exist on disk—do not treat it as refreshed by Sprint028B-R until a successful CH run completes.",
            "",
            "## Decision flags",
            "",
            "```",
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "TRAINING_ALLOWED=false",
            "LABELING_ALLOWED=false",
            "LIVE_ALLOWED=false",
            "PHASE6_ALLOWED=false",
            "H12_ABLATION_ALLOWED=false",
            "ACTIVE_UNIVERSE_MINING_ALLOWED=true  # read-only / feature-only",
            "SCOPED_MICRO_ALPHA_CHARTER_ALLOWED=true",
            "MICRO_LIVE_CALIBRATION_CHARTER_ALLOWED=true",
            "MICRO_LIVE_EXECUTION_ALLOWED=false",
            "SMART_MONEY_GENERAL_LINE_OPEN=false",
            "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC=true",
            "```",
            "",
        ]
        _write_blocked_json_md(
            "BLOCKED_BY_MISSING_CLICKHOUSE_URL",
            "Set `CLICKHOUSE_URL` for read-only discovery or pass `--offline-fixture` for tests only.",
            out_json,
            out_md,
            REPO_ROOT,
            extra={
                "ch_preflight": {
                    "clickhouse_configured": False,
                    "tables_read": [
                        "eatlq.discovered_pools",
                        "eatlq.uniswap_v3_swaps_enriched",
                    ],
                    "row_caps": {
                        "max_pool_candidates": max_pool_cand,
                        "ch_swap_row_cap": swap_row_cap,
                        "per_pool_cap": per_cap,
                        "max_output_rows": max_out,
                        "ch_window_days": window_days,
                        "min_swaps_per_pool": min_swaps,
                    },
                    "read_only_select": True,
                    "ethereum_rpc": False,
                    "dune": False,
                    "redis_writes": False,
                },
                "current_top5_exclusion_count": len(exclude),
            },
            md_sections=ch_block_md,
        )
        return 5

    if args.rescope_grid and args.offline_fixture:
        print("BLOCKED_PRECHECK: --rescope-grid incompatible with --offline-fixture", file=sys.stderr)
        return 7

    grid_json_path = REPO_ROOT / "mvp" / "diagnostics" / "sprint028b_v3_universe_rescope_grid.json"
    rescope_winner_pack: Optional[
        Tuple[
            List[Dict[str, Any]],
            Dict[str, Any],
            Dict[str, Any],
            bool,
            str,
            int,
            int,
            int,
        ]
    ] = None

    if args.rescope_grid:
        t_grid = time.time()
        source_uniq_cache: Dict[int, Dict[str, int]] = {}
        grid_cells: List[Dict[str, Any]] = []
        for lb, ms, mpc in _grid_combo_iter():
            (
                rows_g,
                metrics_g,
                smix_g,
                trunc_g,
                porder_g,
                pmeta_g,
                snote_g,
            ) = _run_ch_pipeline_once(
                exclude,
                ch_url,
                ch_user,
                ch_pw,
                lb,
                ms,
                mpc,
                swap_row_cap,
                per_cap,
                max_out,
            )
            if lb not in source_uniq_cache:
                source_uniq_cache[lb] = _ch_ingest_source_uniq_pools(
                    lb, ch_url, ch_user, ch_pw
                )
            cell = {
                "lookback_days": lb,
                "min_swaps_per_pool": ms,
                "max_pool_candidates": mpc,
                "discovered_pool_count": len(porder_g),
                "concentration_post_cap": metrics_g,
                "swap_fetch_truncated": trunc_g,
                "r2_gates_pass": _r2_gates_ok(metrics_g, trunc_g),
                "fee_bps_histogram": _fee_bps_histogram(pmeta_g),
                "global_swap_source_uniq_pools_by_ingest_source": source_uniq_cache[lb],
            }
            grid_cells.append(cell)
            if rescope_winner_pack is None and cell["r2_gates_pass"]:
                rescope_winner_pack = (
                    rows_g,
                    metrics_g,
                    smix_g,
                    trunc_g,
                    snote_g,
                    lb,
                    ms,
                    mpc,
                )
        grid_wall_s = time.time() - t_grid
        closest_cell = _pick_closest_r2_cell(grid_cells)
        verdict_r2 = "PASS" if rescope_winner_pack else "NEED_RESCOPING_CH_R2"
        selected_summary: Optional[Dict[str, Any]] = None
        if rescope_winner_pack:
            selected_summary = {
                "lookback_days": rescope_winner_pack[5],
                "min_swaps_per_pool": rescope_winner_pack[6],
                "max_pool_candidates": rescope_winner_pack[7],
            }
        else:
            selected_summary = {"closest_cell": closest_cell, "note": "no combo passed R2 gates"}
        _write_rescope_grid_file(grid_json_path, grid_cells, selected_summary, verdict_r2)
        if not rescope_winner_pack:
            fail_summary = {
                "sprint": "028B-R2",
                "universe_gate_verdict": "NEED_RESCOPING_CH_R2",
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rescope_grid_path": str(grid_json_path.relative_to(REPO_ROOT)),
                "closest_cell_params": (
                    {
                        "lookback_days": closest_cell.get("lookback_days"),
                        "min_swaps_per_pool": closest_cell.get("min_swaps_per_pool"),
                        "max_pool_candidates": closest_cell.get("max_pool_candidates"),
                    }
                    if closest_cell
                    else None
                ),
                "decision_flags": DECISION_FLAGS,
                "sprint028c_can_start": False,
            }
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(fail_summary, indent=2), encoding="utf-8")
            fail_md = [
                "# Sprint028B-R2 — rescope grid (no passing cell)",
                "",
                "**Verdict:** `NEED_RESCOPING_CH_R2`",
                "",
                "See `mvp/diagnostics/sprint028b_v3_universe_rescope_grid.json` for all cells. **Feature-only**; **not** alpha; **no** labeling; **no** training; **no** H12; **no** RPC/Dune/Redis writes.",
                "",
                f"*Grid wall time: ~{grid_wall_s:.1f}s*",
                "",
            ]
            out_md.write_text("\n".join(fail_md), encoding="utf-8")
            print("verdict=NEED_RESCOPING_CH_R2 (grid complete, no R2 pass)")
            return 0

    t0 = time.time()
    rows: List[Dict[str, Any]] = []
    source_note = ""
    pool_order: List[str] = []
    pool_meta: Dict[str, Dict[str, Any]] = {}
    swap_truncated = False
    source_mix: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}
    sprint_label = "028B-R2" if (args.rescope_grid and rescope_winner_pack) else "028B-R"

    if rescope_winner_pack:
        (
            rows,
            metrics,
            source_mix,
            swap_truncated,
            source_note,
            window_days,
            min_swaps,
            max_pool_cand,
        ) = (
            rescope_winner_pack[0],
            rescope_winner_pack[1],
            rescope_winner_pack[2],
            rescope_winner_pack[3],
            rescope_winner_pack[4],
            rescope_winner_pack[5],
            rescope_winner_pack[6],
            rescope_winner_pack[7],
        )
    elif args.offline_fixture:
        if not dashcam.is_file():
            print(f"BLOCKED_PRECHECK: dashcam missing: {dashcam}", file=sys.stderr)
            return 2
        ch_rank = _try_ch_v3_pool_rank(exclude, ch_url, ch_user, ch_pw) if ch_url else None
        if ch_rank:
            pool_order = [p for p, _ in ch_rank]
            source_note = "offline_fixture_ch_rank+dashcam_buckets"
        else:
            ctr = _dashcam_v3_counts(dashcam, exclude, max_scan)
            pool_order = [p for p, _ in ctr.most_common(200)]
            source_note = "offline_fixture_dashcam_only"
        if not pool_order:
            print("BLOCKED_PRECHECK: no pools for offline fixture", file=sys.stderr)
            return 3
        rows = _stream_build_sidecar_dashcam(
            dashcam, exclude, pool_order, per_cap, max_out, max_scan
        )
        if ch_url:
            meta = _try_ch_discovered_meta(
                sorted({r["_pool_address_AUDIT_ONLY"] for r in rows}),
                ch_url,
                ch_user,
                ch_pw,
            )
            if meta:
                _merge_discovered_meta(rows, meta)
            else:
                for r in rows:
                    r["_pool_age_meta_status"] = "ch_unreachable"
        else:
            for r in rows:
                r["_pool_age_meta_status"] = "skipped_no_clickhouse_url"
        source_mix = {
            "rows_from_clickhouse_swaps": 0,
            "rows_from_dashcam_overlay": len(rows),
            "universe_discovery": source_note,
        }
        metrics = _apply_cap_metrics(rows)
    else:
        (
            rows,
            metrics,
            source_mix,
            swap_truncated,
            pool_order,
            pool_meta,
            source_note,
        ) = _run_ch_pipeline_once(
            exclude,
            ch_url,
            ch_user,
            ch_pw,
            window_days,
            min_swaps,
            max_pool_cand,
            swap_row_cap,
            per_cap,
            max_out,
        )
        if not pool_order:
            print("BLOCKED_PRECHECK: CH discovery returned no pools", file=sys.stderr)
            return 3

    if rescope_winner_pack:
        verdict = "PASS"
    else:
        verdict = "PASS" if _gates_ok(metrics) else "NEED_RESCOPING_CH"

    wall_s = time.time() - t0
    summary = {
        "sprint": sprint_label,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources_used": {
            "clickhouse_url_set": bool(ch_url),
            "offline_fixture": args.offline_fixture,
            "dashcam_path": (_repo_rel(dashcam) if args.offline_fixture else None),
            "h108_join_sample": _repo_rel(join_sample),
            "excluded_top5_pools": sorted(exclude),
            "universe_rank_source": source_note,
            "rescope_grid": args.rescope_grid,
            "rescope_grid_json": (
                str(grid_json_path.relative_to(REPO_ROOT)) if args.rescope_grid else None
            ),
            "selected_r2_params": (
                {
                    "lookback_days": rescope_winner_pack[5],
                    "min_swaps_per_pool": rescope_winner_pack[6],
                    "max_pool_candidates": rescope_winner_pack[7],
                }
                if rescope_winner_pack
                else None
            ),
        },
        "ch_preflight": {
            "tables_read": [
                "eatlq.discovered_pools",
                "eatlq.uniswap_v3_swaps_enriched",
            ],
            "window_days": window_days,
            "min_swaps_per_pool_gate": min_swaps,
            "max_pool_candidates": max_pool_cand,
            "ch_swap_row_cap": swap_row_cap,
            "read_only_select": True,
            "ethereum_rpc": False,
            "dune": False,
            "redis_writes": False,
        },
        "limits": {
            "max_scan_lines_dashcam": max_scan if args.offline_fixture else None,
            "per_pool_cap": per_cap,
            "max_output_rows_cap": max_out,
        },
        "concentration_post_cap": metrics,
        "per_feature_nonnull_rates": _per_feature_nonnull_rates(rows),
        "source_mix": source_mix,
        "current_top5_exclusion_count": len(exclude),
        "universe_gate_verdict": verdict,
        "sprint028c_can_start": bool(verdict == "PASS"),
        "decision_flags": DECISION_FLAGS,
        "forbidden_claims": [
            "not_alpha",
            "not_labeled",
            "not_training",
            "no_h12",
            "no_rpc_ethereum",
            "no_dune",
            "no_redis_writes",
        ],
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if rows:
        fieldnames = sorted(rows[0].keys())
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    title = (
        "# Sprint028B-R2 — V3/Base feature-only universe mining (rescoped grid)"
        if args.rescope_grid
        else "# Sprint028B-R — V3/Base feature-only universe mining (ClickHouse)"
    )
    lines_md = [
        title,
        "",
        f"**Generated (UTC):** {summary['generated']}",
        "",
        "## Contract",
        "",
        "- **Feature-only** sidecar; **not** labeled; **not** training; **not** alpha; **not** H12.",
        "- **No** Ethereum JSON-RPC, **no** Dune, **no** Redis writes, **no** runtime edits.",
        "- **Read-only** `eatlq.discovered_pools` + `eatlq.uniswap_v3_swaps_enriched` for production runs (`CLICKHOUSE_URL` required).",
        "- `--offline-fixture` is **tests only** (dashcam); not a rescoped production path.",
        "- Identity-only columns use `_*_AUDIT_ONLY`; no `tx_hash` / `log_index` / `block_number` as model features.",
        "",
    ]
    if args.rescope_grid:
        lines_md.extend(
            [
                "## Sprint028B-R2 rescope",
                "",
                f"- Grid artifact: `{grid_json_path.relative_to(REPO_ROOT)}` (bounded param sweep; no large per-cell CSVs).",
                "",
            ]
        )
    lines_md.extend(
        [
        "## T1 Preflight",
        "",
        f"- **CH tables:** `{summary['ch_preflight']['tables_read'][0]}`, `{summary['ch_preflight']['tables_read'][1]}`.",
        f"- **Row caps:** pools ≤ {max_pool_cand}; swap fetch ≤ {swap_row_cap}; output ≤ {max_out}; per-pool ≤ {per_cap}; lookback {window_days}d; min swaps/pool {min_swaps}.",
        f"- **Read-only:** HTTP SELECT only; no RPC/Dune/Redis.",
        f"- **Offline fixture:** `{args.offline_fixture}`.",
        "",
        "## Source mix",
        "",
        f"```json\n{json.dumps(source_mix, indent=2)}\n```",
        "",
        "## Key metrics (post per-pool cap)",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| total_rows | {metrics['total_rows']} |",
        f"| distinct_pools | {metrics['distinct_pools']} |",
        f"| pools_with_>=30_rows | {metrics['pools_with_gte_30_rows']} |",
        f"| top1_share | {metrics['top1_share']} |",
        f"| top5_share | {metrics['top5_share']} |",
        f"| median_rows_per_pool | {metrics['median_rows_per_pool']} |",
        f"| feature_coverage_rate | {metrics['feature_coverage_rate']} |",
        f"| current_top5_exclusion_count | {len(exclude)} |",
        "",
        f"**Universe gate:** `{verdict}`",
        "",
        ],
    )
    if verdict == "NEED_RESCOPING_CH":
        lines_md.extend(
            [
                "### NEED_RESCOPING_CH",
                "",
                "Widen CH time window, lower/raise minimum swap density, stratify fee tiers, or adopt a smaller scoped-universe charter. **Do not** treat this as alpha failure or expand labeling/training/H12.",
                "",
            ]
        )
    lines_md.extend(
        [
            f"**Sprint028C may start:** `{summary['sprint028c_can_start']}`.",
            "",
            "## Decision flags",
            "",
            "```",
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "TRAINING_ALLOWED=false",
            "LABELING_ALLOWED=false",
            "LIVE_ALLOWED=false",
            "PHASE6_ALLOWED=false",
            "H12_ABLATION_ALLOWED=false",
            "ACTIVE_UNIVERSE_MINING_ALLOWED=true  # read-only / feature-only",
            "SCOPED_MICRO_ALPHA_CHARTER_ALLOWED=true",
            "MICRO_LIVE_CALIBRATION_CHARTER_ALLOWED=true",
            "MICRO_LIVE_EXECUTION_ALLOWED=false",
            "SMART_MONEY_GENERAL_LINE_OPEN=false",
            "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC=true",
            "```",
            "",
            f"*Wall time: ~{(wall_s + grid_wall_s) if args.rescope_grid else wall_s:.1f}s*",
            "",
        ]
    )
    out_md.write_text("\n".join(lines_md), encoding="utf-8")

    print(f"sprint028b-r: wrote {out_csv.name}, {out_json.name}, {out_md.name}")
    print(f"verdict={verdict} rows={metrics['total_rows']} pools={metrics['distinct_pools']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
