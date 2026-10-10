#!/usr/bin/env python3
"""Sprint055-B — young-pool-onset Paper Shadow (live, no real trades).

Sprint087 expansion (SPRINT087_UNIVERSE_V2=1, default):
  - pool_age ≤ 172800s (48h), onset ≤ 7200s (2h), heat ≥ 5 swaps/5m
  - fee tiers: 100 / 500 / 3000 / 10000 only (Uniswap V3 standard)
  - hard exclude is_0x6ff5=True pools at entry
  - first-signal-only: 24h cooldown per pool (no echo-alpha repeats)
  - output: logs/sprint087_expanded_paper_shadow.csv

Legacy mode (SPRINT087_UNIVERSE_V2=0):
  - pool_age ≤ 86400s (24h), onset ≤ 3600s (60m), heat ≥ 10 swaps/5m
  - fee_bps < 10000 via RPC, 15min cooldown

For each candidate: records entry quote (QuoterV2 WETH→TOKEN) then records exit
quotes at +5m, +10m, +15m, +30m, +1h, +2h, +4h, +6h, +12h, +24h. NO real trades.
Sprint057 finding: alpha is concentrated in ≤30m; 1h+ horizons are for research only.

Also flags composite signal: heat ≥ 20 + weth_is_token1 + bpr < 0.55

State file: mvp/diagnostics/sprint055/paper_shadow_state.json
  — tracks pending candidates with per-horizon exit marks.

Output: logs/sprint055_paper_shadow.csv  (one row per signal; exit columns filled in-place)
Daily summary: mvp/diagnostics/sprint055/paper_shadow_daily_{YYYY-MM-DD}.json

Run: python3 scripts/sprint055_fee100_paper_shadow.py [--once] [--summarise]
     --once: run one poll cycle and exit (for cron: */5 * * * *)
     --summarise: print current running statistics and exit
"""
from __future__ import annotations

import argparse
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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib.v3_quoterv2_struct import (
    JsonRpcClient, WETH_BASE, quote_exact_input_single_struct,
)
from scripts.lib.real_cost_model import CostBreakdown, RealCostModel

ROOT        = Path(__file__).resolve().parents[1]

# Track B support: override paths/filter via env vars so Track B runs from a
# separate cron entry without touching Track A state or CSV.
# TRACK_SUFFIX=_B  FEE_FILTER_ONLY=500  LOG_CSV_OVERRIDE=sprint083_fee500_paper_shadow.csv
_TRACK_SUFFIX   = os.environ.get("TRACK_SUFFIX", "")          # e.g. "" or "_B"
_FEE_FILTER_ONLY = int(os.environ.get("FEE_FILTER_ONLY", "0")) # 0 = accept whitelist / all
_CSV_OVERRIDE   = os.environ.get("LOG_CSV_OVERRIDE", "")       # relative to logs/
_SPRINT087      = os.environ.get("SPRINT087_UNIVERSE_V2", "1").lower() in ("1", "true", "yes")

if _CSV_OVERRIDE:
    LOG_CSV    = ROOT / "logs" / _CSV_OVERRIDE
    _track_id  = _CSV_OVERRIDE.replace(".csv","")
    STATE_JSON = ROOT / f"mvp/diagnostics/{_track_id}/paper_shadow_state.json"
    DIAG_OUT   = ROOT / f"mvp/diagnostics/{_track_id}"
elif _SPRINT087 and not _FEE_FILTER_ONLY:
    LOG_CSV     = ROOT / "logs/sprint087_expanded_paper_shadow.csv"
    STATE_JSON  = ROOT / "mvp/diagnostics/sprint087_expanded/paper_shadow_state.json"
    DIAG_OUT    = ROOT / "mvp/diagnostics/sprint087_expanded"
else:
    LOG_CSV     = ROOT / "logs/sprint055_paper_shadow.csv"
    STATE_JSON  = ROOT / "mvp/diagnostics/sprint055/paper_shadow_state.json"
    DIAG_OUT    = ROOT / "mvp/diagnostics/sprint055"
DIAG_OUT.mkdir(parents=True, exist_ok=True)
LOG_CSV.parent.mkdir(parents=True, exist_ok=True)

# Sprint080 sender map: loaded once at startup. Maps pool_address → sender metadata.
# New pools not yet in the map get is_0x6ff5='' (backfilled by sprint080 periodic run).
_SENDER_MAP_PATH = ROOT / "logs/sprint080_sender_map.json"
_SENDER_MAP: dict = {}

def _load_sender_map() -> None:
    global _SENDER_MAP
    if not _SENDER_MAP_PATH.exists():
        return
    try:
        raw = json.loads(_SENDER_MAP_PATH.read_text())
        _SENDER_MAP = {k.lower(): v for k, v in raw.items()}
    except Exception:
        pass

_load_sender_map()

def _sender_fields(pool: str) -> dict:
    """Return sender-dimension fields for a pool from the cached sprint080 map.

    For pools not yet in the map, returns empty strings so the bridge skips them.
    Sprint080 backfill should be run periodically (e.g. daily) to fill new pools.
    """
    entry = _SENDER_MAP.get(pool.lower(), {})
    if not entry:
        return {
            "dominant_sender": "",
            "dom_sender_top_swaps": "",
            "dom_sender_total_swaps": "",
            "dom_sender_share": "",
            "is_0x6ff5": "",
        }
    dom = str(entry.get("dominant_sender", "")).lower()
    is_6ff5 = "True" if dom == SENDER_0X6FF5 else "False"
    return {
        "dominant_sender":       dom,
        "dom_sender_top_swaps":  entry.get("dom_sender_top_swaps", ""),
        "dom_sender_total_swaps": entry.get("dom_sender_total_swaps", ""),
        "dom_sender_share":      entry.get("dom_sender_share", ""),
        "is_0x6ff5":             is_6ff5,
    }

