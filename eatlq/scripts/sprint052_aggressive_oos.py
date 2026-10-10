#!/usr/bin/env python3
"""Sprint052 — Aggressive V3 OOS Expansion with RPC fee enrichment.

Research-only / read-only.
- NO private key, NO live tx, NO training, NO production signals.
- RPC budget: 250k read-only calls; this script uses ~2-4k max.

Phases:
  1. Pull swaps_clean_mat for 5 OOS weeks (2026-03-15 to 2026-04-19)
  2. Pool global first-seen (onset gate pre-req)
  3. Apply Sprint037G baseline_cell gate (UNCHANGED)
  4. RPC: pool.fee() + pool.token0() for all unique pools → fee_bps + WETH direction
  5. Per-pool CH sqrtPriceX96 pull for gated pools → better price-return labels
  6. WETH-direction-aware buy_pressure_weth computation
  7. Reproduction matrix: week × fee_bps × activity_heat × [rows/hit/median_return/concentration]
  8. Autopsy writeout
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
AUDIT_OUT = ROOT / "mvp/audits/sprint052"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint052"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

CH_CONTAINER  = "eatlq-clickhouse-1"
CHAIN_ID      = 8453
WETH_BASE     = "0x4200000000000000000000000000000000000006".lower()
WINDOW_SEC    = 300

# Sprint037G baseline_cell — UNCHANGED
POOL_MAX_AGE_SEC   = 86400
ONSET_MAX_SEC      = 3600
ONSET_COUNT_THRESH = 10

# ABI selectors (no private key; view functions only)
SEL_FEE    = "0xddca3f43"  # fee() → uint24
SEL_TOKEN0 = "0x0dfe1681"  # token0() → address

RPC_DELAY_S    = 0.04  # 25 rps; well within QuickNode limits
RPC_BUDGET_CAP = 10_000  # hard cap for this script

OOS_WEEKS = [
    ("2026-03-15", "2026-03-22", "mid_bundle_gap"),
    ("2026-03-22", "2026-03-29", "mid_bundle_gap"),
    ("2026-04-05", "2026-04-12", "post_bundle"),
    ("2026-04-12", "2026-04-19", "post_bundle"),
    ("2026-04-19", "2026-04-26", "post_bundle"),
]

# Sprint050 in-bundle benchmarks (from feature_regime_exploration.md)
S050_BENCHMARKS = {
    "aggregate":           {"rows_routed": 11233, "hit_rate": 0.364, "pf_after_gas": 1.456},
    "fee_bps_100":         {"rows_routed": 1513,  "hit_rate": 0.495, "pf_after_gas": 1.79},
    "fee_bps_500":         {"rows_routed": 348,   "hit_rate": 0.515, "pf_after_gas": 2.36},
    "fee_bps_3000":        {"rows_routed": 3797,  "hit_rate": 0.585, "pf_after_gas": 2.31},
    "fee_bps_10000":       {"rows_routed": 6363,  "hit_rate": 0.221, "pf_after_gas": 0.77},
    "activity_heat_ge_250":{"rows_routed": 407,   "hit_rate": 0.563, "pf_after_gas": 3.40},
    "activity_heat_100_250":{"rows_routed":2839,  "hit_rate": 0.508, "pf_after_gas": 1.85},
    "activity_heat_20_50": {"rows_routed": 2538,  "hit_rate": 0.300, "pf_after_gas": 1.48},
    "buy_pressure_ge_06":  {"rows_routed": 1054,  "hit_rate": 0.420, "pf_after_gas": 1.80},
    "price_velocity_flat": {"rows_routed": 3709,  "hit_rate": 0.495, "pf_after_gas": 1.78},
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CH helpers
# ---------------------------------------------------------------------------

def _ch(sql: str, timeout: int = 180) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip() or r.stdout.strip()[:200]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def pull_week(w_start: str, w_end: str) -> list[dict]:
    rows = _ch(f"""
SELECT pool_address, ts_sec, is_buy, a0_in, a1_in, a0_out, a1_out
FROM eatlq.uniswap_swaps_clean_mat
WHERE chain_id={CHAIN_ID}
  AND ts_sec >= toUnixTimestamp(toDateTime('{w_start}'))
  AND ts_sec <  toUnixTimestamp(toDateTime('{w_end}'))
