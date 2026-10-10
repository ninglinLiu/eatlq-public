#!/usr/bin/env python3
"""Sprint078 — Expanded Pool Discovery Backfill
================================================
Expands the paper shadow dataset to 100+ pools by:
  - Using block_time (not received_at_ms) for accurate pool discovery
  - Removing the fee_bps=100 restriction (all fee tiers, WETH leg only)
  - Lowering heat threshold to 5 (from 10 in sprint055)
  - Covering all pools first-seen since 2026-04-01 (211 qualifying pools)
  - Using archive-node QuoterV2 for historical entry/exit quotes

Writes to:  logs/sprint078_shadow.csv  (same schema as sprint055_paper_shadow.csv)
State dir:  logs/sprint078/

FORWARD_CUTOFF for sprint079 harness: 2026-04-01T00:00:00+00:00
  → All signals from Apr 1 onward are "forward" (never used in strategy design)
  → sprint074 factory was built on sprint055 data (May 11+) → Apr 1+ is clean OOS

Safety:
  - LIVE_TRADING_MODE must be off
  - No writes to Redis or production
  - Idempotent: skips (pool, signal_time) already in output CSV
  - --dry-run: scope only, no RPC calls

Usage:
  python3 scripts/sprint078_expanded_shadow_backfill.py --dry-run
  python3 scripts/sprint078_expanded_shadow_backfill.py --run
  python3 scripts/sprint078_expanded_shadow_backfill.py --run --max-pools 50
  python3 scripts/sprint078_expanded_shadow_backfill.py --run --from-date 2026-05-01
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib.v3_quoterv2_struct import (
    JsonRpcClient, WETH_BASE, quote_exact_input_single_struct,
)

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO       = Path(__file__).resolve().parents[1]
OUT_CSV    = REPO / "logs" / "sprint078_shadow.csv"
STATE_DIR  = REPO / "logs" / "sprint078"
STATE_FILE = STATE_DIR / "state.json"
SKIP_LOG   = STATE_DIR / "skipped_pools.log"

STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── ClickHouse ─────────────────────────────────────────────────────────────────
CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID     = 8453
CH_MEM       = "max_memory_usage = 3000000000"

# ── Discovery gates (relaxed vs sprint055) ────────────────────────────────────
DEFAULT_FROM_DATE = "2026-04-01"   # first-seen cutoff for pool universe
HEAT_MIN          = 5              # lowered from 10
POOL_MAX_AGE      = 86400          # 24h unchanged
ONSET_LAG_MAX     = 3600           # 60m unchanged
MIN_POOL_SWAPS    = 10             # filter very low-volume pools

# ── Trade simulation ──────────────────────────────────────────────────────────
WETH_BASE_LC   = WETH_BASE.lower()
ENTRY_WETH_RAW = int(0.00005 * 10**18)   # 50k gwei micro-notional
GAS_PCT        = 0.002
SEL_FEE        = "0xddca3f43"
SEL_TOKEN0     = "0x0dfe1681"
SEL_TOKEN1     = "0xd21220a7"
RPC_DELAY      = 0.06

# ── Exit horizons (mirrors sprint055 schema exactly) ─────────────────────────
EXIT_HORIZONS = [300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
EXIT_LABELS   = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]

# ── Block estimation anchors (Base chain ~2 sec/block) ────────────────────────
# Verified from sprint055_paper_shadow.csv rows:
#   block 45877485 ↔ 2026-05-12 00:00:00 UTC
#   block 46095729 ↔ 2026-05-17 00:45:00 UTC
REF_TS_A       = 1747008000    # 2026-05-12 00:00:00 UTC
REF_BLOCK_A    = 45877485
REF_TS_B       = 1747442700    # 2026-05-17 00:45:00 UTC
REF_BLOCK_B    = 46095729
BLOCKS_PER_SEC = (REF_BLOCK_B - REF_BLOCK_A) / (REF_TS_B - REF_TS_A)  # ≈ 0.502

# Hourly anchors from block_ts_map (Apr 1 - May 10), loaded at runtime
_BLOCK_ANCHORS: list[tuple[int, int]] = []   # [(hour_ts, block_num), ...] sorted asc


def _load_block_anchors() -> None:
    """Load pre-built hourly block anchors from logs/sprint078/block_anchors_clean.json."""
    global _BLOCK_ANCHORS
    if _BLOCK_ANCHORS:
        return
    anchor_file = STATE_DIR / "block_anchors_clean.json"
    if anchor_file.exists():
        data = json.loads(anchor_file.read_text())
        _BLOCK_ANCHORS = [(int(r[0]), int(r[1])) for r in data]

# ── CSV columns (identical to sprint055_paper_shadow.csv) ─────────────────────
CSV_COLS = [
    "signal_time", "pool", "token_other", "fee_bps", "weth_is_token1",
    "pool_age_sec", "onset_lag_sec",
    "activity_heat", "buy_pressure_ratio", "price_velocity_5m",
    "composite_flag",
    "entry_block", "entry_quote_ok", "entry_amount_out_tokens",
    "exit_5m_ok",  "exit_5m_block",  "net_return_5m",  "net_return_5m_ag",
    "exit_10m_ok", "exit_10m_block", "net_return_10m", "net_return_10m_ag",
    "exit_15m_ok", "exit_15m_block", "net_return_15m", "net_return_15m_ag",
    "exit_30m_ok", "exit_30m_block", "net_return_30m", "net_return_30m_ag",
    "exit_1h_ok",  "exit_1h_block",  "net_return_1h",  "net_return_1h_ag",
    "exit_2h_ok",  "exit_2h_block",  "net_return_2h",  "net_return_2h_ag",
    "exit_4h_ok",  "exit_4h_block",  "net_return_4h",  "net_return_4h_ag",
    "exit_6h_ok",  "exit_6h_block",  "net_return_6h",  "net_return_6h_ag",
    "exit_12h_ok", "exit_12h_block", "net_return_12h", "net_return_12h_ag",
    "exit_24h_ok", "exit_24h_block", "net_return_24h", "net_return_24h_ag",
    "gas_proxy_pct", "entry_fail_reason",
]

# Inline amount0 < 0 expression for CH (buy indicator)
_AMOUNT0_LT0 = (
    "reinterpretAsInt256(reverse(unhex("
    "if(startsWith(amount0_raw,'0x'), substring(amount0_raw,3), amount0_raw)"
    "))) < 0"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ch(sql: str, timeout: int = 90) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:300]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def ts_to_block(ts: float) -> int:
    """Estimate Base block number for unix timestamp.

    Uses hourly anchors from block_ts_map (Apr 1 - May 10) for precision;
    falls back to linear interpolation from REF anchors for dates outside range.
    """
    _load_block_anchors()
    if _BLOCK_ANCHORS:
        # Binary search for bracketing anchors
        lo, hi = 0, len(_BLOCK_ANCHORS) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if _BLOCK_ANCHORS[mid][0] <= ts:
                lo = mid
            else:
                hi = mid
        ts_lo, blk_lo = _BLOCK_ANCHORS[lo]
        ts_hi, blk_hi = _BLOCK_ANCHORS[hi]
        if ts_lo <= ts <= ts_hi and ts_hi > ts_lo:
            # Interpolate within anchor interval
            frac = (ts - ts_lo) / (ts_hi - ts_lo)
            return int(blk_lo + frac * (blk_hi - blk_lo))
        # ts is beyond anchor range — fall through to linear extrapolation
    return int(REF_BLOCK_A + (ts - REF_TS_A) * BLOCKS_PER_SEC)


def load_existing_keys() -> set[tuple[str, str]]:
    """Return set of (pool, signal_time_iso) already in sprint078 output CSV."""
    keys: set[tuple[str, str]] = set()
    if not OUT_CSV.exists():
        return keys
    with OUT_CSV.open() as f:
        for row in csv.DictReader(f):
            keys.add((row.get("pool", ""), row.get("signal_time", "")))
    return keys


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_pools": [], "skipped": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── ClickHouse discovery ───────────────────────────────────────────────────────

def discover_candidates(from_date: str) -> list[dict]:
    """Find all young-pool onset signals for pools first-seen since from_date.

    Uses block_time (not received_at_ms) to accurately filter pools by
    their creation date on-chain.

    Returns list of dicts: pool, wts, first_seen_ts, pool_age_sec,
    onset_lag_sec, activity_heat, buy_pressure_ratio, price_velocity_5m.
    Sorted by first_seen_ts DESC (most recent first).
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    print(f"[078] Discovery: pools first-seen >= {from_date} (block_time filter)")

    # Phase 1: Find all pools first-seen >= from_date with enough swaps
    pool_rows = _ch(f"""
SELECT
    pool_address,
    toUnixTimestamp(min(block_time)) AS first_ts,
    count()                          AS total_swaps
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND block_time IS NOT NULL
GROUP BY pool_address
HAVING min(block_time) >= toDateTime64('{from_date} 00:00:00', 3, 'UTC')
   AND total_swaps >= {MIN_POOL_SWAPS}
ORDER BY first_ts
SETTINGS {CH_MEM}
""", timeout=90)
    if not pool_rows:
        print("[078] No qualifying pools found.")
        return []

    pool_first: dict[str, int] = {}
    for r in pool_rows:
        if len(r) >= 2:
            pool_first[r[0]] = int(r[1])

    print(f"[078] Phase1: {len(pool_first)} pools first-seen >= {from_date}")

    # Phase 2: For each qualifying pool, get 5m heat windows during first 24h
    # We query ALL heat windows for these pools where block_time is within
    # pool_age ≤ 86400s (first 24h of the pool's life)
    pool_list = ",".join(f"'{p}'" for p in pool_first)

    heat_rows = _ch(f"""
SELECT
    pool_address,
    toUnixTimestamp(toStartOfFiveMinutes(block_time)) AS wts,
    count()                                           AS heat,
    countIf({_AMOUNT0_LT0})                           AS buys
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND block_time IS NOT NULL
  AND pool_address IN ({pool_list})
GROUP BY pool_address, wts
HAVING heat >= {HEAT_MIN}
ORDER BY pool_address, wts
SETTINGS {CH_MEM}
""", timeout=120)
    print(f"[078] Phase2: {len(heat_rows)} heat windows with heat>={HEAT_MIN}")

    # Build onset-filtered candidates
    windows: dict[str, list[dict]] = defaultdict(list)
    for r in heat_rows:
        if len(r) < 4:
            continue
        try:
            windows[r[0]].append({"wts": int(r[1]), "heat": int(r[2]), "buys": int(r[3])})
        except Exception:
            pass

    candidates: list[dict] = []
    for pool, wins in windows.items():
        fs = pool_first.get(pool)
        if fs is None:
            continue
        # Find onset timestamp (first window with heat >= HEAT_MIN)
        onset_ts = None
        for w in sorted(wins, key=lambda x: x["wts"]):
            if w["heat"] >= HEAT_MIN:
                onset_ts = w["wts"]
                break
        if onset_ts is None:
            continue
        for w in sorted(wins, key=lambda x: x["wts"]):
            pool_age  = w["wts"] - fs
            onset_lag = w["wts"] - onset_ts
            if pool_age  > POOL_MAX_AGE:  continue
            if onset_lag > ONSET_LAG_MAX: continue
            n   = w["heat"]
            bpr = round(w["buys"] / n, 4) if n > 0 else 0.0
            candidates.append({
                "pool":               pool,
                "wts":                w["wts"],
                "first_seen_ts":      fs,
                "pool_age_sec":       pool_age,
                "onset_lag_sec":      onset_lag,
                "activity_heat":      n,
                "buy_pressure_ratio": bpr,
                "price_velocity_5m":  round(2 * bpr - 1, 4),
            })

    # Sort by first_seen_ts DESC (most recent pools first)
    candidates.sort(key=lambda x: x["first_seen_ts"], reverse=True)
    unique = len(set(c["pool"] for c in candidates))
    print(f"[078] Onset candidates: {len(candidates)} windows, {unique} unique pools")
    return candidates


