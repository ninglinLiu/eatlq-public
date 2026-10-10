#!/usr/bin/env python3
"""Sprint035F — short-horizon and breakout-anchor diagnostic.

Purpose
───────
Sprint035E produced 0% label_1 despite selecting momentum-positive pools.
Post-mortem revealed:
  • All 3 pools had h1 only 0.2–0.7% at discovery (BSR-driven score, not price momentum)
  • CLANKER/WETH had h6 = −15.9% (deep in recovery / downtrend)
  • 4h synthetic entries land in post-pump reversal territory

This script runs THREE diagnostic dimensions on the same 035E pools,
WITHOUT re-running large-scale discovery or changing main label semantics:

  Dim-1  Multi-horizon OHLCV approximate diagnostic
         Re-use 035E manifest entries; compute ≈net_return at 5 m / 15 m / 30 m
         using GeckoTerminal 5 m candle prices (not RPC quoter).
         Clearly marked OHLCV_APPROX — diagnostic only.

  Dim-2  Breakout-anchor diagnostic (RPC, 30 m main semantics)
         Find the candle where each pool's rolling-1h return first crosses
         +2 % from below (within last 24 h).  Use those candles as entry
         timestamps.  Run bounded RPC labels at 30 m horizon — same
         semantics as official labels.

  Dim-3  Temporal decay readout
         Split 035E entries into first-half (oldest) vs second-half (newest),
         compare label_1 shares.  Tests whether entries closer to discovery
         (≤2 h ago) perform worse than older ones (≥5 h ago).

Red lines (never crossed):
  • No training / no H12 / no live / no model promotion
  • OHLCV results are NOT official alpha proof
  • Breakout RPC labels use main semantics (LABEL_EXIT_HORIZON_SEC=1800)
    and count toward no-live gate, not proof-of-alpha
  • No large-scale re-discovery
  • No secrets / dashcam.csv committed

Usage:
  python3 scripts/sprint035f_short_horizon_breakout_diagnostic.py --run
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
BUCKET_SEC = 300
ROUND_TRIP_COST = 0.006
LABEL_NOTIONAL_WEI = int(0.0005e18)
LABEL_EXIT_HORIZON_SEC = 1800  # main semantics — NEVER change for official labels

# ─── OHLCV fetch (up to 24h = 288 × 5m candles) ─────────────────────────────
MAX_HTTP = 30  # 3 pools × ~3 calls each + buffer
GECKO_OHLCV_24H = (
    "https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/minute"
    "?aggregate=5&limit=288"
)

http_req_used: int = 0


def http_get_json(url: str, timeout: float = 25.0) -> dict[str, Any]:
    global http_req_used
    if http_req_used >= MAX_HTTP:
        return {}
    http_req_used += 1
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035F-diag/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except OSError:
        return {}


# ─── 035E artifact paths ──────────────────────────────────────────────────────
E_MANIFEST  = REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_manifest.json"
E_LABELS    = REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_labels.csv"
E_CAND      = REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_candidate_summary.csv"

# ─── output paths ────────────────────────────────────────────────────────────
OUT_OHLCV_DIAG      = REPO / "mvp/diagnostics/sprint035f_ohlcv_multihorizon_diagnostic.csv"
OUT_BREAKOUT_MAN    = REPO / "mvp/diagnostics/sprint035f_breakout_anchor_manifest.json"
OUT_BREAKOUT_LABELS = REPO / "mvp/diagnostics/sprint035f_breakout_anchor_labels.csv"
OUT_COMPARISON      = REPO / "mvp/diagnostics/sprint035f_horizon_comparison.json"
OUT_REPORT          = REPO / "mvp/audits/sprint035f_short_horizon_breakout_diagnostic_report.md"


# ─── module loaders ──────────────────────────────────────────────────────────

def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def load_m33() -> Any:
    return _load_module("sprint033_gate", REPO / "scripts/sprint033a_ch_backed_hard_quota_pipeline.py")


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        fp = REPO / rel
        if not fp.is_file():
            continue
        for line in fp.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ─── OHLCV helpers ───────────────────────────────────────────────────────────

def fetch_ohlcv_24h(pool: str) -> list[dict[str, Any]]:
    """Fetch up to 24 h of 5-min candles from GeckoTerminal.

    Returns list of dicts: ts_sec, open_p, high_p, low_p, close_p, volume.
    """
    url  = GECKO_OHLCV_24H.format(pool=pool.lower())
    blob = http_get_json(url)
    ohlcv = (blob.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = []
    for c in ohlcv:
        try:
            ts_raw = int(float(c[0]))
            ts_sec = (ts_raw // BUCKET_SEC) * BUCKET_SEC  # snap to 5 m boundary
            out.append({
                "ts_sec":  ts_sec,
                "open_p":  float(c[1]),
                "high_p":  float(c[2]),
                "low_p":   float(c[3]),
                "close_p": float(c[4]),
                "volume":  float(c[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["ts_sec"])


def candles_by_ts(candles: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {c["ts_sec"]: c for c in candles}


# ─── Dim-1: Multi-horizon OHLCV approximate diagnostic ───────────────────────

def ohlcv_multi_horizon(
    pool: str,
    manifest_rows: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    pool_label: str = "",
) -> list[dict[str, Any]]:
    """For each manifest row, compute approx net_return at 5 m / 15 m / 30 m.

    Entry price:  open of the bucket candle (conservative — ignores 2-min lag).
    Exit price:   close of the candle at (entry_ts + horizon).
    Round-trip:   0.6 % applied to all horizons (same as main label semantics).

    NOT official label — candle OHLCV ≠ quoter-simulated price.
    Use only for diagnostic comparison, never as alpha proof.
    """
    cbt = candles_by_ts(candles)
    results: list[dict[str, Any]] = []

    for row in manifest_rows:
        if row.get("pool_audit_only", "").lower() != pool.lower():
            continue
        sig_ms  = row["signal_ts_ms_audit_only"]
        bkt_sec = row["bucket_sec"]          # should equal sig_ms // 1000 // 300 * 300
        entry_candle = cbt.get(bkt_sec)
        if entry_candle is None or entry_candle["open_p"] <= 0:
            continue
        entry_p = entry_candle["open_p"]

        rec: dict[str, Any] = {
            "pool": pool[:14],
            "pool_label": pool_label,
            "bucket_sec": bkt_sec,
            "signal_ts_ms": sig_ms,
            "entry_price": round(entry_p, 12),
            "candle_open": round(entry_candle["open_p"], 12),
            "candle_close": round(entry_candle["close_p"], 12),
        }

        for h_name, n_candles in [("h5m", 1), ("h15m", 3), ("h30m", 6)]:
            exit_ts = bkt_sec + n_candles * BUCKET_SEC
            exit_candle = cbt.get(exit_ts)
            if exit_candle is None or exit_candle["close_p"] <= 0:
                rec[f"net_{h_name}"]   = None
                rec[f"label_{h_name}"] = None
            else:
                net = (exit_candle["close_p"] - entry_p) / entry_p - ROUND_TRIP_COST
                rec[f"net_{h_name}"]   = round(net, 6)
                rec[f"label_{h_name}"] = 1 if net > 0 else 0

        results.append(rec)

    return results


# ─── Dim-2: Breakout-anchor detection ────────────────────────────────────────

def find_breakout_candles(
    candles: list[dict[str, Any]],
    h1_window: int = 12,          # 12 × 5 m = 1 h
    threshold_pct: float = 2.0,   # h1 return must cross this
    max_breakouts: int = 3,
    min_age_sec: int = 2100,       # must be older than exit horizon + margin
) -> list[dict[str, Any]]:
    """Find candles where rolling-h1_window return crosses threshold_pct from below.

    Returns up to max_breakouts breakout candles, ordered most recent first.

    A breakout is: r(t-1) < threshold AND r(t) >= threshold
    where r(t) = (close[t] - open[t-h1_window]) / open[t-h1_window] * 100.
    """
    now_sec = int(time.time())
    candles_sorted = sorted(candles, key=lambda x: x["ts_sec"])
    breakouts: list[dict[str, Any]] = []

    for i in range(h1_window + 1, len(candles_sorted)):
        c_now  = candles_sorted[i]
        c_prev = candles_sorted[i - 1]
        c_from_now  = candles_sorted[i - h1_window]
        c_from_prev = candles_sorted[i - h1_window - 1]

        # Age guard: must be old enough for a 30-min exit to be fully in the past
        if now_sec - c_now["ts_sec"] < min_age_sec:
            continue

        op_now  = c_from_now["open_p"]
        op_prev = c_from_prev["open_p"]
        if op_now <= 0 or op_prev <= 0:
            continue

        r_now  = (c_now["close_p"]  - op_now)  / op_now  * 100.0
        r_prev = (c_prev["close_p"] - op_prev) / op_prev * 100.0

        if r_prev < threshold_pct <= r_now:
            breakouts.append({
                "ts_sec":        c_now["ts_sec"],
                "h1_return_pct": round(r_now, 3),
                "close_p":       c_now["close_p"],
                "open_p":        c_now["open_p"],
                "is_bullish":    c_now["close_p"] > c_now["open_p"],
            })

    # Return most recent first
    breakouts.sort(key=lambda x: -x["ts_sec"])
    return breakouts[:max_breakouts]


def build_breakout_manifest(
    pools: list[str],
    pool_breakouts: dict[str, list[dict[str, Any]]],
    pool_labels: dict[str, str],
) -> list[dict[str, Any]]:
    """Build a small manifest from breakout candle timestamps.

    Each breakout candle → one manifest row (and optionally 1–2 follow-on candles).
    Returns list of manifest row dicts with pool_audit_only / bucket_ms / bucket_sec /
    signal_ts_ms_audit_only.
    """
    rows: list[dict[str, Any]] = []
    for pool in pools:
        bkts = pool_breakouts.get(pool, [])
        for bk in bkts:
            ts_sec = (bk["ts_sec"] // BUCKET_SEC) * BUCKET_SEC
            bm     = ts_sec * 1000
            sig_ms = bm + 120_000  # 2 min into bucket — same as main label convention
            rows.append({
                "pool_audit_only":       pool.lower(),
                "pool_label":            pool_labels.get(pool, ""),
                "bucket_ms":             bm,
                "bucket_sec":            ts_sec,
                "signal_ts_ms_audit_only": sig_ms,
                "h1_return_at_breakout": bk["h1_return_pct"],
                "breakout_close_p":      bk["close_p"],
                "breakout_is_bullish":   bk["is_bullish"],
                "source":                "breakout_anchor",
            })
            # Add the immediately following candle as a second entry
            ts_next = ts_sec + BUCKET_SEC
            bm_next = ts_next * 1000
            rows.append({
                "pool_audit_only":       pool.lower(),
                "pool_label":            pool_labels.get(pool, ""),
                "bucket_ms":             bm_next,
                "bucket_sec":            ts_next,
                "signal_ts_ms_audit_only": bm_next + 120_000,
                "h1_return_at_breakout": bk["h1_return_pct"],
                "breakout_close_p":      bk["close_p"],
                "breakout_is_bullish":   bk["is_bullish"],
                "source":                "breakout_anchor_follow",
            })
    # Dedup by (pool, bucket_ms)
    seen: set[tuple[str, int]] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        key = (r["pool_audit_only"], r["bucket_ms"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


# ─── RPC label runner for breakout manifest ──────────────────────────────────

class CountingRPC:
    def __init__(self, rpc: Any) -> None:
        self._rpc           = rpc
        self.rpc_round_trips = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.rpc_round_trips += 1
        return self._rpc.call(method, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._rpc, name)


def rpc_infer_pool_meta(mod: Any, rpc: Any, pool: str) -> dict[str, Any] | None:
    """Infer minimal pool meta (pool_type, t0, t1, fee_bps) via RPC."""
    pool = pool.lower()
    try:
        # eth_getCode
        code = rpc.call("eth_getCode", [pool, "latest"])
        if not (code.get("result") or "0x")[:4] == "0x60":
            pass
        # Try token0 / token1 / fee (V3 ABI)
        def eth_call(data: str) -> str:
            r = rpc.call("eth_call", [{"to": pool, "data": data}, "latest"])
            return r.get("result") or "0x"

        t0_raw = eth_call("0x0dfe1681")  # token0()
        t1_raw = eth_call("0xd21220a7")  # token1()
        fee_raw = eth_call("0xddca3f43")  # fee()
        if len(t0_raw) < 66 or len(t1_raw) < 66:
            return None
        t0 = "0x" + t0_raw[-40:]
        t1 = "0x" + t1_raw[-40:]
        fee = int(fee_raw, 16) if len(fee_raw) >= 10 else 3000
        return {
            "pool_type": "v3",
            "t0": t0.lower(),
            "t1": t1.lower(),
            "fee_bps": fee // 100,
        }
    except (ValueError, TypeError, KeyError):
        return None


def run_breakout_rpc_labels(
    breakout_manifest_rows: list[dict[str, Any]],
    mod: Any,
    rpc: CountingRPC,
) -> list[dict[str, Any]]:
    """Run bounded RPC labels (30 m, main semantics) on breakout entries.

    Max ~300 RPC round-trips (9 pools × 2 entries × ~15 calls each + grid).
    """
    if not breakout_manifest_rows:
        return []

    now_ms  = int(time.time() * 1000)
    min_age = (LABEL_EXIT_HORIZON_SEC + 120) * 1000
    rows = [r for r in breakout_manifest_rows if now_ms - r["bucket_ms"] >= min_age]
    if not rows:
        return []

    pools_set = {r["pool_audit_only"].lower() for r in rows}
    meta: dict[str, Any] = {}
    for pool in sorted(pools_set):
        m = rpc_infer_pool_meta(mod, rpc, pool)
        if m:
            meta[pool] = m

    rows = [r for r in rows if meta.get(r["pool_audit_only"].lower(), {}).get("pool_type") == "v3"]
    if not rows:
        return []

    ts_min = min(r["bucket_sec"] for r in rows)
    ts_max = max(r["bucket_sec"] for r in rows) + LABEL_EXIT_HORIZON_SEC
    bn_arr, ts_arr = mod.build_ts_grid(rpc, ts_min, ts_max)

    results: list[dict[str, Any]] = []
    for r in rows:
        pool     = r["pool_audit_only"].lower()
        sig_sec  = r["signal_ts_ms_audit_only"] // 1000
        exit_sec = sig_sec + LABEL_EXIT_HORIZON_SEC
        eb = mod.ts_to_block(bn_arr, ts_arr, sig_sec)
        xb = mod.ts_to_block(bn_arr, ts_arr, exit_sec)
        m  = meta.get(pool, {"pool_type": "v3", "t0": "", "t1": "", "fee_bps": 30})
        label, net, lt = mod.label_one(rpc, pool, m, WETH, LABEL_NOTIONAL_WEI, eb, xb)
        results.append({
            "pool":           pool,
            "pool_label":     r.get("pool_label", ""),
            "bucket_sec":     r["bucket_sec"],
            "signal_ts_ms":   r["signal_ts_ms_audit_only"],
            "source":         r.get("source", "breakout_anchor"),
            "h1_at_breakout": r.get("h1_return_at_breakout", ""),
            "label":          label,
            "net_return":     round(net, 6) if label >= 0 else None,
            "label_type":     lt,
            "horizon_sec":    LABEL_EXIT_HORIZON_SEC,
        })
    return results


# ─── Dim-3: Temporal decay split ─────────────────────────────────────────────

def temporal_decay_split(
    e035_label_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Split 035E labels into first-half (oldest) / second-half (newest) by bucket_ms.

    Tests whether entries closer to discovery (< 2 h ago) underperform
    vs entries from 5–10 h ago.
    """
    rows = sorted(e035_label_rows, key=lambda r: int(r.get("bucket_ms", 0)))
    n = len(rows)
    halves: dict[str, Any] = {}
    for name, subset in [("oldest_half", rows[: n // 2]), ("newest_half", rows[n // 2 :])]:
        nets = [float(r["net_return"]) for r in subset]
        label1 = sum(1 for n_ in nets if n_ > 0)
        halves[name] = {
            "n":         len(subset),
            "label_1":   label1,
            "label1_pct": round(label1 / len(subset) * 100, 2) if subset else 0,
            "pos_share": round(label1 / len(subset), 4) if subset else 0,
            "mean_net":  round(statistics.mean(nets), 6) if nets else 0,
            "age_h_range": (
                round((int(time.time() * 1000) - int(rows[0]["bucket_ms"])) / 3_600_000, 1),
                round((int(time.time() * 1000) - int(rows[n // 2 - 1]["bucket_ms"])) / 3_600_000, 1),
            ) if name == "oldest_half" else (
                round((int(time.time() * 1000) - int(rows[n // 2]["bucket_ms"])) / 3_600_000, 1),
                round((int(time.time() * 1000) - int(rows[-1]["bucket_ms"])) / 3_600_000, 1),
            ),
        }
    return halves


# ─── statistics helpers ───────────────────────────────────────────────────────

def _trimmed_mean(vals: list[float], trim: float = 0.1) -> float | None:
    if len(vals) < 6:
        return statistics.mean(vals) if vals else None
    n   = len(vals)
    cut = max(1, int(n * trim))
    trimmed = sorted(vals)[cut: n - cut]
    return statistics.mean(trimmed) if trimmed else None


def _summary_stats(rows: list[dict[str, Any]], net_field: str = "net_return") -> dict[str, Any]:
    nets = [float(r[net_field]) for r in rows if r.get(net_field) is not None]
    if not nets:
        return {"n": 0}
    label1 = sum(1 for n in nets if n > 0)
    return {
        "n":                len(nets),
        "label_1":          label1,
        "label1_pct":       round(label1 / len(nets) * 100, 2),
        "pos_share":        round(label1 / len(nets), 4),
        "mean_net":         round(statistics.mean(nets), 6),
        "median_net":       round(statistics.median(nets), 6),
        "trimmed_mean_net": round(tm, 6) if (tm := _trimmed_mean(nets)) else None,
        "coverage":         len(nets),
    }


# ─── CSV writer ──────────────────────────────────────────────────────────────

def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ─── report builder ──────────────────────────────────────────────────────────

def _render_report(
    ohlcv_stats: dict[str, Any],
    breakout_stats: dict[str, Any],
    temporal_stats: dict[str, Any],
    breakout_pools_found: dict[str, int],
    e035_pools_h1: dict[str, str],
    http_used: int,
    rpc_trips: int,
) -> str:
    lines = [
        "# Sprint035F — Short-horizon & breakout-anchor diagnostic",
        "",
        "**Diagnostic only — OHLCV results NOT alpha proof. RPC breakout labels use main 30 m semantics.**",
        "No training / no H12 / no live / no model promotion.",
        "",
        "## Why Sprint035E gave 0% label_1",
        "",
        "| Pool | h1 at discovery | h6 at discovery | Score driver |",
        "|------|-----------------|-----------------|--------------|",
    ]
    for pool, h1 in e035_pools_h1.items():
        lines.append(f"| `{pool[:20]}` | {h1} | (see cand CSV) | BSR/txn density |")
    lines += [
        "",
        "**Root cause**: all 3 pools had h1 = 0.2–0.7% only — barely any price movement.",
        "CLANKER/WETH had h6 = −15.9% (in deep recovery).  The scoring V2 passed these",
        "pools primarily on buy/sell imbalance, not on genuine price momentum.",
        "4h synthetic entries land during post-pump consolidation → 30 m exit always negative.",
        "",
        "---",
        "",
        "## Dim-1: Multi-horizon OHLCV approximate returns",
        "",
        "*(Candle open/close prices — NOT quoter-simulated. Use for trend direction only.)*",
        "",
        "| Horizon | n | label_1 | label1% | pos_share | mean_net | median_net |",
        "|---------|---|---------|---------|-----------|----------|------------|",
    ]
    for h in ["h5m", "h15m", "h30m"]:
        s = ohlcv_stats.get(h, {})
        lines.append(
            f"| {h} | {s.get('n','?')} | {s.get('label_1','?')} | "
            f"{s.get('label1_pct','?')}% | {s.get('pos_share','?')} | "
            f"{s.get('mean_net','?')} | {s.get('median_net','?')} |"
        )
    lines += [
        "",
        "### Interpretation",
        "",
        "- If 5 m label_1 >> 30 m: momentum is short-lived, 30 m horizon overshoots the move.",
        "- If all horizons ~0%: pool had no upward price action at any time during the session.",
        "",
        "---",
        "",
        "## Dim-2: Breakout-anchor diagnostic (RPC, 30 m main semantics)",
        "",
        "| Pool | Breakouts found | Entries labeled | label_1 | mean_net |",
        "|------|-----------------|-----------------|---------|----------|",
    ]
    bp = breakout_stats.get("per_pool", {})
    for pool, cnt in breakout_pools_found.items():
        s = bp.get(pool[:14], {})
        lines.append(
            f"| `{pool[:20]}` | {cnt} | {s.get('n','0')} | "
            f"{s.get('label_1','0')} | {s.get('mean_net','N/A')} |"
        )
    lines += [
        "",
        "**Overall breakout-anchor**:",
        "",
        f"- n = {breakout_stats.get('n','?')}",
        f"- label_1 = {breakout_stats.get('label_1','?')} ({breakout_stats.get('label1_pct','?')}%)",
        f"- pos_share = {breakout_stats.get('pos_share','?')}",
        f"- mean_net = {breakout_stats.get('mean_net','?')}",
        "",
        "---",
        "",
        "## Dim-3: Temporal decay split of 035E labels",
        "",
        "| Half | n | label_1 | label1% | mean_net | age range |",
        "|------|---|---------|---------|----------|-----------|",
    ]
    for half, s in temporal_stats.items():
        lines.append(
            f"| {half} | {s.get('n','?')} | {s.get('label_1','?')} | "
            f"{s.get('label1_pct','?')}% | {s.get('mean_net','?')} | "
            f"{s.get('age_h_range','?')} h |"
        )
    lines += [
        "",
        "- If newest_half (≤ 2 h ago) worse than oldest_half (5–10 h): confirms recency ≠ profitability.",
        "",
        "---",
        "",
        "## Recommended actions",
        "",
        "1. **Fix pool selection filter**: require h1 ≥ 2 % (not just BSR) as HARD gate,",
        "   not just a scoring bonus.  BSR alone without price momentum = consolidation entry.",
        "",
        "2. **Breakout-first entry**: only label entries at/within 2 candles of the",
        "   +2 % h1 breakout crossing.  Discard synthetic ladders that pre-date the breakout.",
        "",
        "3. **Accept 030 m semantics constraint**: if even breakout-anchor entries show < 5 %",
        "   label_1 at 30 m, consider whether the label_1 target requires a shorter horizon",
        "   diagnostic (5 m / 15 m) as the primary signal.",
        "",
        "---",
        "",
        "## Run stats",
        "",
        f"- http_requests_used: {http_used}/{MAX_HTTP}",
        f"- rpc_round_trips: {rpc_trips}",
        "",
        "## Artifacts",
        "",
        f"- `{OUT_OHLCV_DIAG.relative_to(REPO)}`",
        f"- `{OUT_BREAKOUT_MAN.relative_to(REPO)}`",
        f"- `{OUT_BREAKOUT_LABELS.relative_to(REPO)}`",
        f"- `{OUT_COMPARISON.relative_to(REPO)}`",
        "",
    ]
    return "\n".join(lines)


# ─── main diagnostic pipeline ─────────────────────────────────────────────────

def run_diagnostic() -> str:  # noqa: C901
    global http_req_used
    http_req_used = 0

    # Load 035E artifacts
    manifest = json.loads(E_MANIFEST.read_text())
    manifest_rows: list[dict[str, Any]] = manifest.get("manifest_rows", [])
    pools: list[str] = manifest.get("pool_address_audit_keys", [])

    if not manifest_rows or not pools:
        print("[035F] ERROR: 035E manifest missing or empty", file=sys.stderr)
        return "ERROR_MISSING_035E_MANIFEST"

    e035_label_rows: list[dict[str, Any]] = []
    try:
        with E_LABELS.open(encoding="utf-8") as f:
            e035_label_rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print("[035F] WARNING: 035E labels CSV not found; Dim-3 skipped", file=sys.stderr)

    # Pool labels from candidate CSV (pair names)
    pool_labels: dict[str, str] = {}
    pool_h1: dict[str, str] = {}
    try:
        with E_CAND.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                addr = r["pool_address_candidate"].lower()
                if addr in [p.lower() for p in pools]:
                    pool_labels[addr] = r.get("pair_label", addr[:12])
                    pool_h1[addr]     = r.get("price_change_h1", "?")
    except FileNotFoundError:
        for p in pools:
            pool_labels[p.lower()] = p[:14]

    # ── Fetch 24h OHLCV for each pool ────────────────────────────────────────
    print("[035F] Fetching 24h OHLCV for", len(pools), "pools…")
    pool_candles: dict[str, list[dict[str, Any]]] = {}
    for pool in pools:
        candles = fetch_ohlcv_24h(pool)
        print(f"  {pool[:14]}: {len(candles)} candles")
        if candles:
            pool_candles[pool.lower()] = candles

    # ── Dim-1: Multi-horizon OHLCV diagnostic ────────────────────────────────
    print("[035F] Dim-1: multi-horizon OHLCV diagnostic…")
    ohlcv_rows: list[dict[str, Any]] = []
    for pool in pools:
        candles = pool_candles.get(pool.lower(), [])
        rows = ohlcv_multi_horizon(
            pool, manifest_rows, candles,
            pool_label=pool_labels.get(pool.lower(), pool[:12]),
        )
        ohlcv_rows.extend(rows)
        for h in ("h5m", "h15m", "h30m"):
            matched = [r for r in rows if r.get(f"net_{h}") is not None]
            pos = sum(1 for r in matched if (r.get(f"net_{h}") or -1) > 0)
            print(f"  {pool[:14]} {h}: n={len(matched)} pos={pos}")

    ohlcv_fieldnames = [
        "pool", "pool_label", "bucket_sec", "signal_ts_ms",
        "entry_price", "candle_open", "candle_close",
        "net_h5m", "label_h5m",
        "net_h15m", "label_h15m",
        "net_h30m", "label_h30m",
    ]
    write_csv(OUT_OHLCV_DIAG, ohlcv_fieldnames, ohlcv_rows)

    ohlcv_stats: dict[str, Any] = {}
    for h in ("h5m", "h15m", "h30m"):
        valid = [r for r in ohlcv_rows if r.get(f"net_{h}") is not None]
        ohlcv_stats[h] = _summary_stats(valid, f"net_{h}")

    # ── Dim-2: Breakout-anchor detection ─────────────────────────────────────
    print("[035F] Dim-2: finding breakout candles…")
    pool_breakouts: dict[str, list[dict[str, Any]]] = {}
    breakout_counts: dict[str, int] = {}
    for pool in pools:
        candles = pool_candles.get(pool.lower(), [])
        bkts = find_breakout_candles(candles, threshold_pct=2.0, max_breakouts=3)
        pool_breakouts[pool.lower()] = bkts
        breakout_counts[pool.lower()] = len(bkts)
        label = pool_labels.get(pool.lower(), pool[:14])
        print(f"  {pool[:14]} ({label[:18]}): {len(bkts)} breakout(s)")
        for b in bkts:
            ts_h = round((int(time.time()) - b["ts_sec"]) / 3600, 1)
            print(f"    {b['ts_sec']} ({ts_h}h ago) h1_ret={b['h1_return_pct']:.2f}% bull={b['is_bullish']}")

    breakout_manifest = build_breakout_manifest(
        [p.lower() for p in pools], pool_breakouts, pool_labels,
    )

    # Save breakout manifest
    manifest_out = {
        "kind": "sprint035f_breakout_anchor_diagnostic_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "diagnostic_only": True,
        "n_rows": len(breakout_manifest),
        "pools": [p.lower() for p in pools],
        "manifest_rows": breakout_manifest,
    }
    OUT_BREAKOUT_MAN.write_text(json.dumps(manifest_out, indent=2))

    # ── RPC labels for breakout entries ──────────────────────────────────────
    print(f"[035F] Dim-2: running RPC breakout labels ({len(breakout_manifest)} entries)…")

    m33  = load_m33()
    env  = {**load_dotenv_files(), **dict(os.environ)}
    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    mod  = m33.load_label_mod()

    breakout_label_rows: list[dict[str, Any]] = []
    rpc_trips = 0

    if rpc_url and breakout_manifest:
        rpc_plain = mod.RPC(rpc_url, timeout=60, max_retries=4)
        rpc = CountingRPC(rpc_plain)
        breakout_label_rows = run_breakout_rpc_labels(breakout_manifest, mod, rpc)
        rpc_trips = rpc.rpc_round_trips
        print(f"  breakout labels: {len(breakout_label_rows)} rows, {rpc_trips} RPC trips")
    else:
        print("  [SKIP] no RPC URL or empty breakout manifest")

    bkl_fieldnames = [
        "pool", "pool_label", "bucket_sec", "signal_ts_ms",
        "source", "h1_at_breakout", "label", "net_return",
        "label_type", "horizon_sec",
    ]
    write_csv(OUT_BREAKOUT_LABELS, bkl_fieldnames, breakout_label_rows)

    breakout_valid = [r for r in breakout_label_rows if r.get("label", -1) >= 0]
    breakout_stats: dict[str, Any] = _summary_stats(breakout_valid)

    # Per-pool breakout stats
    per_pool_bk: dict[str, Any] = {}
    for pool in pools:
        subset = [r for r in breakout_valid if r["pool"][:14] == pool[:14]]
        per_pool_bk[pool[:14]] = _summary_stats(subset) if subset else {}
    breakout_stats["per_pool"] = per_pool_bk

    # ── Dim-3: Temporal decay split ──────────────────────────────────────────
    print("[035F] Dim-3: temporal decay split…")
    temporal_stats: dict[str, Any] = {}
    if e035_label_rows:
        # Attach bucket_ms from manifest index
        bm_by_sig = {r["signal_ts_ms_audit_only"]: r["bucket_ms"] for r in manifest_rows}
        for lr in e035_label_rows:
            sig = int(lr.get("timestamp_ms", 0))
            lr["bucket_ms"] = bm_by_sig.get(sig, sig)
        temporal_stats = temporal_decay_split(e035_label_rows)
        for half, s in temporal_stats.items():
            print(f"  {half}: n={s['n']} label1%={s['label1_pct']} mean={s['mean_net']}")
    else:
        temporal_stats = {"skipped": {"reason": "no_035e_labels"}}

    # ── Comparison summary ───────────────────────────────────────────────────
    comparison = {
        "kind": "sprint035f_short_horizon_breakout_diagnostic",
        "no_training": True,
        "no_live": True,
        "diagnostic_only_ohlcv_approx": True,
        "e035_30m_rpc_baseline": {
            "n": 153, "label_1": 0, "label1_pct": 0.0, "pos_share": 0.0,
            "mean_net": -0.010179, "manifest_age_median_h": 2.7,
            "note": "BSR-driven selection, low h1 (0.2-0.7%), post-pump consolidation entries",
        },
        "dim1_ohlcv_multihorizon": ohlcv_stats,
        "dim2_breakout_anchor_rpc_30m": {
            k: v for k, v in breakout_stats.items() if k != "per_pool"
        },
        "dim2_breakout_per_pool": per_pool_bk,
        "dim3_temporal_decay": temporal_stats,
        "breakout_candles_found": breakout_counts,
        "pool_h1_at_e035_discovery": pool_h1,
        "http_used": http_req_used,
        "rpc_round_trips": rpc_trips,
        "interpretation": {
            "why_035e_failed": (
                "Low h1 (0.2-0.7%) pools pass scoring on BSR but have no upward price action; "
                "4h synthetic entries land in post-pump consolidation; "
                "30m exit horizon cannot overcome round-trip cost from flat entry."
            ),
            "fix_needed": (
                "Require h1 >= 2% as hard gate (not scoring bonus); "
                "anchor entries to breakout candle (h1 crossing +2%), not synthetic ladder; "
                "consider 5m/15m diagnostic horizon for meme token momentum."
            ),
        },
    }
    OUT_COMPARISON.write_text(json.dumps(comparison, indent=2))

    # ── Report ───────────────────────────────────────────────────────────────
    report = _render_report(
        ohlcv_stats, breakout_stats, temporal_stats,
        breakout_counts, pool_h1, http_req_used, rpc_trips,
    )
    OUT_REPORT.write_text(report)

    status = "DIAGNOSTIC_COMPLETE"
    return status


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint035F short-horizon + breakout diagnostic")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    if args.run:
        status = run_diagnostic()
        print(f"[035F] status={status} http={http_req_used}/{MAX_HTTP}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
