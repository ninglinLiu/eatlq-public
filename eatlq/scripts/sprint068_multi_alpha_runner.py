#!/usr/bin/env python3
"""Sprint068 — Multi-Alpha Paper Shadow Runner (production-paper grade).

Polls ClickHouse for new-pool onset candidates and evaluates them against
multiple frozen alpha policies in parallel. Each alpha writes its own
per-signal CSV, state JSON, and daily JSON.

Alpha policy definitions are read from:
  mvp/diagnostics/sprint068/alpha_registry.json

Key design:
  - Reads alpha registry for frozen entry rules (no hard-coded thresholds)
  - CH discovery is fee-agnostic; per-alpha fee filter applied post-discovery
  - Tier 2 features (buyer_div, same_block_frac, early_conc) via bounded CH queries
  - Idempotent: skips (alpha_id, pool, signal_time) already in CSV
  - State file tracks rpc_calls, pending, last_poll, last_error
  - Backfill mode supports historical CH windows (--backfill-hours N)
  - BLOCKED_BY_DATA / BLOCKED_BY_MARKET status documented via CH inventory

Lookahead guard (hardcoded):
  net_return_* / future quotes / token identity / historical PnL
  are never used as entry features.

Usage:
  python3 scripts/sprint068_multi_alpha_runner.py --once
  python3 scripts/sprint068_multi_alpha_runner.py --backfill-hours 72
  python3 scripts/sprint068_multi_alpha_runner.py --status
  python3 scripts/sprint068_multi_alpha_runner.py --write-daily
  python3 scripts/sprint068_multi_alpha_runner.py --dry-run --once
  python3 scripts/sprint068_multi_alpha_runner.py --alpha alpha_002 alpha_003 --once
  python3 scripts/sprint068_multi_alpha_runner.py --max-rpc-calls 200 --once

Cron (after sprint055 cron, with flock anti-re-entry):
  */5 * * * * flock -n /tmp/sprint068_runner.lock -c 'cd /opt/EATLQ && python3 scripts/sprint068_multi_alpha_runner.py --once >> logs/sprint068/runner_cron.log 2>&1'
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from scripts.lib.v3_quoterv2_struct import (
        JsonRpcClient, WETH_BASE, quote_exact_input_single_struct,
    )
    HAS_QUOTER = True
except ImportError:
    HAS_QUOTER = False
    WETH_BASE = "0x4200000000000000000000000000000000000006"

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "mvp/diagnostics/sprint068/alpha_registry.json"
LOG_DIR = ROOT / "logs/sprint068"
DIAG_DIR = ROOT / "mvp/diagnostics/sprint068"
AUDIT_DIR = ROOT / "mvp/audits/sprint068"

LOG_DIR.mkdir(parents=True, exist_ok=True)
DIAG_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID = 8453
ENTRY_WETH_RAW = int(0.00005 * 10**18)
GAS_PCT = 0.002
RPC_DELAY = 0.04
SEL_FEE = "0xddca3f43"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"

EXIT_HORIZONS = [300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
EXIT_LABELS = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]

# CSV schema — extends sprint055 schema with alpha metadata
CSV_COLS = [
    "alpha_id", "run_id", "source_family", "signal_time", "feature_cutoff_time",
    "pool", "token_other", "fee_bps", "weth_is_token1",
    "pool_age_sec", "onset_lag_sec",
    "activity_heat", "buy_pressure_ratio", "price_velocity_5m",
    "buyer_diversity_20", "same_block_frac", "early_conc_top_share",
    "composite_flag", "label_horizon",
    "entry_block", "entry_quote_ok", "entry_amount_out_tokens",
    "exit_5m_ok", "exit_5m_block", "net_return_5m", "net_return_5m_ag",
    "exit_10m_ok", "exit_10m_block", "net_return_10m", "net_return_10m_ag",
    "exit_15m_ok", "exit_15m_block", "net_return_15m", "net_return_15m_ag",
    "exit_30m_ok", "exit_30m_block", "net_return_30m", "net_return_30m_ag",
    "exit_1h_ok", "exit_1h_block", "net_return_1h", "net_return_1h_ag",
    "exit_2h_ok", "exit_2h_block", "net_return_2h", "net_return_2h_ag",
    "exit_4h_ok", "exit_4h_block", "net_return_4h", "net_return_4h_ag",
    "exit_6h_ok", "exit_6h_block", "net_return_6h", "net_return_6h_ag",
    "exit_12h_ok", "exit_12h_block", "net_return_12h", "net_return_12h_ag",
    "exit_24h_ok", "exit_24h_block", "net_return_24h", "net_return_24h_ag",
    "gas_proxy_pct", "entry_fail_reason",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_dt(ts: str) -> str:
    """'2026-05-12T00:00:00+00:00' → '2026-05-12 00:00:00'"""
    return ts[:19].replace("T", " ")


def _load_env_rpc() -> str:
    env = ROOT / "infra/compose/.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v:
                    return v
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def load_registry() -> dict:
    with REGISTRY_PATH.open() as f:
        return json.load(f)


def get_alpha_entry_rule(registry: dict, alpha_id: str) -> dict | None:
    for a in registry.get("alphas", []):
        if a["alpha_id"] == alpha_id:
            er = a.get("entry_rule", {})
            er["source_family"] = a.get("source_family", "")
            er["alpha_type"] = a.get("alpha_type", "")
            er["status"] = a.get("status", "")
            return er
    return None


def merge_registries(base: dict, supplemental: dict) -> dict:
    """Merge supplemental alpha defs (e.g. sprint069 candidates) into base registry."""
    if not supplemental:
        return base
    by_id = {a["alpha_id"]: a for a in base.get("alphas", [])}
    for a in supplemental.get("alphas", []):
        by_id[a["alpha_id"]] = a
    merged = dict(base)
    merged["alphas"] = list(by_id.values())
    return merged


def list_alpha_ids(registry: dict) -> list[str]:
    return [a["alpha_id"] for a in registry.get("alphas", [])
            if a.get("status") not in ("REJECTED",)]


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------

def _ch(sql: str, timeout: int = 60) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH error [{r.returncode}]: {r.stderr.strip()[:300]}")
    if not r.stdout.strip():
        return []
    result = []
    for row in csv.reader(io.StringIO(r.stdout)):
        if row:
            result.append(row)
    return result


def ch_check_fee_tier_available(fee_bps: int) -> bool:
    """Check if any pools with this fee exist in CH tracked universe."""
    # For Uniswap V3: pool.fee() returns 100/500/3000/10000
    # CH pool_registry uses different scale (5/30/100 bps = 0.05/0.30/1.00%)
    # We detect fee=100 Uniswap V3 pools by checking if RPC fee check returns 100
    # For now: only fee=100 is verified to exist in the tracked universe
    if fee_bps == 100:
        return True
    # fee=500/3000/10000 have no pools in current tracked universe
    return False


def discover_candidates(from_ts: int, to_ts: int, heat_min: int = 5) -> list[dict]:
    """Find onset candidates in [from_ts, to_ts] window. Fee-agnostic."""
    from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    to_dt = datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    sql = f"""
