#!/usr/bin/env python3
"""Sprint053 — fee_100bps young-pool signal expansion beyond smoke threshold.

Research-only. No private key, no live, no training, no production signals.

Strategy:
  - Source: eatlq.uniswap_v3_swaps_decoded (pure V3, block_time ordered)
  - Weeks:  2026-04-05 / 04-12 / 04-19 / 04-26 (all available by block_time)
  - Gate A (canonical, Sprint037G baseline_cell): pool_age≤86400s, onset≤3600s, thresh≥10
  - Gate B (diagnostic, labeled as NON-CANONICAL): pool_age≤259200s, onset≤5400s, thresh≥5
  - Price label: sqrtPriceX96 binary direction (up/down relative to WETH)
  - RPC: pool.fee() + pool.token0() for NEW pools only (reuse Sprint052 pool_meta)
  - RPC budget: ~500 calls max for new pools, conservative

  Gate B is explicitly NOT a production gate. It is a research probe to understand
  whether the fee_100bps signal is robust at lower onset activity levels. Any finding
  from Gate B must be re-validated with Gate A on future data before any production use.

Sprint050 benchmarks (in-bundle, Sprint037G canonical gate, RPC net_return_5m label):
  fee_100bps:  1513 rows, hit=49.5%, PF=1.79
  fee_3000bps: 3797 rows, hit=58.5%, PF=2.31 (absent in OOS)
  aggregate:   11233 rows, hit=36.4%, PF=1.46
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
AUDIT_OUT = ROOT / "mvp/audits/sprint053"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint053"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID     = 8453
WETH_BASE    = "0x4200000000000000000000000000000000000006".lower()
WINDOW_SEC   = 300  # 5 min

# Sprint037G canonical gate
GATE_A = dict(pool_max_age_sec=86400, onset_max_sec=3600, onset_thresh=10, label="CANONICAL_037G")
# Diagnostic gate (NOT a production guard change)
GATE_B = dict(pool_max_age_sec=259200, onset_max_sec=5400, onset_thresh=5,  label="DIAGNOSTIC_RELAXED")

SEL_FEE    = "0xddca3f43"
SEL_TOKEN0 = "0x0dfe1681"
RPC_DELAY  = 0.04

# Reuse Sprint052 pool_meta if present
S052_META_PATH = ROOT / "mvp/diagnostics/sprint052/oos_candidates.csv.json"
S052_MATRIX_PATH = ROOT / "mvp/diagnostics/sprint052/oos_reproduction_matrix.json"

OOS_WEEKS = [
    # Pre-bundle (before first q4 bundle week 2025-10-20)
    ("2025-09-28", "2025-10-05"),   # pre-bundle, 218 pools
    ("2025-10-05", "2025-10-12"),   # pre-bundle, 39 pools
    ("2025-10-12", "2025-10-19"),   # pre-bundle (week ends before 2025-10-20 bundle start)
    # Mid-bundle gaps (between in-bundle weeks, not in q4 bundle)
    ("2025-11-16", "2025-11-23"),   # mid-bundle gap, 30 pools
    ("2025-11-23", "2025-11-30"),   # mid-bundle gap, 16 pools
    ("2025-12-07", "2025-12-14"),   # mid-bundle gap, 38 pools
    ("2025-12-21", "2025-12-28"),   # mid-bundle gap, 30 pools
    ("2026-03-15", "2026-03-22"),   # mid-bundle gap, 30 pools
    ("2026-03-22", "2026-03-29"),   # mid-bundle gap, 26 pools
    # Post-bundle (after last q4 bundle week 2026-03-30)
    ("2026-04-05", "2026-04-12"),   # post-bundle, 104 pools
    ("2026-04-12", "2026-04-19"),   # post-bundle, 55 pools
    ("2026-04-19", "2026-04-26"),   # post-bundle, 26 pools
    ("2026-04-26", "2026-05-03"),   # post-bundle, 7 pools
]

# Sprint050 in-bundle benchmarks
S050 = {
    "fee_100bps":  {"rows": 1513, "hit": 0.495, "pf": 1.79,  "label_type": "rpc_net_return"},
    "fee_3000bps": {"rows": 3797, "hit": 0.585, "pf": 2.31,  "label_type": "rpc_net_return"},
    "fee_500bps":  {"rows": 348,  "hit": 0.515, "pf": 2.36,  "label_type": "rpc_net_return"},
    "aggregate":   {"rows": 11233,"hit": 0.364, "pf": 1.456, "label_type": "rpc_net_return"},
    "heat_ge_250": {"rows": 407,  "hit": 0.563, "pf": 3.40,  "label_type": "rpc_net_return"},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CH helpers
# ---------------------------------------------------------------------------

def _ch(sql: str, timeout: int = 300) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:300] or r.stdout.strip()[:300]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def pull_pool_first_seen_v3(pools: set[str] | None = None) -> dict[str, int]:
    """Get global first block_time for all V3 pools (or a specific set)."""
    pool_filter = ""
    if pools:
        pl = ",".join(f"'{p}'" for p in pools)
        pool_filter = f"AND pool_address IN ({pl})"
    rows = _ch(f"""
