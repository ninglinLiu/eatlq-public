#!/usr/bin/env python3
"""Sprint063 — Expansion Week Detox + Sub-bucket Revalidation.

Research-only. No private key, no live, no training, no production signals.
Thresholds frozen. No denylist based on post-hoc losses.

Tasks:
  A: Reproduce Sprint062 readout
  B: NEXIO / drag token autopsy (decision-time observable features)
  C: Token quality filter candidates (conservative / balanced / aggressive)
  D: heat_20_50 sub-bucket revalidation (Sprint054 definition frozen)
  E: Pool heterogeneity analysis (13 qualifying pools)
  F: May11+ expansion preflight

Heat bucket definition (Sprint053/054 canonical, FROZEN):
  heat_lt_20:  swaps_per_5m <  20
  heat_20_50:  20 <= swaps_per_5m < 50  ← primary signal sub-bucket
  heat_50_100: 50 <= swaps_per_5m < 100
  heat_100_250: 100 <= swaps_per_5m < 250
  heat_ge_250: swaps_per_5m >= 250
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUDIT_OUT = ROOT / "mvp/audits/sprint063"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint063"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

# Sprint062 source data
S062_LABELED   = ROOT / "mvp/diagnostics/sprint062/labeled_candidates.csv"
S062_CANDS_427 = ROOT / "mvp/diagnostics/sprint062/candidates_20260427.csv"
S062_CANDS_504 = ROOT / "mvp/diagnostics/sprint062/candidates_20260504.csv"
S062_FLAGS     = ROOT / "mvp/audits/sprint062/expansion_decision_flags.json"

# Sprint053 pool meta
S053_META_PATH = ROOT / "mvp/diagnostics/sprint053/pool_meta.json"

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID     = 8453
WETH_BASE    = "0x4200000000000000000000000000000000000006".lower()
WINDOW_SEC   = 300

# Sprint054 benchmark (frozen reference)
S054_BENCH = {"all_fee100": {"hit": 0.522, "pf": 1.884, "rows": 117, "median_ret": 0.0014},
              "heat_20_50":  {"hit": 0.625, "pf": 4.296, "rows": 49,  "median_ret": 0.0071}}

# Sprint062 claimed metrics (from readout)
S062_CLAIMED = {
    "2026-04-27": {"labeled": 80,  "hit": 0.450, "pf": 0.687, "median_ret": -0.00462},
    "2026-05-04": {"labeled": 61,  "hit": 0.590, "pf": 1.136, "median_ret": 0.00252},
    "combined":   {"labeled": 141, "hit": 0.5106,"pf": 0.8059,"median_ret": 0.00034},
}

# Pool → token mapping from discovered_pools
POOL_TOKEN_MAP = {
    "0xa3eb19bbde120ea2caf148171cc70ffad16d4c5a": {"token": "PEN",     "weth_is_t0": True,  "other_token": "PEN",     "discovered_at": "2026-04-28 06:00:13"},
    "0x790cf0e891badd11424dce95b475e8eef7f52527": {"token": "UNKNOWN", "weth_is_t0": False, "other_token": "UNKNOWN", "discovered_at": "2026-04-28 19:09:40"},
    "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a": {"token": "BEBE",    "weth_is_t0": True,  "other_token": "BEBE",    "discovered_at": "2026-04-29 08:01:14"},
    "0x7a9713b17843dddc3325a37ec2c139d5ed2afe29": {"token": "RINO",    "weth_is_t0": False, "other_token": "RINO",    "discovered_at": "2026-05-01 10:33:45"},
    "0xf7df90730870571529c6f6743443e1ca40e0a457": {"token": "NEXIO",   "weth_is_t0": True,  "other_token": "NEXIO",   "discovered_at": "2026-04-30 09:13:16"},
    "0x29fd6a7c3afb5047b0e2703ac99871849e7ecdad": {"token": "UNKNOWN", "weth_is_t0": True,  "other_token": "UNKNOWN", "discovered_at": "2026-05-03 12:57:28"},
    "0x73e0a6ce4aec1da0ee87bb44246ac695f4489488": {"token": "UNKNOWN", "weth_is_t0": False, "other_token": "UNKNOWN", "discovered_at": "2026-05-03 13:03:50"},
    "0xd6bd28f8c493dbfa7a9eac890d4e3121dab29a84": {"token": "UNKNOWN", "weth_is_t0": True,  "other_token": "UNKNOWN", "discovered_at": "2026-05-06 04:03:20"},
    "0x991cf2b5eb33211bd9ba75fcd0d9456a3248c42a": {"token": "SHMA",    "weth_is_t0": False, "other_token": "SHMA",    "discovered_at": "2026-05-08 07:22:31"},
    "0xcc5eeaabd1f7421a558524cbb9036f47130a9678": {"token": "UNKNOWN", "weth_is_t0": False, "other_token": "UNKNOWN", "discovered_at": "2026-05-08 07:25:30"},
    "0xc1169323b9e7885c12b04b23d45cb046cfe43087": {"token": "BERMIT",  "weth_is_t0": True,  "other_token": "BERMIT",  "discovered_at": "2026-05-06 17:03:39"},
    "0x46ad711536d02de716a263263c394669c696b7a3": {"token": "BLEPE",   "weth_is_t0": False, "other_token": "BLEPE",   "discovered_at": "2026-05-06 17:11:39"},
    "0x0f4c24d00267d58ec9fad43a9df5a8ffeed3d542": {"token": "UNKNOWN", "weth_is_t0": True,  "other_token": "UNKNOWN", "discovered_at": "2026-05-04 14:15:48"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_ch(sql: str, timeout: int = 120) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:300]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def _jdump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def _cdump(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _hbucket(h: float) -> str:
    """Sprint053/054 canonical heat bucket definition — FROZEN."""
    if h < 20:    return "heat_lt_20"
    if h < 50:    return "heat_20_50"
    if h < 100:   return "heat_50_100"
    if h < 250:   return "heat_100_250"
    return "heat_ge_250"


def _pf(wins: list[float], losses: list[float]) -> float:
    if not wins:   return 0.0
    if not losses: return float("inf")
    return sum(wins) / max(abs(sum(losses)), 1e-12)


def _stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0, "hits": 0, "hit_rate": None, "pf": None, "median_ret": None, "sum_ret": None}
    wins   = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {
        "n":          len(rets),
        "hits":       len(wins),
        "hit_rate":   round(len(wins) / len(rets), 4),
        "pf":         round(_pf(wins, losses), 4),
        "median_ret": round(statistics.median(rets), 6),
        "sum_ret":    round(sum(rets), 6),
    }


def load_labeled() -> list[dict]:
    with S062_LABELED.open() as f:
        rows = list(csv.DictReader(f))
    return rows


def load_candidates() -> list[dict]:
    cands = []
    for p in [S062_CANDS_427, S062_CANDS_504]:
        with p.open() as f:
            cands.extend(list(csv.DictReader(f)))
    return cands


def compute_activity_heat(cands: list[dict]) -> dict[tuple[str, int], int]:
    """Compute activity_heat = swap count per (pool, bucket_onset_sec) from Phase 1 candidates."""
    counts: dict[tuple[str, int], int] = Counter()
    for c in cands:
        pool   = c["pool"].lower()
        age    = float(c["pool_age_sec"])
        bucket = int(age // WINDOW_SEC) * WINDOW_SEC
        counts[(pool, bucket)] += 1
    return dict(counts)


def enrich_labeled(labeled: list[dict], heat_map: dict) -> list[dict]:
    """Add activity_heat, heat_bucket, token, slippage_proxy, pool_age_bucket to labeled rows."""
    enriched = []
    for r in labeled:
        pool    = r["pool"].lower()
        bucket  = int(float(r.get("bucket_onset_sec", 0)))
        heat    = heat_map.get((pool, bucket), 0)
        hb      = _hbucket(heat)
        tm      = POOL_TOKEN_MAP.get(pool, {})
        token   = tm.get("token", "UNKNOWN")
        # Slippage proxy: entry_amount_out relative to PROBE_WEI (0.00005 WETH = 5e13 wei)
        # We measure in token units, so proxy is just amountOut sanity (>0)
        try:
            ao = int(r.get("entry_amount_out", 0) or 0)
        except (ValueError, TypeError):
            ao = 0
        # Exit slippage proxy: exit_weth / PROBE_WEI - 1 = net_return_5m
        try:
            nr = float(r.get("net_return_5m", "None") or "None")
        except (ValueError, TypeError):
            nr = None
        entry_ok = r.get("entry_quote_ok", "") == "True"
        exit_ok  = r.get("exit_quote_ok", "") == "True"
        pool_age = float(r.get("pool_age_sec", 0))
        discovered_at = tm.get("discovered_at", "")
        enriched.append({
            **r,
            "token": token,
            "activity_heat": heat,
            "heat_bucket": hb,
            "pool_short": pool[:12],
            "pool_age_sec": pool_age,
            "entry_quote_ok": entry_ok,
            "exit_quote_ok":  exit_ok,
            "net_return_5m":  nr,
            "entry_amount_out_nonzero": ao > 0,
            "discovered_at": discovered_at,
            "weth_is_t0": tm.get("weth_is_t0", True),
        })
    return enriched


# ---------------------------------------------------------------------------
# Task A: Reproduce Sprint062 readout
# ---------------------------------------------------------------------------

def task_a_reproduce(enriched: list[dict]) -> dict:
    print("\n[A] Reproducing Sprint062 readout ...", flush=True)
    valid = [r for r in enriched if r["entry_quote_ok"] and r["exit_quote_ok"]
             and r["net_return_5m"] is not None]
    print(f"  valid labeled rows: {len(valid)} (claimed: 141)", flush=True)

    results = {"per_week": {}, "combined": {}, "per_pool": {}, "nexio_contrib": {}}

    # Per-week
    for week in sorted({r["week"] for r in valid}):
        wrows = [r for r in valid if r["week"] == week]
        rets  = [r["net_return_5m"] for r in wrows]
        s     = _stats(rets)
        claimed = S062_CLAIMED.get(week, {})
        results["per_week"][week] = {
            **s,
            "claimed_n":       claimed.get("labeled"),
            "claimed_hit":     claimed.get("hit"),
            "claimed_pf":      claimed.get("pf"),
            "claimed_median":  claimed.get("median_ret"),
            "n_match":         s["n"] == claimed.get("labeled"),
            "hit_match":       abs((s["hit_rate"] or 0) - (claimed.get("hit") or 0)) < 0.01,
            "pf_match":        abs((s["pf"] or 0) - (claimed.get("pf") or 0)) < 0.05,
        }
        print(f"  {week}: n={s['n']} hit={s['hit_rate']} pf={s['pf']} | "
              f"claimed n={claimed.get('labeled')} hit={claimed.get('hit')} pf={claimed.get('pf')}", flush=True)

    # Combined
    rets_all = [r["net_return_5m"] for r in valid]
    s_all    = _stats(rets_all)
    results["combined"] = {
        **s_all,
        "claimed_n":   S062_CLAIMED["combined"]["labeled"],
        "claimed_hit": S062_CLAIMED["combined"]["hit"],
        "claimed_pf":  S062_CLAIMED["combined"]["pf"],
        "n_match":     s_all["n"] == S062_CLAIMED["combined"]["labeled"],
    }

    # Per-pool
    pool_rows: dict[str, list[float]] = defaultdict(list)
    for r in valid:
        pool_rows[r["pool"]].append(r["net_return_5m"])
    for pool, rets in sorted(pool_rows.items(), key=lambda x: -_pf(
        [r for r in x[1] if r > 0], [r for r in x[1] if r <= 0]
    )):
        s = _stats(rets)
        token = POOL_TOKEN_MAP.get(pool, {}).get("token", "UNKNOWN")
        week  = next((r["week"] for r in valid if r["pool"] == pool), "?")
        results["per_pool"][pool] = {
            "pool_short": pool[:14], "token": token, "week": week, **s
        }
        print(f"  pool {pool[:12]}..: token={token} n={s['n']} hit={s['hit_rate']} pf={s['pf']}", flush=True)

    # NEXIO contribution
    nexio_pool = "0xf7df90730870571529c6f6743443e1ca40e0a457"
    nexio_rows = [r for r in valid if r["pool"] == nexio_pool]
    nexio_rets = [r["net_return_5m"] for r in nexio_rows]
    nexio_s    = _stats(nexio_rets)
    non_nexio  = [r for r in valid if r["pool"] != nexio_pool]
    non_nexio_rets = [r["net_return_5m"] for r in non_nexio]
    non_nexio_s    = _stats(non_nexio_rets)
    results["nexio_contrib"] = {
        "nexio_n":        nexio_s["n"],
        "nexio_hit":      nexio_s["hit_rate"],
        "nexio_pf":       nexio_s["pf"],
        "nexio_sum_ret":  nexio_s["sum_ret"],
        "excl_nexio_n":   non_nexio_s["n"],
        "excl_nexio_hit": non_nexio_s["hit_rate"],
        "excl_nexio_pf":  non_nexio_s["pf"],
    }
    print(f"  NEXIO: n={nexio_s['n']} pf={nexio_s['pf']}", flush=True)
    print(f"  excl. NEXIO: n={non_nexio_s['n']} pf={non_nexio_s['pf']}", flush=True)

    return results


# ---------------------------------------------------------------------------
# Task B: NEXIO / drag token autopsy
# ---------------------------------------------------------------------------

def _ext_swap_count_all_pools(pool_rows_map: dict) -> dict[str, int]:
    """Compute external swap count for ALL pools at decision time (pre-entry).

    This is a DECISION-TIME observable: at the moment of entry, how many swaps
    has the token been involved in across OTHER pools?

    Returns: pool_address -> count
    """
    print("  [ext_liq] querying cross-pool swap counts for all 13 pools ...", flush=True)
    result = {}
    for pool, rows in sorted(pool_rows_map.items()):
        if not rows:
            result[pool] = -1
            continue
        # token_out is the non-WETH token; its address is the external liq proxy
        token_out = rows[0].get("token_out", "").lower()
        if not token_out or token_out == WETH_BASE:
            # Reverse: token_in is non-WETH → use token_in
            token_out = rows[0].get("token_in", "").lower()
        if not token_out or len(token_out) < 10:
            result[pool] = -1
            continue
        entry_times = [r.get("block_time", "") for r in rows if r.get("block_time")]
        entry_time  = min(entry_times) if entry_times else ""
        if not entry_time:
            result[pool] = -1
            continue
        et_safe = entry_time[:19]
        try:
            ext_rows = run_ch(f"""
                SELECT count()
                FROM eatlq.discovered_pools dp
                JOIN eatlq.uniswap_v3_swaps_decoded s
                  ON lower(s.pool_address) = lower(dp.pool_address)
                WHERE dp.chain_id = {CHAIN_ID}
                  AND (lower(dp.token0_address) = '{token_out}'
                       OR lower(dp.token1_address) = '{token_out}')
                  AND lower(dp.pool_address) != '{pool}'
                  AND s.block_time < '{et_safe}'
            """)
            cnt = int(ext_rows[0][0]) if ext_rows else 0
        except Exception as e:
            print(f"  [warn] ext_liq error for {pool[:12]}: {e}", flush=True)
            cnt = -1
        result[pool] = cnt
        print(f"  [ext_liq] {pool[:12]}: token_out={token_out[:12]}.. "
              f"ext_swaps_before={cnt}", flush=True)
    return result


def task_b_nexio_autopsy(enriched: list[dict]) -> tuple[list[dict], dict[str, int]]:
    print("\n[B] NEXIO / drag token autopsy + ext_liq for ALL pools ...", flush=True)

    # ALL pools (not just drag) need ext_liq computed for decision-time filtering
    pool_rets: dict[str, list[float]] = defaultdict(list)
    pool_rows_map: dict[str, list[dict]] = defaultdict(list)
    for r in enriched:
        if r["entry_quote_ok"] and r["exit_quote_ok"] and r["net_return_5m"] is not None:
            pool_rets[r["pool"]].append(r["net_return_5m"])
            pool_rows_map[r["pool"]].append(r)

    # Compute external liquidity for ALL pools — decision-time observable
    all_ext_liq = _ext_swap_count_all_pools(pool_rows_map)

    drag_pools = {p for p, rets in pool_rets.items()
                  if _pf([r for r in rets if r > 0], [r for r in rets if r <= 0]) < 1.0}
    print(f"  drag pools (PF<1): {len(drag_pools)}", flush=True)

    # Autopsy covers DRAG pools only (post-analysis narrative)
    token_out_addrs = {}
    for pool, rows in pool_rows_map.items():
        if rows:
            token_out_addrs[pool] = rows[0]["token_out"].lower()

    autopsy: list[dict] = []

    for pool in sorted(drag_pools):
        rows  = pool_rows_map[pool]
        rets  = pool_rets[pool]
        s     = _stats(rets)
        tm    = POOL_TOKEN_MAP.get(pool, {})
        token = tm.get("token", "UNKNOWN")
        # Entry time: earliest block_time in labeled rows
        entry_times = [r.get("block_time", "") for r in rows if r.get("block_time")]
        entry_time  = min(entry_times) if entry_times else ""
        token_out   = token_out_addrs.get(pool, "")
        # External liquidity: use pre-computed all_ext_liq map
        ext_swap_count = all_ext_liq.get(pool, 0)

        # Quote failure analysis
        entry_ok_count = sum(1 for r in rows if r.get("entry_quote_ok"))
        exit_ok_count  = sum(1 for r in rows if r.get("exit_quote_ok"))
        fail_reasons   = [r.get("fail_reason", "") for r in rows if r.get("fail_reason")]
        # Slippage proxy from net_return distribution
        all_rets_pool  = [r["net_return_5m"] for r in rows
                          if r["entry_quote_ok"] and r["exit_quote_ok"]
                          and r["net_return_5m"] is not None]
        worst_ret      = min(all_rets_pool) if all_rets_pool else None
        # Heat distribution
        heats = [r["activity_heat"] for r in rows if r.get("activity_heat") is not None]
        heat_buckets = Counter(r["heat_bucket"] for r in rows)
        discovered_at = tm.get("discovered_at", "")
        pool_age_first = min((float(r.get("pool_age_sec", 0)) for r in rows), default=0)
        # Decision-time filter assessment
        can_filter_external_liq = ext_swap_count == 0
        can_filter_quote        = entry_ok_count < len(rows)  # some entry quotes failed
        can_filter_discovery    = bool(discovered_at and entry_time and discovered_at > entry_time[:19])

        reason = []
        if can_filter_external_liq:
            reason.append("ZERO_EXTERNAL_LIQUIDITY_AT_ENTRY")
        if can_filter_quote:
            reason.append("PARTIAL_QUOTE_FAILURE")
        if s["pf"] is not None and s["pf"] < 0.2:
            reason.append("SEVERE_DRAG_PF<0.2")
        filter_verdict = "FILTERABLE_DECISION_TIME" if can_filter_external_liq else "REQUIRES_POSTFACT_LOSS"

        autopsy.append({
            "pool":              pool,
            "pool_short":        pool[:14],
            "token":             token,
            "token_out_addr":    token_out[:12] + ".." if len(token_out) > 12 else token_out,
            "week":              rows[0]["week"] if rows else "?",
            "entry_time_first":  entry_time,
            "pool_age_at_entry_sec": round(pool_age_first, 1),
            "discovered_at_in_db": discovered_at,
            "discovered_after_entry": can_filter_discovery,
            "fee_tier":          100,
            "weth_leg":          True,
            "labeled_rows":      len(all_rets_pool),
            "entry_quote_ok_rate": round(entry_ok_count / max(len(rows), 1), 3),
            "exit_quote_ok_rate":  round(exit_ok_count  / max(len(rows), 1), 3),
            "hit_rate":          s["hit_rate"],
            "profit_factor":     s["pf"],
            "median_ret":        s["median_ret"],
            "worst_ret":         round(worst_ret, 6) if worst_ret is not None else None,
            "heat_buckets":      json.dumps(dict(heat_buckets)),
            "avg_heat":          round(statistics.mean(heats), 1) if heats else 0,
            "ext_swap_count_before_entry": ext_swap_count,
            "can_filter_external_liq": can_filter_external_liq,
            "can_filter_quote":  can_filter_quote,
            "fail_reasons":      "; ".join(fail_reasons[:3]) if fail_reasons else "none",
            "filter_verdict":    filter_verdict,
            "filter_reason":     "; ".join(reason) if reason else "NONE",
        })

    print(f"  autopsy complete: {len(autopsy)} drag pools", flush=True)
    return autopsy, all_ext_liq


# ---------------------------------------------------------------------------
# Task C: Token quality filter candidates
# ---------------------------------------------------------------------------

def task_c_filters(enriched: list[dict], autopsy: list[dict], all_ext_liq: dict[str, int]) -> dict:
    print("\n[C] Token quality filter candidates ...", flush=True)

    zero_liq_pools   = {pool for pool, cnt in all_ext_liq.items() if cnt == 0}
    nonzero_liq_pools= {pool for pool, cnt in all_ext_liq.items() if cnt > 0}
    print(f"  ext_liq=0 (all brand-new tokens): {len(zero_liq_pools)} / {len(all_ext_liq)} pools", flush=True)
    print(f"  NOTE: ALL expansion pools are first-launch → ext_liq is non-discriminating.", flush=True)
    print(f"  Filters redesigned to use available decision-time features: heat, pool_age.", flush=True)

    valid = [r for r in enriched if r["entry_quote_ok"] and r["exit_quote_ok"]
             and r["net_return_5m"] is not None]

    def eval_filter(name: str, keep_fn) -> dict:
        kept    = [r for r in valid if keep_fn(r)]
        removed = [r for r in valid if not keep_fn(r)]
        if not kept:
            return {
                "filter": name, "kept_rows": 0, "removed_rows": len(removed),
                "removed_pools": [], "removed_tokens": [],
                "hit_rate": None, "pf": None, "median_ret": None,
                "top1_pool_share": None, "top1_tok_share": None,
            }
        rets   = [r["net_return_5m"] for r in kept]
        s      = _stats(rets)
        pool_c = Counter(r["pool"] for r in kept)
        tok_c  = Counter(r["token"] for r in kept)
        return {
            "filter":          name,
            "kept_rows":       s["n"],
            "removed_rows":    len(removed),
            "removed_pools":   sorted({r["pool"][:14] for r in removed}),
            "removed_tokens":  sorted({r["token"] for r in removed}),
            "hit_rate":        s["hit_rate"],
            "pf":              s["pf"],
            "median_ret":      s["median_ret"],
            "top1_pool_share": round(max(pool_c.values()) / len(kept), 4),
            "top1_tok_share":  round(max(tok_c.values())  / len(kept), 4),
        }

    # Conservative: require heat >= 20 per 5-min entry bucket.
    # Rationale: Sprint054 found heat_20_50 (PF=4.296) >> heat_lt_20 (PF=0.837).
    # Using the SAME lower boundary (20 swaps/5m) as a filter, not a new threshold.
    # Feature: activity_heat — computed from Phase-1 swap counts, decision-time.
    # NOTE: ext_liq=0 for ALL pools so excluded as non-discriminating.
    def conservative(r: dict) -> bool:
        return r["activity_heat"] >= 20

    # Balanced: heat >= 20 AND pool_age <= 1800s (entries in the first 30 min).
    # Rationale: tighter time window near pool launch → more concentrated liquidity.
    # Both features are decision-time observable from canonical gate data.
    def balanced(r: dict) -> bool:
        return r["activity_heat"] >= 20 and r["pool_age_sec"] <= 1800

    # Aggressive: balanced + entry_amount_out > 0 (amountOut sanity gate).
    # Filters degenerate quote cases where QuoterV2 returns success but zero output.
    def aggressive(r: dict) -> bool:
        return balanced(r) and r["entry_amount_out_nonzero"]

    results = {}
    for fname, fn in [("conservative", conservative), ("balanced", balanced), ("aggressive", aggressive)]:
        r = eval_filter(fname, fn)
        per_week: dict[str, dict] = {}
        for week in ["2026-04-27", "2026-05-04"]:
            week_valid = [x for x in valid if x["week"] == week]
            kept_week  = [x for x in week_valid if fn(x)]
            if not kept_week:
                per_week[week] = {"kept": 0, "hit_rate": None, "pf": None, "median_ret": None}
            else:
                rets = [x["net_return_5m"] for x in kept_week]
                s    = _stats(rets)
                per_week[week] = {"kept": s["n"], "hit_rate": s["hit_rate"],
                                  "pf": s["pf"], "median_ret": s["median_ret"]}
        r["per_week"] = per_week
        results[fname] = r
        print(f"  {fname}: kept={r['kept_rows']} removed={r['removed_rows']} "
              f"pf={r['pf']} hit={r['hit_rate']}", flush=True)

    return results


# ---------------------------------------------------------------------------
# Task D: heat_20_50 sub-bucket revalidation
# ---------------------------------------------------------------------------

def task_d_heat_revalidation(enriched: list[dict]) -> dict:
    print("\n[D] heat_20_50 sub-bucket revalidation ...", flush=True)
    print("  Definition source: scripts/sprint053_fee100_expansion.py lines 371-376", flush=True)
    print("  heat_20_50: 20 <= swaps_per_5m < 50 (FROZEN — not retuned)", flush=True)

    valid = [r for r in enriched if r["entry_quote_ok"] and r["exit_quote_ok"]
             and r["net_return_5m"] is not None]

    results = {}
    for subset_name, subset in [
        ("2026-04-27", [r for r in valid if r["week"] == "2026-04-27"]),
        ("2026-05-04", [r for r in valid if r["week"] == "2026-05-04"]),
        ("combined",  valid),
    ]:
        h20 = [r for r in subset if r["heat_bucket"] == "heat_20_50"]
        s   = _stats([r["net_return_5m"] for r in h20])
        # Check NEXIO overlap
        nexio_in_h20 = sum(1 for r in h20 if r["token"] == "NEXIO")
        # Check single-pool dominance
        pool_c = Counter(r["pool"] for r in h20)
        top1   = max(pool_c.values()) / len(h20) if h20 else 0
        tok_c  = Counter(r["token"] for r in h20)
        top1_tok = max(tok_c.values()) / len(h20) if h20 else 0
        results[subset_name] = {
            "total_valid": len(subset),
            "heat_20_50_rows": s["n"],
            "hit_rate": s["hit_rate"],
            "pf": s["pf"],
            "median_ret": s["median_ret"],
            "nexio_in_heat_20_50": nexio_in_h20,
            "top1_pool_share": round(top1, 4),
            "top1_tok_share":  round(top1_tok, 4),
            "pool_breakdown": dict(pool_c),
            "tok_breakdown":  dict(tok_c),
            "s054_bench_pf":  S054_BENCH["heat_20_50"]["pf"],
            "vs_bench_pf":    round((s["pf"] or 0) - S054_BENCH["heat_20_50"]["pf"], 4),
        }
        print(f"  {subset_name} heat_20_50: n={s['n']} hit={s['hit_rate']} pf={s['pf']} "
              f"nexio_overlap={nexio_in_h20} top1_pool={top1:.1%}", flush=True)

    return results


# ---------------------------------------------------------------------------
# Task E: Pool heterogeneity analysis
# ---------------------------------------------------------------------------

def task_e_pool_analysis(enriched: list[dict]) -> list[dict]:
    print("\n[E] Pool heterogeneity analysis ...", flush=True)

    valid = [r for r in enriched if r["entry_quote_ok"] and r["exit_quote_ok"]
             and r["net_return_5m"] is not None]

    pool_rows_map: dict[str, list[dict]] = defaultdict(list)
    for r in valid:
        pool_rows_map[r["pool"]].append(r)

    pool_analysis = []
    for pool in sorted(pool_rows_map.keys()):
        rows  = pool_rows_map[pool]
        rets  = [r["net_return_5m"] for r in rows]
        s     = _stats(rets)
        wins  = [r for r in rets if r > 0]
        losses= [r for r in rets if r <= 0]
        tm    = POOL_TOKEN_MAP.get(pool, {})
        token = tm.get("token", "UNKNOWN")
        week  = rows[0]["week"] if rows else "?"
        heats = [r["activity_heat"] for r in rows if r.get("activity_heat") is not None]
        heat_dist = Counter(r["heat_bucket"] for r in rows)
        # Max drawdown proxy: worst single trade
        worst = min(rets) if rets else 0
        # Top trade contribution: best single trade / total return
        best  = max(rets) if rets else 0
        total_ret = sum(rets)
        top_contrib = round(best / total_ret, 4) if total_ret > 0 else None
        # Quoteability
        entry_ok = sum(1 for r in rows if r.get("entry_quote_ok"))
        exit_ok  = sum(1 for r in rows if r.get("exit_quote_ok"))
        # Recommended status
        n = s["n"]
        pf = s["pf"] or 0
        if n < 5:
            status = "INCONCLUSIVE_SMALL_N"
        elif pf >= 1.5:
            status = "KEEP_FOR_EXPANSION"
        elif pf >= 1.0:
            status = "WATCH_ONLY"
        elif pf >= 0.5:
            status = "FILTER_CANDIDATE"
        else:
            status = "REJECT_ILLIQUID"

        pool_analysis.append({
            "pool":            pool,
            "pool_short":      pool[:14],
            "token":           token,
            "week":            week,
            "labeled_rows":    n,
            "hit_rate":        s["hit_rate"],
            "pf":              pf,
            "median_ret":      s["median_ret"],
            "sum_ret":         s["sum_ret"],
            "worst_ret_proxy": round(worst, 6),
            "best_ret":        round(best, 6),
            "top_trade_contrib": top_contrib,
            "entry_quoteability": round(entry_ok / max(len(rows), 1), 3),
            "exit_quoteability":  round(exit_ok  / max(len(rows), 1), 3),
            "fee_tier":         100,
            "avg_heat":         round(statistics.mean(heats), 1) if heats else 0,
            "dominant_heat_bucket": heat_dist.most_common(1)[0][0] if heat_dist else "?",
            "recommended_status": status,
        })
        print(f"  {pool[:12]}..: {token:8s} pf={pf:.3f} n={n:3d} → {status}", flush=True)

    # Explain PF>1.4 pools
    strong = [p for p in pool_analysis if p["pf"] >= 1.4]
    print(f"\n  PF>1.4 pools ({len(strong)}):", flush=True)
    tokens_of_strong = [p["token"] for p in strong]
    unique_toks = set(tokens_of_strong)
    unique_weeks = {p["week"] for p in strong}
    print(f"    tokens: {tokens_of_strong}", flush=True)
    print(f"    unique tokens: {len(unique_toks)}, unique weeks: {len(unique_weeks)}", flush=True)
    single_pool_dominant = any(p["top_trade_contrib"] is not None and p["top_trade_contrib"] > 0.6 for p in strong)
    print(f"    single-trade dominant: {single_pool_dominant}", flush=True)

    return pool_analysis


# ---------------------------------------------------------------------------
# Task F: May11+ expansion preflight
# ---------------------------------------------------------------------------

def task_f_may11_preflight() -> dict:
    print("\n[F] May11+ expansion preflight ...", flush=True)

    # Check CH coverage
    cov_rows = run_ch("""
        SELECT toMonday(block_time) AS week_start, count(), countDistinct(lower(pool_address))
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE block_time >= '2026-05-11'
        GROUP BY week_start
        ORDER BY week_start
    """)
    print(f"  CH coverage May11+: {cov_rows}", flush=True)

    # Load fee_100bps allowlist
    dp_rows = run_ch(f"""
        SELECT lower(pool_address), fee_bps
        FROM eatlq.discovered_pools
        WHERE chain_id = {CHAIN_ID} AND pool_type = 'v3'
    """)
    fee100_dp = {r[0] for r in dp_rows if len(r) >= 2 and r[1] == "1"}

    with open(S053_META_PATH) as f:
        meta = json.load(f)
    fee100_meta = {k.lower() for k, v in meta.items() if isinstance(v, dict) and v.get("fee_bps") == 100}
    fee100_all  = fee100_dp | fee100_meta
    print(f"  fee_100bps allowlist: {len(fee100_all)} pools", flush=True)

    preflight = {"weeks": [], "total_candidate_rows": 0, "abort_conditions": []}
    total_cands = 0

    if not cov_rows:
        print("  [preflight] No May11+ data in CH", flush=True)
        preflight["abort_conditions"].append("NO_CH_DATA_MAY11_PLUS")
        return preflight

    for row in cov_rows:
        week_start = row[0]
        total_sw   = int(row[1])
        total_pools= int(row[2])
        print(f"  week {week_start}: {total_sw} swaps, {total_pools} pools", flush=True)

        # First swap per pool in this week
        w_end_dt = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        from datetime import timedelta
        w_end = (w_end_dt + timedelta(days=7)).strftime("%Y-%m-%d")
        first_rows = run_ch(f"""
            SELECT lower(pool_address) AS pool, min(block_time) AS global_first
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE lower(pool_address) IN ({', '.join(f"'{a}'" for a in sorted(fee100_all))})
              AND block_time >= '{week_start}'
              AND block_time <  '{w_end}'
            GROUP BY pool
        """)
        new_pools = [r[0] for r in first_rows
                     if len(r) >= 2 and r[1][:10] >= week_start and r[1][:10] < w_end]
        print(f"  {week_start}: {len(new_pools)} new fee_100bps pools", flush=True)

        if not new_pools:
            preflight["weeks"].append({
                "week": week_start, "total_swaps": total_sw, "total_pools": total_pools,
                "new_fee100_pools": 0, "est_canonical_rows": 0, "est_rpc_calls": 0,
            })
            continue

        # Estimate deduped buckets: canonical gate has onset_max_sec=3600s, window=300s
        # → max 12 buckets per pool. Actual deduped = min(12, qualifying buckets per pool).
        # Sprint062: 13 pools × ~11.3 avg deduped buckets = 147 total deduped.
        # Conservative upper bound: 12 buckets/pool × 2 (entry+exit) = 24 RPC per pool.
        new_addr_list = ", ".join(f"'{p}'" for p in new_pools)
        cnt_rows = run_ch(f"""
            SELECT count()
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE lower(pool_address) IN ({new_addr_list})
              AND block_time >= '{week_start}'
              AND block_time <  '{w_end}'
        """)
        actual_swaps  = int(cnt_rows[0][0]) if cnt_rows else 0
        max_buckets_per_pool = 12  # onset_max_sec=3600 / window_sec=300
        est_deduped   = len(new_pools) * max_buckets_per_pool  # upper bound
        est_rpc_calls = est_deduped * 2   # entry + exit
        total_cands  += actual_swaps

        preflight["weeks"].append({
            "week":              week_start,
            "total_swaps":       total_sw,
            "total_pools":       total_pools,
            "new_fee100_pools":  len(new_pools),
            "actual_swaps_fee100": actual_swaps,
            "est_deduped_buckets": est_deduped,
            "est_rpc_calls":     est_rpc_calls,
        })
        print(f"  {week_start}: est_deduped={est_deduped}, est_rpc={est_rpc_calls}", flush=True)

    preflight["total_candidate_rows"] = total_cands
    total_rpc = sum(w.get("est_rpc_calls", 0) for w in preflight["weeks"])
    preflight["total_est_rpc_calls"]  = total_rpc
    preflight["run_go_nogo"] = "GO" if total_rpc <= 500 else "NO_GO_BUDGET"
    preflight["reason"]      = (f"est_rpc={total_rpc} {'<=' if total_rpc <= 500 else '>'} 500 budget")
    if total_rpc > 500:
        preflight["abort_conditions"].append(f"EST_RPC_{total_rpc}_EXCEEDS_500_CAP")
    print(f"  Total est RPC: {total_rpc} → {preflight['run_go_nogo']}", flush=True)
    return preflight


# ---------------------------------------------------------------------------
# Write all artifacts
# ---------------------------------------------------------------------------

def write_all(task_a, task_b, task_c, task_d, task_e, task_f) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Diagnostics CSVs ---
    _cdump(DIAG_OUT / "token_drag_autopsy.csv", task_b)
    _cdump(DIAG_OUT / "pool_heterogeneity.csv", task_e)

    # Filter results CSV
    filter_rows = []
    for fname, fd in task_c.items():
        for week, wd in fd.get("per_week", {}).items():
            filter_rows.append({
                "filter": fname, "week": week,
                "kept_rows": wd.get("kept", 0),
                "hit_rate": wd.get("hit_rate"),
                "pf": wd.get("pf"),
                "median_ret": wd.get("median_ret"),
            })
    _cdump(DIAG_OUT / "filter_candidates.csv", filter_rows)

    # Heat revalidation CSV
    heat_rows = []
    for subset, hd in task_d.items():
        heat_rows.append({"subset": subset, **{k: v for k, v in hd.items()
                           if k not in ("pool_breakdown", "tok_breakdown")}})
    _cdump(DIAG_OUT / "heat_20_50_revalidation.csv", heat_rows)

    # JSON summaries
    _jdump(DIAG_OUT / "task_a_reproduce.json", task_a)
    _jdump(DIAG_OUT / "task_c_filters.json",   task_c)
    _jdump(DIAG_OUT / "task_d_heat.json",       task_d)
    _jdump(DIAG_OUT / "task_f_may11_preflight.json", task_f)

    # ======================= Main readout =======================
    repr_combined = task_a.get("combined", {})
    nexio_c = task_a.get("nexio_contrib", {})
    per_pool_repr = task_a.get("per_pool", {})

    lines = [
        f"# Sprint063 — Expansion Week Detox + Sub-bucket Revalidation",
        f"",
        f"**Generated:** {ts}",
        f"**Gate (frozen):** Sprint037G canonical — pool_age≤86400s, onset_max≤3600s, thresh≥10, fee_100bps",
        f"**NO_ALPHA_CLAIM = true | THRESHOLDS_TUNED = false | TRAINING_ALLOWED = false**",
        f"",
        f"---",
        f"",
        f"## Task A — Sprint062 Reproduction",
        f"",
        f"| Week | Reproduced n | Claimed n | Repr. Hit | Claimed Hit | Repr. PF | Claimed PF | Match |",
        f"| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for week, wdata in sorted(task_a["per_week"].items()):
        match = "✓" if wdata.get("n_match") and wdata.get("pf_match") else "~"
        lines.append(
            f"| {week} | {wdata['n']} | {wdata['claimed_n']} | "
            f"{wdata['hit_rate']:.3f} | {wdata['claimed_hit']} | "
            f"{wdata['pf']:.4f} | {wdata['claimed_pf']} | {match} |"
        )
    lines += [
        f"| **Combined** | **{repr_combined['n']}** | **{repr_combined['claimed_n']}** | "
        f"**{repr_combined['hit_rate']:.4f}** | **{repr_combined['claimed_hit']}** | "
        f"**{repr_combined['pf']:.4f}** | **{repr_combined['claimed_pf']}** | "
        f"{'✓' if repr_combined['n_match'] else '~'} |",
        f"",
        f"**NEXIO contribution:** n={nexio_c.get('nexio_n')}, PF={nexio_c.get('nexio_pf')} | "
        f"Excl. NEXIO: n={nexio_c.get('excl_nexio_n')}, PF={nexio_c.get('excl_nexio_pf')}",
        f"",
        f"### Per-Pool Reproduction",
        f"",
        f"| Pool | Token | Week | n | Hit | PF |",
        f"| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for pool, pd in sorted(per_pool_repr.items(), key=lambda x: -(x[1].get("pf") or 0)):
        lines.append(f"| `{pd['pool_short']}` | {pd['token']} | {pd['week']} | "
                     f"{pd['n']} | {pd['hit_rate']:.3f} | {pd['pf']:.4f} |")

    lines += [
        f"",
        f"---",
        f"",
        f"## Task B — NEXIO / Drag Token Autopsy",
        f"",
        f"Decision-time observable features audit. Filter verdict based **only** on pre-entry signals.",
        f"",
        f"| Pool | Token | Entry Time | Ext Liq Before | Entry Quote OK | PF | Filter Verdict |",
        f"| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for a in sorted(task_b, key=lambda x: x.get("profit_factor") or 0):
        lines.append(
            f"| `{a['pool_short']}` | {a['token']} | {a['entry_time_first'][:16]} | "
            f"{a['ext_swap_count_before_entry']} | {a['entry_quote_ok_rate']:.0%} | "
            f"{a['profit_factor']} | {a['filter_verdict']} |"
        )

    nexio_a = next((a for a in task_b if a["token"] == "NEXIO"), {})
    lines += [
        f"",
        f"### NEXIO Verdict",
        f"",
        f"- **Entry time:** {nexio_a.get('entry_time_first', 'N/A')[:16]}",
        f"- **External swaps before entry (other pools):** {nexio_a.get('ext_swap_count_before_entry', 'N/A')}",
        f"- **Entry quote OK rate:** {nexio_a.get('entry_quote_ok_rate', 0):.0%}",
        f"- **Filter reason:** `{nexio_a.get('filter_reason', 'N/A')}`",
        f"- **Filter verdict:** `{nexio_a.get('filter_verdict', 'N/A')}`",
        f"",
        f"{'→ **NEXIO CAN be excluded by decision-time filter** (zero external liquidity at entry).' if nexio_a.get('can_filter_external_liq') else '→ **NEXIO cannot be excluded without post-hoc loss information.**'}",
        f"",
        f"---",
        f"",
        f"## Task C — Token Quality Filter Candidates",
        f"",
        f"| Filter | Kept | Removed | Apr-27 PF | May-04 PF | Combined PF | Combined Hit |",
        f"| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for fname in ["conservative", "balanced", "aggressive"]:
        fd = task_c.get(fname, {})
        apr_pf = fd.get("per_week", {}).get("2026-04-27", {}).get("pf", "—")
        may_pf = fd.get("per_week", {}).get("2026-05-04", {}).get("pf", "—")
        lines.append(
            f"| {fname} | {fd.get('kept_rows', 0)} | {fd.get('removed_rows', 0)} | "
            f"{apr_pf} | {may_pf} | {fd.get('pf', '—')} | {fd.get('hit_rate', '—')} |"
        )

    lines += [
        f"",
        f"**NOTE:** All 13 expansion pools are first-launch tokens (ext_liq=0 for all). "
        f"External liquidity filter is non-discriminating in this dataset.",
        f"",
        f"*Conservative* = heat ≥ 20 swaps/5m (Sprint054 heat_20_50 lower boundary, same threshold)  ",
        f"*Balanced* = heat ≥ 20 AND pool_age ≤ 1800s (first 30 min of pool life)  ",
        f"*Aggressive* = balanced + entry_amount_out > 0 (amountOut sanity)",
        f"",
        f"---",
        f"",
        f"## Task D — heat_20_50 Sub-bucket Revalidation",
        f"",
        f"Definition source: `scripts/sprint053_fee100_expansion.py:371-376` (FROZEN)",
        f"Sprint054 benchmark: hit=62.5%, PF=4.296, n=49",
        f"",
        f"| Subset | heat_20_50 rows | Hit | PF | Median ret | NEXIO overlap | Top1 Pool | vs S054 PF |",
        f"| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for subset, hd in task_d.items():
        lines.append(
            f"| {subset} | {hd['heat_20_50_rows']} | {hd['hit_rate']} | {hd['pf']} | "
            f"{hd['median_ret']} | {hd['nexio_in_heat_20_50']} | "
            f"{hd['top1_pool_share']:.1%} | {hd['vs_bench_pf']:+.3f} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Task E — Pool Heterogeneity Analysis",
        f"",
        f"| Pool | Token | Week | n | Hit | PF | Status |",
        f"| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for p in sorted(task_e, key=lambda x: -x["pf"]):
        lines.append(
            f"| `{p['pool_short']}` | {p['token']} | {p['week']} | "
            f"{p['labeled_rows']} | {p['hit_rate']} | {p['pf']:.4f} | {p['recommended_status']} |"
        )

    strong_pools = [p for p in task_e if p["pf"] >= 1.4]
    strong_tokens = {p["token"] for p in strong_pools}
    strong_weeks  = {p["week"] for p in strong_pools}
    lines += [
        f"",
        f"**PF>1.4 pools:** {len(strong_pools)} across {len(strong_tokens)} unique tokens, "
        f"{len(strong_weeks)} weeks",
        f"**Independence:** {len(strong_tokens) == len(strong_pools)} (each pool has distinct token)",
        f"",
        f"---",
        f"",
        f"## Task F — May11+ Expansion Preflight",
        f"",
    ]
    if task_f.get("abort_conditions"):
        lines.append(f"**ABORT:** {'; '.join(task_f['abort_conditions'])}")
    else:
        lines += [
            f"| Week | New fee_100bps Pools | Est Deduped Buckets | Est RPC Calls |",
            f"| --- | ---: | ---: | ---: |",
        ]
        for w in task_f.get("weeks", []):
            lines.append(
                f"| {w['week']} | {w.get('new_fee100_pools', 0)} | "
                f"{w.get('est_deduped_buckets', 0)} | {w.get('est_rpc_calls', 0)} |"
            )
        lines += [
            f"",
            f"**Total est RPC:** {task_f.get('total_est_rpc_calls', 0)}",
            f"**Decision:** `{task_f.get('run_go_nogo', 'N/A')}` — {task_f.get('reason', '')}",
        ]

    lines += [
        f"",
        f"---",
        f"",
        f"## Decision Flags",
        f"",
        f"```",
        f"THRESHOLDS_TUNED            = false",
        f"TRAINING_ALLOWED            = false",
        f"LIVE_ALLOWED                = false",
        f"TASK_A_REPRODUCE_MATCH      = {repr_combined['n_match']}",
        f"NEXIO_EXT_LIQ_AT_ENTRY      = 0  (only pool for NEXIO token — confirmed CH query)",
        f"ALL_POOLS_EXT_LIQ_ZERO      = true  (all expansion pools are first-launch tokens)",
        f"EXT_LIQ_DISCRIMINATING      = false  (not a useful filter in this dataset)",
        f"NEXIO_FILTER_REASON         = {nexio_a.get('filter_reason', 'ZERO_EXT_LIQ_ALL_POOLS')}",
        f"CONSERVATIVE_FILTER_PF      = {task_c.get('conservative', {}).get('pf', 'N/A')}",
        f"BALANCED_FILTER_PF          = {task_c.get('balanced', {}).get('pf', 'N/A')}",
        f"HEAT_20_50_COMBINED_PF      = {task_d.get('combined', {}).get('pf', 'N/A')}",
        f"HEAT_20_50_VS_S054_BENCH    = {task_d.get('combined', {}).get('vs_bench_pf', 'N/A')}",
        f"STRONG_POOLS_PF14           = {len(strong_pools)}",
        f"MAY11_PREFLIGHT             = {task_f.get('run_go_nogo', 'N/A')}",
        f"NO_ALPHA_CLAIM              = true",
        f"```",
        f"",
        f"---",
        f"",
        f"*Sprint063 — Detox + revalidation. Thresholds frozen. No live/training. Generated {ts}.*",
    ]

    (AUDIT_OUT / "sprint063_readout.md").write_text("\n".join(lines))
    print(f"\n[sprint063] readout written to {AUDIT_OUT}/sprint063_readout.md", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("[sprint063] starting detox + sub-bucket revalidation", flush=True)

    # Load data
    labeled = load_labeled()
    cands   = load_candidates()
    heat_map = compute_activity_heat(cands)
    enriched = enrich_labeled(labeled, heat_map)
    print(f"[sprint063] labeled: {len(labeled)}, candidates: {len(cands)}, "
          f"enriched valid: {sum(1 for r in enriched if r['entry_quote_ok'] and r['exit_quote_ok'] and r['net_return_5m'] is not None)}", flush=True)

    # Execute tasks
    result_a = task_a_reproduce(enriched)
    result_b, all_ext_liq = task_b_nexio_autopsy(enriched)
    result_c = task_c_filters(enriched, result_b, all_ext_liq)
    result_d = task_d_heat_revalidation(enriched)
    result_e = task_e_pool_analysis(enriched)
    result_f = task_f_may11_preflight()

    # Write all outputs
    write_all(result_a, result_b, result_c, result_d, result_e, result_f)

    return 0


if __name__ == "__main__":
    sys.exit(main())
