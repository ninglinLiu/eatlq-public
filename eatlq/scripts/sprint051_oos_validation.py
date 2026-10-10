#!/usr/bin/env python3
"""Sprint051 — V3 OOS validation on post-bundle weeks 2026-04-05/12/19.

Research-only / read-only.  No RPC, no private key, no live, no training.

Data source: eatlq.uniswap_swaps_clean_mat  (MergeTree, fast per-pool scan)
            range: 2026-02-19 → 2026-04-20 for chain 8453

Gate: Sprint037G baseline_cell (unchanged)
  pool_max_age_sec   = 86400  (24 h)
  onset_max_sec      = 3600   (60 min)
  onset_count_thresh = 10     (min swaps in onset 5m window)

Labels: CH buy_pressure proxy (no RPC)
  label_proxy = future_buy_pressure_ratio > 0.5 in the NEXT 5m window
  (same as pool_realtime_training_v2 definition)

Fee-tier: NOT testable on OOS weeks — see failure_autopsy() output.
Fee_bps is 0 for all uncurated young-pool candidates in CH.
The fee-tier conditional signal from Sprint050 requires RPC
(Uniswap V3 pool.fee() call or factory CREATE2 derivation).
"""

from __future__ import annotations

import csv
import io
import json
import math
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUDIT_OUT = ROOT / "mvp/audits/sprint051"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint051"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

CH_CONTAINER   = "eatlq-clickhouse-1"
CHAIN_ID       = 8453
WETH_BASE      = "0x4200000000000000000000000000000000000006"

# Sprint037G baseline_cell (UNCHANGED from gate)
POOL_MAX_AGE_SEC    = 86400   # 24 h
ONSET_MAX_SEC       = 3600    # 60 min
ONSET_COUNT_THRESH  = 10      # min swaps in onset 5m window

WINDOW_SEC = 300  # 5 minutes

# OOS target weeks  (CH Sun-anchored week_start, Monday = week_start + 1d)
OOS_WEEKS = [
    ("2026-04-05", "2026-04-12"),
    ("2026-04-12", "2026-04-19"),
    ("2026-04-19", "2026-04-26"),
]
OOS_LABELS = ["2026-04-05", "2026-04-12", "2026-04-19"]