CH_CONTAINER   = "eatlq-clickhouse-1"
CHAIN_ID       = 8453
WINDOW_SEC     = 300          # 5 min
SENDER_0X6FF5  = "0x6ff5693b99212da76ad316178a184ab56d299b43"

if _SPRINT087:
    POOL_MAX_AGE   = int(os.environ.get("POOL_MAX_AGE", "172800"))   # 48h
    ONSET_MAX      = int(os.environ.get("ONSET_MAX", "7200"))        # 2h
    ONSET_THRESH   = int(os.environ.get("ONSET_THRESH", "5"))
    COOLDOWN_SEC   = int(os.environ.get("COOLDOWN_SEC", "86400"))    # 24h first-signal lock
    FIRST_SIGNAL_WINDOW_SEC = int(os.environ.get("FIRST_SIGNAL_WINDOW_SEC", "86400"))
    EXCLUDE_0X6FF5 = os.environ.get("EXCLUDE_0X6FF5", "1").lower() in ("1", "true", "yes")
    ALLOWED_FEE_BPS: set[int] | None = {100, 500, 3000, 10000}
    UNIQUE_POOLS_MIN = int(os.environ.get("UNIQUE_POOLS_MIN", "30"))
    TOP_POOL_SHARE_MAX = float(os.environ.get("TOP_POOL_SHARE_MAX", "0.15"))
else:
    POOL_MAX_AGE   = 86400
    ONSET_MAX      = 3600
    ONSET_THRESH   = 10
    COOLDOWN_SEC   = 15 * 60
    FIRST_SIGNAL_WINDOW_SEC = COOLDOWN_SEC
    EXCLUDE_0X6FF5 = False
    ALLOWED_FEE_BPS = None
    UNIQUE_POOLS_MIN = 30
    TOP_POOL_SHARE_MAX = 0.15

ENTRY_WETH_RAW = int(0.00005 * 10**18)  # 50,000 gwei micro notional
GAS_PCT        = 0.002        # gas proxy (legacy _ag columns)
REAL_COST_ENABLED     = os.environ.get("REAL_COST_ENABLED", "1").lower() in ("1", "true", "yes")
REAL_COST_NOTIONAL_USD = float(os.environ.get("REAL_COST_NOTIONAL_USD", "10"))
REAL_COST_FALLBACK_RPC = os.environ.get("REAL_COST_FALLBACK_RPC", "https://mainnet.base.org")
SEL_FEE        = "0xddca3f43"
SEL_TOKEN0     = "0x0dfe1681"
SEL_TOKEN1     = "0xd21220a7"
RPC_DELAY      = 0.04

# multi-horizon exit marks (seconds after entry signal)
# Sprint057: extended from 30m to 24h per horizon sweep findings
EXIT_HORIZONS  = [300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
EXIT_LABELS    = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]

# composite signal gates (corrected in Sprint056E)
# weth_is_token1 pools have median heat=75 → no upper heat cap
COMPOSITE_BPR_MAX  = 0.55
COMPOSITE_HEAT_MIN = 20
COMPOSITE_HEAT_MAX = None  # no upper cap

# 7-day acceptance thresholds
TARGET_SIGNALS  = 30
TARGET_HIT      = 0.45
TARGET_PF       = 1.2
TARGET_QUOTE_RT = 0.80


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_env_rpc() -> str:
    env = ROOT / "infra/compose/.env"
    if env.exists():
        for l in env.read_text().splitlines():
            if "=" not in l or l.startswith("#"): continue
            k, v = l.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v: return v
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")


def _ch(sql: str, timeout: int = 60) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:200]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text())
    return {"pending": [], "pool_fee_cache": {}, "pool_token_cache": {}, "pool_last_close": {}}


def save_state(state: dict) -> None:
    STATE_JSON.write_text(json.dumps(state, indent=2, default=str))


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def get_pool_fee(rpc: JsonRpcClient, pool: str, cache: dict) -> int | None:
    if pool in cache:
        return cache[pool]
    try:
        resp = rpc.session.post(rpc.url, json={"jsonrpc":"2.0","id":1,"method":"eth_call",
                                               "params":[{"to":pool,"data":SEL_FEE},"latest"]}, timeout=15)
        raw = resp.json().get("result", "0x")
        v = int(raw[2:][-8:], 16) if raw and raw != "0x" else None
        cache[pool] = v
        time.sleep(RPC_DELAY)
        return v
    except Exception:
        return None


def get_pool_tokens(rpc: JsonRpcClient, pool: str, token_cache: dict) -> tuple[str | None, str | None]:
    if pool in token_cache:
        cached = token_cache[pool]
        return cached.get("token0"), cached.get("token1")
    token0, token1 = None, None
    for sel, name in [(SEL_TOKEN0, "token0"), (SEL_TOKEN1, "token1")]:
        try:
            resp = rpc.session.post(rpc.url, json={"jsonrpc":"2.0","id":1,"method":"eth_call",
                                                   "params":[{"to":pool,"data":sel},"latest"]}, timeout=15)
            raw = resp.json().get("result", "0x")
            if raw and len(raw) >= 42:
                addr = ("0x" + raw[-40:]).lower()
                if name == "token0": token0 = addr
                else: token1 = addr
        except Exception:
            pass
        time.sleep(RPC_DELAY)
    token_cache[pool] = {"token0": token0, "token1": token1}
    return token0, token1


def _get_latest_block(rpc: JsonRpcClient) -> int:
    try:
        result = rpc.call("eth_blockNumber", [])
        return int(result, 16)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# CH: discover onset candidates (last 90 min window)