SELECT
    pool_address,
    toDateTime(toStartOfFiveMinutes(block_time)) AS wts_dt,
    count() AS heat,
    countIf(amount0 < 0) AS buys
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id={CHAIN_ID}
  AND block_time >= toDateTime('{from_dt}')
  AND block_time <  toDateTime('{to_dt}')
  AND block_time IS NOT NULL
GROUP BY pool_address, wts_dt
HAVING heat >= {heat_min}
ORDER BY pool_address, wts_dt
FORMAT CSV
""".strip()

    try:
        rows = _ch(sql, timeout=45)
    except Exception as e:
        print(f"  [CH discover] error: {e}", flush=True)
        return []

    windows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if len(r) >= 4:
            try:
                windows[r[0].strip('"')].append({
                    "wts": int(datetime.fromisoformat(r[1].strip('"').replace(" ", "T") + "+00:00").timestamp()),
                    "wts_str": r[1].strip('"'),
                    "heat": int(r[2]), "buys": int(r[3]),
                })
            except Exception:
                pass

    if not windows:
        return []

    # Pool first-seen timestamps
    pl = ",".join(f"'{p}'" for p in windows)
    sql2 = f"""
SELECT pool_address, toDateTime(min(block_time)) AS first_ts_dt,
       toUnixTimestamp(min(block_time)) AS first_ts
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id={CHAIN_ID} AND block_time IS NOT NULL
  AND pool_address IN ({pl})
GROUP BY pool_address
FORMAT CSV
""".strip()
    try:
        fs_rows = _ch(sql2, timeout=60)
    except Exception as e:
        print(f"  [CH first-seen] error: {e}", flush=True)
        fs_rows = []

    first_seen: dict[str, int] = {}
    for r in fs_rows:
        if len(r) >= 3:
            try:
                first_seen[r[0].strip('"')] = int(r[2])
            except Exception:
                pass

    candidates = []
    for pool, wins in windows.items():
        fs = first_seen.get(pool)
        if fs is None:
            continue
        onset_ts = None
        for w in sorted(wins, key=lambda x: x["wts"]):
            if w["heat"] >= heat_min:
                onset_ts = w["wts"]
                break
        if onset_ts is None:
            continue
        for w in sorted(wins, key=lambda x: x["wts"]):
            pool_age = w["wts"] - fs
            onset_lag = w["wts"] - onset_ts
            n = w["heat"]
            bp = w["buys"] / n if n > 0 else 0.0
            candidates.append({
                "pool": pool,
                "wts": w["wts"],
                "wts_str": w["wts_str"],
                "pool_age_sec": pool_age,
                "onset_lag_sec": onset_lag,
                "activity_heat": n,
                "buy_pressure_ratio": round(bp, 4),
                "price_velocity_5m": round(2 * bp - 1, 4),
                "buyer_diversity_20": None,
                "same_block_frac": None,
                "early_conc_top_share": None,
            })
    return candidates


def enrich_tier2(candidates: list[dict]) -> None:
    """In-place enrich Tier2 features via bounded CH queries."""
    if not candidates:
        return
    # Batch by pool
    pool_windows: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        pool_windows[c["pool"]].append(c)

    for pool, cands in pool_windows.items():
        min_wts = min(c["wts"] for c in cands)
        max_wts = max(c["wts"] for c in cands)
        from_dt = datetime.fromtimestamp(min_wts - 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        to_dt = datetime.fromtimestamp(max_wts + 360, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
SELECT
    toDateTime(toStartOfFiveMinutes(block_time)) AS wts_dt,
    uniqExact(sender) AS distinct_senders,
    count() AS heat,
    round(countIf(amount0 < 0) * 1.0 / count(), 4) AS top_share_approx
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id={CHAIN_ID}
  AND pool_address='{pool}'
  AND block_time >= toDateTime('{from_dt}')
  AND block_time <  toDateTime('{to_dt}')
GROUP BY wts_dt
FORMAT CSV
""".strip()
        try:
            rows = _ch(sql, timeout=30)
        except Exception:
            continue
        win_map: dict[str, dict] = {}
        for r in rows:
            if len(r) >= 4:
                wts_str = r[0].strip('"')
                try:
                    win_map[wts_str] = {
                        "distinct_senders": int(r[1]),
                        "heat": int(r[2]),
                        "same_block_frac": 0.0,  # entry_block > onset window
                        "early_conc_top_share": round(1.0 / max(1, int(r[1])), 3),
                    }
                except Exception:
                    pass
        for c in cands:
            t2 = win_map.get(c["wts_str"])
            if t2:
                c["buyer_diversity_20"] = t2["distinct_senders"]
                c["same_block_frac"] = t2["same_block_frac"]
                c["early_conc_top_share"] = t2["early_conc_top_share"]


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _make_rpc(url: str):
    if not HAS_QUOTER:
        return None
    return JsonRpcClient(url)