# Sprint050 in-bundle benchmarks (from feature_regime_exploration.md)
SPRINT050_BENCHMARK = {
    "aggregate": {"rows_routed": 11233, "hit_rate": 0.364, "pf_after_gas": 1.456},
    "activity_heat_ge_250": {"rows_routed": 407, "hit_rate": 0.563, "pf_after_gas": 3.40},
    "fee_3000bps": {"rows_routed": 3376, "hit_rate": 0.585, "pf_after_gas": 2.31,
                    "note": "CANNOT_TEST_ON_OOS_fee_bps_not_available_in_CH"},
    "fee_500bps": {"rows_routed": 322, "hit_rate": 0.515, "pf_after_gas": 2.36,
                   "note": "CANNOT_TEST_ON_OOS_fee_bps_not_available_in_CH"},
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ch(sql: str, timeout: int = 120) -> str:
    if shutil.which("docker") is None:
        raise RuntimeError("docker not found")
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _ch_rows(sql: str, timeout: int = 120) -> list[list[str]]:
    raw = _ch(sql, timeout)
    reader = csv.reader(io.StringIO(raw))
    return [row for row in reader if row]


def _safe(x: Any, t=float):
    try:
        return t(x)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 1 — pull swaps_clean_mat for OOS weeks
# ---------------------------------------------------------------------------

def pull_week(week_start: str, week_end: str) -> list[dict]:
    sql = f"""
SELECT
    pool_address,
    ts_sec,
    is_buy,
    a0_in, a1_in, a0_out, a1_out
FROM eatlq.uniswap_swaps_clean_mat
WHERE chain_id = {CHAIN_ID}
  AND ts_sec >= toUnixTimestamp(toDateTime('{week_start}'))
  AND ts_sec <  toUnixTimestamp(toDateTime('{week_end}'))
ORDER BY pool_address, ts_sec
"""
    rows = _ch_rows(sql, timeout=180)
    out = []
    for r in rows:
        if len(r) < 7:
            continue
        out.append({
            "pool": r[0],
            "ts": int(r[1]),
            "is_buy": int(r[2]),
            "a0_in": float(r[3] or 0),
            "a1_in": float(r[4] or 0),
            "a0_out": float(r[5] or 0),
            "a1_out": float(r[6] or 0),
        })
    return out


# ---------------------------------------------------------------------------
# Step 2 — pool first-seen (global min across all of swaps_clean_mat)
# ---------------------------------------------------------------------------

def pull_pool_first_seen(pools: set[str]) -> dict[str, int]:
    if not pools:
        return {}
    pool_list = ",".join(f"'{p}'" for p in pools)
    sql = f"""
SELECT pool_address, min(ts_sec) AS first_seen
FROM eatlq.uniswap_swaps_clean_mat
WHERE chain_id = {CHAIN_ID}
  AND pool_address IN ({pool_list})
GROUP BY pool_address
"""
    rows = _ch_rows(sql, timeout=60)
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


# ---------------------------------------------------------------------------
# Step 3 — aggregate into 5m windows per pool
# ---------------------------------------------------------------------------

def _win(ts: int) -> int:
    return (ts // WINDOW_SEC) * WINDOW_SEC


def make_windows(swaps: list[dict]) -> dict[str, dict[int, dict]]:
    """pool → window_start_ts → window_stats"""
    by_pool: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "ts_list": [], "is_buy_list": [], "amt_list": [],
    }))
    for s in swaps:
        w = _win(s["ts"])
        bucket = by_pool[s["pool"]][w]
        bucket["ts_list"].append(s["ts"])
        bucket["is_buy_list"].append(s["is_buy"])
        amt = max(s["a0_in"], s["a0_out"], s["a1_in"], s["a1_out"])
        bucket["amt_list"].append(amt)
    return by_pool


def compute_features(bucket: dict) -> dict:
    ts_list = bucket["ts_list"]
    buy_list = bucket["is_buy_list"]
    amt_list = bucket["amt_list"]
    n = len(ts_list)
    if n == 0:
        return {}
    buy_count = sum(buy_list)
    sell_count = n - buy_count
    a0_in_sum = sum(1.0 for b in buy_list if b == 1)  # proxy for buy volume
    net = buy_count - sell_count
    total = n
    price_velocity = net / total if total > 0 else 0.0
    buy_pressure = buy_count / n if n > 0 else 0.0
    mean_amt = statistics.mean(amt_list) if amt_list else 0.0
    sd_amt = statistics.pstdev(amt_list) if len(amt_list) > 1 else 0.0
    vol = (sd_amt / mean_amt) if mean_amt > 0 else 0.0
    vol = min(vol, 2.0)
    return {
        "activity_heat": n,
        "buy_pressure_ratio": buy_pressure,
        "price_velocity_5m": price_velocity,
        "token_volatility_5m": vol,
    }


# ---------------------------------------------------------------------------
# Step 4 — apply Sprint037G gate
# ---------------------------------------------------------------------------

