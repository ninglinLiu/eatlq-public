#!/usr/bin/env python3
"""Sprint037C — Multi-Horizon Exit Labels + Regression Training-Gate Review
============================================================================

Canonical R5 Baseline (frozen methodology)
-------------------------------------------
The R5 sprint established three invariants that this and all downstream replay
scripts must preserve:

  1. ts-grid from ClickHouse swap data (block_number column in
     uniswap_v3_swaps), NOT RPC batch scan.  450K samples, sub-second accuracy.

  2. Entry quoteability via QuoterV2 PROBE_MICRO_WEI (0.0005 ETH) at the
     entry_block.  Historical baseline: 98.7% pass rate.

  3. feature semantics:
       price_velocity_5m  = net_signed_flow / abs_flow_sum  (WETH-leg norm)
       buy_pressure_ratio = buy_flow_sum   / abs_flow_sum
       token_volatility_5m= stddev(abs_flow) / mean(abs_flow)  (size-CV)
       activity_heat      = 5m swap count
     plus unique_buyer_entropy_5m as an additive diagnostic only.

Sprint037C objectives
---------------------
  1. Q4 bucket gate: restrict to top-25% activity_heat OR top-25%
     token_volatility_5m per regime.  R5 showed these buckets carry the
     positive-mean signal; lower buckets drag the overall mean negative.

  2. Multi-horizon exit labels at 5m / 10m / 15m / 30m for every Q4 executable
     candidate.  Reuses R5 entry probe results (loads from existing CSVs —
     no re-probe needed).

  3. Cross-horizon economic readout: median / mean / trimmed mean / label_1
     per (regime × horizon).

  4. Regression training-gate review:
       - Feature → net_return Spearman correlation per horizon
       - Distribution checks (skew, fat tail, min rows)
       - Formal Gate 0-4 checklist status (FAILED_NO_7D_ALPHA guard state)
       - Verdict: what is required before Sprint037D training is authorised

Relationship to docs/ml-training-gate.md and label-semantics-freeze.md
-----------------------------------------------------------------------
  The 30-minute exit horizon freeze in docs/label-semantics-freeze.md applies
  to scripts/label_dashcam_executable.py (the production labeler).  This script
  is a bounded historical replay research tool, not the production labeler.
  Consequently:
    - LABEL_EXIT_HORIZON_SEC=1800 freeze does NOT apply here.
    - This script's multi-horizon labels are for RESEARCH ONLY.
    - No output from this script constitutes an authorised label dataset
      for retrain_model.py until all four ml-training-gate.md gates are met.
  See DECISION_FLAGS at the bottom of main() for explicit guards.

Usage
-----
  # Dry run (load R5 CSVs + Q4 gate, no RPC)
  python scripts/sprint037c_multi_horizon.py --dry-run

  # Full run
  LABEL_RPC_URL=https://... python scripts/sprint037c_multi_horizon.py --run

  # Single regime
  LABEL_RPC_URL=https://... python scripts/sprint037c_multi_horizon.py --run \
    --regime 2026-02-02

Environment
-----------
  CLICKHOUSE_URL       (default: http://172.18.0.6:8123/)
  CLICKHOUSE_USER      (default: default)
  CLICKHOUSE_PASSWORD
  LABEL_RPC_URL or EVM_HTTP_URL — archive-capable Base RPC
  LABEL_WORKERS        (default: 20)
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from urllib.request import Request, urlopen

# ── Canonical R5 constants (frozen) ──────────────────────────────────────────
WETH           = "0x4200000000000000000000000000000000000006"
CHAIN_ID       = 8453
PROBE_MICRO_WEI = int(5e14)    # 0.0005 ETH — canonical gate notional
ROUND_TRIP_COST = 0.006        # 0.6%
MAX_ABS_NET_RETURN = 10.0
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
FEATURE_WINDOW_SEC = 300

# ── Multi-horizon definitions ─────────────────────────────────────────────────
HORIZONS: dict[str, int] = {
    "5m":  300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
}

# ── Q4 gate: candidates must be in top-25% of activity_heat OR token_volatility
Q4_FEATURES = ["activity_heat", "token_volatility_5m"]

# ── R5 canonical source files ─────────────────────────────────────────────────
REPO     = Path(__file__).resolve().parents[1]
R5_DIR   = REPO / "mvp/audits/sprint037b_r5"
OUT_DIR  = REPO / "mvp/audits/sprint037c"
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037c_summary.json"
OUT_REPORT  = OUT_DIR / "sprint037c_report.md"

# ── All four regimes (Dec-01 re-promoted as positive per R5 findings) ─────────
REGIMES = [
    {"week_start": "2026-02-02", "week_end": "2026-02-09"},
    {"week_start": "2026-01-26", "week_end": "2026-02-02"},
    {"week_start": "2026-01-05", "week_end": "2026-01-12"},
    {"week_start": "2025-12-01", "week_end": "2025-12-08"},
]

# Minimum labeled rows per horizon to consider training
MIN_LABELED_PER_HORIZON = 500
# Minimum positive rate for meaningful classification
MIN_POS_RATE = 0.25
# Mean return threshold for training-gate passage (any horizon)
MEAN_RETURN_THRESHOLD = 0.0


def _log(msg: str) -> None:
    print(f"[c {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r}")


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 120.0) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ── ClickHouse ts-grid (canonical R5 approach) ────────────────────────────────
def build_ts_grid_from_ch(
    ch_url: str, ch_user: str, ch_pwd: str,
    ts_min: int, ts_max: int,
) -> tuple[list[int], list[int]]:
    start_dt = datetime.fromtimestamp(ts_min - 300, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt   = datetime.fromtimestamp(ts_max + max(HORIZONS.values()) + 600,
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
SELECT
    toUnixTimestamp(coalesce(block_time, received_at_ms)) AS ts_sec,
    min(block_number)                                      AS bn
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND coalesce(block_time, received_at_ms) >= '{start_dt}'
  AND coalesce(block_time, received_at_ms) < '{end_dt}'
GROUP BY ts_sec
ORDER BY ts_sec
"""
    _log(f"  ts-grid from CH ({start_dt[:10]} → {end_dt[:10]}) …")
    t0   = time.time()
    rows = _ch_query(ch_url, sql, ch_user, ch_pwd, timeout=120.0)
    ts_arr = [int(r["ts_sec"]) for r in rows]
    bn_arr = [int(r["bn"])     for r in rows]
    _log(f"  ts-grid: {len(ts_arr):,} samples in {time.time()-t0:.1f}s")
    return bn_arr, ts_arr