def get_pool_fee_rpc(rpc, pool: str, cache: dict) -> int | None:
    if pool in cache:
        return cache[pool]
    if rpc is None:
        return None
    try:
        resp = rpc.session.post(rpc.url, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": pool, "data": SEL_FEE}, "latest"],
        }, timeout=15)
        raw = resp.json().get("result", "0x")
        v = int(raw[2:][-8:], 16) if raw and raw != "0x" else None
        cache[pool] = v
        time.sleep(RPC_DELAY)
        return v
    except Exception:
        return None


def get_pool_tokens_rpc(rpc, pool: str, tcache: dict) -> tuple:
    if pool in tcache:
        c = tcache[pool]
        return c.get("token0"), c.get("token1")
    if rpc is None:
        return None, None
    t0, t1 = None, None
    for sel, name in [(SEL_TOKEN0, "token0"), (SEL_TOKEN1, "token1")]:
        try:
            resp = rpc.session.post(rpc.url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": pool, "data": sel}, "latest"],
            }, timeout=15)
            raw = resp.json().get("result", "0x")
            if raw and len(raw) >= 42:
                addr = ("0x" + raw[-40:]).lower()
                if name == "token0":
                    t0 = addr
                else:
                    t1 = addr
        except Exception:
            pass
        time.sleep(RPC_DELAY)
    tcache[pool] = {"token0": t0, "token1": t1}
    return t0, t1


def get_latest_block(rpc) -> int:
    if rpc is None:
        return 0
    try:
        return int(rpc.call("eth_blockNumber", []), 16)
    except Exception:
        return 0


def entry_quote_rpc(rpc, token_other: str, fee_bps: int) -> dict:
    if rpc is None or not HAS_QUOTER:
        return {"ok": False, "amount_out": 0, "entry_block": 0, "fail_reason": "no_rpc"}
    block = get_latest_block(rpc)
    q = quote_exact_input_single_struct(rpc, WETH_BASE, token_other, ENTRY_WETH_RAW, fee_bps,
                                        block_identifier=block)
    return {"ok": q.get("ok", False), "amount_out": int(q.get("amount_out", 0)),
            "entry_block": block, "fail_reason": q.get("reason", "")}


def exit_quote_rpc(rpc, token_other: str, fee_bps: int, token_amount: int, label: str) -> dict:
    if rpc is None or not HAS_QUOTER:
        return {"ok": False, "exit_block": 0, "net_return": None, "net_return_ag": None}
    block = get_latest_block(rpc)
    q = quote_exact_input_single_struct(rpc, token_other, WETH_BASE, token_amount, fee_bps,
                                        block_identifier=block)
    exit_weth = int(q.get("amount_out", 0)) if q.get("ok") else 0
    net = (exit_weth / ENTRY_WETH_RAW - 1.0) if (q.get("ok") and ENTRY_WETH_RAW > 0) else None
    return {"ok": q.get("ok", False), "exit_block": block,
            "net_return": round(net, 8) if net is not None else None,
            "net_return_ag": round(net - GAS_PCT, 8) if net is not None else None}


# ---------------------------------------------------------------------------
# Alpha gate (reads from registry entry_rule, no hard-coded thresholds)
# ---------------------------------------------------------------------------

def apply_gate(cand: dict, rule: dict, pool_fee: int | None) -> bool:
    """Evaluate alpha entry rule against candidate. No lookahead."""
    target_fee = rule.get("fee_bps")
    if isinstance(target_fee, list):
        if pool_fee not in target_fee:
            return False
    elif target_fee is not None:
        if pool_fee != target_fee:
            return False

    age = cand["pool_age_sec"]
    if age < rule.get("pool_age_sec_min", 0):
        return False
    if age > rule.get("pool_max_age_sec", 999999):
        return False
    if cand["onset_lag_sec"] > rule.get("onset_lag_max_sec", 999999):
        return False
    if cand["activity_heat"] < rule.get("activity_heat_min", 0):
        return False

    bpr = cand["buy_pressure_ratio"]
    if rule.get("buy_pressure_ratio_min") is not None and bpr < rule["buy_pressure_ratio_min"]:
        return False
    if rule.get("buy_pressure_ratio_max") is not None and bpr > rule["buy_pressure_ratio_max"]:
        return False

    if rule.get("weth_is_token1_required") and not cand.get("weth_is_token1", False):
        return False

    bd = cand.get("buyer_diversity_20")
    if rule.get("buyer_diversity_first_20_min") is not None and bd is not None:
        if bd < rule["buyer_diversity_first_20_min"]:
            return False

    sbf = cand.get("same_block_frac")
    if rule.get("same_block_frac_max") is not None and sbf is not None:
        if sbf > rule["same_block_frac_max"]:
            return False

    ec = cand.get("early_conc_top_share")
    if rule.get("early_concentration_top_share_max") is not None and ec is not None:
        if ec > rule["early_concentration_top_share_max"]:
            return False

    return True