SELECT pool_address, toUnixTimestamp(min(block_time)) AS first_ts
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id={CHAIN_ID} AND block_time IS NOT NULL {pool_filter}
GROUP BY pool_address
""", timeout=120)
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


def pull_windows_for_week(w_start: str, w_end: str) -> list[dict]:
    """Aggregate swaps into 5m windows per pool for one week. Returns minimal set."""
    rows = _ch(f"""
SELECT
    pool_address,
    toUnixTimestamp(toStartOfFiveMinutes(block_time)) AS wts,
    count()                                            AS heat,
    countIf(amount0 < 0)                               AS buys,
    min(toUnixTimestamp(block_time))                   AS min_ts,
    max(toUnixTimestamp(block_time))                   AS max_ts,
    toFloat64(argMax(sqrt_price_x96, block_time))       AS last_sqrt_raw,
    toFloat64(argMin(sqrt_price_x96, block_time))       AS first_sqrt_raw
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = {CHAIN_ID}
  AND block_time >= toDateTime('{w_start}')
  AND block_time <  toDateTime('{w_end}')
  AND block_time IS NOT NULL
GROUP BY pool_address, wts
HAVING count() >= 2
ORDER BY pool_address, wts
""", timeout=180)

    result = []
    for r in rows:
        if len(r) < 8:
            continue
        try:
            result.append({
                "pool":       r[0],
                "wts":        int(r[1]),
                "heat":       int(r[2]),
                "buys":       int(r[3]),
                "min_ts":     int(r[4]),
                "max_ts":     int(r[5]),
                "last_sqrt":  float(r[6]),
                "first_sqrt": float(r[7]),
            })
        except (ValueError, OverflowError):
            pass
    return result


# ---------------------------------------------------------------------------
# Pool meta (reuse Sprint052 where possible)
# ---------------------------------------------------------------------------

def load_sprint052_pool_meta() -> dict[str, dict]:
    """Load Sprint052 RPC enrichment if available."""
    # Sprint052 stored pool meta inside the candidates JSON
    # We reconstruct it from the matrix which has fee_label per pool group
    # Better: re-read from the autopsy which has per-pool enrichment
    # Actually Sprint052 didn't store per-pool meta directly; we'll just return empty
    # and rely on RPC for all pools (budget allows it)
    return {}


def rpc_enrich(pools: list[str], rpc_url: str) -> dict[str, dict]:
    import requests
    session = requests.Session()
    out: dict[str, dict] = {}
    total = len(pools)
    for i, pool in enumerate(pools):
        if i % 200 == 0:
            print(f"  rpc {i}/{total} ...", flush=True)
        req_id = i * 2 + 1
        # fee()
        resp_fee = None
        resp_t0  = None
        try:
            r = session.post(rpc_url, json={"jsonrpc":"2.0","id":req_id,"method":"eth_call",
                                            "params":[{"to":pool,"data":SEL_FEE},"latest"]}, timeout=15)
            if r.status_code != 429:
                resp_fee = r.json().get("result")
        except Exception:
            pass
        time.sleep(RPC_DELAY)
        try:
            r = session.post(rpc_url, json={"jsonrpc":"2.0","id":req_id+1,"method":"eth_call",
                                            "params":[{"to":pool,"data":SEL_TOKEN0},"latest"]}, timeout=15)
            if r.status_code != 429:
                resp_t0 = r.json().get("result")
        except Exception:
            pass
        time.sleep(RPC_DELAY)
        fee = _dec_uint24(resp_fee)
        t0  = _dec_addr(resp_t0)
        out[pool] = {
            "fee_bps": fee,
            "token0":  t0,
            "weth_is_token0": (t0 == WETH_BASE) if t0 else None,
        }
    return out


def _dec_uint24(h: str | None) -> int | None:
    if not h or h == "0x": return None
    raw = h[2:] if h.startswith("0x") else h
    try:
        v = int(raw[-8:], 16)
        return v if v > 0 else None
    except Exception:
        return None


def _dec_addr(h: str | None) -> str | None:
    if not h: return None
    raw = h[2:] if h.startswith("0x") else h
    return ("0x" + raw[-40:]).lower() if len(raw) >= 40 else None


def _load_rpc() -> str:
    env = ROOT / "infra/compose/.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" not in line or line.startswith("#"): continue
            k, v = line.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v: return v
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url: raise RuntimeError("No RPC URL found")
    return url


# ---------------------------------------------------------------------------
# Gate + feature computation
# ---------------------------------------------------------------------------

def _win(ts: int) -> int:
    return (ts // WINDOW_SEC) * WINDOW_SEC


def apply_gate(
    windows_by_pool: dict[str, list[dict]],
    first_seen: dict[str, int],
    w_start_ts: int,
    w_end_ts: int,
    week_label: str,
    gate: dict,
) -> list[dict]:
    candidates = []
    for pool, wins in windows_by_pool.items():
        fs = first_seen.get(pool)
        if fs is None: continue
        # find onset window (first win in week with heat >= onset_thresh)
        onset_ts = None
        for w in wins:
            if w["wts"] < w_start_ts or w["wts"] >= w_end_ts: continue
            if w["heat"] >= gate["onset_thresh"]:
                onset_ts = w["wts"]
                break
        if onset_ts is None: continue
        for w in wins:
            if w["wts"] < w_start_ts or w["wts"] >= w_end_ts: continue
            if w["heat"] < gate["onset_thresh"]: continue
            pool_age = w["wts"] - fs
            onset_lag = w["wts"] - onset_ts
            if pool_age > gate["pool_max_age_sec"]: continue
            if onset_lag > gate["onset_max_sec"]: continue
            n = w["heat"]
            bp = w["buys"] / n if n > 0 else 0.0
            candidates.append({
                "gate":         gate["label"],
                "regime_week":  week_label,
                "pool_address": pool,
                "wts":          w["wts"],
                "pool_age_sec": pool_age,
                "onset_lag_sec":onset_lag,
                "activity_heat": n,
                "buy_pressure_ratio": round(bp, 4),
                "price_velocity_5m": round((2 * bp - 1), 4),
                "last_sqrt":    w["last_sqrt"],
            })
    return candidates


# ---------------------------------------------------------------------------
# Price-direction label from sqrtPriceX96
# ---------------------------------------------------------------------------

def add_sqrt_label(candidates: list[dict], windows_by_pool: dict[str, list[dict]], pool_meta: dict[str, dict]) -> None:
    # Build lookup: pool → wts → {first_sqrt, last_sqrt}
    lookup: dict[str, dict[int, dict]] = {}
    for pool, wins in windows_by_pool.items():
        lookup[pool] = {w["wts"]: w for w in wins}

    for c in candidates:
        pool = c["pool_address"]
        entry_wts = c["wts"]
        exit_wts  = entry_wts + WINDOW_SEC
        entry_w   = lookup.get(pool, {}).get(entry_wts)
        exit_w    = lookup.get(pool, {}).get(exit_wts)
        if not entry_w or not exit_w:
            c["label_sqrt"] = None
            c["net_ret_sqrt"] = None
            continue
        entry_sqrt = entry_w["last_sqrt"]
        exit_sqrt  = exit_w["first_sqrt"]
        if entry_sqrt == 0:
            c["label_sqrt"] = None
            c["net_ret_sqrt"] = None
            continue
        meta = pool_meta.get(pool, {})
        weth0 = meta.get("weth_is_token0")
        try:
            sqrt_ratio = exit_sqrt / entry_sqrt  # sqrtPrice ratio
            # Sanity: if ratio is absurd (overflow artifact), skip
            if not (0.001 < sqrt_ratio < 1000):
                c["label_sqrt"] = None
                c["net_ret_sqrt"] = None
                continue
            # price ratio = sqrt_ratio^2
            # For WETH-TOKEN price (token appreciation):
            #   token1=WETH: price_token0_per_weth = (sqrtP/Q96)^2 → higher sqrtP = token0 more expensive in WETH → label=1 if ratio>1
            #   token0=WETH: price_token1_per_weth = (Q96/sqrtP)^2 → lower sqrtP  = token1 more expensive in WETH → label=1 if ratio<1
            if weth0 is True:
                label = int(sqrt_ratio < 1.0)
                ret   = (1.0 / sqrt_ratio ** 2) - 1.0
            elif weth0 is False:
                label = int(sqrt_ratio > 1.0)
                ret   = sqrt_ratio ** 2 - 1.0
            else:
                label = None
                ret   = abs(sqrt_ratio ** 2 - 1.0)
            # Cap extreme returns (>200% in 5m is almost certainly an artifact)
            if abs(ret) > 2.0:
                c["label_sqrt"] = None
                c["net_ret_sqrt"] = None
                continue
        except Exception:
            c["label_sqrt"] = None
            c["net_ret_sqrt"] = None
            continue
        c["label_sqrt"] = label
        c["net_ret_sqrt"] = round(float(ret), 6) if ret is not None else None


# ---------------------------------------------------------------------------
# Reproduction matrix
# ---------------------------------------------------------------------------

def _hbucket(h: int) -> str:
    if h < 20:   return "heat_lt_20"
    if h < 50:   return "heat_20_50"
    if h < 100:  return "heat_50_100"
    if h < 250:  return "heat_100_250"
    return "heat_ge_250"


def _fee_label(f: int | None) -> str:
    if f is None:    return "fee_unknown"
    if f == 100:     return "fee_100bps"
    if f == 500:     return "fee_500bps"
    if f == 3000:    return "fee_3000bps"
    if f == 10000:   return "fee_10000bps"
    return f"fee_{f}bps"


def _sclass(n: int) -> str:
    if n < 300:   return "smoke"
    if n < 1000:  return "very_weak"
    if n < 5000:  return "scoped_dryrun"
    if n < 20000: return "scoped_backtest"
    return "formal_research"


def _stats(items: list[dict]) -> dict:
    labeled = [c for c in items if c.get("label_sqrt") is not None]
    n = len(labeled)
    if n == 0:
        return {"n": 0, "hit": None, "mean_ret": None, "median_ret": None,
                "pf": None, "top1": None, "sclass": _sclass(len(items))}
    pos = sum(1 for c in labeled if c["label_sqrt"] == 1)
    rets = [c["net_ret_sqrt"] for c in labeled if c.get("net_ret_sqrt") is not None]
    gains = [v for v in rets if v > 0]
    losses = [-v for v in rets if v < 0]
    pf = (sum(gains) / sum(losses)) if losses else (None if not gains else 999.0)
    top1 = None
    if labeled:
        pc = Counter(c["pool_address"] for c in labeled)
        top1 = pc.most_common(1)[0][1] / sum(pc.values())
    return {
        "n_labeled": n,
        "hit_rate": round(pos / n, 4),
        "mean_ret": round(statistics.mean(rets), 5) if rets else None,
        "median_ret": round(statistics.median(rets), 5) if rets else None,
        "profit_factor": round(pf, 3) if pf and pf < 999 else pf,
        "pool_top1_share": round(top1, 3) if top1 else None,
        "sample_class": _sclass(len(items)),
    }


def build_matrix(candidates: list[dict], pool_meta: dict[str, dict]) -> list[dict]:
    enriched = []
    for c in candidates:
        meta = pool_meta.get(c["pool_address"], {})
        enriched.append({
            **c,
            "fee_bps":   meta.get("fee_bps"),
            "fee_label": _fee_label(meta.get("fee_bps")),
            "heat_bucket": _hbucket(c.get("activity_heat", 0)),
        })

    rows = []

    def _grp(items, *keys):
        g: dict = defaultdict(list)
        for item in items:
            g[tuple(item.get(k) for k in keys)].append(item)
        return g

    # by gate × fee_label (all weeks combined)
    for (gate, fee_label), items in sorted(_grp(enriched, "gate", "fee_label").items()):
        s = _stats(items)
        rows.append({
            "scope": "ALL_WEEKS",
            "gate": gate, "fee_label": fee_label or "unknown",
            "heat_bucket": "ALL", "regime_week": "ALL",
            "gated_rows": len(items),
            "unique_pools": len({c["pool_address"] for c in items}),
            **s,
        })

    # by gate × fee_label × heat_bucket (all weeks)
    for (gate, fee_label, hb), items in sorted(_grp(enriched, "gate", "fee_label", "heat_bucket").items()):
        s = _stats(items)
        rows.append({
            "scope": "ALL_WEEKS_BY_HEAT",
            "gate": gate, "fee_label": fee_label or "unknown",
            "heat_bucket": hb or "?", "regime_week": "ALL",
            "gated_rows": len(items),
            "unique_pools": len({c["pool_address"] for c in items}),
            **s,
        })

    # by gate × regime_week × fee_label
    for (gate, week, fee_label), items in sorted(_grp(enriched, "gate", "regime_week", "fee_label").items()):
        s = _stats(items)
        rows.append({
            "scope": "BY_WEEK",
            "gate": gate, "fee_label": fee_label or "unknown",
            "heat_bucket": "ALL", "regime_week": week or "?",
            "gated_rows": len(items),
            "unique_pools": len({c["pool_address"] for c in items}),
            **s,
        })

    return rows


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _jdump(path: Path, d: Any) -> None:
    with path.open("w") as f:
        json.dump(d, f, indent=2, default=str)


def _cdump(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("# empty\n")
        return
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_readout(matrix: list[dict], gate_summary: dict, rpc_calls: int, pool_meta: dict) -> None:
    # Focus: fee_100bps canonical vs diagnostic
    ca_all = [r for r in matrix if r["gate"]=="CANONICAL_037G" and r["scope"]=="ALL_WEEKS"]
    db_all = [r for r in matrix if r["gate"]=="DIAGNOSTIC_RELAXED" and r["scope"]=="ALL_WEEKS"]

    def _row_for_fee(rows, fee):
        return next((r for r in rows if r["fee_label"]==fee), None)

    canon_f100  = _row_for_fee(ca_all, "fee_100bps")
    diag_f100   = _row_for_fee(db_all, "fee_100bps")
    canon_f3k   = _row_for_fee(ca_all, "fee_3000bps")
    canon_f500  = _row_for_fee(ca_all, "fee_500bps")
    canon_f10k  = _row_for_fee(ca_all, "fee_10000bps")

    out = ["# Run-053 — fee_100bps Young Pool Expansion\n"]
    out.append(f"**Generated:** {_now()} · **Branch:** `sprint053-fee100-expansion`")
    out.append(f"\n**RPC calls:** {rpc_calls} · **Pools with fee_bps:** {sum(1 for v in pool_meta.values() if v.get('fee_bps'))}")
    out.append(f"\n**Total weeks scanned:** {len(OOS_WEEKS)} (2026-04-05 → 2026-04-26)\n")

    out.append("## Gate results summary\n")
    out.append("| gate | total_gated | fee_100bps_rows | fee_100bps_hit | fee_100bps_pf | fee_100bps_sclass |")
    out.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for gate_label, row in [("CANONICAL_037G", canon_f100), ("DIAGNOSTIC_RELAXED", diag_f100)]:
        if row:
            tg = sum(r["gated_rows"] for r in matrix if r["gate"]==gate_label and r["scope"]=="ALL_WEEKS")
            out.append(f"| {gate_label} | {tg} | {row['gated_rows']} | {row['hit_rate']} | {row['profit_factor']} | {row['sample_class']} |")

    out.append("\n## Sprint050 benchmark vs OOS\n")
    s050_f100 = S050["fee_100bps"]
    out.append("| benchmark | Sprint050 (in-bundle, RPC label) | Sprint053 Canonical | Sprint053 Diagnostic |")
    out.append("| --- | --- | --- | --- |")
    c = f"{canon_f100['gated_rows']}r / hit={canon_f100['hit_rate']} / pf={canon_f100['profit_factor']}" if canon_f100 else "—"
    d = f"{diag_f100['gated_rows']}r / hit={diag_f100['hit_rate']} / pf={diag_f100['profit_factor']}" if diag_f100 else "—"
    out.append(f"| fee_100bps | {s050_f100['rows']}r / hit={s050_f100['hit']} / pf={s050_f100['pf']} | {c} | {d} |")
    out.append(f"| fee_3000bps | {S050['fee_3000bps']['rows']}r / hit={S050['fee_3000bps']['hit']} / pf={S050['fee_3000bps']['pf']} | {'ZERO ROWS' if not canon_f3k or not canon_f3k['gated_rows'] else canon_f3k['gated_rows']} | — |")

    out.append("\n## Per-week fee_100bps breakdown (Canonical gate)\n")
    out.append("| week | gated_rows | labeled | hit | pf | median_ret | top1 | sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in sorted([r for r in matrix if r["gate"]=="CANONICAL_037G" and r["scope"]=="BY_WEEK" and r["fee_label"]=="fee_100bps"], key=lambda x: x["regime_week"]):
        out.append(f"| {r['regime_week']} | {r['gated_rows']} | {r.get('n_labeled',0)} | {r['hit_rate']} | {r['profit_factor']} | {r.get('median_ret')} | {r.get('pool_top1_share')} | {r['sample_class']} |")

    out.append("\n## Diagnostic gate (onset_thresh=5) fee_100bps breakdown by heat\n")
    out.append("| heat_bucket | gated | labeled | hit | pf | median_ret | top1 | sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in sorted([r for r in matrix if r["gate"]=="DIAGNOSTIC_RELAXED" and r["scope"]=="ALL_WEEKS_BY_HEAT" and r["fee_label"]=="fee_100bps"], key=lambda x: x["heat_bucket"]):
        out.append(f"| {r['heat_bucket']} | {r['gated_rows']} | {r.get('n_labeled',0)} | {r['hit_rate']} | {r['profit_factor']} | {r.get('median_ret')} | {r.get('pool_top1_share')} | {r['sample_class']} |")

    out.append("\n## Signal verdict\n")
    if canon_f100 and canon_f100["gated_rows"] >= 300:
        out.append(f"**CANONICAL GATE EXITS SMOKE: {canon_f100['gated_rows']} rows.** hit={canon_f100['hit_rate']}, pf={canon_f100['profit_factor']}")
        if canon_f100["profit_factor"] and canon_f100["profit_factor"] >= 1.5 and (canon_f100["hit_rate"] or 0) >= 0.5:
            out.append("→ `REPRODUCES_SPRINT050_DIRECTION` — candidate for Sprint054 economic readout with cost replay")
        else:
            out.append("→ Signal present but below Sprint050 benchmark — needs more weeks or deeper analysis")
    elif diag_f100 and diag_f100["gated_rows"] >= 300:
        out.append(f"**DIAGNOSTIC GATE EXITS SMOKE: {diag_f100['gated_rows']} rows.** hit={diag_f100['hit_rate']}, pf={diag_f100['profit_factor']}")
        out.append("→ Canonical gate still at smoke level. Use diagnostic finding to frame Sprint054 onset_thresh sensitivity study.")
        out.append("→ **NON-CANONICAL result** — not production-ready; document as exploratory finding only.")
    else:
        c_rows = canon_f100["gated_rows"] if canon_f100 else 0
        d_rows = diag_f100["gated_rows"] if diag_f100 else 0
        out.append(f"Both gates below smoke threshold: canonical={c_rows}, diagnostic={d_rows}")
        out.append("→ Need more weeks or gate redesign. Do NOT claim signal exists.")

    out.append("\n## Decision flags\n")
    out.append("- TRAINING_ALLOWED = `false`")
    out.append("- LIVE_ALLOWED = `false`")
    out.append("- PRIVATE_KEY_USED = `false`")
    out.append("- PRODUCTION_SIGNALS_WRITTEN = `false`")
    out.append("- NO_ALPHA_CLAIM = `true`")
    out.append(f"- GATE_B_IS_NON_CANONICAL = `true` (diagnostic only, onset_thresh=5)")

    txt = "\n".join(out)
    (AUDIT_OUT / "sprint053_fee100_expansion_readout.md").write_text(txt)
    (ROOT / "mvp/runs/run-053-fee100-expansion-readout.md").write_text(txt)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[sprint053] start {_now()}", flush=True)
    assert os.environ.get("LIVE_TRADING_MODE", "").lower() not in ("1","true","yes","on")

    rpc_url = _load_rpc()
    print(f"[sprint053] RPC URL loaded", flush=True)

    # Step 1: pool global first-seen from V3 decoded (all history)
    print(f"[sprint053] pulling global pool first-seen ...", flush=True)
    all_first_seen = pull_pool_first_seen_v3()
    print(f"  {len(all_first_seen)} pools with first-seen data", flush=True)

    # Step 2: pull 5m windows for each OOS week
    all_windows_by_pool: dict[str, dict[str, list[dict]]] = {}  # week → pool → windows
    week_stats: dict[str, dict] = {}
    all_pools: set[str] = set()

    for w_start, w_end in OOS_WEEKS:
        week_label = w_start
        print(f"\n[sprint053] === week {week_label} ===", flush=True)
        try:
            wins = pull_windows_for_week(w_start, w_end)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            week_stats[week_label] = {"error": str(e), "windows": 0, "pools": 0}
            continue
        print(f"  {len(wins)} 5m windows pulled", flush=True)
        by_pool: dict[str, list[dict]] = defaultdict(list)
        for w in wins:
            by_pool[w["pool"]].append(w)
        all_windows_by_pool[week_label] = dict(by_pool)
        pools_wk = set(by_pool.keys())
        all_pools.update(pools_wk)
        week_stats[week_label] = {"windows": len(wins), "pools": len(pools_wk)}

    print(f"\n[sprint053] total unique pools across all weeks: {len(all_pools)}", flush=True)

    # Step 3: RPC fee enrichment for all unique pools
    print(f"\n[sprint053] RPC fee enrichment for {len(all_pools)} pools ...", flush=True)
    pool_meta = rpc_enrich(sorted(all_pools), rpc_url)
    rpc_calls = len(all_pools) * 2
    print(f"  enriched {len(pool_meta)} pools ({rpc_calls} RPC calls)", flush=True)

    # Step 4: apply both gates per week, collect candidates
    all_candidates: list[dict] = []

    for w_start, w_end in OOS_WEEKS:
        week_label = w_start
        by_pool = all_windows_by_pool.get(week_label, {})
        if not by_pool:
            continue
        w_start_ts = int(datetime.strptime(w_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        w_end_ts   = int(datetime.strptime(w_end,   "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

        for gate in (GATE_A, GATE_B):
            cands = apply_gate(by_pool, all_first_seen, w_start_ts, w_end_ts, week_label, gate)
            # add sqrtPriceX96 label
            add_sqrt_label(cands, by_pool, pool_meta)
            all_candidates.extend(cands)
            print(f"  {week_label} {gate['label']}: {len(cands)} gated", flush=True)

    print(f"\n[sprint053] total candidates: {len(all_candidates)}", flush=True)

    # Step 5: build matrix
    matrix = build_matrix(all_candidates, pool_meta)

    # Write artifacts
    _jdump(DIAG_OUT / "oos_reproduction_matrix.json", matrix)
    _cdump(DIAG_OUT / "oos_reproduction_matrix.csv", matrix)
    _jdump(DIAG_OUT / "oos_candidates_gated.json", all_candidates)
    _cdump(DIAG_OUT / "oos_candidates_gated.csv",
           [{k: v for k, v in c.items() if k not in ("last_sqrt",)} for c in all_candidates])
    _jdump(DIAG_OUT / "pool_meta.json", pool_meta)
    _jdump(DIAG_OUT / "week_stats.json", week_stats)
    write_readout(matrix, week_stats, rpc_calls, pool_meta)

    # Print summary
    print(f"\n[sprint053] matrix rows: {len(matrix)}", flush=True)
    print("[sprint053] ALL_WEEKS fee summary:", flush=True)
    for r in sorted([r for r in matrix if r["scope"]=="ALL_WEEKS"],
                    key=lambda x: (-x["gated_rows"])):
        print(f"  {r['gate']} {r['fee_label']}: rows={r['gated_rows']} hit={r['hit_rate']} pf={r['profit_factor']} sclass={r['sample_class']}", flush=True)

    print(f"\n[sprint053] artifacts written to {DIAG_OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