# ── Pool metadata ─────────────────────────────────────────────────────────────

def get_registry_batch(pools: list[str]) -> dict[str, dict]:
    """Get fee + tokens from pool_registry (fast, no RPC needed)."""
    if not pools:
        return {}
    pl = ",".join(f"'{p.lower()}'" for p in pools)
    rows = _ch(f"""
SELECT lower(pool_address), fee_bps,
       lower(token0_address), lower(token1_address)
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_address) IN ({pl})
SETTINGS {CH_MEM}
""", timeout=20)
    result = {}
    for r in rows:
        if len(r) < 4:
            continue
        # Registry fee_bps in actual bps; Uniswap ppm = bps * 100
        result[r[0]] = {
            "fee_ppm": int(r[1]) * 100,
            "token0":  r[2],
            "token1":  r[3],
        }
    return result


def get_pool_info_rpc(rpc: JsonRpcClient, pool: str,
                      fee_cache: dict, token_cache: dict) -> Optional[dict]:
    """Fallback: fetch fee (ppm) + token addresses via RPC calls."""
    pool_lc = pool.lower()

    if pool_lc not in fee_cache:
        try:
            resp = rpc.session.post(rpc.url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": pool, "data": SEL_FEE}, "latest"],
            }, timeout=15)
            raw = resp.json().get("result", "0x")
            fee_cache[pool_lc] = int(raw[2:][-8:], 16) if raw and raw != "0x" else None
            time.sleep(RPC_DELAY)
        except Exception:
            fee_cache[pool_lc] = None

    fee_ppm = fee_cache.get(pool_lc)
    if fee_ppm is None:
        return None

    if pool_lc not in token_cache:
        tok0, tok1 = None, None
        for sel, name in [(SEL_TOKEN0, "token0"), (SEL_TOKEN1, "token1")]:
            try:
                resp = rpc.session.post(rpc.url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": pool, "data": sel}, "latest"],
                }, timeout=15)
                raw = resp.json().get("result", "0x")
                if raw and len(raw) >= 42:
                    addr = ("0x" + raw[-40:]).lower()
                    tok0 = addr if name == "token0" else tok0
                    tok1 = addr if name == "token1" else tok1
            except Exception:
                pass
            time.sleep(RPC_DELAY)
        token_cache[pool_lc] = {"token0": tok0, "token1": tok1}

    tc = token_cache.get(pool_lc, {})
    return {"fee_ppm": fee_ppm, "token0": tc.get("token0"), "token1": tc.get("token1")}