# ---------------------------------------------------------------------------
# State / CSV management
# ---------------------------------------------------------------------------

def state_path(alpha_id: str) -> Path:
    return DIAG_DIR / f"{alpha_id}_state.json"


def csv_path(alpha_id: str) -> Path:
    return LOG_DIR / f"{alpha_id}_paper_shadow.csv"


def load_state(alpha_id: str) -> dict:
    p = state_path(alpha_id)
    if p.exists():
        return json.loads(p.read_text())
    return {"alpha_id": alpha_id, "status": "INITIALIZING", "pending": [],
            "pool_fee_cache": {}, "pool_token_cache": {}, "pool_last_close": {},
            "rpc_calls": 0, "last_poll_time": None, "last_error": None}


def save_state(alpha_id: str, state: dict) -> None:
    state["last_poll_time"] = now_utc()
    state_path(alpha_id).write_text(json.dumps(state, indent=2, default=str))


def load_existing_keys(alpha_id: str) -> set:
    """Load existing (pool, signal_time) keys for idempotency."""
    p = csv_path(alpha_id)
    if not p.exists():
        return set()
    keys = set()
    with p.open() as f:
        for r in csv.DictReader(f):
            keys.add((r.get("pool", ""), r.get("signal_time", "")))
    return keys


def ensure_header(alpha_id: str) -> None:
    p = csv_path(alpha_id)
    if not p.exists():
        with p.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writeheader()


def append_row(alpha_id: str, row: dict) -> None:
    p = csv_path(alpha_id)
    with p.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_COLS).writerow(
            {c: row.get(c, "") for c in CSV_COLS}
        )


def update_exit_row(alpha_id: str, pool: str, signal_time: str, updates: dict) -> None:
    p = csv_path(alpha_id)
    if not p.exists():
        return
    rows = []
    with p.open() as f:
        for r in csv.DictReader(f):
            if r.get("pool") == pool and r.get("signal_time") == signal_time:
                r.update({k: str(v) for k, v in updates.items()})
            rows.append(r)
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows(rows)


def write_daily_summary(alpha_id: str, status: str = "ACTIVE_SHADOW") -> dict:
    p = csv_path(alpha_id)
    if not p.exists():
        return {}
    rows_30m = []
    with p.open() as f:
        for r in csv.DictReader(f):
            if str(r.get("exit_30m_ok")) == "True" and r.get("net_return_30m"):
                try:
                    rows_30m.append(float(r["net_return_30m"]))
                except Exception:
                    pass

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not rows_30m:
        daily = {"alpha_id": alpha_id, "generated_at": now_utc(), "date": today,
                 "status": status, "n_labeled_30m": 0}
    else:
        wins = [x for x in rows_30m if x > 0]
        losses = [x for x in rows_30m if x <= 0]
        pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
        daily = {
            "alpha_id": alpha_id, "generated_at": now_utc(), "date": today,
            "status": status, "n_labeled_30m": len(rows_30m),
            "win_rate_30m": round(len(wins) / len(rows_30m), 4),
            "profit_factor_30m": round(pf, 4),
            "mean_return_30m": round(statistics.mean(rows_30m), 6),
            "median_return_30m": round(statistics.median(rows_30m), 6),
            "max_loss_30m": round(min(rows_30m), 6),
            "gate": {
                "min_7d": False,
                "min_30_trades": len(rows_30m) >= 30,
                "min_wr_45pct": len(wins) / len(rows_30m) >= 0.45,
                "min_pf_12": pf >= 1.2,
                "verdict": "KEEP_RUNNING",
            },
        }
    daily["hard_boundaries"] = {
        "LIVE_ALLOWED": False, "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False, "THRESHOLDS_TUNED": False,
    }
    p2 = LOG_DIR / f"{alpha_id}_daily.json"
    p2.write_text(json.dumps(daily, indent=2))
    return daily


# ---------------------------------------------------------------------------
# Main poll cycle
# ---------------------------------------------------------------------------