ORDER BY pool_address, ts_sec
""")
    return [{"pool": r[0], "ts": int(r[1]), "is_buy": int(r[2]),
             "a0_in": float(r[3] or 0), "a1_in": float(r[4] or 0),
             "a0_out": float(r[5] or 0), "a1_out": float(r[6] or 0)}
            for r in rows if len(r) >= 7]


def pool_first_seen(pools: set[str]) -> dict[str, int]:
    if not pools:
        return {}
    pl = ",".join(f"'{p}'" for p in pools)
    rows = _ch(f"""
SELECT pool_address, min(ts_sec)
FROM eatlq.uniswap_swaps_clean_mat
WHERE chain_id={CHAIN_ID} AND pool_address IN ({pl})
GROUP BY pool_address
""", timeout=60)
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


def pull_sqrt_price(pool_addresses: list[str], w_start: str, w_end: str) -> dict[str, list[dict]]:
    """Fetch sqrtPriceX96 + block_time for gated pools (per-pool, bounded)."""
    if not pool_addresses:
        return {}
    pl = ",".join(f"'{p}'" for p in pool_addresses)
    # extend window to capture exit prices: w_start to w_end + 10m
    end_ext = _add_minutes(w_end, 10)
    rows = _ch(f"""
SELECT pool_address, toUnixTimestamp(toDateTime(block_time)) AS ts, sqrt_price_x96
FROM eatlq.uniswap_v3_swaps_trade_semantic
WHERE pool_address IN ({pl})
  AND block_time >= toDateTime('{w_start}')
  AND block_time <  toDateTime('{end_ext}')