# ---------------------------------------------------------------------------
#
# v2 (sprint069_ch_oom_repair): Three-phase bounded query to prevent OOM.
#
# Root cause of original OOM:
#   - uniswap_v3_swaps_decoded → uniswap_v3_swaps_deduped → uniswap_v3_swaps
#   - deduped view uses argMax(block_time, ingested_at) across ALL 8.5M rows
#   - outer WHERE block_time >= X cannot be pushed through the argMax GROUP BY
#   - full table scan → 7+ GB memory → CH Code 241 server OOM
#   - failure handler used `return 0`, silently suppressing all candidates
#
# Fix: use raw uniswap_v3_swaps table directly with received_at_ms (partition key)
#   Phase 1: get recently-active pool list (received_at_ms >= now - lookback_min - 1m)
#            → partition-pruned to current month, ~0.26s, returns O(10) pools
#   Phase 2A: heat scan on raw table with pool IN(phase1_list)
#              → uses sort key (chain_id, pool_address, ...) for fast index seek
#              → inline amount0 decode (same arithmetic as decoded view)
#              → ~0.79s, no dedup overhead, <1% duplicate rate on recent data
#   Phase 2B: first_seen on raw table bounded to last 25h
#              → received_at_ms partition prune, pool IN(candidate_set)
#              → sufficient for pool_age ≤ POOL_MAX_AGE = 86400s check
#              → ~0.23s
#
# Onset semantics: UNCHANGED (ONSET_THRESH, POOL_MAX_AGE, ONSET_MAX, cooldown all preserved)
#
# Fallback: if Phase 2A primary window OOMs, retry as 3x shard of (lookback/3) minutes each,
#           merge-dedup results. If all shards fail → raise DiscoveryDegraded (NOT return 0).

# Inline SQL expression to decode hex-encoded Int256 amount0 from raw table
_AMOUNT0_LT0 = (
    "reinterpretAsInt256(reverse(unhex("
    "if(startsWith(amount0_raw,'0x'), substring(amount0_raw,3), amount0_raw)"
    "))) < 0"
)

# CH query memory cap per query (3 GiB) — prevents single discovery query from
# exhausting server memory budget (server max = 7.5 GiB).
_CH_QUERY_MEM = "max_memory_usage = 3000000000"


class DiscoveryDegraded(RuntimeError):
    """Raised when discover_candidates() fails all fallback attempts.
    Caller must NOT return 0; log DISCOVERY_DEGRADED=true and continue poll cycle.
    """