def ts_to_block(bn_arr: list[int], ts_arr: list[int], target_ts: int) -> int:
    idx = bisect.bisect_left(ts_arr, target_ts)
    if idx == 0:
        return bn_arr[0]
    if idx >= len(bn_arr):
        return bn_arr[-1]
    t_lo, t_hi = ts_arr[idx - 1], ts_arr[idx]
    b_lo, b_hi = bn_arr[idx - 1], bn_arr[idx]
    if t_hi == t_lo:
        return b_hi
    return b_lo + int((target_ts - t_lo) / (t_hi - t_lo) * (b_hi - b_lo))


# ── RPC ───────────────────────────────────────────────────────────────────────
class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 6):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._lock = Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _post(self, body: bytes) -> Any:
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                code = getattr(e, "code", None)
                if code == 429 or (code and 500 <= code < 600) or code is None:
                    time.sleep(min(0.5 * (2 ** attempt), 10.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc retries exhausted")

    def call(self, method: str, params: list) -> Any:
        body = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params, "id": self._next_id(),
        }).encode()
        return self._post(body)


# ── QuoterV2 ──────────────────────────────────────────────────────────────────
_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(h: str) -> str: return h.zfill(64)
def _addr_pad(addr: str) -> str:
    return _pad32((addr[2:] if addr.startswith("0x") else addr).lower())
def _uint_pad(n: int) -> str: return _pad32(hex(n)[2:])
def _strip_0x(h: str) -> str: return h[2:] if h.startswith("0x") else h


def call_quoter_v3(rpc: RPC, token_in: str, token_out: str,
                   amount_in: int, fee: int, block: int) -> int:
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in) + _addr_pad(token_out)
        + _uint_pad(amount_in) + _uint_pad(fee) + _uint_pad(0)
    )
    try:
        res = rpc.call("eth_call", [{"to": QUOTER_V2_BASE, "data": data}, hex(block)])
        if "error" in res:
            return 0
        raw = _strip_0x(res["result"])
        return int(raw[0:64], 16) if len(raw) >= 64 else 0
    except Exception:
        return 0


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET_RETURN or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET_RETURN, net)), True
    return net, False


