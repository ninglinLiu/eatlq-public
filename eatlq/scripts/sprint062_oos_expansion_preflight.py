#!/usr/bin/env python3
"""Sprint062 — OOS week expansion preflight + bounded label readout.

Research-only. No private key, no live, no training, no production signals.
Thresholds frozen at Sprint037G canonical gate — NOT tuned to produce results.

Sprint053 data gap:
  sprint053 ran on 2026-05-10 when raw_evm_logs was stale (max ~Apr-30).
  Week 2026-04-26→05-03 had only 2,591 swaps in CH at that time.
  Now CH has full data through 2026-05-10 (51,767 swaps in Apr-27 week).
  This sprint scans the newly-available weeks with the same frozen gate.

Canonical gate (Sprint037G, FROZEN):
  - pool_age  ≤ 86400s  (pool must have launched within 24h)
  - onset_max ≤ 3600s   (swaps within first 60min of pool launch)
  - thresh    ≥ 10      swaps per 5-min window during onset
  - fee tier = 100 bps only (fee_100 = 0.01%)

Phase 1 (data expansion preflight): CH query, no RPC labels.
Phase 2 (bounded label readout): QuoterV2 RPC for fee_100bps candidates.
  Budget: ≤800 RPC calls total (dedup to unique 5-min windows first).
  Uses historical blocks matching sprint054 methodology.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib.v3_quoterv2_struct import (
    JsonRpcClient,
    WETH_BASE,
    QUOTER_V2_BASE,
    quote_exact_input_single_struct,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_OUT = ROOT / "mvp/audits/sprint062"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint062"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID     = 8453
WINDOW_SEC   = 300  # 5 min label window

# Sprint037G canonical gate — FROZEN, do not tune
GATE_A = dict(pool_max_age_sec=86400, onset_max_sec=3600, onset_thresh=10, label="CANONICAL_037G")

# Sprint053/054 benchmarks (real QuoterV2 labels, historical blocks)
S053_BENCH = dict(hit_rate=0.522, profit_factor=1.884, rows=117)
S054_BENCH = dict(hit_rate=0.522, profit_factor=1.884, median_ret=0.0014)

# Expansion weeks (beyond sprint053 scan, + complete Apr-27 week)
EXPANSION_WEEKS = [
    ("2026-04-27", "2026-05-04"),   # full week, previously truncated → primary
    ("2026-05-04", "2026-05-11"),   # current partial week → secondary
]

# Pool fee metadata sources
S053_META_PATH = ROOT / "mvp/diagnostics/sprint053/pool_meta.json"

SEL_TOKEN1  = "0xd21220a7"   # pool.token1() selector
RPC_DELAY   = 0.04
MAX_RPC     = 800   # hard cap for Phase 2

# WETH 5-min notional probe (same as sprint054)
PROBE_WEI   = int(0.00005 * 10**18)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_ch(sql: str) -> list[list[str]]:
    result = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CH query failed: {result.stderr[:400]}")
    reader = csv.reader(io.StringIO(result.stdout))
    return list(reader)


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


def load_pool_meta() -> dict[str, dict]:
    """Load fee metadata from sprint053 pool_meta.json."""
    if S053_META_PATH.exists():
        with open(S053_META_PATH) as f:
            raw = json.load(f)
        # sprint053 meta: {addr: {fee_bps: int, token0: str, weth_is_token0: bool}}
        return {k.lower(): v for k, v in raw.items()}
    return {}


WETH_BASE_L = WETH_BASE.lower()


def load_discovered_pools() -> tuple[dict[str, int], dict[str, dict]]:
    """Query discovered_pools. Returns:
      fee_map:  {addr: fee_bps_sprint_units}  (fee_bps=1 → 100 in sprint units)
      meta_map: {addr: {fee_bps, token0, token1, weth_is_token0}}  for V3 pools
    """
    try:
        rows = run_ch(f"""
            SELECT lower(pool_address), fee_bps, pool_type,
                   lower(token0_address), lower(token1_address)
            FROM eatlq.discovered_pools
            WHERE chain_id = {CHAIN_ID}
        """)
    except Exception as e:
        print(f"  [warn] discovered_pools query failed: {e}", flush=True)
        return {}, {}
    fee_map: dict[str, int]  = {}
    meta_map: dict[str, dict] = {}
    for row in rows:
        if len(row) < 5:
            continue
        addr      = row[0].lower()
        pool_type = row[2]
        if pool_type != "v3":
            continue
        try:
            fee_raw = int(row[1])
        except ValueError:
            continue
        fee_units = fee_raw * 100   # 1 bps → 100 (Uniswap internal)
        fee_map[addr] = fee_units
        t0 = row[3].lower()
        t1 = row[4].lower()
        meta_map[addr] = {
            "fee_bps": fee_units,
            "token0": t0,
            "token1": t1,
            "weth_is_token0": t0 == WETH_BASE_L,
        }
    return fee_map, meta_map


# ---------------------------------------------------------------------------
# Phase 1: canonical gate dry-run (no labels, no RPC)
# ---------------------------------------------------------------------------

def phase1_gate_dryrun(pool_meta: dict, discovered_fee: dict) -> dict:
    """Count fee_100bps canonical gate rows per expansion week. No labels.

    Strategy: build the fee_100bps allowlist first, then query CH only for
    those pools — keeps data volume small and avoids CH memory exhaustion.
    """
    print("\n[sprint062] Phase 1: canonical gate dry-run", flush=True)

    # Build fee_100bps allowlist from both sources
    fee100_all: set[str] = set()
    for addr, info in pool_meta.items():
        if isinstance(info, dict) and info.get("fee_bps") == 100:
            fee100_all.add(addr.lower())
    for addr, fee_units in discovered_fee.items():
        if fee_units == 100:
            fee100_all.add(addr.lower())

    print(f"  fee_100bps allowlist: {len(fee100_all)} pools", flush=True)

    # Also need per-pool global first_swap to check if "new" during each week.
    # Query min(block_time) for all fee_100bps pools in the allowlist.
    if not fee100_all:
        print("  [warn] no fee_100bps pools in allowlist", flush=True)
        return {}

    # Format for IN clause (batch up to avoid CH query limit)
    addr_list = ", ".join(f"'{a}'" for a in sorted(fee100_all))
    global_first_rows = run_ch(f"""
        SELECT lower(pool_address) AS pool, min(block_time) AS global_first_swap
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE lower(pool_address) IN ({addr_list})
        GROUP BY pool
    """)
    global_first: dict[str, str] = {r[0]: r[1] for r in global_first_rows if len(r) >= 2}
    print(f"  global first_swap resolved for {len(global_first)} fee_100bps pools", flush=True)

    results = {}

    for w_start, w_end in EXPANSION_WEEKS:
        print(f"\n  Week {w_start}→{w_end}", flush=True)

        # Find fee_100bps pools where global first swap falls WITHIN this week
        # (pool_age constraint: must be a NEW pool launched in this week)
        new_pools_in_week: list[str] = []
        for pool, gf in global_first.items():
            # gf is like "2026-04-27 00:00:33.000"
            gf_date = gf[:10]  # "YYYY-MM-DD"
            if w_start <= gf_date < w_end:
                new_pools_in_week.append(pool)

        print(f"  {w_start}: {len(new_pools_in_week)} fee_100bps pools with first swap in this week",
              flush=True)

        # Count total swaps in week for context
        week_totals = run_ch(f"""
            SELECT count(), countDistinct(lower(pool_address))
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE block_time >= '{w_start}' AND block_time < '{w_end}'
        """)
        total_swaps = int(week_totals[0][0]) if week_totals else 0
        total_pools = int(week_totals[0][1]) if week_totals else 0

        if not new_pools_in_week:
            print(f"  {w_start}: no new fee_100bps pools → 0 canonical rows", flush=True)
            results[w_start] = {
                "week": w_start, "total_swaps": total_swaps, "total_pools": total_pools,
                "fee100_pools": 0, "canonical_gate_rows": 0, "qualified_pools": 0,
                "top1_pool_share": 0.0, "candidates": [], "pool_counts": {},
            }
            continue

        # Fetch swaps for new fee_100bps pools within pool_age window (≤86400s = 1 day)
        new_addr_list = ", ".join(f"'{a}'" for a in new_pools_in_week)
        rows = run_ch(f"""
            SELECT
                lower(pool_address) AS pool,
                toString(block_time) AS block_time,
                toString(sqrt_price_x96) AS sqrt_price_x96,
                toString(amount0) AS amount0,
                toString(amount1) AS amount1
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE lower(pool_address) IN ({new_addr_list})
              AND block_time >= '{w_start}'
              AND block_time <  '{w_end}'
            ORDER BY block_time
        """)

        if not rows:
            print(f"  {w_start}: no swap rows for new fee_100bps pools", flush=True)
            results[w_start] = {
                "week": w_start, "total_swaps": total_swaps, "total_pools": total_pools,
                "fee100_pools": len(new_pools_in_week), "canonical_gate_rows": 0,
                "qualified_pools": 0, "top1_pool_share": 0.0,
                "candidates": [], "pool_counts": {},
            }
            continue

        print(f"  {w_start}: {len(rows)} swaps for {len(new_pools_in_week)} new fee_100bps pools",
              flush=True)

        # Apply canonical gate in Python
        gate_rows: list[dict] = []
        for row in rows:
            pool    = row[0].lower()
            bt_str  = row[1]
            sqrt_px = int(row[2]) if row[2] else 0
            amt0    = int(row[3]) if row[3] else 0
            amt1    = int(row[4]) if row[4] else 0

            gf_str = global_first.get(pool, "")
            if not gf_str:
                continue

            try:
                bt = datetime.fromisoformat(bt_str.replace(" ", "T"))
                ft = datetime.fromisoformat(gf_str.replace(" ", "T"))
                if bt.tzinfo is None: bt = bt.replace(tzinfo=timezone.utc)
                if ft.tzinfo is None: ft = ft.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            pool_age_sec = (bt - ft).total_seconds()
            if pool_age_sec < 0 or pool_age_sec > GATE_A["pool_max_age_sec"]:
                continue

            gate_rows.append({
                "pool": pool, "block_time": bt_str,
                "pool_age_sec": round(pool_age_sec, 1),
                "sqrt_price_x96": sqrt_px, "amount0": amt0, "amount1": amt1,
                "week": w_start,
            })

        # Onset check: ≥10 swaps in some 5-min bucket within first 3600s
        pool_onset_ok: set[str] = set()
        pool_swaps_by_5m: dict[str, Counter] = defaultdict(Counter)
        for r in gate_rows:
            if r["pool_age_sec"] <= GATE_A["onset_max_sec"]:
                bucket = int(r["pool_age_sec"] // WINDOW_SEC)
                pool_swaps_by_5m[r["pool"]][bucket] += 1

        for pool, buckets in pool_swaps_by_5m.items():
            if max(buckets.values()) >= GATE_A["onset_thresh"]:
                pool_onset_ok.add(pool)

        canonical = [r for r in gate_rows
                     if r["pool"] in pool_onset_ok
                     and r["pool_age_sec"] <= GATE_A["onset_max_sec"]]

        print(f"  {w_start}: canonical_gate_rows={len(canonical)}, "
              f"qualified_pools={len(pool_onset_ok)}", flush=True)

        pool_counts = Counter(r["pool"] for r in canonical)
        top1_share  = max(pool_counts.values()) / len(canonical) if canonical else 0.0

        results[w_start] = {
            "week": w_start, "total_swaps": total_swaps, "total_pools": total_pools,
            "fee100_pools": len(new_pools_in_week),
            "canonical_gate_rows": len(canonical),
            "qualified_pools": len(pool_onset_ok),
            "top1_pool_share": round(top1_share, 4),
            "candidates": canonical, "pool_counts": dict(pool_counts),
        }

    return results


# ---------------------------------------------------------------------------
# Phase 2: bounded QuoterV2 label readout (historical blocks, sprint054 method)
# ---------------------------------------------------------------------------

def load_rpc_url() -> str | None:
    env_path = ROOT / "infra/compose/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v:
                    return v
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or None


def build_block_index(
    w_start: str, w_end_ext: str, pools: set[str]
) -> dict[str, list[tuple[int, int]]]:
    if not pools:
        return {}
    pl = ", ".join(f"'{p}'" for p in sorted(pools))
    rows = run_ch(f"""
        SELECT lower(pool_address), toUnixTimestamp(block_time) AS ts, block_number
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE lower(pool_address) IN ({pl})
          AND block_time >= '{w_start}'
          AND block_time <  '{w_end_ext}'
          AND block_time IS NOT NULL
        ORDER BY pool_address, ts
    """)
    idx: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        if len(r) >= 3:
            try:
                idx[r[0].lower()].append((int(r[1]), int(r[2])))
            except (ValueError, OverflowError):
                pass
    return dict(idx)


def resolve_blocks(
    pool: str, wts: int, idx: dict[str, list[tuple[int, int]]]
) -> tuple[int | None, int | None]:
    ticks = idx.get(pool, [])
    if not ticks:
        return None, None
    ts_list  = [t[0] for t in ticks]
    blk_list = [t[1] for t in ticks]
    end_entry = wts + WINDOW_SEC
    i_entry   = bisect.bisect_left(ts_list, end_entry) - 1
    if i_entry < 0:
        return None, None
    entry_block = blk_list[i_entry]
    end_exit  = wts + 2 * WINDOW_SEC
    i_exit    = bisect.bisect_left(ts_list, end_entry)
    if i_exit >= len(ts_list) or ts_list[i_exit] >= end_exit:
        return None, None
    exit_block = blk_list[i_exit]
    return entry_block, exit_block


def get_token1(rpc: JsonRpcClient, pool: str) -> str | None:
    try:
        resp = rpc.session.post(
            rpc.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": pool, "data": SEL_TOKEN1}, "latest"]},
            timeout=15,
        )
        result = resp.json().get("result", "0x")
        if result and len(result) >= 42:
            return ("0x" + result[-40:]).lower()
    except Exception:
        pass
    return None


def run_quote(
    rpc: JsonRpcClient, token_in: str, token_out: str,
    fee_bps: int, amount_in: int,
    entry_block: int, exit_block: int,
) -> dict:
    q_entry = quote_exact_input_single_struct(
        rpc, token_in, token_out, amount_in, fee_bps,
        block_identifier=entry_block,
    )
    if not q_entry.get("ok"):
        return {"entry_quote_ok": False, "exit_quote_ok": False,
                "net_return_5m": None,
                "fail_reason": "entry_" + (q_entry.get("reason") or "fail")}
    token_amount = int(q_entry["amount_out"])
    q_exit = quote_exact_input_single_struct(
        rpc, token_out, token_in, token_amount, fee_bps,
        block_identifier=exit_block,
    )
    if not q_exit.get("ok"):
        return {"entry_quote_ok": True, "exit_quote_ok": False,
                "net_return_5m": None, "entry_amount_out": token_amount,
                "fail_reason": "exit_" + (q_exit.get("reason") or "fail")}
    exit_weth = int(q_exit["amount_out"])
    net_ret   = exit_weth / amount_in - 1.0
    return {"entry_quote_ok": True, "exit_quote_ok": True,
            "net_return_5m": round(net_ret, 8),
            "entry_amount_out": token_amount, "exit_amount_out_weth": exit_weth,
            "fail_reason": ""}


def phase2_label_readout(phase1: dict, pool_meta: dict, rpc_url: str | None) -> dict:
    """Historical-block QuoterV2 label — dedup to unique (pool, 5-min bucket) first."""
    if rpc_url is None:
        print("\n[sprint062] Phase 2: SKIP — no RPC URL", flush=True)
        return {"skipped": True, "reason": "no_rpc_url"}

    print(f"\n[sprint062] Phase 2: bounded label readout (max {MAX_RPC} RPC calls)", flush=True)
    rpc = JsonRpcClient(rpc_url, delay_s=RPC_DELAY)

    # Dedup to unique (pool, 5-min bucket, week)
    deduped: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for week in sorted(phase1.keys()):
        for c in phase1[week].get("candidates", []):
            pool       = c["pool"]
            bucket_sec = int(c["pool_age_sec"] // WINDOW_SEC) * WINDOW_SEC
            key        = (pool, bucket_sec, week)
            if key not in seen:
                seen.add(key)
                deduped.append({
                    "pool": pool, "week": week,
                    "pool_age_sec": c["pool_age_sec"],
                    "bucket_onset_sec": bucket_sec,
                    "block_time": c["block_time"],
                })

    print(f"  {len(deduped)} unique (pool, 5m-bucket) entries to label", flush=True)
    if not deduped:
        return {"skipped": False, "rpc_calls": 0, "total_deduped": 0,
                "labeled_rows": 0, "hit_rate": None, "profit_factor": None}

    # Resolve token1 for pools where needed
    need_t1 = {
        d["pool"] for d in deduped
        if pool_meta.get(d["pool"], {}).get("weth_is_token0") is True
        and pool_meta.get(d["pool"], {}).get("token1") is None
    }
    token1_map: dict[str, str | None] = {}
    if need_t1:
        print(f"  [rpc] token1 lookups for {len(need_t1)} pools ...", flush=True)
    for pool in sorted(need_t1):
        if rpc._calls >= MAX_RPC:
            break
        token1_map[pool] = get_token1(rpc, pool)
        time.sleep(RPC_DELAY)

    # Block indices per week
    block_idx: dict[str, dict] = {}
    for wk in sorted({d["week"] for d in deduped}):
        wk_pools  = {d["pool"] for d in deduped if d["week"] == wk}
        wk_end_dt = datetime.strptime(wk, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=8)
        wk_end    = wk_end_dt.strftime("%Y-%m-%d")
        print(f"  [ch] block index week {wk}, {len(wk_pools)} pools ...", flush=True)
        block_idx[wk] = build_block_index(wk, wk_end, wk_pools)

    # Label each unique entry
    labeled: list[dict] = []
    for d in deduped:
        if rpc._calls >= MAX_RPC:
            print(f"  [rpc cap] hit {MAX_RPC} calls, stopping", flush=True)
            break
        pool = d["pool"]
        wk   = d["week"]
        meta = pool_meta.get(pool, {})
        fee_bps    = meta.get("fee_bps") or 100
        weth_is_t0 = meta.get("weth_is_token0", True)
        t0         = (meta.get("token0") or "").lower()
        t1_known   = meta.get("token1") or token1_map.get(pool)

        token_in  = WETH_BASE.lower()
        token_out = (t1_known or "").lower() if weth_is_t0 else t0

        if not token_out or token_out in ("", "0x0000000000000000000000000000000000000000"):
            labeled.append({**d, "entry_quote_ok": False, "exit_quote_ok": False,
                             "net_return_5m": None, "fail_reason": "no_token_out"})
            continue

        try:
            bt = datetime.fromisoformat(d["block_time"].replace(" ", "T"))
            if bt.tzinfo is None:
                bt = bt.replace(tzinfo=timezone.utc)
            wts = int(bt.timestamp())
        except ValueError:
            labeled.append({**d, "entry_quote_ok": False, "exit_quote_ok": False,
                             "net_return_5m": None, "fail_reason": "ts_parse_error"})
            continue

        bucket_wts = wts - int(d["pool_age_sec"] % WINDOW_SEC)
        e_blk, x_blk = resolve_blocks(pool, bucket_wts, block_idx.get(wk, {}))
        if e_blk is None or x_blk is None:
            labeled.append({**d, "entry_quote_ok": False, "exit_quote_ok": False,
                             "net_return_5m": None, "fail_reason": "no_blocks"})
            continue

        q = run_quote(rpc, token_in, token_out, fee_bps, PROBE_WEI, e_blk, x_blk)
        labeled.append({**d, **q, "entry_block": e_blk, "exit_block": x_blk,
                        "token_in": token_in, "token_out": token_out})
        time.sleep(RPC_DELAY)

    valid = [c for c in labeled
             if c.get("entry_quote_ok") and c.get("exit_quote_ok")
             and c.get("net_return_5m") is not None]
    if not valid:
        return {"skipped": False, "rpc_calls": rpc._calls,
                "total_deduped": len(deduped), "labeled_rows": 0,
                "hit_rate": None, "profit_factor": None, "labeled_candidates": labeled}

    rets   = [c["net_return_5m"] for c in valid]
    wins   = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    hit_rate = len(wins) / len(valid)
    pf = (sum(wins) / max(abs(sum(losses)), 1e-12)) if losses else float("inf")

    return {
        "skipped": False,
        "rpc_calls": rpc._calls,
        "total_deduped": len(deduped),
        "labeled_rows": len(valid),
        "quote_fail_rows": len(labeled) - len(valid),
        "hit_rate": round(hit_rate, 4),
        "profit_factor": round(pf, 4),
        "median_ret": round(statistics.median(rets), 8),
        "s053_bench_hit": S053_BENCH["hit_rate"],
        "s053_bench_pf":  S053_BENCH["profit_factor"],
        "beats_bench_hit": hit_rate >= S053_BENCH["hit_rate"],
        "beats_bench_pf":  pf >= S053_BENCH["profit_factor"],
        "labeled_candidates": labeled,
    }


# ---------------------------------------------------------------------------
# Write audit artifacts
# ---------------------------------------------------------------------------

def write_readout(phase1: dict, phase2: dict, rpc_calls_p1: int = 0) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- candidate_weeks.csv ---
    rows_csv = []
    for week, info in sorted(phase1.items()):
        rows_csv.append({
            "regime_week": info["week"],
            "total_swaps": info.get("total_swaps", 0),
            "total_pools": info.get("total_pools", 0),
            "fee100_pools": info.get("fee100_pools", 0),
            "canonical_gate_rows": info.get("canonical_gate_rows", 0),
            "qualified_pools": info.get("qualified_pools", 0),
            "top1_pool_share": info.get("top1_pool_share", 0),
            "has_gate_pass": int(info.get("canonical_gate_rows", 0) > 0),
        })
    _cdump(DIAG_OUT / "expansion_candidate_weeks.csv", rows_csv)

    # --- decision flags ---
    total_gate_rows = sum(i.get("canonical_gate_rows", 0) for i in phase1.values())
    weeks_with_rows = [w for w, i in phase1.items() if i.get("canonical_gate_rows", 0) > 0]

    p2_skipped = phase2.get("skipped", False)
    label_ok   = not p2_skipped and (phase2.get("labeled_rows") or 0) > 0

    flags = {
        "generated_at": ts,
        "sprint": "Sprint062 — OOS week expansion preflight + bounded label readout",
        "gate_frozen": "Sprint037G canonical: pool_age<=86400s, onset_max<=3600s, thresh>=10",
        "fee_tier_target": "fee_100bps (0.01%)",
        "thresholds_tuned": False,
        "training_allowed": False,
        "live_allowed": False,
        "rpc_touched": not p2_skipped and not phase2.get("skipped"),
        "rpc_calls_phase1": rpc_calls_p1,
        "rpc_calls_phase2": phase2.get("rpc_calls", 0),
        "phase1_total_canonical_rows": total_gate_rows,
        "phase1_weeks_with_rows": weeks_with_rows,
        "phase1_pass": len(weeks_with_rows) > 0,
        "phase2_run": not p2_skipped,
        "phase2_labeled_rows": phase2.get("labeled_rows", 0),
        "phase2_hit_rate": phase2.get("hit_rate"),
        "phase2_profit_factor": phase2.get("profit_factor"),
        "phase2_beats_s053_hit": phase2.get("beats_bench_hit"),
        "phase2_beats_s053_pf":  phase2.get("beats_bench_pf"),
        "s053_bench_hit": S053_BENCH["hit_rate"],
        "s053_bench_pf":  S053_BENCH["profit_factor"],
    }
    _jdump(AUDIT_OUT / "expansion_decision_flags.json", flags)

    # --- readout.md ---
    lines = [
        f"# Sprint062 — OOS Week Expansion Preflight + Bounded Label Readout",
        f"",
        f"**Generated:** {ts}",
        f"**Gate:** Sprint037G canonical (FROZEN) — pool_age≤86400s, onset_max≤3600s, thresh≥10, fee_100bps",
        f"**Thresholds tuned:** false",
        f"",
        f"---",
        f"",
        f"## Context",
        f"",
        f"Sprint053 scanned 13 OOS weeks (2025-09-28 → 2026-04-26). The final week",
        f"(2026-04-26→05-03) returned only 2,591 swaps because `raw_evm_logs` was stale",
        f"at scan time (max ~Apr-30). CH now holds 51,767 swaps for that full week.",
        f"Sprint062 rescans the complete week plus the current May-04 week.",
        f"",
        f"---",
        f"",
        f"## Phase 1 — Data Expansion Preflight",
        f"",
        f"| Week | Total Swaps | Pools | fee_100bps Pools | Canonical Gate Rows | Qual Pools | Top1 Pool Share |",
        f"| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows_csv:
        lines.append(
            f"| {r['regime_week']} | {r['total_swaps']:,} | {r['total_pools']} | "
            f"{r['fee100_pools']} | **{r['canonical_gate_rows']}** | "
            f"{r['qualified_pools']} | {r['top1_pool_share']:.1%} |"
        )

    lines += [
        f"",
        f"**Total canonical gate rows across expansion weeks:** {total_gate_rows}",
        f"**Weeks with gate_pass > 0:** {', '.join(weeks_with_rows) if weeks_with_rows else 'NONE'}",
        f"",
        f"---",
        f"",
    ]

    if total_gate_rows == 0:
        lines += [
            f"## Phase 1 Verdict: NO_EXPANSION_ROWS",
            f"",
            f"No fee_100bps rows pass the canonical gate in expansion weeks.",
            f"Possible causes:",
            f"- High-volume pools in Apr-27 week are older pools (age >86400s at swap time)",
            f"- Onset activity not ≥10 swaps/5m in first 60min",
            f"",
            f"**Phase 2 not run.** No candidates available.",
            f"",
            f"### Per-pool analysis available in:**",
            f"`mvp/diagnostics/sprint062/expansion_candidate_weeks.csv`",
        ]
    else:
        lines += [
            f"## Phase 1 Verdict: EXPANSION_ROWS_FOUND",
            f"",
            f"**{total_gate_rows}** fee_100bps canonical gate rows found in expansion weeks.",
        ]

    if not p2_skipped and label_ok:
        ph2 = phase2
        lines += [
            f"",
            f"---",
            f"",
            f"## Phase 2 — Bounded Label Readout",
            f"",
            f"| Metric | Sprint062 | Sprint053 bench | Beat bench? |",
            f"| --- | ---: | ---: | ---: |",
            f"| Rows labeled | {ph2.get('labeled_rows', 0)} | {S053_BENCH['rows']} | — |",
            f"| Hit rate | {ph2.get('hit_rate', 0):.1%} | {S053_BENCH['hit_rate']:.1%} | {'✓' if ph2.get('beats_bench_hit') else '✗'} |",
            f"| Profit factor | {ph2.get('profit_factor', 0):.3f} | {S053_BENCH['profit_factor']:.3f} | {'✓' if ph2.get('beats_bench_pf') else '✗'} |",
            f"| Median ret | {ph2.get('median_ret', 0)*100:.3f}% | — | — |",
            f"| RPC calls | {ph2.get('rpc_calls', 0)} | 1,087 | — |",
            f"",
        ]
    elif p2_skipped:
        lines += [
            f"",
            f"---",
            f"",
            f"## Phase 2 — Skipped (no RPC URL)",
            f"",
            f"Set `EVM_HTTP_URL` env var to run QuoterV2 label validation.",
            f"Expected budget: ≤{MAX_RPC} calls.",
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
        f"PHASE1_PASS                 = {str(len(weeks_with_rows) > 0).lower()}",
        f"PHASE1_CANONICAL_ROWS       = {total_gate_rows}",
        f"PHASE2_RUN                  = {str(not p2_skipped).lower()}",
        f"PHASE2_LABELED_ROWS         = {phase2.get('labeled_rows', 0)}",
        f"S053_BENCH_HIT              = {S053_BENCH['hit_rate']}",
        f"S053_BENCH_PF               = {S053_BENCH['profit_factor']}",
        f"```",
        f"",
        f"---",
        f"",
        f"*Sprint062 — OOS expansion, thresholds frozen, no live/training/RPC (Phase1)*",
    ]

    (AUDIT_OUT / "sprint062_readout.md").write_text("\n".join(lines))
    print(f"\n[sprint062] artifacts written to {AUDIT_OUT} and {DIAG_OUT}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("[sprint062] starting OOS expansion preflight", flush=True)
    print(f"[sprint062] gate: pool_age≤{GATE_A['pool_max_age_sec']}s, "
          f"onset≤{GATE_A['onset_max_sec']}s, thresh≥{GATE_A['onset_thresh']}", flush=True)
    print(f"[sprint062] expansion weeks: {[w[0] for w in EXPANSION_WEEKS]}", flush=True)

    # Load fee metadata
    pool_meta              = load_pool_meta()
    discovered_fee, dp_meta = load_discovered_pools()
    # Merge discovered_pools meta into pool_meta (for expansion week pools not in sprint053)
    for addr, info in dp_meta.items():
        if addr not in pool_meta:
            pool_meta[addr] = info
    print(f"[sprint062] pool_meta loaded: {len(pool_meta)} pools (sprint053 + discovered_pools)", flush=True)
    print(f"[sprint062] discovered_pools loaded: {len(discovered_fee)} V3 fee entries", flush=True)

    # Phase 1
    phase1 = phase1_gate_dryrun(pool_meta, discovered_fee)

    # Phase 2 (conditional)
    rpc_url    = load_rpc_url()
    total_rows = sum(i.get("canonical_gate_rows", 0) for i in phase1.values())

    if total_rows == 0:
        print("[sprint062] Phase 1: no rows → skip Phase 2", flush=True)
        phase2 = {"skipped": True, "reason": "no_candidates"}
    elif rpc_url is None:
        print("[sprint062] Phase 2: no RPC URL in env or .env → skip", flush=True)
        phase2 = {"skipped": True, "reason": "no_rpc_url"}
    else:
        phase2 = phase2_label_readout(phase1, pool_meta, rpc_url)

    # Write artifacts
    write_readout(phase1, phase2)

    # Save diagnostics
    for week, info in phase1.items():
        week_cands = info.pop("candidates", [])
        _cdump(DIAG_OUT / f"candidates_{week.replace('-', '')}.csv", week_cands)
        _jdump(DIAG_OUT / f"week_stats_{week.replace('-', '')}.json", info)

    if not phase2.get("skipped"):
        labeled = phase2.pop("labeled_candidates", [])
        _cdump(DIAG_OUT / "labeled_candidates.csv", labeled)
        _jdump(DIAG_OUT / "phase2_summary.json", phase2)

    # Print summary
    print("\n[sprint062] === SUMMARY ===", flush=True)
    for week, info in sorted(phase1.items()):
        print(f"  {week}: {info.get('canonical_gate_rows',0)} canonical rows "
              f"({info.get('fee100_pools',0)} fee100 pools)", flush=True)

    if not phase2.get("skipped") and (phase2.get("labeled_rows") or 0) > 0:
        print(f"\n[sprint062] Phase 2: {phase2.get('labeled_rows')} labeled, "
              f"hit={phase2.get('hit_rate')}, PF={phase2.get('profit_factor')}", flush=True)
        print(f"  vs Sprint053 bench: hit={S053_BENCH['hit_rate']}, PF={S053_BENCH['profit_factor']}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