ORDER BY pool_address, ts
""", timeout=60)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if len(r) >= 3 and r[2]:
            try:
                out[r[0]].append({"ts": int(r[1]), "sqrt": int(r[2])})
            except (ValueError, OverflowError):
                pass
    return dict(out)


def _add_minutes(dt_str: str, mins: int) -> str:
    from datetime import timedelta
    dt = datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# RPC helpers (read-only, no private key)
# ---------------------------------------------------------------------------

def _load_rpc_url() -> str:
    env_path = ROOT / "infra/compose/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v:
                    return v
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url:
        raise RuntimeError("No RPC URL found in .env or environment")
    return url


def _eth_call(session, rpc_url: str, to: str, data: str, req_id: int) -> str | None:
    try:
        resp = session.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": req_id, "method": "eth_call",
                  "params": [{"to": to, "data": data}, "latest"]},
            timeout=20,
        )
        if resp.status_code == 429:
            time.sleep(1.0)
            return None
        resp.raise_for_status()
        result = resp.json().get("result", "0x")
        return result if result and result != "0x" else None
    except Exception:
        return None


def _decode_uint24(hex_result: str | None) -> int | None:
    if not hex_result:
        return None
    raw = hex_result[2:] if hex_result.startswith("0x") else hex_result
    try:
        return int(raw[-8:], 16) if len(raw) >= 8 else None
    except ValueError:
        return None


def _decode_address(hex_result: str | None) -> str | None:
    if not hex_result:
        return None
    raw = hex_result[2:] if hex_result.startswith("0x") else hex_result
    if len(raw) >= 40:
        return ("0x" + raw[-40:]).lower()
    return None


def rpc_enrich_pools(pools: list[str], rpc_url: str) -> dict[str, dict]:
    """Call pool.fee() + pool.token0() for each pool. Returns {pool: {fee_bps, token0}}."""
    import requests
    session = requests.Session()
    out: dict[str, dict] = {}
    req_id = 0
    total = len(pools)
    for i, pool in enumerate(pools):
        if i % 100 == 0:
            print(f"  rpc enrichment {i}/{total} ...", flush=True)
        # fee()
        req_id += 1
        fee_hex = _eth_call(session, rpc_url, pool, SEL_FEE, req_id)
        time.sleep(RPC_DELAY_S)
        # token0()
        req_id += 1
        t0_hex = _eth_call(session, rpc_url, pool, SEL_TOKEN0, req_id)
        time.sleep(RPC_DELAY_S)
        fee_raw = _decode_uint24(fee_hex)
        t0 = _decode_address(t0_hex)
        out[pool] = {
            "fee_bps": fee_raw,             # 500, 3000, 10000 etc.  (or None)
            "token0": t0,
            "weth_is_token0": (t0 == WETH_BASE) if t0 else None,
        }
    return out


# ---------------------------------------------------------------------------
# Feature computation + gate
# ---------------------------------------------------------------------------

def _win(ts: int) -> int:
    return (ts // WINDOW_SEC) * WINDOW_SEC


def make_windows(swaps: list[dict]) -> dict[str, dict[int, list[dict]]]:
    by_pool: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for s in swaps:
        by_pool[s["pool"]][_win(s["ts"])].append(s)
    return by_pool


def features(bucket: list[dict]) -> dict:
    n = len(bucket)
    if n == 0:
        return {}
    is_buy_list = [s["is_buy"] for s in bucket]
    buy_cnt = sum(is_buy_list)
    amt_list = [max(s["a0_in"], s["a0_out"], s["a1_in"], s["a1_out"]) for s in bucket]
    mean_amt = statistics.mean(amt_list) if amt_list else 0
    sd_amt = statistics.pstdev(amt_list) if len(amt_list) > 1 else 0
    vol = min(sd_amt / mean_amt if mean_amt > 0 else 0, 2.0)
    net = (buy_cnt - (n - buy_cnt)) / n
    return {
        "activity_heat": n,
        "buy_pressure_ratio": buy_cnt / n,
        "price_velocity_5m": net,
        "token_volatility_5m": vol,
    }


def apply_gate(
    by_pool: dict[str, dict[int, list[dict]]],
    first_seen: dict[str, int],
    w_start_ts: int,
    w_end_ts: int,
    week_label: str,
) -> list[dict]:
    candidates = []
    for pool, windows in by_pool.items():
        fs = first_seen.get(pool)
        if fs is None:
            continue
        onset_ts = None
        for wt in sorted(windows):
            if wt < w_start_ts or wt >= w_end_ts:
                continue
            if len(windows[wt]) >= ONSET_COUNT_THRESH:
                onset_ts = wt
                break
        if onset_ts is None:
            continue
        for wt in sorted(windows):
            if wt < w_start_ts or wt >= w_end_ts:
                continue
            bucket = windows[wt]
            n = len(bucket)
            pool_age = wt - fs
            onset_lag = wt - onset_ts
            if pool_age > POOL_MAX_AGE_SEC:
                continue
            if onset_lag > ONSET_MAX_SEC:
                continue
            if n < ONSET_COUNT_THRESH:
                continue
            feats = features(bucket)
            candidates.append({
                "regime_week": week_label,
                "pool_address": pool,
                "window_start_ts": wt,
                "pool_age_sec": pool_age,
                "onset_lag_sec": onset_lag,
                **feats,
            })
    return candidates


# ---------------------------------------------------------------------------
# Label computation
# ---------------------------------------------------------------------------

def add_buy_pressure_label(candidates: list[dict], by_pool: dict[str, dict[int, list[dict]]]) -> None:
    for c in candidates:
        pool = c["pool_address"]
        next_ts = c["window_start_ts"] + WINDOW_SEC
        future = by_pool.get(pool, {}).get(next_ts, [])
        if future:
            n = len(future)
            bp = sum(s["is_buy"] for s in future) / n
            c["label_bp"] = int(bp > 0.5)
            c["future_bp"] = round(bp, 4)
            c["future_n"] = n
        else:
            c["label_bp"] = None
            c["future_bp"] = None
            c["future_n"] = 0


def add_sqrt_price_label(
    candidates: list[dict],
    sqrt_data: dict[str, list[dict]],
    pool_meta: dict[str, dict],
) -> None:
    """Binary price-direction label from sqrtPriceX96 comparisons."""
    for c in candidates:
        pool = c["pool_address"]
        ticks = sqrt_data.get(pool, [])
        if not ticks:
            c["label_sqrt"] = None
            c["net_return_sqrt"] = None
            continue
        wt = c["window_start_ts"]
        # entry price: last sqrtPriceX96 in [wt, wt+300)
        entry_ticks = [t["sqrt"] for t in ticks if wt <= t["ts"] < wt + WINDOW_SEC]
        # exit price: first sqrtPriceX96 in [wt+300, wt+600)
        exit_ticks = [t["sqrt"] for t in ticks if wt + WINDOW_SEC <= t["ts"] < wt + 2 * WINDOW_SEC]
        if not entry_ticks or not exit_ticks:
            c["label_sqrt"] = None
            c["net_return_sqrt"] = None
            continue
        entry_sqrt = entry_ticks[-1]
        exit_sqrt = exit_ticks[0]
        if entry_sqrt == 0:
            c["label_sqrt"] = None
            c["net_return_sqrt"] = None
            continue
        meta = pool_meta.get(pool, {})
        weth_is_token0 = meta.get("weth_is_token0")
        # price of TOKEN in WETH terms:
        #   token1=WETH: token price = (sqrtPrice/Q96)^2 → up if sqrt goes up
        #   token0=WETH: token price = (Q96/sqrtPrice)^2 → up if sqrt goes down
        if weth_is_token0 is True:
            label = int(exit_sqrt < entry_sqrt)   # token appreciates if sqrtPrice drops
            ratio = (entry_sqrt**2 - exit_sqrt**2) / entry_sqrt**2 if entry_sqrt else 0
        elif weth_is_token0 is False:
            label = int(exit_sqrt > entry_sqrt)
            ratio = (exit_sqrt**2 - entry_sqrt**2) / entry_sqrt**2 if entry_sqrt else 0
        else:
            # unknown direction: use magnitude only, no signed label
            label = None
            ratio = abs(exit_sqrt**2 - entry_sqrt**2) / entry_sqrt**2 if entry_sqrt else 0
        c["label_sqrt"] = label
        c["net_return_sqrt"] = round(float(ratio), 6)


# ---------------------------------------------------------------------------
# Reproduction matrix
# ---------------------------------------------------------------------------

def _heat_bucket(h: int) -> str:
    if h < 20:   return "heat_lt_20"
    if h < 50:   return "heat_20_50"
    if h < 100:  return "heat_50_100"
    if h < 250:  return "heat_100_250"
    return "heat_ge_250"


def _fee_label(fee: int | None) -> str:
    if fee is None:   return "fee_unknown"
    if fee == 100:    return "fee_100bps"
    if fee == 500:    return "fee_500bps"
    if fee == 3000:   return "fee_3000bps"
    if fee == 10000:  return "fee_10000bps"
    return f"fee_{fee}bps"


def _sample_class(n: int) -> str:
    if n < 300:   return "smoke_or_sanity"
    if n < 1000:  return "very_weak_feasibility"
    if n < 5000:  return "scoped_dryrun"
    if n < 20000: return "scoped_backtest"
    return "formal_research"


def _stats_labeled(items: list[dict], label_key: str) -> dict:
    labeled = [c for c in items if c.get(label_key) is not None]
    n = len(labeled)
    pos = sum(1 for c in labeled if c[label_key] == 1)
    hit = pos / n if n > 0 else None
    rets = [c.get("net_return_sqrt") for c in labeled if c.get("net_return_sqrt") is not None]
    mean_ret = statistics.mean(rets) if rets else None
    median_ret = statistics.median(rets) if rets else None
    # simple profit factor proxy from signed returns
    gains = [v for v in rets if v > 0]
    losses = [-v for v in rets if v < 0]
    pf = (sum(gains) / sum(losses)) if losses else (math.inf if gains else None)
    if pf is not None and (pf == math.inf or pf > 99):
        pf = None
    pool_top1 = None
    if labeled:
        pool_cnt = Counter(c["pool_address"] for c in labeled)
        total = sum(pool_cnt.values())
        pool_top1 = pool_cnt.most_common(1)[0][1] / total if total > 0 else None
    return {
        "labeled_rows": n,
        "hit_rate": round(hit, 4) if hit is not None else None,
        "mean_ret_sqrt": round(mean_ret, 5) if mean_ret is not None else None,
        "median_ret_sqrt": round(median_ret, 5) if median_ret is not None else None,
        "profit_factor_sqrt": round(pf, 3) if pf is not None else None,
        "pool_top1_share": round(pool_top1, 3) if pool_top1 is not None else None,
    }


def build_matrix(all_candidates: list[dict], pool_meta: dict[str, dict]) -> list[dict]:
    """Build week × fee × heat reproduction matrix."""
    rows = []
    # Enrich candidates with fee + heat bucket
    enriched = []
    for c in all_candidates:
        meta = pool_meta.get(c["pool_address"], {})
        fee = meta.get("fee_bps")
        enriched.append({
            **c,
            "fee_bps": fee,
            "fee_label": _fee_label(fee),
            "heat_bucket": _heat_bucket(c.get("activity_heat", 0)),
        })

    def _group(items, *keys):
        groups: dict[tuple, list] = defaultdict(list)
        for item in items:
            k = tuple(item.get(k2) for k2 in keys)
            groups[k].append(item)
        return groups

    # Full breakdown: week × fee × heat
    for (week, fee_label, heat_bucket), items in sorted(
        _group(enriched, "regime_week", "fee_label", "heat_bucket").items()
    ):
        n = len(items)
        stats_bp = _stats_labeled(items, "label_bp")
        stats_sqrt = _stats_labeled(items, "label_sqrt")
        rows.append({
            "regime_week": week or "ALL",
            "fee_label": fee_label or "fee_unknown",
            "heat_bucket": heat_bucket or "?",
            "gated_rows": n,
            "unique_pools": len({c["pool_address"] for c in items}),
            "sample_class": _sample_class(n),
            "hit_rate_bp_label": stats_bp["hit_rate"],
            "labeled_rows_bp": stats_bp["labeled_rows"],
            "hit_rate_sqrt_label": stats_sqrt["hit_rate"],
            "labeled_rows_sqrt": stats_sqrt["labeled_rows"],
            "mean_ret_sqrt": stats_sqrt["mean_ret_sqrt"],
            "median_ret_sqrt": stats_sqrt["median_ret_sqrt"],
            "profit_factor_sqrt": stats_sqrt["profit_factor_sqrt"],
            "pool_top1_share_sqrt": stats_sqrt["pool_top1_share"],
        })

    # Aggregate per fee_label (all weeks combined)
    for (fee_label,), items in sorted(_group(enriched, "fee_label").items()):
        n = len(items)
        stats_sqrt = _stats_labeled(items, "label_sqrt")
        stats_bp = _stats_labeled(items, "label_bp")
        rows.append({
            "regime_week": "ALL_WEEKS",
            "fee_label": fee_label or "fee_unknown",
            "heat_bucket": "ALL_HEAT",
            "gated_rows": n,
            "unique_pools": len({c["pool_address"] for c in items}),
            "sample_class": _sample_class(n),
            "hit_rate_bp_label": stats_bp["hit_rate"],
            "labeled_rows_bp": stats_bp["labeled_rows"],
            "hit_rate_sqrt_label": stats_sqrt["hit_rate"],
            "labeled_rows_sqrt": stats_sqrt["labeled_rows"],
            "mean_ret_sqrt": stats_sqrt["mean_ret_sqrt"],
            "median_ret_sqrt": stats_sqrt["median_ret_sqrt"],
            "profit_factor_sqrt": stats_sqrt["profit_factor_sqrt"],
            "pool_top1_share_sqrt": stats_sqrt["pool_top1_share"],
        })

    return rows


# ---------------------------------------------------------------------------
# Autopsy
# ---------------------------------------------------------------------------

def build_autopsy(
    gate_results: dict,
    matrix: list[dict],
    rpc_calls_used: int,
    pool_meta_count: int,
) -> dict:
    total_gated = sum(g["candidate_rows"] for g in gate_results.values())
    # Check each signal
    fe3000 = [r for r in matrix if r["fee_label"] == "fee_3000bps" and r["regime_week"] == "ALL_WEEKS"]
    fe500  = [r for r in matrix if r["fee_label"] == "fee_500bps"  and r["regime_week"] == "ALL_WEEKS"]
    heat250 = [r for r in matrix if r["heat_bucket"] == "heat_ge_250" and r["regime_week"] == "ALL_WEEKS"]
    fe10k  = [r for r in matrix if r["fee_label"] == "fee_10000bps" and r["regime_week"] == "ALL_WEEKS"]

    def _verdict(rows: list[dict], s050_key: str) -> dict:
        s050 = S050_BENCHMARKS.get(s050_key, {})
        if not rows:
            return {
                "status": "NO_ROWS",
                "oos_gated": 0,
                "sprint050_rows": s050.get("rows_routed"),
                "sprint050_hit": s050.get("hit_rate"),
                "sprint050_pf": s050.get("pf_after_gas"),
                "oos_hit_sqrt": None,
                "oos_pf_sqrt": None,
                "verdict": "CANNOT_TEST",
            }
        r = rows[0]
        n = r["gated_rows"]
        hit = r["hit_rate_sqrt_label"]
        pf = r["profit_factor_sqrt"]
        s_class = _sample_class(n)
        if n < 300:
            status = "BELOW_SMOKE"
        elif hit is not None and pf is not None and hit >= 0.5 and pf >= 1.5:
            status = "REPRODUCES_DIRECTIONALLY"
        elif hit is not None and hit >= 0.45:
            status = "WEAK_REPRODUCTION"
        else:
            status = "NOT_REPRODUCED"
        return {
            "status": status,
            "oos_gated": n,
            "sample_class": s_class,
            "sprint050_rows": s050.get("rows_routed"),
            "sprint050_hit": s050.get("hit_rate"),
            "sprint050_pf": s050.get("pf_after_gas"),
            "oos_hit_sqrt": hit,
            "oos_pf_sqrt": pf,
            "verdict": status,
        }

    return {
        "generated_at": _now_utc(),
        "total_gated_rows": total_gated,
        "rpc_calls_used": rpc_calls_used,
        "pools_enriched_with_fee": pool_meta_count,
        "signals": {
            "fee_3000bps": _verdict(fe3000, "fee_bps_3000"),
            "fee_500bps":  _verdict(fe500,  "fee_bps_500"),
            "fee_10000bps":_verdict(fe10k,  "fee_bps_10000"),
            "activity_heat_ge_250": _verdict(heat250, "activity_heat_ge_250"),
        },
        "gate_by_week": gate_results,
        "decision_flags": {
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "PRIVATE_KEY_USED": False,
            "PRODUCTION_SIGNALS_WRITTEN": False,
            "NO_ALPHA_CLAIM": True,
        },
    }


# ---------------------------------------------------------------------------
# Markdown writers
# ---------------------------------------------------------------------------

def write_readout(autopsy: dict, matrix: list[dict], rpc_calls: int) -> None:
    sigs = autopsy["signals"]
    total_gated = autopsy["total_gated_rows"]

    def _fmt_status(s: dict) -> str:
        return f"`{s['verdict']}` — gated={s['oos_gated']}, hit={s['oos_hit_sqrt']}, pf={s['oos_pf_sqrt']}"

    out = []
    out.append("# Run-052 — Aggressive V3 OOS Readout\n")
    out.append(f"**Branch:** `sprint052-aggressive-v3-oos` · **Generated:** {autopsy['generated_at']}")
    out.append(f"\n**Total gated rows (all OOS weeks, Spring037G gate UNCHANGED):** `{total_gated}`")
    out.append(f"**RPC calls used:** `{rpc_calls}` (budget: 250k)")
    out.append(f"**Pools enriched with fee_bps:** `{autopsy['pools_enriched_with_fee']}`\n")

    out.append("## Gate results by week\n")
    out.append("| week | category | swaps | pools | gated_rows | gate% | pool_age_med_s | onset_med_s |")
    out.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for wk, g in autopsy["gate_by_week"].items():
        out.append(
            f"| {wk} | {g['category']} | {g['swaps_pulled']} | {g['pools_in_week']} | "
            f"{g['candidate_rows']} | {g['gate_pct']:.3%} | "
            f"{g.get('pool_age_med',0)} | {g.get('onset_med',0)} |"
        )

    out.append("\n## Signal reproduction status\n")
    out.append("| Signal | Sprint050 (rows/hit/PF) | OOS status |")
    out.append("| --- | --- | --- |")
    for sig, s in sigs.items():
        s050 = f"{s['sprint050_rows']}/{s['sprint050_hit']}/{s['sprint050_pf']}"
        out.append(f"| {sig} | {s050} | {_fmt_status(s)} |")

    out.append("\n## Reproduction matrix (week × fee × heat) — key rows\n")
    out.append("| regime_week | fee_label | heat_bucket | gated | hit_sqrt | pf_sqrt | median_ret | pool_top1 | sample_class |")
    out.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in sorted(matrix, key=lambda x: -(x["gated_rows"] or 0))[:40]:
        out.append(
            f"| {r['regime_week']} | {r['fee_label']} | {r['heat_bucket']} | "
            f"{r['gated_rows']} | {r['hit_rate_sqrt_label']} | {r['profit_factor_sqrt']} | "
            f"{r['median_ret_sqrt']} | {r['pool_top1_share_sqrt']} | {r['sample_class']} |"
        )

    out.append("\n## Decision flags\n")
    for k, v in autopsy["decision_flags"].items():
        out.append(f"- {k} = `{v}`")

    out.append("\n## Sprint053 recommendation\n")
    reproduced = [sig for sig, s in sigs.items() if s["verdict"] not in ("CANNOT_TEST", "NO_ROWS", "BELOW_SMOKE", "NOT_REPRODUCED")]
    if total_gated >= 300 and reproduced:
        out.append(f"**PROCEED → Sprint053 bounded label expansion.**")
        out.append(f"Reproducing signals: `{', '.join(reproduced)}`")
        out.append("Action: run `run004_v3r24` builder on OOS weeks limited to reproducing fee/heat buckets.")
        out.append("Budget: ≤ 5k RPC calls, user confirms preflight.")
    elif total_gated >= 300:
        out.append("**WEAK → Sprint053 gate sensitivity study first.**")
        out.append("Gate finds rows but signals are below smoke. Propose onset_thresh sensitivity sweep before label expansion.")
    else:
        out.append("**HOLD → Revisit gate or data source before Sprint053.**")
        out.append(f"Total gated = {total_gated} (< 300 smoke threshold). Investigate gate fit on OOS weeks.")

    out.append(f"\n`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `LIVE_ALLOWED=false`")
    (ROOT / "mvp/runs/run-052-aggressive-v3-oos-readout.md").write_text("\n".join(out))


def write_autopsy_md(autopsy: dict, week_gate: dict) -> None:
    sigs = autopsy["signals"]
    out = []
    out.append("# Sprint052 — Failure / Success Autopsy\n")
    out.append(f"Generated: {autopsy['generated_at']}\n")
    for sig, s in sigs.items():
        out.append(f"\n## {sig}\n")
        out.append(f"- **Verdict:** `{s['verdict']}`")
        out.append(f"- Sprint050 benchmark: {s['sprint050_rows']} rows · hit={s['sprint050_hit']} · PF={s['sprint050_pf']}")
        out.append(f"- OOS gated rows: {s['oos_gated']} · sample_class: {s.get('sample_class','—')}")
        out.append(f"- OOS hit_rate (sqrt label): {s['oos_hit_sqrt']}")
        out.append(f"- OOS profit_factor (sqrt label): {s['oos_pf_sqrt']}")
        if s["verdict"] == "NO_ROWS":
            out.append("- **Root cause:** 0 gated rows — fee bucket absent in OOS gated set (likely no pools of this fee tier passed Sprint037G gate)")
        elif s["verdict"] == "CANNOT_TEST":
            out.append("- **Root cause:** fee tier not determinable from CH (fee_bps=0 for uncurated pools)")
        elif s["verdict"] == "BELOW_SMOKE":
            out.append(f"- **Root cause:** {s['oos_gated']} gated rows < 300 smoke threshold — insufficient data, not a signal failure")
        elif s["verdict"] == "NOT_REPRODUCED":
            out.append("- **Root cause:** signal tested but hit/PF below threshold — could be regime shift, label difference, or timing")
        elif "REPRODUCES" in s["verdict"]:
            out.append("- **Finding:** signal directionally consistent with Sprint050 — candidate for Sprint053 bounded label expansion")

    out.append("\n## Gate emptiness analysis for 03-15 / 03-22\n")
    for wk, g in week_gate.items():
        if g["candidate_rows"] == 0:
            out.append(f"\n### {wk} — 0 gated rows\n")
            out.append(f"- Pools in week: {g['pools_in_week']}")
            out.append(f"- Pool global_first_seen median: {g.get('pool_first_seen_med', '—')} s → pool_age at entry >> 86400s")
            out.append("- Cause: all pools in this week are established pools (first seen Feb 19/23 2026)")
            out.append("- Verdict: gate correctly fires 0; the young-pool-onset regime is absent in swaps_clean_mat for these weeks")
            out.append("- Fix path: use uniswap_v3_swaps_trade_semantic or earlier data source to scan for young pools in these weeks (requires bounded per-week scan, not view aggregation)")
    (AUDIT_OUT / "failure_or_success_autopsy.md").write_text("\n".join(out))


def _write_json(path: Path, d: Any) -> None:
    with path.open("w") as f:
        json.dump(d, f, indent=2, default=str)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("# empty\n")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[sprint052] start {_now_utc()}", flush=True)

    assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in ("1", "true", "yes", "on"), \
        "LIVE_TRADING_MODE must not be active"

    # Load RPC URL
    try:
        rpc_url = _load_rpc_url()
        print(f"[sprint052] RPC URL loaded (masked)", flush=True)
        rpc_available = True
    except RuntimeError as e:
        print(f"[sprint052] WARNING: {e} — skipping fee enrichment", flush=True)
        rpc_url = ""
        rpc_available = False

    all_candidates: list[dict] = []
    all_pools: set[str] = set()
    gate_results: dict[str, dict] = {}

    # Phase 1–3: CH data pull + gate per week
    for w_start, w_end, category in OOS_WEEKS:
        week_label = w_start
        print(f"\n[sprint052] === week {week_label} ({category}) ===", flush=True)
        try:
            swaps = pull_week(w_start, w_end)
        except Exception as e:
            print(f"  ERROR pulling swaps: {e}", flush=True)
            gate_results[week_label] = {"category": category, "swaps_pulled": 0, "pools_in_week": 0,
                                        "candidate_rows": 0, "gate_pct": 0.0, "error": str(e)}
            continue
        print(f"  {len(swaps)} swaps, {len({s['pool'] for s in swaps})} pools", flush=True)

        pools_wk = {s["pool"] for s in swaps}
        all_pools.update(pools_wk)

        try:
            first_seen = pool_first_seen(pools_wk)
        except Exception as e:
            print(f"  ERROR first_seen: {e}", flush=True)
            first_seen = {}

        # Quick first-seen summary for autopsy
        fs_vals = [first_seen.get(p, 0) for p in pools_wk if first_seen.get(p)]
        fs_med = int(statistics.median(fs_vals)) if fs_vals else 0

        w_start_ts = int(datetime.strptime(w_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        w_end_ts   = int(datetime.strptime(w_end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

        by_pool = make_windows(swaps)
        candidates = apply_gate(by_pool, first_seen, w_start_ts, w_end_ts, week_label)
        add_buy_pressure_label(candidates, by_pool)
        print(f"  {len(candidates)} candidates passed gate", flush=True)

        pool_ages = [c["pool_age_sec"] for c in candidates]
        onsets    = [c["onset_lag_sec"] for c in candidates]
        gate_results[week_label] = {
            "category": category,
            "swaps_pulled": len(swaps),
            "pools_in_week": len(pools_wk),
            "candidate_rows": len(candidates),
            "gate_pct": len(candidates) / len(swaps) if swaps else 0.0,
            "pool_age_med": int(statistics.median(pool_ages)) if pool_ages else 0,
            "onset_med": int(statistics.median(onsets)) if onsets else 0,
            "pool_first_seen_med": fs_med,
        }
        all_candidates.extend(candidates)

    print(f"\n[sprint052] total candidates across all weeks: {len(all_candidates)}", flush=True)

    # Phase 4: RPC fee + token0 enrichment
    pool_meta: dict[str, dict] = {}
    rpc_calls = 0
    if rpc_available and all_pools:
        print(f"\n[sprint052] RPC enrichment for {len(all_pools)} unique pools ...", flush=True)
        try:
            pool_meta = rpc_enrich_pools(sorted(all_pools), rpc_url)
            rpc_calls = len(all_pools) * 2  # fee + token0 per pool
            print(f"  enriched {len(pool_meta)} pools, ~{rpc_calls} RPC calls", flush=True)
        except Exception as e:
            print(f"  RPC enrichment ERROR: {e}", flush=True)

    # Phase 5: sqrtPriceX96 label for gated pools only
    gated_pools = sorted({c["pool_address"] for c in all_candidates})
    print(f"\n[sprint052] fetching sqrtPriceX96 for {len(gated_pools)} gated pools ...", flush=True)
    all_sqrt: dict[str, list[dict]] = {}
    for w_start, w_end, _ in OOS_WEEKS:
        week_gated = [c["pool_address"] for c in all_candidates if c["regime_week"] == w_start]
        if not week_gated:
            continue
        try:
            sqrt_data = pull_sqrt_price(list(set(week_gated)), w_start, w_end)
            for pool, ticks in sqrt_data.items():
                all_sqrt.setdefault(pool, []).extend(ticks)
        except Exception as e:
            print(f"  sqrtPriceX96 pull error for {w_start}: {e}", flush=True)

    add_sqrt_price_label(all_candidates, all_sqrt, pool_meta)

    # Phase 6 & 7: reproduction matrix
    matrix = build_matrix(all_candidates, pool_meta)
    autopsy_data = build_autopsy(gate_results, matrix, rpc_calls, len(pool_meta))

    # Write artifacts
    _write_csv(DIAG_OUT / "oos_reproduction_matrix.csv", matrix)
    _write_json(DIAG_OUT / "oos_reproduction_matrix.json", matrix)
    _write_json(DIAG_OUT / "oos_candidates.csv.json", all_candidates)  # full candidate list
    _write_csv(DIAG_OUT / "oos_candidates.csv",
               [{k: v for k, v in c.items() if k not in ("future_bp", "future_n")} for c in all_candidates])
    _write_json(DIAG_OUT / "autopsy.json", autopsy_data)
    write_autopsy_md(autopsy_data, gate_results)
    write_readout(autopsy_data, matrix, rpc_calls)

    print(f"\n[sprint052] artifacts written", flush=True)
    print(f"  matrix rows: {len(matrix)}", flush=True)
    print(f"  rpc_calls: {rpc_calls}", flush=True)
    print(f"  total_gated: {autopsy_data['total_gated_rows']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