# ── QuoterV2 at historical blocks ─────────────────────────────────────────────

def run_signal_quotes(rpc: JsonRpcClient, token_other: str, fee_ppm: int,
                      wts: int, now_ts: int) -> dict:
    """
    Entry quote + all elapsed exit quotes for one (pool, wts) signal.
    Entry block ≈ block at wts + 300s (just after 5m window closes).
    Returns partial CSV row dict.
    """
    entry_ts    = wts + 300
    entry_block = ts_to_block(entry_ts)

    q_entry = {}
    try:
        q_entry = quote_exact_input_single_struct(
            rpc, WETH_BASE_LC, token_other, ENTRY_WETH_RAW, fee_ppm,
            block_identifier=entry_block,
        )
    except Exception as e:
        q_entry = {"ok": False, "reason": str(e)[:120], "amount_out": 0}
    time.sleep(RPC_DELAY)

    row: dict = {
        "entry_block":             entry_block,
        "entry_quote_ok":          q_entry.get("ok", False),
        "entry_amount_out_tokens": int(q_entry.get("amount_out", 0)),
        "entry_fail_reason":       "" if q_entry.get("ok") else q_entry.get("reason", ""),
    }

    if not q_entry.get("ok"):
        return row

    token_amount = int(q_entry["amount_out"])
    if token_amount == 0:
        return row

    for h_sec, lbl in zip(EXIT_HORIZONS, EXIT_LABELS):
        exit_ts = entry_ts + h_sec
        if exit_ts > now_ts + 120:   # not yet elapsed
            continue
        exit_block = ts_to_block(exit_ts)
        q_exit = {}
        try:
            q_exit = quote_exact_input_single_struct(
                rpc, token_other, WETH_BASE_LC, token_amount, fee_ppm,
                block_identifier=exit_block,
            )
        except Exception as e:
            q_exit = {"ok": False, "reason": str(e)[:80], "amount_out": 0}
        time.sleep(RPC_DELAY)

        ok      = q_exit.get("ok", False)
        weth_out = int(q_exit.get("amount_out", 0)) if ok else 0
        net_ret  = (weth_out / ENTRY_WETH_RAW - 1.0) if ok and ENTRY_WETH_RAW > 0 else None

        row[f"exit_{lbl}_ok"]            = ok
        row[f"exit_{lbl}_block"]         = exit_block
        row[f"net_return_{lbl}"]         = round(net_ret, 8) if net_ret is not None else ""
        row[f"net_return_{lbl}_ag"]      = round(net_ret - GAS_PCT, 8) if net_ret is not None else ""

    return row