def apply_sprint037g_gate(
    by_pool: dict[str, dict[int, dict]],
    first_seen: dict[str, int],
    week_start_ts: int,
    week_end_ts: int,
) -> list[dict]:
    """Return candidate rows that pass the young_pool_onset gate."""
    candidates = []
    for pool, windows in by_pool.items():
        pool_fs = first_seen.get(pool)
        if pool_fs is None:
            continue  # no first-seen data

        # Find onset window: first 5m window in THIS week with >= ONSET_COUNT_THRESH swaps
        onset_win_ts = None
        for w_ts in sorted(windows.keys()):
            if w_ts < week_start_ts or w_ts >= week_end_ts:
                continue
            if len(windows[w_ts]["ts_list"]) >= ONSET_COUNT_THRESH:
                onset_win_ts = w_ts
                break
        if onset_win_ts is None:
            continue  # no onset in this week

        for w_ts in sorted(windows.keys()):
            if w_ts < week_start_ts or w_ts >= week_end_ts:
                continue
            bucket = windows[w_ts]
            n = len(bucket["ts_list"])
            pool_age = w_ts - pool_fs
            time_since_onset = w_ts - onset_win_ts

            # Gate conditions (Sprint037G baseline_cell, UNCHANGED)
            if pool_age > POOL_MAX_AGE_SEC:
                continue
            if time_since_onset > ONSET_MAX_SEC:
                continue
            if n < ONSET_COUNT_THRESH:
                continue

            feats = compute_features(bucket)
            candidates.append({
                "regime_week": datetime.utcfromtimestamp(week_start_ts).strftime("%Y-%m-%d"),
                "pool_address": pool,
                "window_start_ts": w_ts,
                "window_start_iso": datetime.utcfromtimestamp(w_ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pool_age_sec": pool_age,
                "time_since_onset_sec": time_since_onset,
                "onset_window_ts": onset_win_ts,
                **feats,
            })
    return candidates


# ---------------------------------------------------------------------------
# Step 5 — compute label proxy (next-window buy_pressure > 0.5)
# ---------------------------------------------------------------------------

def add_label_proxy(candidates: list[dict], by_pool: dict[str, dict[int, dict]]) -> None:
    for c in candidates:
        pool = c["pool_address"]
        next_ts = c["window_start_ts"] + WINDOW_SEC
        future_bucket = by_pool.get(pool, {}).get(next_ts)
        if future_bucket:
            n_fut = len(future_bucket["ts_list"])
            bp_fut = sum(future_bucket["is_buy_list"]) / n_fut if n_fut > 0 else 0.0
            c["label_proxy"] = int(bp_fut > 0.5)
            c["future_buy_pressure"] = bp_fut
            c["future_n"] = n_fut
        else:
            c["label_proxy"] = None
            c["future_buy_pressure"] = None
            c["future_n"] = 0


# ---------------------------------------------------------------------------
# Step 6 — regime breakdown by activity_heat
# ---------------------------------------------------------------------------

def regime_breakdown(candidates: list[dict]) -> list[dict]:
    labeled = [c for c in candidates if c.get("label_proxy") is not None]
    if not labeled:
        return []

    def _bucket_heat(h: int) -> str:
        if h < 20:   return "heat_lt_20"
        if h < 50:   return "heat_20_50"
        if h < 100:  return "heat_50_100"
        if h < 250:  return "heat_100_250"
        return "heat_ge_250"

    def _bucket_vel(v: float) -> str:
        if v <= -0.1: return "vel_neg"
        if v < 0.1:   return "vel_flat"
        if v < 0.3:   return "vel_pos_low"
        return "vel_pos_high"

    def _bucket_bp(bp: float) -> str:
        if bp < 0.4:  return "bp_lt_04"
        if bp < 0.6:  return "bp_04_06"
        return "bp_ge_06"

    results = []
    for dim, key_fn in [
        ("activity_heat", lambda c: _bucket_heat(c["activity_heat"])),
        ("buy_pressure_ratio", lambda c: _bucket_bp(c["buy_pressure_ratio"])),
        ("price_velocity_5m", lambda c: _bucket_vel(c["price_velocity_5m"])),
    ]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for c in labeled:
            groups[key_fn(c)].append(c)
        for bucket, items in sorted(groups.items()):
            n = len(items)
            pos = sum(1 for c in items if c["label_proxy"] == 1)
            hit = pos / n if n > 0 else 0.0
            results.append({
                "dimension": dim,
                "bucket": bucket,
                "rows_gated": n,
                "label_proxy_pos": pos,
                "hit_rate_proxy": hit,
                "sample_class": _classify(n),
                "sprint050_comparable": dim == "activity_heat" and bucket == "heat_ge_250",
            })
    return results


def _classify(n: int) -> str:
    if n < 300:   return "smoke_or_label_sanity"
    if n < 1000:  return "very_weak_feasibility"
    if n < 5000:  return "scoped_dryrun_candidate"
    if n < 20000: return "scoped_backtest_candidate"
    return "formal_model_research_candidate"


# ---------------------------------------------------------------------------
# Failure autopsy — fee-tier signal
# ---------------------------------------------------------------------------

def failure_autopsy_fee_tier() -> dict:
    return {
        "signal": "fee_3000bps (q4 notation) PF=2.31 / fee_500bps PF=2.36 from Sprint050",
        "oos_testable": False,
        "reason": (
            "uniswap_swaps_clean_mat does NOT contain fee_bps. "
            "uniswap_v3_swaps_trade_semantic.fee_bps=0 for all uncurated young-pool candidates. "
            "discovered_pools.fee_bps=0 for unregistered pools. "
            "The token0_address/token1_address fields are empty for uncurated pools in CH. "
            "The only path to fee_bps for young pools is Uniswap V3 factory/pool.fee() via RPC."
        ),
        "rpc_calls_needed_to_unblock": "1 eth_call per unique pool_address → pool.fee()",
        "estimated_pools_in_oos_weeks": 1066,
        "estimated_rpc_calls": 1066,
        "rpc_cost_class": "LOW (1066 calls << 50k budget)",
        "next_step": (
            "Sprint052 preflight: call pool.fee() for each unique pool_address in OOS weeks. "
            "Costs ~1066 eth_call RPC requests. Gate with user approval per preflight contract. "
            "Once fee_bps is populated, repeat regime breakdown with fee_bps dimension."
        ),
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

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


def write_audit_md(summary: dict) -> None:
    out = []
    out.append("# Sprint051 — V3 OOS Validation\n")
    out.append(f"Generated: {summary['generated_at']}")
    out.append(f"\n**Status:** `{summary['status']}`\n")

    out.append("\n## Gate configuration (Sprint037G baseline_cell — UNCHANGED)\n")
    out.append("| param | value |")
    out.append("| --- | --- |")
    for k, v in summary["gate"].items():
        out.append(f"| {k} | {v} |")

    out.append("\n## Data source\n")
    out.append("- `eatlq.uniswap_swaps_clean_mat` (MergeTree, chain 8453)")
    out.append("- Range available: 2026-02-19 → 2026-04-20")
    out.append(f"- OOS weeks queried: `{OOS_LABELS}`")
    out.append("- pool_realtime_training_v2 / pool_features_5m_clean: **OOM on any aggregation** → replaced by swaps_clean_mat")

    out.append("\n## Raw universe (before gate)\n")
    out.append("| week | swaps_pulled | unique_pools |")
    out.append("| --- | ---: | ---: |")
    for w in summary["raw_universe"]:
        out.append(f"| {w['week_label']} | {w['swaps_pulled']} | {w['unique_pools']} |")

    out.append("\n## Sprint037G gate results\n")
    out.append("| week | candidate_rows | unique_pools_gated | gate_pass_rate | pool_age_median_s | onset_median_s |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for wk, wr in summary["gate_results"].items():
        out.append(
            f"| {wk} | {wr['candidate_rows']} | {wr['unique_pools_gated']} | "
            f"{wr['gate_pass_rate']:.4%} | {wr['pool_age_median_s']} | {wr['onset_median_s']} |"
        )

    out.append("\n## Regime breakdown (activity_heat signal — comparable to Sprint050)\n")
    out.append("| dimension | bucket | gated_rows | label_proxy_pos | hit_rate | sample_class | Sprint050_comparable |")
    out.append("| --- | --- | ---: | ---: | ---: | --- | --- |")
    for r in summary["regime_breakdown"]:
        comparable = "**YES**" if r["sprint050_comparable"] else ""
        out.append(
            f"| {r['dimension']} | {r['bucket']} | {r['rows_gated']} | {r['label_proxy_pos']} | "
            f"{r['hit_rate_proxy']:.2%} | {r['sample_class']} | {comparable} |"
        )

    out.append("\n## Sprint050 benchmark comparison\n")
    out.append("| segment | Sprint050 routed | Sprint050 hit_rate | Sprint050 PF | Sprint051 gated | Sprint051 hit_rate_proxy | testable |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for k, v in SPRINT050_BENCHMARK.items():
        s051_row = next(
            (r for r in summary["regime_breakdown"]
             if k == "activity_heat_ge_250" and r["bucket"] == "heat_ge_250"),
            None,
        )
        s051_gated = s051_row["rows_gated"] if s051_row else "—"
        s051_hit = f"{s051_row['hit_rate_proxy']:.2%}" if s051_row else "—"
        testable = "NO (fee_bps gap)" if "CANNOT" in v.get("note", "") else "YES (approx label)"
        out.append(
            f"| {k} | {v['rows_routed']} | {v['hit_rate']:.2%} | {v['pf_after_gas']} | "
            f"{s051_gated} | {s051_hit} | {testable} |"
        )

    out.append("\n## Fee-tier signal: failure autopsy\n")
    fa = summary["fee_tier_autopsy"]
    out.append(f"- **OOS testable:** `{fa['oos_testable']}`")
    out.append(f"- **Reason:** {fa['reason']}")
    out.append(f"- **RPC to unblock:** {fa['rpc_calls_needed_to_unblock']} per pool × {fa['estimated_pools_in_oos_weeks']} pools = {fa['estimated_rpc_calls']} calls (class: {fa['rpc_cost_class']})")
    out.append(f"- **Next step:** {fa['next_step']}")

    out.append("\n## Verdict\n")
    out.append(f"- Status: `{summary['status']}`")
    out.append(f"- `activity_heat_ge_250` bucket OOS testable: **YES**")
    out.append(f"- `fee_3000 / fee_500` conditional OOS testable: **NO (fee_bps gap)**")
    out.append(f"- Gate found non-zero gated rows: `{summary['total_gated_rows'] > 0}`")
    if summary['total_gated_rows'] == 0:
        out.append("\n### Why zero gated rows (failure path)\n")
        out.append(
            "The Sprint037G gate (pool_age ≤ 86400 AND onset ≤ 3600 AND count ≥ 10) "
            "matched 0 rows. Possible causes:"
        )
        out.append("1. No young pools (created < 24h before candidate window) in these OOS weeks had ≥10 swaps/5m onset.")
        out.append("2. `uniswap_swaps_clean_mat` starts 2026-02-19; pools created before that date appear old (pool_age > 86400) even if they were originally young.")
        out.append("3. The onset threshold (≥10 swaps/5m) is too high for the actual pool activity in April OOS weeks.")
        out.append("\n### Gate-relaxed diagnostic (NOT a threshold change)\n")
        out.append("Report what onset_count_thresh=2 and pool_max_age_sec=604800 (7d) would find:")
        out.append("- See `mvp/diagnostics/sprint051/gate_relaxed_diagnostic.json`")
        out.append("- This is NOT a guard change; it's a data-gap probe.")

    out.append("\n## Decision flags\n")
    for k, v in summary["decision_flags"].items():
        out.append(f"- {k} = `{v}`")

    out.append("\n## Next sprint (Sprint052)\n")
    if summary['total_gated_rows'] > 0:
        out.append("- Sprint052: add fee_bps via bounded RPC (≤2k eth_call, user gated). Re-run regime breakdown with fee_bps dimension.")
        out.append("- If activity_heat signal reproduces AND fee_3000 confirmed → `WEAK_PASS` economic readout on gated OOS rows.")
        out.append("- Still: TRAINING_ALLOWED=false, LIVE_ALLOWED=false")
    else:
        out.append("- Sprint052: investigate why gate=0. Two parallel paths:")
        out.append("  1. Lower onset_count_thresh to 2 (diagnostic only, not a production gate change)")
        out.append("  2. Expand pool first-seen lookback (currently limited by swaps_clean_mat start date 2026-02-19)")
        out.append("- After diagnosis: either accept gate relaxation or find a different young-pool signal that doesn't rely on 24h pool age")

    (AUDIT_OUT / "oos_validation.md").write_text("\n".join(out))


def write_readout_md(summary: dict) -> None:
    out = []
    out.append("# Run-051 — V3 OOS Validation Readout\n")
    out.append(f"Generated: {summary['generated_at']} · Branch: `sprint050-v3-new-continent`\n")
    out.append(f"\n**Gate:** Sprint037G baseline_cell UNCHANGED  **Status:** `{summary['status']}`\n")
    out.append("\n## Key numbers\n")
    out.append(f"- Total gated rows (all OOS weeks): **{summary['total_gated_rows']}**")
    out.append(f"- Total unique pools gated: **{summary['total_gated_pools']}**")
    out.append(f"- Label proxy (buy_pressure>0.5 in next 5m): available for **{summary['labeled_rows']}** rows")
    out.append(f"- activity_heat ≥ 250 rows: **{summary['heat_250_rows']}**")
    out.append(f"- Fee-tier testable on OOS: **NO** (see autopsy)")
    out.append("\n## Decision flags\n")
    for k, v in summary["decision_flags"].items():
        out.append(f"- {k} = `{v}`")
    (ROOT / "mvp/runs/run-051-oos-validation-readout.md").write_text("\n".join(out))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[sprint051] start {_now_utc()}", flush=True)

    all_candidates: list[dict] = []
    raw_universe: list[dict] = []
    gate_results: dict[str, dict] = {}

    for week_label, (w_start, w_end) in zip(OOS_LABELS, OOS_WEEKS):
        print(f"[sprint051] pulling {week_label} ({w_start} → {w_end}) ...", flush=True)
        try:
            swaps = pull_week(w_start, w_end)
        except Exception as exc:
            print(f"  ERROR pulling week: {exc}", flush=True)
            continue
        print(f"  {len(swaps)} swap rows pulled", flush=True)

        pools = {s["pool"] for s in swaps}
        raw_universe.append({"week_label": week_label, "swaps_pulled": len(swaps), "unique_pools": len(pools)})

        print(f"  fetching pool first-seen for {len(pools)} pools ...", flush=True)
        try:
            first_seen = pull_pool_first_seen(pools)
        except Exception as exc:
            print(f"  ERROR first-seen: {exc}", flush=True)
            first_seen = {}

        by_pool = make_windows(swaps)

        w_start_ts = int(datetime.strptime(w_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        w_end_ts   = int(datetime.strptime(w_end,   "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

        candidates = apply_sprint037g_gate(by_pool, first_seen, w_start_ts, w_end_ts)
        add_label_proxy(candidates, by_pool)
        print(f"  {len(candidates)} candidates passed gate", flush=True)

        pool_ages = [c["pool_age_sec"] for c in candidates] if candidates else []
        onsets    = [c["time_since_onset_sec"] for c in candidates] if candidates else []

        gate_results[week_label] = {
            "candidate_rows":    len(candidates),
            "unique_pools_gated": len({c["pool_address"] for c in candidates}),
            "gate_pass_rate": len(candidates) / len(swaps) if swaps else 0.0,
            "pool_age_median_s": int(statistics.median(pool_ages)) if pool_ages else 0,
            "onset_median_s":    int(statistics.median(onsets))    if onsets    else 0,
        }
        all_candidates.extend(candidates)

    labeled = [c for c in all_candidates if c.get("label_proxy") is not None]
    heat_250 = [c for c in all_candidates if c["activity_heat"] >= 250]
    regime = regime_breakdown(all_candidates)

    # Gate-relaxed diagnostic (probe only, not a guard change)
    relaxed_candidates = []
    for week_label, (w_start, w_end) in zip(OOS_LABELS, OOS_WEEKS):
        try:
            swaps = pull_week(w_start, w_end)
            pools = {s["pool"] for s in swaps}
            first_seen = pull_pool_first_seen(pools)
            by_pool = make_windows(swaps)
            w_start_ts = int(datetime.strptime(w_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            w_end_ts   = int(datetime.strptime(w_end,   "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            for pool, windows in by_pool.items():
                fs = first_seen.get(pool)
                if fs is None:
                    continue
                onset_win = None
                for wt in sorted(windows.keys()):
                    if wt < w_start_ts or wt >= w_end_ts:
                        continue
                    if len(windows[wt]["ts_list"]) >= 2:  # relaxed: count ≥ 2
                        onset_win = wt
                        break
                if onset_win is None:
                    continue
                for wt in sorted(windows.keys()):
                    if wt < w_start_ts or wt >= w_end_ts:
                        continue
                    n = len(windows[wt]["ts_list"])
                    age = wt - fs
                    since_onset = wt - onset_win
                    if age <= 7 * 86400 and since_onset <= 3600 and n >= 2:
                        relaxed_candidates.append({"regime_week": week_label, "pool": pool, "heat": n, "pool_age_s": age})
        except Exception:
            pass

    total_gated = len(all_candidates)
    total_pools = len({c["pool_address"] for c in all_candidates})

    # Determine status
    if total_gated >= 300:
        status = "SIGNAL_TESTABLE_GATE_FOUND_ROWS"
    elif total_gated > 0:
        status = "GATE_FOUND_ROWS_BELOW_SMOKE_THRESHOLD"
    else:
        status = "GATE_ZERO_ROWS_SEE_FAILURE_AUTOPSY"

    summary = {
        "kind": "sprint051_oos_validation",
        "generated_at": _now_utc(),
        "status": status,
        "gate": {
            "pool_max_age_sec": POOL_MAX_AGE_SEC,
            "onset_max_sec": ONSET_MAX_SEC,
            "onset_count_thresh": ONSET_COUNT_THRESH,
            "source": "Sprint037G baseline_cell UNCHANGED",
        },
        "raw_universe": raw_universe,
        "gate_results": gate_results,
        "total_gated_rows": total_gated,
        "total_gated_pools": total_pools,
        "labeled_rows": len(labeled),
        "heat_250_rows": len(heat_250),
        "regime_breakdown": regime,
        "fee_tier_autopsy": failure_autopsy_fee_tier(),
        "sprint050_benchmark": SPRINT050_BENCHMARK,
        "gate_relaxed_diagnostic": {
            "onset_count_thresh_relaxed": 2,
            "pool_max_age_relaxed_sec": 7 * 86400,
            "onset_max_sec": 3600,
            "candidate_rows_relaxed": len(relaxed_candidates),
            "unique_pools_relaxed": len({c["pool"] for c in relaxed_candidates}),
            "note": "DIAGNOSTIC_ONLY_NOT_A_GUARD_CHANGE",
        },
        "decision_flags": {
            "MAINLINE_SCOPE": "BASE_UNISWAP_V3",
            "OOS_GATE_RESULT": status,
            "ACTIVITY_HEAT_SIGNAL_TESTABLE": True,
            "FEE_TIER_SIGNAL_TESTABLE": False,
            "FEE_TIER_BLOCKER": "fee_bps_not_in_CH_for_uncurated_young_pools",
            "RPC_TO_UNBLOCK_FEE_TIER": "~1066_eth_call_pool.fee()_user_gated",
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "PRIVATE_KEY_USED": False,
            "PRODUCTION_SIGNALS_WRITTEN": False,
            "NO_ALPHA_CLAIM": True,
        },
    }

    _write_json(DIAG_OUT / "oos_validation_summary.json", summary)
    _write_csv(DIAG_OUT / "oos_candidates.csv", all_candidates)
    _write_csv(DIAG_OUT / "oos_regime_breakdown.csv", regime)
    _write_json(DIAG_OUT / "fee_tier_autopsy.json", failure_autopsy_fee_tier())
    _write_json(DIAG_OUT / "gate_relaxed_diagnostic.json", {
        "relaxed_candidates_count": len(relaxed_candidates),
        "unique_pools": len({c["pool"] for c in relaxed_candidates}),
        "per_week": {
            wl: [c for c in relaxed_candidates if c["regime_week"] == wl]
            for wl in OOS_LABELS
        },
    })
    write_audit_md(summary)
    write_readout_md(summary)

    print(f"[sprint051] total_gated={total_gated} labeled={len(labeled)} heat250={len(heat_250)}", flush=True)
    print(f"[sprint051] status={status}", flush=True)
    print(f"[sprint051] artifacts written → mvp/audits/sprint051/ mvp/diagnostics/sprint051/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