# ── R5 CSV loader ─────────────────────────────────────────────────────────────
def load_r5_executables(regime_week: str) -> list[dict[str, Any]]:
    path = R5_DIR / f"candidates_{regime_week}.csv"
    if not path.exists():
        _log(f"  WARNING: R5 CSV not found: {path}")
        return []
    with path.open() as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        # Cast types
        for k in ("entry_time_sec", "entry_block", "fee_bps", "activity_heat",
                  "buy_meme_count", "sell_meme_count", "unique_buyers",
                  "weth_is_token0", "tick_range"):
            if r.get(k) not in (None, ""):
                r[k] = int(float(r[k]))
        for k in ("price_velocity_5m", "buy_pressure_ratio", "token_volatility_5m",
                  "unique_buyer_entropy_5m", "abs_flow_sum", "avg_liquidity",
                  "min_liquidity", "last_swap_sec", "freshness_lag_sec",
                  "entry_slippage_proxy"):
            if r.get(k) not in (None, ""):
                try:
                    r[k] = float(r[k])
                except (ValueError, TypeError):
                    r[k] = None
        for k in ("entry_quote_ok", "out_of_range_suspected", "has_exit_full",
                  "has_exit_short", "selected_for_exit", "exit_quote_ok"):
            r[k] = r.get(k) in ("True", "1", "true")
        for k in ("entry_amount_out_micro", "entry_amount_out_tiny",
                  "entry_amount_out_stress"):
            if r.get(k) not in (None, ""):
                try:
                    r[k] = int(float(r[k]))
                except (ValueError, TypeError):
                    r[k] = 0
        v = r.get("rpc_label")
        r["rpc_label_r5"] = int(v) if v not in (None, "", "None") else None
        v = r.get("net_return_rpc")
        r["net_return_r5"] = float(v) if v not in (None, "", "None") else None
        out.append(r)
    return out