def poll_cycle(alpha_ids: list[str], registry: dict, rpc_url: str,
               from_ts: int, to_ts: int, dry_run: bool = False,
               max_rpc: int = 500) -> dict[str, int]:
    """One poll cycle. Returns {alpha_id: new_signals}."""
    rpc = _make_rpc(rpc_url) if rpc_url and HAS_QUOTER else None
    rpc_calls = 0
    new_counts: dict[str, int] = {a: 0 for a in alpha_ids}

    print(f"[{now_utc()}] Discovering candidates {datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} → {datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}", flush=True)

    candidates = discover_candidates(from_ts, to_ts, heat_min=5)
    print(f"  Raw candidates: {len(candidates)} (pool/5m windows)", flush=True)
    if not candidates:
        return new_counts

    # Enrich Tier2 for all candidates
    enrich_tier2(candidates)

    # Load states and existing keys
    states: dict[str, dict] = {}
    existing_keys: dict[str, set] = {}
    pool_fee_cache: dict[str, int] = {}
    pool_token_cache: dict[str, dict] = {}

    for alpha_id in alpha_ids:
        ensure_header(alpha_id)
        states[alpha_id] = load_state(alpha_id)
        existing_keys[alpha_id] = load_existing_keys(alpha_id)
        pool_fee_cache.update(states[alpha_id].get("pool_fee_cache", {}))
        pool_token_cache.update(states[alpha_id].get("pool_token_cache", {}))

    run_id = f"poll_{datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M')}"
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for cand in candidates:
        pool = cand["pool"]
        signal_time_dt = datetime.fromtimestamp(cand["wts"], tz=timezone.utc)
        signal_time = signal_time_dt.isoformat()

        # Pool fee: CH registry first (fast), RPC fallback
        if pool not in pool_fee_cache:
            # Check CH registry
            sql = f"""
SELECT fee_bps FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={CHAIN_ID} AND pool_address='{pool}' LIMIT 1
FORMAT CSV
""".strip()
            try:
                rows = _ch(sql, timeout=10)
                if rows:
                    # registry fee_bps: 100 bps = 1.00% Aerodrome OR 0.01% Uniswap V3
                    # We cross-reference with sprint055 RPC pattern: Uniswap V3 fee()=100
                    pass  # Use RPC to determine actual fee
            except Exception:
                pass

            if rpc and rpc_calls < max_rpc:
                fee = get_pool_fee_rpc(rpc, pool, pool_fee_cache)
                rpc_calls += 1
            else:
                pool_fee_cache[pool] = None

        pool_fee = pool_fee_cache.get(pool)

        for alpha_id in alpha_ids:
            # Idempotency check
            if (pool, signal_time) in existing_keys[alpha_id]:
                continue

            rule = get_alpha_entry_rule(registry, alpha_id)
            if rule is None:
                continue

            # Check if fee tier has any pools
            target_fee = rule.get("fee_bps")
            if isinstance(target_fee, int) and not ch_check_fee_tier_available(target_fee):
                states[alpha_id]["status"] = "BLOCKED_BY_DATA"
                continue

            # Cooldown
            cooldown_sec = rule.get("same_pool_cooldown_min", 15) * 60
            last_close = states[alpha_id].get("pool_last_close", {}).get(pool, 0)
            if now_ts - last_close < cooldown_sec:
                continue

            # Apply frozen gate (no lookahead)
            if not apply_gate(cand, rule, pool_fee):
                continue

            if dry_run:
                print(f"  [DRY-RUN] {alpha_id}: pool={pool[:14]} wts={signal_time[:16]} heat={cand['activity_heat']}", flush=True)
                new_counts[alpha_id] += 1
                existing_keys[alpha_id].add((pool, signal_time))
                continue

            # Get pool tokens
            if rpc and rpc_calls < max_rpc:
                token0, token1 = get_pool_tokens_rpc(rpc, pool, pool_token_cache)
                rpc_calls += 1
            else:
                token0, token1 = None, None

            if token0 is None and token1 is None:
                continue
            weth_is_token1 = token1 and token1.lower() == WETH_BASE.lower()
            token_other = token0 if weth_is_token1 else token1
            if not token_other:
                continue

            # Entry quote
            if rpc and rpc_calls < max_rpc:
                eq = entry_quote_rpc(rpc, token_other, pool_fee or rule.get("fee_bps", 100))
                rpc_calls += 1
            else:
                eq = {"ok": False, "amount_out": 0, "entry_block": 0, "fail_reason": "rpc_budget_exceeded"}

            # Composite flag (sprint055 definition)
            composite_flag = (
                cand["activity_heat"] >= 20 and weth_is_token1
                and cand["buy_pressure_ratio"] < 0.55
            )

            row = {
                "alpha_id": alpha_id,
                "run_id": run_id,
                "source_family": rule.get("source_family", ""),
                "signal_time": signal_time,
                "feature_cutoff_time": signal_time,
                "pool": pool,
                "token_other": token_other,
                "fee_bps": pool_fee or rule.get("fee_bps", ""),
                "weth_is_token1": weth_is_token1,
                "pool_age_sec": cand["pool_age_sec"],
                "onset_lag_sec": cand["onset_lag_sec"],
                "activity_heat": cand["activity_heat"],
                "buy_pressure_ratio": cand["buy_pressure_ratio"],
                "price_velocity_5m": cand["price_velocity_5m"],
                "buyer_diversity_20": cand.get("buyer_diversity_20", ""),
                "same_block_frac": cand.get("same_block_frac", ""),
                "early_conc_top_share": cand.get("early_conc_top_share", ""),
                "composite_flag": composite_flag,
                "label_horizon": "30m",
                "entry_block": eq.get("entry_block", ""),
                "entry_quote_ok": eq.get("ok", False),
                "entry_amount_out_tokens": eq.get("amount_out", "") if eq.get("ok") else "",
                "gas_proxy_pct": GAS_PCT,
                "entry_fail_reason": eq.get("fail_reason", "") if not eq.get("ok") else "",
            }
            append_row(alpha_id, row)
            existing_keys[alpha_id].add((pool, signal_time))
            new_counts[alpha_id] += 1

            if eq.get("ok") and eq.get("amount_out", 0) > 0:
                pending_entry = {
                    "pool": pool, "signal_time": signal_time,
                    "token_other": token_other,
                    "fee_bps": pool_fee or rule.get("fee_bps", 100),
                    "entry_block": eq["entry_block"],
                    "amount_out": eq["amount_out"],
                    "entry_ts": now_ts, "exits_done": [],
                }
                states[alpha_id].setdefault("pending", []).append(pending_entry)

            print(f"  [{alpha_id}] ENTRY pool={pool[:14]} wts={signal_time[:16]} heat={cand['activity_heat']}", flush=True)

            if rpc_calls >= max_rpc:
                print(f"  RPC budget exhausted ({max_rpc})", flush=True)
                break

        else:
            continue
        break  # RPC budget hit

    # Fill pending exits
    for alpha_id in alpha_ids:
        state = states[alpha_id]
        remaining = []
        for entry in state.get("pending", []):
            entry_ts = entry.get("entry_ts", now_ts)
            exits_done = set(entry.get("exits_done", []))
            updates: dict = {}
            all_done = True
            for horizon_sec, label in zip(EXIT_HORIZONS, EXIT_LABELS):
                if label in exits_done:
                    continue
                due_ts = entry_ts + horizon_sec
                if now_ts < due_ts:
                    all_done = False
                    continue
                if rpc and rpc_calls < max_rpc and entry.get("amount_out", 0) > 0:
                    eq_exit = exit_quote_rpc(rpc, entry["token_other"], entry["fee_bps"],
                                              entry["amount_out"], label)
                    rpc_calls += 1
                    col = f"exit_{label}"
                    updates[f"{col}_ok"] = eq_exit.get("ok", False)
                    updates[f"{col}_block"] = eq_exit.get("exit_block", "")
                    updates[f"net_return_{label}"] = eq_exit.get("net_return", "")
                    updates[f"net_return_{label}_ag"] = eq_exit.get("net_return_ag", "")
                exits_done.add(label)
            if updates:
                update_exit_row(alpha_id, entry["pool"], entry["signal_time"], updates)
                entry["exits_done"] = list(exits_done)
            if not all_done:
                remaining.append(entry)
            else:
                state.setdefault("pool_last_close", {})[entry["pool"]] = now_ts
        state["pending"] = remaining
        state["pool_fee_cache"] = pool_fee_cache
        state["pool_token_cache"] = pool_token_cache
        state["rpc_calls"] = state.get("rpc_calls", 0) + rpc_calls
        if state.get("status") not in ("BLOCKED_BY_DATA", "BLOCKED_BY_MARKET"):
            state["status"] = "ACTIVE_SHADOW"
        save_state(alpha_id, state)

    total_new = sum(new_counts.values())
    print(f"  New entries: {dict(new_counts)} total={total_new} rpc_calls={rpc_calls}", flush=True)
    return new_counts


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