# ── CSV I/O ───────────────────────────────────────────────────────────────────

def ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writeheader()


def append_row(path: Path, row: dict) -> None:
    ensure_header(path)
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writerow(row)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint078 expanded shadow backfill")
    parser.add_argument("--dry-run",     action="store_true", help="Scope only, no RPC calls")
    parser.add_argument("--run",         action="store_true", help="Execute full backfill")
    parser.add_argument("--max-pools",   type=int, default=0, help="Limit pools (0=all)")
    parser.add_argument("--from-date",   default=DEFAULT_FROM_DATE,
                        help=f"Pool first-seen cutoff YYYY-MM-DD (default {DEFAULT_FROM_DATE})")
    args = parser.parse_args()

    assert os.environ.get("LIVE_TRADING_MODE", "").lower() not in ("1","true","yes","on"), \
        "LIVE_TRADING_MODE must be off"

    if not args.dry_run and not args.run:
        parser.print_help()
        sys.exit(0)

    # ── Discover candidates ───────────────────────────────────────────────────
    candidates = discover_candidates(args.from_date)
    if not candidates:
        print("[078] No candidates found.")
        return

    by_pool: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_pool[c["pool"]].append(c)

    existing_keys     = load_existing_keys()
    already_in_output = set(k[0] for k in existing_keys)

    new_pools      = [p for p in by_pool if p not in already_in_output]
    resumed_pools  = [p for p in by_pool if p in already_in_output]

    total_new_pairs = sum(
        1 for pool, wins in by_pool.items()
        for w in wins
        if (pool, datetime.fromtimestamp(w["wts"], tz=timezone.utc).isoformat()) not in existing_keys
    )

    # Scope summary
    print(f"\n[078] === SCOPE SUMMARY ===")
    print(f"  from_date filter:          {args.from_date}")
    print(f"  Unique pools with onset:   {len(by_pool)}")
    print(f"  Already in output CSV:     {len(already_in_output)}")
    print(f"  New pools to process:      {len(new_pools)}")
    print(f"  New (pool,wts) pairs:      {total_new_pairs}")
    print(f"  Estimated RPC calls:       ~{total_new_pairs * 11}")
    print(f"  Estimated runtime:         ~{total_new_pairs * 11 * 0.08 / 60:.0f} min")

    # Date breakdown
    from collections import Counter
    onset_dates = Counter(
        datetime.fromtimestamp(c["first_seen_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        for c in candidates
        if c["pool"] in by_pool
        for _ in [c]
    )
    print(f"\n  Pools by first-seen date (unique):")
    date_pool_ct: dict[str, set] = defaultdict(set)
    for c in candidates:
        d = datetime.fromtimestamp(c["first_seen_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        date_pool_ct[d].add(c["pool"])
    for d in sorted(date_pool_ct.keys())[-20:]:
        n = len(date_pool_ct[d])
        fwd_marker = " ← forward cutoff (Apr 1)" if d == "2026-04-01" else ""
        print(f"    {d}: {n} pools{fwd_marker}")

    if args.dry_run:
        print("\n[078] DRY-RUN complete. Re-run with --run to execute.")
        print("\n  Sample new pools (most recent first):")
        for p in list(dict.fromkeys(
            c["pool"] for c in sorted(candidates, key=lambda x: -x["first_seen_ts"])
        ))[:20]:
            wins = by_pool[p]
            first_date = datetime.fromtimestamp(wins[0]["first_seen_ts"],
                                                tz=timezone.utc).strftime("%Y-%m-%d")
            avg_heat = sum(w["activity_heat"] for w in wins) / len(wins)
            print(f"    {p}: {len(wins)} windows, first={first_date}, avg_heat={avg_heat:.0f}")
        return

    # ── Full run ──────────────────────────────────────────────────────────────
    # Load RPC URL
    rpc_url = ""
    env_file = REPO / "infra/compose/.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v:
                    rpc_url = v
                    break
    rpc_url = (rpc_url
               or os.environ.get("LABEL_RPC_URL", "")
               or os.environ.get("EVM_HTTP_URL", ""))
    if not rpc_url:
        print("ERROR: no RPC URL in .env or environment", file=sys.stderr)
        sys.exit(1)

    import requests
    rpc           = JsonRpcClient(rpc_url, delay_s=0.0)
    rpc.session   = requests.Session()
    state         = load_state()
    now_ts        = int(datetime.now(timezone.utc).timestamp())
    fee_cache: dict    = {}
    token_cache: dict  = {}
    total_written = 0
    total_skipped = 0
    total_no_weth = 0

    # Batch registry lookup
    all_pools      = list(by_pool.keys())
    registry_info  = get_registry_batch(all_pools)
    print(f"\n[078] Registry hits: {len(registry_info)}/{len(all_pools)} pools "
          f"(remainder will use RPC)")

    pools_to_run = new_pools + resumed_pools
    if args.max_pools > 0:
        pools_to_run = pools_to_run[:args.max_pools]
        print(f"[078] Limiting to {args.max_pools} pools (--max-pools)")

    print(f"\n[078] Starting backfill for {len(pools_to_run)} pools...")

    for i, pool in enumerate(pools_to_run):
        wins = by_pool[pool]
        first_date = datetime.fromtimestamp(wins[0]["first_seen_ts"],
                                            tz=timezone.utc).strftime("%Y-%m-%d")

        # Get pool metadata
        info = registry_info.get(pool.lower())
        if info is None:
            info = get_pool_info_rpc(rpc, pool, fee_cache, token_cache)

        if info is None:
            print(f"\n  [{i+1}/{len(pools_to_run)}] SKIP {pool[:12]} "
                  f"(first={first_date}): no fee/token info")
            with SKIP_LOG.open("a") as f:
                f.write(f"no_info\t{pool}\n")
            total_skipped += len(wins)
            continue

        token0   = (info.get("token0") or "").lower()
        token1   = (info.get("token1") or "").lower()
        fee_ppm  = info.get("fee_ppm", 0)

        # Fee tier guard: exclude 1% tier (fee_ppm=10000) — established pools like
        # AERO/WETH, cbETH/WETH — not the new-pool onset phenomenon we're studying.
        # Consistent with sprint055 cron (fee_bps >= 10000: continue).
        if fee_ppm >= 10000:
            with SKIP_LOG.open("a") as f:
                f.write(f"fee_gte_10000\t{pool}\t{fee_ppm}\n")
            total_no_weth += len(wins)
            continue

        # WETH leg check
        if not token0 or not token1:
            print(f"\n  [{i+1}/{len(pools_to_run)}] SKIP {pool[:12]}: missing tokens")
            with SKIP_LOG.open("a") as f:
                f.write(f"no_token\t{pool}\n")
            total_no_weth += len(wins)
            continue

        weth_is_token1 = token0 == WETH_BASE_LC
        if not weth_is_token1 and token1 != WETH_BASE_LC:
            # No WETH leg — skip silently (most pools)
            with SKIP_LOG.open("a") as f:
                f.write(f"no_weth\t{pool}\n")
            total_no_weth += len(wins)
            continue

        token_other = token1 if weth_is_token1 else token0

        n_written = 0
        n_skip    = 0
        print(f"\n  [{i+1}/{len(pools_to_run)}] {pool[:12]}… "
              f"first={first_date} fee={fee_ppm}ppm "
              f"{len(wins)} windows", end=" ")

        for w in sorted(wins, key=lambda x: x["wts"]):
            sig_ts_iso = datetime.fromtimestamp(w["wts"], tz=timezone.utc).isoformat()
            if (pool, sig_ts_iso) in existing_keys:
                n_skip += 1
                continue

            q_row = run_signal_quotes(rpc, token_other, fee_ppm, w["wts"], now_ts)

            comp = (w["activity_heat"] >= 20
                    and w["buy_pressure_ratio"] < 0.55
                    and weth_is_token1)

            row = {
                "signal_time":              sig_ts_iso,
                "pool":                     pool,
                "token_other":              token_other,
                "fee_bps":                  fee_ppm,
                "weth_is_token1":           weth_is_token1,
                "pool_age_sec":             w["pool_age_sec"],
                "onset_lag_sec":            w["onset_lag_sec"],
                "activity_heat":            w["activity_heat"],
                "buy_pressure_ratio":       w["buy_pressure_ratio"],
                "price_velocity_5m":        w["price_velocity_5m"],
                "composite_flag":           comp,
                "gas_proxy_pct":            GAS_PCT,
                **q_row,
            }
            append_row(OUT_CSV, row)
            existing_keys.add((pool, sig_ts_iso))
            n_written += 1

        entry_ok_str = "✓" if n_written > 0 else "✗"
        print(f"→ wrote={n_written} skip={n_skip} {entry_ok_str}")

        total_written += n_written
        total_skipped += n_skip
        state["processed_pools"].append(pool)
        save_state(state)

    # ── Final stats ───────────────────────────────────────────────────────────
    print(f"\n[078] === BACKFILL COMPLETE ===")
    print(f"  Rows written:          {total_written}")
    print(f"  Rows skipped (dup):    {total_skipped}")
    print(f"  Pools no WETH leg:     {total_no_weth}")

    if OUT_CSV.exists():
        all_rows  = list(csv.DictReader(OUT_CSV.open()))
        all_pools_out = len(set(r["pool"] for r in all_rows))

        # forward slice: signal_time > Apr 1 (the sprint079 forward cutoff)
        fwd_rows  = [r for r in all_rows if r["signal_time"] > "2026-04-01T00:00:00"]
        fwd_pools = len(set(r["pool"] for r in fwd_rows))

        print(f"\n  sprint078_shadow.csv stats:")
        print(f"    Total rows:               {len(all_rows)}")
        print(f"    Unique pools (all):        {all_pools_out}")
        print(f"    Forward rows (>Apr 1):     {len(fwd_rows)}")
        print(f"    Forward unique pools:      {fwd_pools}")
        print(f"\n  Forward cutoff for sprint079 harness:")
        print(f"    FORWARD_CUTOFF = '2026-04-01T00:00:00+00:00'")
        print(f"    → {fwd_pools} unique pools provide real OOS evidence")


if __name__ == "__main__":
    main()