def _phase1_active_pools(lookback_min: int, now_ts: int) -> list[str]:
    """Phase 1: list of pool_addresses active in the last lookback_min minutes.
    Uses received_at_ms (partition key) for fast scan. No fee/WETH filter —
    preserves original all-pool semantics; fee gate is applied via RPC later.
    """
    # Add 1 minute buffer to account for ingestion lag
    buf_ts = now_ts - (lookback_min + 1) * 60
    buf_dt = datetime.fromtimestamp(buf_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = _ch(f"""
SELECT DISTINCT pool_address
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND received_at_ms >= toDateTime64('{buf_dt}', 3, 'UTC')
SETTINGS {_CH_QUERY_MEM}
""", timeout=15)
    return [r[0] for r in rows if r]


def _phase2a_heat_scan(pool_list: list[str], from_dt: str, to_dt: str) -> dict[str, list[dict]]:
    """Phase 2A: per-pool 5m bucket heat scan on raw table.
    Bounded by received_at_ms (partition key) + pool IN(pool_list).
    """
    pl = ",".join(f"'{p}'" for p in pool_list)
    rows = _ch(f"""
SELECT
    pool_address,
    toUnixTimestamp(toStartOfFiveMinutes(block_time)) AS wts,
    count() AS heat,
    countIf({_AMOUNT0_LT0}) AS buys,
    min(toUnixTimestamp(block_time)) AS min_ts
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND received_at_ms >= toDateTime64('{from_dt}', 3, 'UTC')
  AND received_at_ms <  toDateTime64('{to_dt}',   3, 'UTC')
  AND block_time IS NOT NULL
  AND pool_address IN ({pl})
GROUP BY pool_address, wts
HAVING count() >= {ONSET_THRESH}
ORDER BY pool_address, wts
SETTINGS {_CH_QUERY_MEM}
""", timeout=25)
    windows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if len(r) >= 5:
            try:
                windows[r[0]].append({
                    "wts": int(r[1]), "heat": int(r[2]),
                    "buys": int(r[3]), "min_ts": int(r[4]),
                })
            except Exception:
                pass
    return dict(windows)


def _phase2b_first_seen(pool_list: list[str], now_ts: int) -> dict[str, int]:
    """Phase 2B: true global first_seen per pool.
    Uses (chain_id, pool_address) sort key index seek — no block_time time
    bound needed. CH can efficiently find the minimum block_time for a small
    pool list via index seek even across all partitions (~1.3s for O(10) pools).
    A time-bounded approximation would produce false pool_age values for
    long-lived pools with intermittent activity.
    """
    pl = ",".join(f"'{p}'" for p in pool_list)
    rows = _ch(f"""
SELECT pool_address, toUnixTimestamp(min(block_time)) AS first_ts
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND block_time IS NOT NULL
  AND pool_address IN ({pl})
GROUP BY pool_address
SETTINGS {_CH_QUERY_MEM}
""", timeout=30)
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


def discover_candidates(lookback_min: int = 90) -> list[dict]:
    """Find pools with young_pool_onset in last lookback_min minutes.

    v2: three-phase bounded query (see module comment above for root cause / fix).
    Onset gate semantics are UNCHANGED: pool_age ≤ 86400s, onset_lag ≤ 3600s,
    heat ≥ 10 per 5m window.

    Raises DiscoveryDegraded if all fallback shards fail (never returns empty
    list as a silent proxy for OOM).
    """
    now_ts  = int(datetime.now(timezone.utc).timestamp())
    from_ts = now_ts - lookback_min * 60
    from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    now_dt  = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Phase 1: active pool universe
    try:
        pool_list = _phase1_active_pools(lookback_min, now_ts)
    except Exception as e:
        raise DiscoveryDegraded(f"phase1_failed: {e}") from e
    if not pool_list:
        return []

    # Phase 2A: heat scan — primary attempt, then shard fallback
    windows: dict[str, list[dict]] = {}
    errors: list[str] = []

    try:
        windows = _phase2a_heat_scan(pool_list, from_dt, now_dt)
    except Exception as e:
        errors.append(f"primary({lookback_min}m): {e}")
        # Shard fallback: split into 3 equal sub-windows, merge+dedup
        shard_min = max(lookback_min // 3, 10)
        for i in range(3):
            shard_end_ts   = now_ts - i * shard_min * 60
            shard_start_ts = shard_end_ts - shard_min * 60
            s_from = datetime.fromtimestamp(shard_start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            s_to   = datetime.fromtimestamp(shard_end_ts,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            try:
                partial = _phase2a_heat_scan(pool_list, s_from, s_to)
                for pool, wins in partial.items():
                    if pool not in windows:
                        windows[pool] = wins
                    else:
                        seen_wts = {w["wts"] for w in windows[pool]}
                        windows[pool].extend(w for w in wins if w["wts"] not in seen_wts)
            except Exception as e2:
                errors.append(f"shard{i}({shard_min}m): {e2}")

        if not windows and len(errors) >= 4:
            raise DiscoveryDegraded("; ".join(errors))

    if not windows:
        return []

    # Phase 2B: first_seen lookup (bounded to 25h)
    try:
        first_seen = _phase2b_first_seen(list(windows.keys()), now_ts)
    except Exception as e:
        raise DiscoveryDegraded(f"phase2b_failed: {e}") from e

    # Build candidates — onset gate logic UNCHANGED
    candidates = []
    for pool, wins in windows.items():
        fs = first_seen.get(pool)
        if fs is None:
            continue
        onset_ts = None
        for w in sorted(wins, key=lambda x: x["wts"]):
            if w["heat"] >= ONSET_THRESH:
                onset_ts = w["wts"]
                break
        if onset_ts is None:
            continue
        for w in sorted(wins, key=lambda x: x["wts"]):
            pool_age  = w["wts"] - fs
            onset_lag = w["wts"] - onset_ts
            if pool_age  > POOL_MAX_AGE: continue
            if onset_lag > ONSET_MAX:    continue
            n  = w["heat"]
            bp = w["buys"] / n if n > 0 else 0.0
            candidates.append({
                "pool":               pool,
                "wts":                w["wts"],
                "pool_age_sec":       pool_age,
                "onset_lag_sec":      onset_lag,
                "activity_heat":      n,
                "buy_pressure_ratio": round(bp, 4),
                "price_velocity_5m":  round(2 * bp - 1, 4),
            })
    return candidates


# ---------------------------------------------------------------------------
# Quote helpers
# ---------------------------------------------------------------------------

def entry_quote(rpc: JsonRpcClient, token_other: str, fee_bps: int) -> dict:
    """WETH → TOKEN entry at current block."""
    block = _get_latest_block(rpc)
    q = quote_exact_input_single_struct(rpc, WETH_BASE, token_other, ENTRY_WETH_RAW, fee_bps,
                                        block_identifier=block)
    return {"ok": q.get("ok", False), "amount_out": int(q.get("amount_out", 0)),
            "entry_block": block, "fail_reason": q.get("reason", "")}


def exit_quote_at_horizon(rpc: JsonRpcClient, token_other: str, fee_bps: int,
                           token_amount: int, label: str) -> dict:
    """TOKEN → WETH exit at current block. label = '5m'/'10m'/'15m'/'30m'."""
    block = _get_latest_block(rpc)
    q = quote_exact_input_single_struct(rpc, token_other, WETH_BASE, token_amount, fee_bps,
                                        block_identifier=block)
    exit_weth = int(q.get("amount_out", 0)) if q.get("ok") else 0
    net_ret = exit_weth / ENTRY_WETH_RAW - 1.0 if q.get("ok") and ENTRY_WETH_RAW > 0 else None
    return {
        "ok":            q.get("ok", False),
        "exit_block":    block,
        "net_return":    round(net_ret, 8) if net_ret is not None else None,
        "net_return_ag": round(net_ret - GAS_PCT, 8) if net_ret is not None else None,
        "fail_reason":   q.get("reason", ""),
        "label":         label,
    }


# ---------------------------------------------------------------------------
# CSV output — multi-horizon schema
# ---------------------------------------------------------------------------
#
# TIMING SEMANTICS (Sprint057S clarification — see mvp/audits/sprint057s/timing_semantics.md)
#
#   signal_time      = CH 5m bucket start (UTC ISO); this is signal_window_start.
#                      Feature window = [signal_time, signal_time + 300s).
#   signal_window_end= signal_time + 300s (the moment the feature window closes).
#   detection_time   = wall-clock time of cron CH query; always >= signal_window_end.
#   entry_block      = block obtained at entry_quote_time >= detection_time >= signal_window_end.
#                      Features CANNOT see this block — they come from the closed window.
#   exit_due_time_{h}= entry_quote_time + h_seconds.
#                      EXIT TIMERS ARE ANCHORED TO entry_quote_time, NOT TO signal_time.
#                      This means paper net_return_{h} measures holding from entry quote,
#                      not from wts+h. Do not compare directly with historical replay
#                      net_return_{h} which uses wts+h as the exit anchor.
#   exit_block       = block at actual exit_quote_time >= exit_due_time_{h}.
#
# HORIZON SEMANTICS (Sprint057S):
#   5m/10m/15m/30m = PAPER_VALIDATION: real QuoterV2 calls; gate-eligible.
#   1h/2h/4h/6h/12h/24h = RESEARCH_ONLY: real QuoterV2 calls, but long-horizon
#                          forward paper research; NOT live gate triggers.
#                          Historical Sprint057 sweep used sqrtPriceX96 proxy for these;
#                          paper shadow uses real QuoterV2 — these are different evidence.

CSV_COLS = [
    # signal identity
    # signal_time = CH 5m bucket start = signal_window_start (feature window begins here)
    "signal_time", "pool", "token_other", "fee_bps", "weth_is_token1",
    "pool_age_sec", "onset_lag_sec",
    "activity_heat", "buy_pressure_ratio", "price_velocity_5m",
    # composite flag (Sprint056C candidate)
    "composite_flag",
    # entry
    "entry_block", "entry_quote_ok", "entry_amount_out_tokens",
    # 5m exit
    "exit_5m_ok", "exit_5m_block", "net_return_5m", "net_return_5m_ag",
    # 10m exit
    "exit_10m_ok", "exit_10m_block", "net_return_10m", "net_return_10m_ag",
    # 15m exit
    "exit_15m_ok", "exit_15m_block", "net_return_15m", "net_return_15m_ag",
    # 30m exit
    "exit_30m_ok", "exit_30m_block", "net_return_30m", "net_return_30m_ag",
    # extended horizons (Sprint057) — sqrtPriceX96 regime fades past 30m; recorded for research only
    "exit_1h_ok",  "exit_1h_block",  "net_return_1h",  "net_return_1h_ag",
    "exit_2h_ok",  "exit_2h_block",  "net_return_2h",  "net_return_2h_ag",
    "exit_4h_ok",  "exit_4h_block",  "net_return_4h",  "net_return_4h_ag",
    "exit_6h_ok",  "exit_6h_block",  "net_return_6h",  "net_return_6h_ag",
    "exit_12h_ok", "exit_12h_block", "net_return_12h", "net_return_12h_ag",
    "exit_24h_ok", "exit_24h_block", "net_return_24h", "net_return_24h_ag",
    # real-cost returns (Sprint086 inline — gas + slippage + sell-path at REAL_COST_NOTIONAL_USD)
    "net_return_5m_real", "net_return_10m_real", "net_return_15m_real", "net_return_30m_real",
    "net_return_1h_real", "net_return_2h_real", "net_return_4h_real", "net_return_6h_real",
    "net_return_12h_real", "net_return_24h_real",
    # meta
    "gas_proxy_pct", "entry_fail_reason",
    # real-cost breakdown at entry (Sprint086)
    "gas_real_pct", "slip_real_buy_pct", "slip_real_sell_pct", "pool_fee_pct",
    "sell_path_ok", "honeypot_flag", "rpc_error", "liquidity_depth_ok",
    "cost_total_pct", "cost_status",
    # sprint080 sender dimension (backfilled from sender map; empty for new unknown pools)
    "dominant_sender", "dom_sender_top_swaps", "dom_sender_total_swaps",
    "dom_sender_share", "is_0x6ff5",
]

# Key used to match rows for in-place update
ROW_KEY = ("signal_time", "pool")


def fee_tier_allowed(fee_bps: int) -> bool:
    if ALLOWED_FEE_BPS is not None and int(fee_bps) not in ALLOWED_FEE_BPS:
        return False
    if _FEE_FILTER_ONLY and int(fee_bps) != _FEE_FILTER_ONLY:
        return False
    if ALLOWED_FEE_BPS is None and int(fee_bps) >= 10000:
        return False
    return True


def _parse_signal_ts(signal_time: str) -> int | None:
    if not signal_time:
        return None
    try:
        return int(datetime.fromisoformat(signal_time.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def pool_has_recent_signal(pool: str, now_ts: int, window_sec: int) -> bool:
    """True if CSV already has a row for this pool within window_sec."""
    if not LOG_CSV.exists():
        return False
    pool_l = pool.lower()
    with LOG_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("pool") or "").lower() != pool_l:
                continue
            ts = _parse_signal_ts(r.get("signal_time", ""))
            if ts is not None and now_ts - ts < window_sec:
                return True
    return False


def first_signal_allowed(pool: str, now_ts: int, state: dict) -> tuple[bool, str]:
    """24h first-signal-only gate: one entry row per pool per window."""
    pool_l = pool.lower()
    window = FIRST_SIGNAL_WINDOW_SEC

    last_close = int(state.get("pool_last_close", {}).get(pool, 0) or 0)
    if now_ts < last_close + window:
        return False, "cooldown_state"

    last_fs = int(state.get("pool_first_signal_ts", {}).get(pool_l, 0) or 0)
    if last_fs and now_ts < last_fs + window:
        return False, "first_signal_state"

    if pool_has_recent_signal(pool, now_ts, window):
        return False, "first_signal_csv"

    return True, "ok"


def mark_pool_signal(pool: str, now_ts: int, state: dict) -> None:
    pool_l = pool.lower()
    state.setdefault("pool_last_close", {})[pool] = now_ts
    state.setdefault("pool_first_signal_ts", {})[pool_l] = now_ts


def make_cost_model(rpc: JsonRpcClient) -> RealCostModel | None:
    if not REAL_COST_ENABLED:
        return None
    fallback = JsonRpcClient(REAL_COST_FALLBACK_RPC, delay_s=0.1)
    return RealCostModel(rpc=rpc, fallback_rpc=fallback)


def cost_breakdown_to_row(cb: CostBreakdown) -> dict:
    return {
        "gas_real_pct":        round(cb.gas_buy_pct + cb.gas_sell_pct, 8),
        "slip_real_buy_pct":   round(cb.slippage_buy_pct, 8),
        "slip_real_sell_pct":  round(cb.slippage_sell_pct, 8),
        "pool_fee_pct":        round(cb.pool_fee_pct, 8),
        "sell_path_ok":        str(cb.sell_path_ok),
        "honeypot_flag":       str(cb.honeypot_flag),
        "rpc_error":           str(cb.rpc_error),
        "liquidity_depth_ok":  str(cb.liquidity_depth_ok),
        "cost_total_pct":      round(cb.round_trip_cost_pct(), 8),
        "cost_status":         cb.sell_revert_reason or "ok",
    }


def cost_snap_from_breakdown(cb: CostBreakdown) -> dict:
    """JSON-serializable cost snapshot for state['pending']."""
    return {
        "sell_path_ok":   cb.sell_path_ok,
        "honeypot_flag":  cb.honeypot_flag,
        "rpc_error":      cb.rpc_error,
        "cost_total_pct": cb.round_trip_cost_pct(),
    }


def net_return_real(gross: float | None, cost_snap: dict | CostBreakdown | None) -> str | float:
    if gross is None or cost_snap is None:
        return ""
    if isinstance(cost_snap, CostBreakdown):
        cb = cost_snap
    else:
        rpc_err = bool(cost_snap.get("rpc_error"))
        honeypot = bool(cost_snap.get("honeypot_flag"))
        sell_ok = bool(cost_snap.get("sell_path_ok", True))
        if rpc_err and not honeypot:
            return ""
        if not sell_ok or honeypot:
            return -1.0
        return round(float(gross) - float(cost_snap.get("cost_total_pct", 0)), 8)
    if cb.rpc_error and not cb.honeypot_flag:
        return ""
    net = cb.apply_to_gross_return(float(gross))
    if isinstance(net, float) and math.isnan(net):
        return ""
    return round(net, 8)


def ensure_csv_header() -> None:
    if not LOG_CSV.exists():
        with LOG_CSV.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writeheader()
        return
    # Migrate existing CSV if new Sprint086 columns were added
    with LOG_CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        existing = list(reader.fieldnames or [])
    missing = [c for c in CSV_COLS if c not in existing]
    if not missing:
        return
    rows = list(csv.DictReader(LOG_CSV.open(newline="")))
    for r in rows:
        for c in missing:
            r.setdefault(c, "")
    with LOG_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_entry_row(row: dict) -> None:
    """Write a new row with entry data (exit columns blank)."""
    ensure_csv_header()
    with LOG_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writerow(row)


def update_csv_exit(signal_time: str, pool: str, horizon_label: str, q: dict,
                    cost_snap: dict | CostBreakdown | None = None) -> None:
    """Find the row matching signal_time+pool and fill in the horizon exit columns."""
    if not LOG_CSV.exists():
        return
    rows = list(csv.DictReader(LOG_CSV.open()))
    found = False
    pfx = f"exit_{horizon_label}"
    for r in rows:
        if r.get("signal_time") == signal_time and r.get("pool") == pool:
            r[f"{pfx}_ok"]    = q["ok"]
            r[f"{pfx}_block"] = q.get("exit_block", "")
            r[f"net_return_{horizon_label}"]    = q.get("net_return", "")
            r[f"net_return_{horizon_label}_ag"] = q.get("net_return_ag", "")
            real = net_return_real(q.get("net_return"), cost_snap)
            if real != "":
                r[f"net_return_{horizon_label}_real"] = real
            found = True
            break
    if not found:
        return
    with LOG_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def _horizon_stats(rows: list[dict], label: str) -> dict:
    """Compute hit/PF/mean for a given exit horizon label."""
    col_ag = f"net_return_{label}_ag"
    labeled = [r for r in rows if r.get(f"exit_{label}_ok") == "True" and r.get(col_ag)]
    if not labeled:
        return {"n": 0, "hit": None, "pf": None, "mean": None}
    rets  = [float(r[col_ag]) for r in labeled]
    pos   = [r for r in rets if r > 0]
    neg   = [-r for r in rets if r <= 0]
    pf    = round(sum(pos)/sum(neg), 3) if neg else None
    return {
        "n":    len(rets),
        "hit":  round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "pf":   pf,
        "mean": round(statistics.mean(rets), 6),
    }


def _horizon_stats_real(rows: list[dict], label: str) -> dict:
    """Compute hit/PF/mean using net_return_{label}_real (Sprint086 inline cost)."""
    col = f"net_return_{label}_real"
    labeled = [r for r in rows if r.get(f"exit_{label}_ok") == "True" and r.get(col) not in ("", None)]
    if not labeled:
        return {"n": 0, "hit": None, "pf": None, "mean": None}
    rets = []
    for r in labeled:
        try:
            rets.append(float(r[col]))
        except (TypeError, ValueError):
            continue
    if not rets:
        return {"n": 0, "hit": None, "pf": None, "mean": None}
    pos = [r for r in rets if r > 0]
    neg = [-r for r in rets if r <= 0]
    pf_val = round(sum(pos)/sum(neg), 3) if neg else None
    return {
        "n":    len(rets),
        "hit":  round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "pf":   pf_val,
        "mean": round(statistics.mean(rets), 6),
    }


def _max_drawdown(rets: list[float]) -> float:
    """Maximum peak-to-trough drawdown on cumulative equity."""
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rets:
        eq += r
        peak = max(peak, eq)
        dd   = max(dd, peak - eq)
    return round(dd, 6)


def compute_and_save_daily_summary() -> dict:
    if not LOG_CSV.exists():
        return {}
    rows = list(csv.DictReader(LOG_CSV.open()))
    if not rows:
        return {}

    entry_ok  = sum(1 for r in rows if r.get("entry_quote_ok") == "True")
    composite = [r for r in rows if r.get("composite_flag") == "True"]
    pool_cnt  = Counter(r["pool"] for r in rows if r.get("entry_quote_ok") == "True")
    total_entry_pools = sum(pool_cnt.values()) or 1
    top_pool_share = round(pool_cnt.most_common(1)[0][1] / total_entry_pools, 4) if pool_cnt else 0.0

    # Per-horizon stats
    horizon_stats: dict[str, dict] = {}
    horizon_stats_real: dict[str, dict] = {}
    for lbl in EXIT_LABELS:
        horizon_stats[lbl] = _horizon_stats(rows, lbl)
        horizon_stats_real[lbl] = _horizon_stats_real(rows, lbl)

    # 5m drawdown (main horizon)
    h5 = horizon_stats.get("5m", {})
    h30_real = horizon_stats_real.get("30m", {})
    rets_5m = []
    for r in rows:
        v = r.get("net_return_5m_ag")
        if v and r.get("exit_5m_ok") == "True":
            try: rets_5m.append(float(v))
            except: pass

    diversity_ok = (
        len(pool_cnt) >= UNIQUE_POOLS_MIN
        and top_pool_share <= TOP_POOL_SHARE_MAX
    )

    summary = {
        "date":           datetime.now(timezone.utc).date().isoformat(),
        "track":          "sprint087_expanded" if _SPRINT087 else "sprint055_legacy",
        "csv_path":       str(LOG_CSV.relative_to(ROOT)),
        "sprint087_universe_v2": _SPRINT087,
        "signals_total":  len(rows),
        "entry_quote_ok": entry_ok,
        "composite_signals": len(composite),
        "unique_pools":   len(pool_cnt),
        "pool_top1_share": top_pool_share,
        "diversity_gate": {
            "unique_pools_min": UNIQUE_POOLS_MIN,
            "unique_pools_ok": len(pool_cnt) >= UNIQUE_POOLS_MIN,
            "top_pool_share_max": TOP_POOL_SHARE_MAX,
            "top_pool_share_ok": top_pool_share <= TOP_POOL_SHARE_MAX,
            "overall_ok": diversity_ok,
        },
        "onset_params": {
            "pool_max_age_sec": POOL_MAX_AGE,
            "onset_max_sec": ONSET_MAX,
            "onset_thresh": ONSET_THRESH,
            "cooldown_sec": COOLDOWN_SEC,
            "first_signal_window_sec": FIRST_SIGNAL_WINDOW_SEC,
            "exclude_0x6ff5": EXCLUDE_0X6FF5,
            "allowed_fee_bps": sorted(ALLOWED_FEE_BPS) if ALLOWED_FEE_BPS else "legacy_lt_10000",
        },
        "horizons": {
            lbl: horizon_stats[lbl] for lbl in EXIT_LABELS
        },
        "horizons_real": {
            lbl: horizon_stats_real[lbl] for lbl in EXIT_LABELS
        },
        "max_drawdown_5m": _max_drawdown(rets_5m) if rets_5m else None,
        "threshold_check": {
            "signals >= 30":       entry_ok >= TARGET_SIGNALS,
            "5m hit >= 45%":       (h5.get("hit") or 0) >= TARGET_HIT,
            "5m PF >= 1.2":        (h5.get("pf") or 0) >= TARGET_PF,
            "quoteability >= 80%": entry_ok / len(rows) >= TARGET_QUOTE_RT if rows else False,
            "unique_pools >= min": len(pool_cnt) >= UNIQUE_POOLS_MIN,
            "top_pool_share <= max": top_pool_share <= TOP_POOL_SHARE_MAX,
            "30m real PF >= 1.3":  (h30_real.get("pf") or 0) >= 1.3 if h30_real.get("n", 0) >= 10 else False,
        },
    }
    date_str = datetime.now(timezone.utc).date().isoformat()
    out_path = DIAG_OUT / f"paper_shadow_daily_{date_str}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    return summary


# ---------------------------------------------------------------------------
# Main poll cycle
# ---------------------------------------------------------------------------

def _composite_flag(heat: float, bpr: float, weth_is_token1: bool) -> bool:
    """True if candidate matches Sprint056E composite signal.
    fee_100 + heat>=20 + weth_is_token1 + BPR<0.55  (n=76, PF=3.065 in historical data)
    """
    return heat >= COMPOSITE_HEAT_MIN and bpr < COMPOSITE_BPR_MAX and weth_is_token1


def poll_once(rpc: JsonRpcClient, state: dict, cost_model: RealCostModel | None = None) -> int:
    """One poll cycle. Returns number of new signals processed."""
    import requests
    rpc.session = rpc.session or requests.Session()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # 1. Fire any due exit marks on pending candidates
    still_pending = []
    for p in state.get("pending", []):
        all_done = True
        for mark in p.get("exit_marks", []):
            if mark["done"]:
                continue
            if now_ts >= mark["exit_due_ts"]:
                lbl = mark["label"]
                q = exit_quote_at_horizon(rpc, p["token_other"], p["fee_bps"],
                                          p["entry_amount_out"], lbl)
                update_csv_exit(p["signal_time"], p["pool"], lbl, q, p.get("cost_snap"))
                mark["done"] = True
                if lbl == "5m" and not _SPRINT087:
                    # Legacy: 15min cooldown anchored to 5m exit
                    state["pool_last_close"][p["pool"]] = now_ts
                print(f"  [exit-{lbl}] {p['pool'][:10]} ok={q['ok']} "
                      f"ret={q.get('net_return')}", flush=True)
            else:
                all_done = False
        if not all_done:
            still_pending.append(p)
        # If all done: item drops out naturally

    state["pending"] = still_pending

    # 2. Discover new onset candidates
    discovery_degraded = False
    try:
        candidates = discover_candidates(lookback_min=90)
    except DiscoveryDegraded as e:
        print(f"  [discover] DEGRADED: {e}", flush=True)
        candidates = []
        discovery_degraded = True
        state["discovery_degraded_count"] = state.get("discovery_degraded_count", 0) + 1
    except Exception as e:
        print(f"  [discover] ERROR (unexpected): {e}", flush=True)
        candidates = []
        discovery_degraded = True
        state["discovery_degraded_count"] = state.get("discovery_degraded_count", 0) + 1

    # 3. Process new candidates
    new_signals = 0
    skipped: Counter[str] = Counter()
    already_seen = {(p["pool"], p["wts"]) for p in state.get("pending", [])}
    for c in candidates:
        key = (c["pool"], c["wts"])
        if key in already_seen:
            skipped["pending_dup"] += 1
            continue

        pool = c["pool"]
        allowed, reason = first_signal_allowed(pool, now_ts, state)
        if not allowed:
            skipped[reason] += 1
            continue

        # Fee check via RPC (Sprint087: whitelist 100/500/3000/10000)
        fee_bps = get_pool_fee(rpc, pool, state["pool_fee_cache"])
        if fee_bps is None:
            skipped["fee_rpc_fail"] += 1
            continue
        if not fee_tier_allowed(int(fee_bps)):
            skipped["fee_tier"] += 1
            continue

        # Token addresses
        token0, token1 = get_pool_tokens(rpc, pool, state["pool_token_cache"])
        if token0 is None or token1 is None:
            skipped["token_rpc_fail"] += 1
            continue
        weth_is_token1 = token0.lower() != WETH_BASE
        token_other = token1 if not weth_is_token1 else token0
        if token_other.lower() == WETH_BASE:
            skipped["no_weth_leg"] += 1
            continue

        sender_meta = _sender_fields(pool)
        if EXCLUDE_0X6FF5 and sender_meta.get("is_0x6ff5") == "True":
            skipped["exclude_0x6ff5"] += 1
            continue

        # Entry quote
        q_entry = entry_quote(rpc, token_other, fee_bps)
        signal_time = datetime.fromtimestamp(c["wts"], tz=timezone.utc).isoformat()
        comp = _composite_flag(c["activity_heat"], c["buy_pressure_ratio"], weth_is_token1)

        cost_snap: dict | None = None
        cost_fields: dict = {}
        if q_entry["ok"] and cost_model is not None:
            try:
                cb = cost_model.estimate(
                    token=token_other,
                    fee_bps=int(fee_bps),
                    notional_usd=REAL_COST_NOTIONAL_USD,
                    block_identifier=q_entry.get("entry_block"),
                )
                cost_fields = cost_breakdown_to_row(cb)
                cost_snap = cost_snap_from_breakdown(cb)
            except Exception as exc:
                cost_fields = {"cost_status": f"cost_error:{type(exc).__name__}"}

        entry_row = {
            "signal_time":          signal_time,
            "pool":                 pool,
            "token_other":          token_other,
            "fee_bps":              fee_bps,
            "weth_is_token1":       weth_is_token1,
            "pool_age_sec":         c["pool_age_sec"],
            "onset_lag_sec":        c["onset_lag_sec"],
            "activity_heat":        c["activity_heat"],
            "buy_pressure_ratio":   c["buy_pressure_ratio"],
            "price_velocity_5m":    c["price_velocity_5m"],
            "composite_flag":       comp,
            "entry_block":          q_entry.get("entry_block"),
            "entry_quote_ok":       q_entry["ok"],
            "entry_amount_out_tokens": q_entry.get("amount_out"),
            "gas_proxy_pct":        GAS_PCT,
            "entry_fail_reason":    q_entry.get("fail_reason", "") if not q_entry["ok"] else "",
            **sender_meta,
            **cost_fields,
        }
        write_entry_row(entry_row)

        if q_entry["ok"]:
            mark_pool_signal(pool, now_ts, state)
            # Build multi-horizon exit marks
            exit_marks = [
                {"horizon_sec": h, "label": lbl,
                 "exit_due_ts": now_ts + h, "done": False}
                for h, lbl in zip(EXIT_HORIZONS, EXIT_LABELS)
            ]
            state["pending"].append({
                "pool":           pool,
                "wts":            c["wts"],
                "signal_time":    signal_time,
                "fee_bps":        fee_bps,
                "token_other":    token_other,
                "entry_amount_out": q_entry["amount_out"],
                "entry_block":    q_entry["entry_block"],
                "exit_marks":     exit_marks,
                "cost_snap":      cost_snap,
            })
            already_seen.add(key)

        new_signals += 1
        print(f"  [signal] {pool[:10]} heat={c['activity_heat']} "
              f"age={c['pool_age_sec']}s entry_ok={q_entry['ok']} "
              f"composite={comp}", flush=True)

    if skipped:
        print(f"  [filters] {dict(skipped)}", flush=True)
    if discovery_degraded:
        print(f"  [discover] DISCOVERY_DEGRADED=true  "
              f"degraded_count={state.get('discovery_degraded_count', 0)}", flush=True)
    return new_signals


def summarise() -> None:
    s = compute_and_save_daily_summary()
    print(json.dumps(s, indent=2, default=str))
    print("\nHorizon stats:")
    for lbl, hs in s.get("horizons", {}).items():
        print(f"  {lbl:4s}: n={hs.get('n',0):3d} hit={hs.get('hit') or 0:.3f} "
              f"pf={hs.get('pf','?')} mean={hs.get('mean') or 0:.4f}")
    print("\nThreshold check:")
    for k, v in s.get("threshold_check", {}).items():
        status = "✓" if v else "✗"
        print(f"  {status} {k}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one poll cycle then exit")
    parser.add_argument("--summarise", action="store_true", help="print stats and exit")
    parser.add_argument("--daemon", action="store_true", help="run every 5m until Ctrl-C")
    args = parser.parse_args()

    if args.summarise:
        summarise()
        return

    assert os.environ.get("LIVE_TRADING_MODE", "").lower() not in ("1","true","yes","on"), \
        "LIVE_TRADING_MODE must be off"

    rpc_url = _load_env_rpc()
    if not rpc_url:
        print("ERROR: no RPC URL found", file=sys.stderr)
        sys.exit(1)
    rpc = JsonRpcClient(rpc_url, delay_s=0.0)
    cost_model = make_cost_model(rpc)

    if args.once or args.daemon:
        ensure_csv_header()
        while True:
            print(f"[paper_shadow] poll {_now_utc()} track={'sprint087' if _SPRINT087 else 'legacy'} real_cost={REAL_COST_ENABLED}", flush=True)
            state = load_state()
            n_new = poll_once(rpc, state, cost_model)
            save_state(state)
            compute_and_save_daily_summary()
            print(f"[paper_shadow] new_signals={n_new} pending={len(state['pending'])} rpc={rpc.calls}", flush=True)
            if args.once:
                break
            time.sleep(WINDOW_SEC)  # 5 min
    else:
        print("Usage: --once | --daemon | --summarise")
        print("  --once: one poll cycle (for cron: */5 * * * * python3 scripts/sprint055_fee100_paper_shadow.py --once)")
        print("  --daemon: run every 5m forever")
        print("  --summarise: print running stats")


if __name__ == "__main__":
    main()