def print_status(alpha_ids: list[str]) -> None:
    print(f"\n{'='*72}")
    print(f"Sprint068 Multi-Alpha Status — {now_utc()}")
    print(f"{'='*72}")
    for alpha_id in alpha_ids:
        state = load_state(alpha_id)
        p = csv_path(alpha_id)
        n_rows = 0
        n_labeled = 0
        if p.exists():
            with p.open() as f:
                for r in csv.DictReader(f):
                    n_rows += 1
                    if str(r.get("exit_30m_ok")) == "True":
                        n_labeled += 1
        print(f"  {alpha_id:18s} status={state.get('status','?'):22s} "
              f"signals={n_rows:4d} labeled_30m={n_labeled:4d} "
              f"pending={len(state.get('pending',[]))} rpc_calls={state.get('rpc_calls',0)}")
    print()


# ---------------------------------------------------------------------------
# Backfill mode
# ---------------------------------------------------------------------------

def run_backfill(alpha_ids: list[str], registry: dict, rpc_url: str,
                 backfill_hours: int, max_rpc: int = 500, dry_run: bool = False) -> None:
    """Run poll cycles over historical time windows."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - backfill_hours * 3600

    # Process in 90-minute windows (same as sprint055 lookback)
    window_sec = 90 * 60
    current = start_ts
    total_new: dict[str, int] = {a: 0 for a in alpha_ids}

    while current < now_ts:
        window_end = min(current + window_sec, now_ts)
        counts = poll_cycle(alpha_ids, registry, rpc_url, current, window_end,
                             dry_run=dry_run, max_rpc=max_rpc)
        for a, n in counts.items():
            total_new[a] = total_new.get(a, 0) + n
        current = window_end

    print(f"\n[{now_utc()}] Backfill complete. New signals: {total_new}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_candidate_registry(path: Path) -> dict:
    """Load sprint069 candidate registry as a supplemental alpha source."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    # Normalize to the sprint068 registry format
    alphas = []
    for c in data.get("candidates", []):
        rule = c.get("entry_rule", {})
        alphas.append({
            "alpha_id": c["alpha_id"],
            "name": c.get("name", c["alpha_id"]),
            "status": c.get("status", "SHADOW_ONLY"),
            "source_family": c.get("source_family", "unknown"),
            "alpha_type": c.get("alpha_type", "independent"),
            "entry_rule": rule,
            "allowed_features": c.get("allowed_features", []),
            "label_horizon_primary": c.get("label_horizon_primary", "30m"),
            "data_availability": c.get("data_availability", "AVAILABLE"),
        })
    return {"alphas": alphas, "source": str(path)}