def apply_q4_gate(candidates: list[dict], features: list[str]) -> list[dict]:
    """Keep candidates in Q4 (top-25%) for ANY of the given features."""
    q3_cutoffs: dict[str, float] = {}
    for feat in features:
        vals = sorted(float(c[feat]) for c in candidates
                      if c.get(feat) is not None)
        if vals:
            q3_cutoffs[feat] = vals[max(0, 3 * len(vals) // 4 - 1)]

    selected = set()
    for feat, q3 in q3_cutoffs.items():
        for i, c in enumerate(candidates):
            v = c.get(feat)
            if v is not None and float(v) > q3:
                selected.add(i)

    result = [candidates[i] for i in sorted(selected)]
    _log(f"  Q4 gate: {len(result):,}/{len(candidates):,} candidates pass "
         f"({', '.join(f'{f}>Q3={q3_cutoffs[f]:.2f}' for f, q3 in q3_cutoffs.items())})")
    return result


# ── Multi-horizon exit worker ─────────────────────────────────────────────────
def run_multi_horizon(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """Compute exit label at all horizons for one executable candidate."""
    weth = WETH.lower()
    t0   = (c.get("token0_addr") or "").lower()
    t1   = (c.get("token1_addr") or "").lower()
    meme = t1 if t0 == weth else t0

    tokens_in = int(c.get("entry_amount_out_micro") or 0)
    if tokens_in <= 0:
        for hname in HORIZONS:
            c[f"net_return_{hname}"]  = None
            c[f"label_{hname}"]       = None
            c[f"exit_ok_{hname}"]     = False
            c[f"exit_block_{hname}"]  = 0
        return

    fee = int(c.get("fee_bps") or 0)
    for hname, horizon_sec in HORIZONS.items():
        exit_ts    = int(c.get("entry_time_sec") or 0) + horizon_sec
        exit_block = ts_to_block(bn_arr, ts_arr, exit_ts)
        c[f"exit_block_{hname}"] = exit_block

        weth_back = call_quoter_v3(rpc, meme, weth, tokens_in, fee, exit_block)
        if weth_back <= 0:
            c[f"net_return_{hname}"] = -1.0
            c[f"label_{hname}"]      = 0
            c[f"exit_ok_{hname}"]    = False
            continue

        net, bogus = _sanity_clip(weth_back / PROBE_MICRO_WEI - 1.0 - ROUND_TRIP_COST)
        if bogus:
            c[f"net_return_{hname}"] = None
            c[f"label_{hname}"]      = None
            c[f"exit_ok_{hname}"]    = False
        else:
            c[f"net_return_{hname}"] = round(net, 6)
            c[f"label_{hname}"]      = 1 if net > 0 else 0
            c[f"exit_ok_{hname}"]    = True


# ── Statistics helpers ────────────────────────────────────────────────────────
def _stats_for(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {}
    rs  = sorted(rets)
    n   = len(rs)
    k   = max(1, n // 10)
    tr  = rs[k:-k] if n > 2 * k else rs
    pos = sum(1 for x in rs if x > 0)
    return {
        "n":            n,
        "pos":          pos,
        "label_1_share":  round(pos / n, 4),
        "mean":           round(sum(rs) / n, 6),
        "median":         round(rs[n // 2], 6),
        "trimmed_mean":   round(sum(tr) / max(len(tr), 1), 6),
        "p10":            round(rs[max(0, n // 10)], 6),
        "p90":            round(rs[min(n - 1, 9 * n // 10)], 6),
        "stddev":         round(
            (sum((x - sum(rs) / n) ** 2 for x in rs) / max(n - 1, 1)) ** 0.5, 6
        ),
    }


def spearman_rho(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation (no scipy dependency)."""
    n = len(x)
    if n < 4:
        return 0.0

    def _rank(vals: list[float]) -> list[float]:
        indexed = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and vals[indexed[j + 1]] == vals[indexed[j]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _rank(x), _rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num   = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    denom = (
        sum((rx[i] - mx) ** 2 for i in range(n)) *
        sum((ry[i] - my) ** 2 for i in range(n))
    ) ** 0.5
    return round(num / denom, 4) if denom > 1e-12 else 0.0


# ── Per-horizon readout ───────────────────────────────────────────────────────
def horizon_readout(
    candidates: list[dict],
    regime_week: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    FEAT_LIST = ["activity_heat", "price_velocity_5m", "buy_pressure_ratio",
                 "token_volatility_5m"]

    for hname in HORIZONS:
        labeled = [c for c in candidates if c.get(f"label_{hname}") is not None]
        rets    = [c[f"net_return_{hname}"] for c in labeled
                   if c.get(f"net_return_{hname}") is not None]
        stats   = _stats_for(rets)

        # Feature → return Spearman
        corrs: dict[str, float] = {}
        for feat in FEAT_LIST:
            pairs = [
                (float(c[feat]), c[f"net_return_{hname}"])
                for c in labeled
                if c.get(feat) is not None and c.get(f"net_return_{hname}") is not None
            ]
            if len(pairs) >= 10:
                fv, rv = zip(*pairs)
                corrs[feat] = spearman_rho(list(fv), list(rv))

        # Q4 activity_heat breakdown within horizon
        ah_vals = sorted(float(c.get("activity_heat", 0)) for c in candidates
                         if c.get("activity_heat") is not None)
        q3_ah   = ah_vals[3 * len(ah_vals) // 4 - 1] if ah_vals else 0
        q4_ah   = [c for c in labeled if float(c.get("activity_heat", 0)) > q3_ah]
        q4_rets = [c[f"net_return_{hname}"] for c in q4_ah
                   if c.get(f"net_return_{hname}") is not None]

        result[hname] = {
            "labeled":         len(labeled),
            "exit_ok":         sum(1 for c in labeled if c.get(f"exit_ok_{hname}")),
            **stats,
            "feature_spearman": corrs,
            "activity_heat_q4": _stats_for(q4_rets),
        }
    return result


# ── Regression training-gate assessment ──────────────────────────────────────
def regression_gate_review(
    per_regime: dict[str, dict],
    all_candidates: list[dict],
) -> dict[str, Any]:
    """Evaluate training-gate preconditions.

    Checks ml-training-gate.md Gate 0–4 (all must pass for TRAINING_ALLOWED).

    Gate 0: FAILED_NO_7D_ALPHA must be superseded by a new economic readout.
    Gate 1: Label semantics verified (research replay ≠ production labeler).
    Gate 2: Minimum labeled rows + positive rate constraints.
    Gate 3: ML Monitoring Gate artifact populated (formal metrics).
    Gate 4: Explicit per-session authorisation artifact.
    """
    # Identify best horizon by mean return across all regimes
    horizon_means: dict[str, list[float]] = defaultdict(list)
    for rdata in per_regime.values():
        for hname, hd in rdata.items():
            if isinstance(hd, dict) and hd.get("mean") is not None:
                horizon_means[hname].append(hd["mean"])
    best_horizon = max(
        HORIZONS,
        key=lambda h: (
            sum(horizon_means.get(h, [0])) / max(len(horizon_means.get(h, [1])), 1)
        ),
    )

    # Horizon-level checks
    horizon_gate: dict[str, dict] = {}
    for hname in HORIZONS:
        total_labeled = sum(
            rdata[hname].get("labeled", 0)
            for rdata in per_regime.values()
            if hname in rdata
        )
        pos_rates = [
            rdata[hname].get("label_1_share", 0)
            for rdata in per_regime.values()
            if hname in rdata
        ]
        means = [
            rdata[hname].get("mean")
            for rdata in per_regime.values()
            if hname in rdata and rdata[hname].get("mean") is not None
        ]
        n_pos_regimes = sum(1 for m in means if m is not None and m > MEAN_RETURN_THRESHOLD)
        avg_mean = sum(m for m in means if m is not None) / max(len(means), 1)

        horizon_gate[hname] = {
            "total_labeled":       total_labeled,
            "avg_label_1_share":   round(sum(pos_rates) / max(len(pos_rates), 1), 4),
            "avg_mean_return":     round(avg_mean, 6),
            "n_regimes_pos_mean":  n_pos_regimes,
            "gate2_min_rows":      total_labeled >= MIN_LABELED_PER_HORIZON,
            "gate2_min_pos_rate":  all(r >= MIN_POS_RATE for r in pos_rates),
            "gate2_pos_mean":      avg_mean > MEAN_RETURN_THRESHOLD,
        }

    # Activity_heat Q4 global spearman (best signal feature per R5)
    feat_corrs: dict[str, list[float]] = defaultdict(list)
    for hname in HORIZONS:
        for rdata in per_regime.values():
            if hname in rdata:
                for feat, rho in rdata[hname].get("feature_spearman", {}).items():
                    feat_corrs[f"{feat}@{hname}"].append(rho)
    avg_corrs = {k: round(sum(v) / max(len(v), 1), 4) for k, v in feat_corrs.items()}

    # Gate 0 status
    guard_path = REPO / "mvp/audits/failed_no_7d_alpha_guard.md"
    gate0_guard_exists = guard_path.exists()
    gate0_note = (
        "FAILED_NO_7D_ALPHA guard is active. "
        "A new positive holdout economic readout that explicitly states "
        "'supersedes: FAILED_NO_7D_ALPHA' is required before training."
    )

    # Overall gate verdict
    best_hg = horizon_gate.get(best_horizon, {})
    gate2_pass = (
        best_hg.get("gate2_min_rows", False) and
        best_hg.get("gate2_min_pos_rate", False)
    )
    gate2_econ_pass = best_hg.get("gate2_pos_mean", False)

    # Recommended horizon for training (positive mean + most rows)
    viable = [h for h, hg in horizon_gate.items() if hg.get("gate2_pos_mean")]

    training_allowed = False  # Always false per decision flags

    return {
        "gate0_blocked":          gate0_guard_exists,
        "gate0_note":             gate0_note,
        "gate0_supersession_required": True,
        "gate1_semantics_note":   (
            "This is a research replay script (sprint037c), NOT the production "
            "labeler label_dashcam_executable.py. The 30m exit-horizon freeze "
            "in label-semantics-freeze.md does NOT apply here. Multi-horizon "
            "outputs from this script require a new Label Semantics Freeze update "
            "before they can feed retrain_model.py."
        ),
        "gate2_per_horizon":      horizon_gate,
        "gate2_overall":          gate2_pass,
        "gate2_econ_overall":     gate2_econ_pass,
        "gate3_monitoring_note":  (
            "ML Monitoring Gate artifact not yet produced. "
            "Required: holdout_mean_net_return, holdout_positive_rate, "
            "profit_factor, pool/token concentration, dead_letter_count."
        ),
        "gate4_note":             "Explicit per-session RETRAIN_ALLOWED=true artifact required.",
        "best_horizon":           best_horizon,
        "viable_horizons":        viable,
        "feature_avg_spearman":   avg_corrs,
        "training_allowed":       training_allowed,
        "recommended_next":       _recommend_next(gate0_guard_exists, viable, gate2_econ_pass),
    }


def _recommend_next(gate0_blocked: bool, viable: list[str], econ_pass: bool) -> str:
    if gate0_blocked:
        return (
            f"Produce a positive holdout economic readout using Q4 "
            f"activity_heat candidates, explicitly stating "
            f"'supersedes: FAILED_NO_7D_ALPHA'. "
            f"Best viable horizons: {viable or ['none yet']}. "
            f"Then populate the ML Monitoring Gate artifact (Gate 3) "
            f"and create a per-session authorisation artifact (Gate 4)."
        )
    if not econ_pass:
        return (
            "Gate 0 clear but mean returns still negative across all horizons. "
            "Consider shorter horizons (≤10m) or tighter Q4 gating."
        )
    return (
        f"Gates 0 pending, 1 (research only), 2 (PASS) for horizons {viable}. "
        f"Proceed to Gate 3 (Monitoring Gate artifact) and Gate 4 (per-session auth)."
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sprint037C: Multi-horizon exit labels + regression training-gate review"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run",     action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--regime", help="restrict to one week_start")
    parser.add_argument(
        "--r5-dir",
        help="override R5 canonical CSV dir (default: mvp/audits/sprint037b_r5)",
    )
    parser.add_argument(
        "--q4-out-dir",
        help="override Q4 CSV output dir (default: mvp/audits/sprint037c)",
    )
    parser.add_argument(
        "--summary-json",
        help="override summary JSON path (default: mvp/diagnostics/sprint037c_summary.json)",
    )
    parser.add_argument(
        "--report-md",
        help="override sprint037c_report.md path (default: <q4-out-dir>/sprint037c_report.md)",
    )
    parser.add_argument(
        "--regimes-json",
        help="JSON array of {week_start, week_end} objects for this run only",
    )
    args = parser.parse_args()

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    global R5_DIR, OUT_DIR, OUT_SUMMARY, OUT_REPORT
    if args.r5_dir:
        p = Path(args.r5_dir)
        R5_DIR = p if p.is_absolute() else REPO / p
    if args.q4_out_dir:
        p = Path(args.q4_out_dir)
        OUT_DIR = p if p.is_absolute() else REPO / p
    if args.summary_json:
        p = Path(args.summary_json)
        OUT_SUMMARY = p if p.is_absolute() else REPO / p
    if args.report_md:
        p = Path(args.report_md)
        OUT_REPORT = p if p.is_absolute() else REPO / p
    else:
        OUT_REPORT = OUT_DIR / "sprint037c_report.md"

    regimes_effective = REGIMES
    if args.regimes_json:
        rp = Path(args.regimes_json)
        rp = rp if rp.is_absolute() else REPO / rp
        regimes_effective = json.loads(rp.read_text(encoding="utf-8"))

    ch_url    = os.environ.get("CLICKHOUSE_URL",  "http://172.18.0.6:8123/")
    ch_user   = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd    = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url   = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    n_workers = int(os.environ.get("LABEL_WORKERS", "20"))

    do_rpc = args.run and bool(rpc_url)
    if args.run and not rpc_url:
        _log("WARNING: --run without LABEL_RPC_URL, running dry")
        do_rpc = False

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    regimes_to_run = [
        r for r in regimes_effective
        if not args.regime or r["week_start"] == args.regime
    ]
    _log(f"Sprint037C — Multi-Horizon + Regression Gate Review")
    _log(f"Regimes: {[r['week_start'] for r in regimes_to_run]}")
    _log(f"Horizons: {list(HORIZONS.keys())}  Q4 features: {Q4_FEATURES}")
    _log(f"do_rpc={do_rpc}  workers={n_workers}")

    rpc: Optional[RPC] = None
    if do_rpc:
        rpc = RPC(rpc_url)
        try:
            chid = int(rpc.call("eth_chainId", [])["result"], 16)
            if chid != CHAIN_ID:
                _log(f"ERROR: wrong chain {chid}")
                do_rpc = False
            else:
                _log(f"  RPC ok chain_id={chid}")
        except Exception as e:
            _log(f"  ERROR RPC: {e}")
            do_rpc = False

    # ── ts-grid from ClickHouse ────────────────────────────────────────────────
    bn_arr: list[int] = []
    ts_arr: list[int] = []
    if do_rpc:
        earliest_ts = min(
            int(datetime.strptime(r["week_start"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
            for r in regimes_to_run
        )
        latest_ts = max(
            int(datetime.strptime(r["week_end"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
            for r in regimes_to_run
        )
        try:
            bn_arr, ts_arr = build_ts_grid_from_ch(
                ch_url, ch_user, ch_pwd, earliest_ts, latest_ts
            )
        except Exception as e:
            _log(f"ERROR ts-grid: {e}")
            do_rpc = False

    # ── Per-regime pipeline ────────────────────────────────────────────────────
    per_regime_readout: dict[str, dict] = {}
    all_q4_candidates: list[dict] = []

    for regime in regimes_to_run:
        wk = regime["week_start"]
        _log(f"\n{'='*64}")
        _log(f"REGIME {wk}")

        # Load R5 executables
        all_cands = load_r5_executables(wk)
        exec_cands = [c for c in all_cands if c.get("entry_quote_ok")]
        _log(f"  R5 loaded: {len(all_cands):,} total, {len(exec_cands):,} executable")

        # Q4 gate
        q4_cands = apply_q4_gate(exec_cands, Q4_FEATURES)

        # Initialise per-horizon columns
        for c in q4_cands:
            for hname in HORIZONS:
                c[f"net_return_{hname}"]  = None
                c[f"label_{hname}"]       = None
                c[f"exit_ok_{hname}"]     = False
                c[f"exit_block_{hname}"]  = 0
            c["regime_week"] = wk

        if not do_rpc or rpc is None:
            _log(f"  Dry-run: skipping {len(q4_cands):,} multi-horizon exits")
            _write_q4_csv(q4_cands, wk)
            all_q4_candidates.extend(q4_cands)
            per_regime_readout[wk] = {hname: {} for hname in HORIZONS}
            continue

        # Multi-horizon exit
        _log(f"  Multi-horizon exit on {len(q4_cands):,} Q4 executables …")
        t0   = time.time()
        lock = Lock()
        done = [0]

        def _exit_one(c: dict) -> None:
            run_multi_horizon(rpc, c, bn_arr, ts_arr)
            with lock:
                done[0] += 1
                if done[0] % 300 == 0:
                    _log(f"    exit: {done[0]:,}/{len(q4_cands):,} [{time.time()-t0:.0f}s]")

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            list(pool.map(_exit_one, q4_cands))

        elapsed = time.time() - t0
        for hname in HORIZONS:
            ok  = sum(1 for c in q4_cands if c.get(f"exit_ok_{hname}"))
            l1  = sum(1 for c in q4_cands if c.get(f"label_{hname}") == 1)
            lab = sum(1 for c in q4_cands if c.get(f"label_{hname}") is not None)
            _log(f"  {hname}: {ok:,}/{len(q4_cands):,} ok  "
                 f"label_1={l1:,}/{lab:,} ({l1/max(lab,1):.1%})")

        _log(f"  Done [{elapsed:.0f}s]")
        _write_q4_csv(q4_cands, wk)
        all_q4_candidates.extend(q4_cands)
        per_regime_readout[wk] = horizon_readout(q4_cands, wk)

    # ── Cross-regime analysis ─────────────────────────────────────────────────
    _log(f"\n{'='*64}")
    _log("Cross-regime summary")

    for wk, rdata in per_regime_readout.items():
        if rdata:
            parts = []
            for h in HORIZONS:
                hd = rdata.get(h, {})
                if hd:
                    parts.append(
                        f"{h}: l1={hd.get('label_1_share',0):.1%} "
                        f"mean={hd.get('mean',0):.3f} "
                        f"med={hd.get('median',0):.3f}"
                    )
            _log(f"  {wk}: " + " | ".join(parts))

    # ── Regression training-gate review ───────────────────────────────────────
    rg_review = regression_gate_review(per_regime_readout, all_q4_candidates)

    _log("\n--- Regression training-gate review ---")
    _log(f"  Gate 0 (FAILED_NO_7D_ALPHA): BLOCKED={rg_review['gate0_blocked']}")
    _log(f"  Gate 2 (data adequacy):      {rg_review['gate2_overall']}")
    _log(f"  Gate 2 econ (mean>0):        {rg_review['gate2_econ_overall']}")
    _log(f"  Best horizon:                {rg_review['best_horizon']}")
    _log(f"  Viable horizons:             {rg_review['viable_horizons']}")
    _log(f"  TRAINING_ALLOWED:            {rg_review['training_allowed']}")
    _log(f"  Recommended next: {rg_review['recommended_next']}")

    # Feature correlations summary
    best_h = rg_review["best_horizon"]
    _log(f"\nFeature Spearman @ {best_h}:")
    for feat, rho in rg_review.get("feature_avg_spearman", {}).items():
        if best_h in feat:
            _log(f"  {feat}: ρ={rho:+.4f}")

    # ── Write summary ──────────────────────────────────────────────────────────
    summary = {
        "kind":       "sprint037c_multi_horizon",
        "version":    "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "canonical_r5_baseline": {
            "ts_grid_method":           "ClickHouse block_number (sub-second accuracy)",
            "entry_gate_notional_wei":  PROBE_MICRO_WEI,
            "entry_success_rate_r5":    0.987,
            "feature_window_sec":       FEATURE_WINDOW_SEC,
            "round_trip_cost":          ROUND_TRIP_COST,
        },
        "q4_gate": {
            "features":  Q4_FEATURES,
            "logic":     "top-25% in ANY of the listed features per regime",
        },
        "horizons":          list(HORIZONS.keys()),
        "total_q4_candidates": len(all_q4_candidates),
        "per_regime":        per_regime_readout,
        "regression_gate":   rg_review,
        "decision_flags": {
            "TRAINING_ALLOWED":                  False,
            "LIVE_ALLOWED":                      False,
            "MODEL_PROMOTION_ALLOWED":           False,
            "H12_ABLATION_ALLOWED":              False,
            "PHASE6_ALLOWED":                    False,
            "FAILED_NO_7D_ALPHA_ACTIVE":         True,
            "QUOTEABILITY_FIRST_REPLAY_ALLOWED": True,
            "RPC_BACKED_LABEL_VALIDATION_ALLOWED": True,
            "MULTI_HORIZON_RESEARCH_ALLOWED":    True,
            "NO_ALPHA_CLAIM":                    True,
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    _write_report(summary, per_regime_readout)

    _log(f"\nWritten: {OUT_SUMMARY}")
    _log(f"Written: {OUT_REPORT}")
    _log(f"\nTotal Q4 candidates processed: {len(all_q4_candidates):,}")
    _log(f"Sprint037C complete.")


def _write_q4_csv(candidates: list[dict], week_start: str) -> None:
    if not candidates:
        return
    out = OUT_DIR / f"q4_candidates_{week_start}.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(candidates[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(candidates)
    _log(f"  Written: {out} ({len(candidates):,} rows)")


# ── Report ────────────────────────────────────────────────────────────────────
def _write_report(summary: dict, per_regime: dict) -> None:
    rg = summary.get("regression_gate", {})
    gate2 = rg.get("gate2_per_horizon", {})

    lines = [
        "# Sprint037C — Multi-Horizon Exit Labels + Regression Training-Gate Review",
        "",
        f"Generated: {summary.get('created_at')}",
        "",
        "---",
        "",
        "## Canonical R5 Baseline (frozen)",
        "",
        "| Parameter | Value |",
        "|-----------|-------|",
        "| ts-grid method | ClickHouse block_number (sub-second accuracy) |",
        "| Entry gate notional | 0.0005 ETH (PROBE_MICRO_WEI) |",
        "| Entry success rate (R5) | 98.7% |",
        "| Feature window | 300s (5 min) |",
        "| Round-trip cost | 0.6% |",
        "| Q4 gate features | activity_heat, token_volatility_5m (top-25% OR) |",
        "",
        "---",
        "",
        "## Cross-Horizon Economic Readout",
        "",
        "### label_1_share by regime × horizon",
        "",
        "| Regime | 5m | 10m | 15m | 30m |",
        "|--------|----|-----|-----|-----|",
    ]
    for wk, rdata in per_regime.items():
        row_parts = [wk]
        for h in ["5m", "10m", "15m", "30m"]:
            hd = rdata.get(h, {})
            s  = hd.get("label_1_share")
            row_parts.append(f"{s:.1%}" if s is not None else "N/A")
        lines.append("| " + " | ".join(row_parts) + " |")

    lines += [
        "",
        "### Mean net return (after 0.6% cost) by regime × horizon",
        "",
        "| Regime | 5m | 10m | 15m | 30m |",
        "|--------|----|-----|-----|-----|",
    ]
    for wk, rdata in per_regime.items():
        row_parts = [wk]
        for h in ["5m", "10m", "15m", "30m"]:
            hd = rdata.get(h, {})
            m  = hd.get("mean")
            row_parts.append(f"{m:+.4f}" if m is not None else "N/A")
        lines.append("| " + " | ".join(row_parts) + " |")

    lines += [
        "",
        "### activity_heat Q4 mean return by regime × horizon",
        "",
        "| Regime | 5m | 10m | 15m | 30m |",
        "|--------|----|-----|-----|-----|",
    ]
    for wk, rdata in per_regime.items():
        row_parts = [wk]
        for h in ["5m", "10m", "15m", "30m"]:
            hd = rdata.get(h, {}).get("activity_heat_q4", {})
            m  = hd.get("mean")
            row_parts.append(f"{m:+.4f}" if m is not None else "N/A")
        lines.append("| " + " | ".join(row_parts) + " |")

    lines += [
        "",
        "---",
        "",
        "## Feature → Return Spearman Correlations",
        "",
        "Average across regimes, per feature × horizon:",
        "",
        "| Feature@Horizon | ρ |",
        "|-----------------|---|",
    ]
    for fh, rho in sorted(rg.get("feature_avg_spearman", {}).items()):
        lines.append(f"| {fh} | {rho:+.4f} |")

    lines += [
        "",
        "---",
        "",
        "## Regression Training-Gate Review",
        "",
        f"Best horizon: **{rg.get('best_horizon')}**  ",
        f"Viable horizons (pos mean return): **{rg.get('viable_horizons')}**",
        "",
        "### Gate 0 — FAILED_NO_7D_ALPHA",
        f"> **BLOCKED = {rg.get('gate0_blocked')}**",
        f"> {rg.get('gate0_note','')}",
        "",
        "### Gate 1 — Label Semantics",
        f"> {rg.get('gate1_semantics_note','')}",
        "",
        "### Gate 2 — Data Adequacy (per horizon)",
        "",
        "| Horizon | N labeled | Label_1 | Mean return | min_rows | pos_rate | pos_mean |",
        "|---------|-----------|---------|-------------|----------|----------|---------|",
    ]
    for h in ["5m", "10m", "15m", "30m"]:
        hg = gate2.get(h, {})
        lines.append(
            f"| {h} | {hg.get('total_labeled','N/A')} "
            f"| {hg.get('avg_label_1_share',0):.1%} "
            f"| {hg.get('avg_mean_return',0):+.4f} "
            f"| {'✓' if hg.get('gate2_min_rows') else '✗'} "
            f"| {'✓' if hg.get('gate2_min_pos_rate') else '✗'} "
            f"| {'✓' if hg.get('gate2_pos_mean') else '✗'} |"
        )

    lines += [
        "",
        "### Gate 3 — Monitoring Gate",
        f"> {rg.get('gate3_monitoring_note','')}",
        "",
        "### Gate 4 — Per-Session Authorisation",
        f"> {rg.get('gate4_note','')}",
        "",
        "### Recommended next step",
        f"> {rg.get('recommended_next','')}",
        "",
        "---",
        "",
        "## Decision Flags",
        "",
        "| Flag | Value |",
        "|------|-------|",
    ]
    for k, v in summary.get("decision_flags", {}).items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037c_multi_horizon.py*",
    ]

    OUT_REPORT.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