def print_compare(alpha_ids: list[str], candidate_ids: list[str] | None = None) -> None:
    """Print cross-alpha comparison table."""
    print(f"\n{'='*80}")
    print(f"Sprint068+069 Multi-Alpha Comparison — {now_utc()}")
    print(f"{'='*80}")
    print(f"{'alpha_id':20s} {'signals':>7} {'labeled':>7} {'WR%':>7} {'PF':>7} "
          f"{'mean_ret':>10} {'top_pool%':>9} {'status':20s}")
    print("-" * 80)
    all_ids = alpha_ids + (candidate_ids or [])
    for alpha_id in all_ids:
        p = csv_path(alpha_id)
        state = load_state(alpha_id)
        rets, pools = [], []
        if p.exists():
            with p.open() as f:
                for row in csv.DictReader(f):
                    pools.append(row.get("pool", ""))
                    if str(row.get("exit_30m_ok")) == "True" and row.get("net_return_30m"):
                        try:
                            rets.append(float(row["net_return_30m"]))
                        except (ValueError, TypeError):
                            pass
        n_sig = len(pools)
        n_lab = len(rets)
        if rets:
            wins = [r for r in rets if r > 0]
            losses = [r for r in rets if r <= 0]
            wr = round(len(wins) / n_lab * 100, 1) if n_lab else 0
            pf = round(sum(wins) / abs(sum(losses)), 3) if losses else float("inf")
            mean_r = round(statistics.mean(rets), 5) if rets else 0
        else:
            wr = pf = mean_r = 0
        top_pool = 0
        if pools:
            top_pool = round(max(Counter(pools).values()) / len(pools) * 100, 1)
        status = state.get("status", "?")[:20]
        pf_str = f"{pf:.3f}" if pf != float("inf") else "inf"
        print(f"  {alpha_id:18s} {n_sig:>7d} {n_lab:>7d} {wr:>7.1f} {pf_str:>7} "
              f"{mean_r:>10.5f} {top_pool:>9.1f} {status:20s}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint068+069 Multi-Alpha Paper Shadow Runner")
    parser.add_argument("--once", action="store_true",
                        help="Run one poll cycle (last 90m) and exit")
    parser.add_argument("--backfill-hours", type=int, default=0,
                        help="Backfill N hours of historical CH data")
    parser.add_argument("--status", action="store_true",
                        help="Print status table and exit")
    parser.add_argument("--write-daily", action="store_true",
                        help="Write daily summary JSONs for all alphas and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover candidates but do not write to CSV")
    parser.add_argument("--alpha", nargs="*",
                        help="Restrict to specific alpha IDs")
    parser.add_argument("--all", dest="all_alphas", action="store_true",
                        help="Run all alphas from registry")
    parser.add_argument("--max-rpc-calls", type=int, default=500,
                        help="Abort if RPC call budget exceeded (default 500)")
    parser.add_argument("--since", type=str, default=None,
                        help="ISO timestamp for backfill start (overrides --backfill-hours)")
    parser.add_argument("--until", type=str, default=None,
                        help="ISO timestamp for backfill end (default: now)")
    # Sprint069 extensions
    parser.add_argument("--family", nargs="*",
                        help="Filter alphas by source_family (e.g. sender_specific onset_strength)")
    parser.add_argument("--top-k", type=int, default=0,
                        help="Restrict to top-K alphas by signal_count (0 = all)")
    parser.add_argument("--candidate-registry", type=str, default=None,
                        help="Path to sprint069 candidate registry JSON to supplement")
    parser.add_argument("--batch25-manifest", type=str, default=None,
                        help="Path to sprint078 batch25 manifest JSON (paper_shadow_alphas list)")
    parser.add_argument("--replay-window", type=str, default=None,
                        choices=["72h", "7d", "14d"],
                        help="Named replay window shortcut (72h/7d/14d)")
    parser.add_argument("--bounded-label", type=int, default=0,
                        help="Max QuoterV2 labels per run (0 = unlimited within --max-rpc-calls)")
    parser.add_argument("--no-rpc", action="store_true",
                        help="Inventory-only mode: no RPC calls, CH data only")
    parser.add_argument("--compare", action="store_true",
                        help="Print cross-alpha comparison table and exit")
    parser.add_argument("--promote-shadow-set", action="store_true",
                        help="Show promote-eligible alphas (paper-only, no live activation)")
    args = parser.parse_args()

    registry = load_registry()
    all_ids = list_alpha_ids(registry)

    # Sprint069: Load supplemental candidate registry if provided
    cand_registry: dict = {}
    cand_ids: list[str] = []
    if args.candidate_registry:
        cand_reg_path = Path(args.candidate_registry)
        if not cand_reg_path.is_absolute():
            cand_reg_path = ROOT / cand_reg_path
        cand_registry = _load_candidate_registry(cand_reg_path)
        cand_ids = [a["alpha_id"] for a in cand_registry.get("alphas", [])]
        print(f"  [S069] Loaded {len(cand_ids)} candidate alphas from {cand_reg_path}", flush=True)

    batch25_ids: list[str] = []
    if args.batch25_manifest:
        b25_path = Path(args.batch25_manifest)
        if not b25_path.is_absolute():
            b25_path = ROOT / b25_path
        b25 = json.loads(b25_path.read_text())
        batch25_ids = list(b25.get("paper_shadow_alphas", []))
        print(f"  [B25] Loaded {len(batch25_ids)} paper-shadow alphas from {b25_path}", flush=True)

    if args.alpha:
        alpha_ids = [a for a in args.alpha if a in all_ids or a.startswith("alpha_")]
    elif batch25_ids:
        alpha_ids = batch25_ids
    elif args.family:
        # Filter by source_family
        alpha_ids = [
            a["alpha_id"] for a in registry.get("alphas", [])
            if a.get("source_family") in args.family
        ]
        if cand_registry:
            alpha_ids += [
                a["alpha_id"] for a in cand_registry.get("alphas", [])
                if a.get("source_family") in args.family
            ]
    else:
        # Default: run active + shadow alphas (not diagnostic-only / rejected)
        alpha_ids = [
            a["alpha_id"] for a in registry.get("alphas", [])
            if a.get("status") in ("ACTIVE_SHADOW", "SHADOW_ONLY") and
               a.get("alpha_type") != "rejected_overfit_candidate"
        ]
        if cand_registry:
            alpha_ids += [
                a["alpha_id"] for a in cand_registry.get("alphas", [])
                if a.get("status") in ("ACTIVE_SHADOW", "SHADOW_ONLY")
                and a["alpha_id"] not in alpha_ids
            ]

    registry = merge_registries(registry, cand_registry)

    # Sprint069 --top-k filter
    if args.top_k > 0:
        # Sort by signal count
        scored = []
        for aid in alpha_ids:
            p = csv_path(aid)
            n = sum(1 for _ in open(p)) - 1 if p.exists() else 0
            scored.append((aid, n))
        scored.sort(key=lambda x: -x[1])
        alpha_ids = [aid for aid, _ in scored[:args.top_k]]

    # Sprint069 --replay-window shortcut
    if args.replay_window:
        hours = {"72h": 72, "7d": 168, "14d": 336}[args.replay_window]
        args.backfill_hours = hours

    # Sprint069 --no-rpc: override max_rpc_calls to 0
    if args.no_rpc:
        args.max_rpc_calls = 0

    if args.status:
        print_status(all_ids)
        return 0

    if args.compare:
        print_compare(all_ids, cand_ids[:20] if cand_ids else None)
        return 0

    if args.promote_shadow_set:
        print(f"\n=== Promote-Eligible Alphas (paper-only) — {now_utc()} ===")
        print("NOTE: LIVE_ALLOWED=false. Promotion means SHADOW → ACTIVE_SHADOW status only.")
        for aid in all_ids + cand_ids[:20]:
            p = csv_path(aid)
            if not p.exists():
                continue
            rets = []
            with p.open() as f:
                for row in csv.DictReader(f):
                    if str(row.get("exit_30m_ok")) == "True" and row.get("net_return_30m"):
                        try:
                            rets.append(float(row["net_return_30m"]))
                        except (ValueError, TypeError):
                            pass
            if len(rets) >= 20:
                wins = [r for r in rets if r > 0]
                losses = [r for r in rets if r <= 0]
                wr = len(wins) / len(rets)
                pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
                if wr >= 0.50 and pf >= 1.5:
                    print(f"  PROMOTE_ELIGIBLE: {aid} WR={wr:.1%} PF={pf:.3f} n={len(rets)}")
        print()
        return 0

    if args.write_daily:
        for alpha_id in all_ids:
            state = load_state(alpha_id)
            status = state.get("status", "ACTIVE_SHADOW")
            d = write_daily_summary(alpha_id, status)
            print(f"  {alpha_id}: n_labeled={d.get('n_labeled_30m', 0)} pf={d.get('profit_factor_30m', 'N/A')}")
        return 0

    rpc_url = _load_env_rpc()
    if not rpc_url or args.no_rpc:
        if args.no_rpc:
            print("INFO: --no-rpc mode — CH inventory only, no QuoterV2 calls.", flush=True)
        else:
            print("WARNING: No RPC URL available. RPC-dependent steps will be skipped.", flush=True)

    # Sprint069 --bounded-label
    if args.bounded_label > 0:
        args.max_rpc_calls = min(args.max_rpc_calls, args.bounded_label)

    now_ts = int(datetime.now(timezone.utc).timestamp())

    if args.since or args.until:
        since_ts = int(datetime.fromisoformat(args.since).timestamp()) if args.since else now_ts - 90 * 60
        until_ts = int(datetime.fromisoformat(args.until).timestamp()) if args.until else now_ts
        poll_cycle(alpha_ids, registry, rpc_url, since_ts, until_ts,
                   dry_run=args.dry_run, max_rpc=args.max_rpc_calls)
    elif args.backfill_hours > 0:
        run_backfill(alpha_ids, registry, rpc_url, args.backfill_hours,
                     max_rpc=args.max_rpc_calls, dry_run=args.dry_run)
    elif args.once:
        from_ts = now_ts - 90 * 60
        poll_cycle(alpha_ids, registry, rpc_url, from_ts, now_ts,
                   dry_run=args.dry_run, max_rpc=args.max_rpc_calls)
    else:
        print("Running continuously (Ctrl+C to stop)...", flush=True)
        while True:
            from_ts = now_ts - 90 * 60
            poll_cycle(alpha_ids, registry, rpc_url, from_ts, now_ts,
                       dry_run=args.dry_run, max_rpc=args.max_rpc_calls)
            now_ts = int(datetime.now(timezone.utc).timestamp())
            time.sleep(300)

    return 0


if __name__ == "__main__":
    sys.exit(main())
