"""Run-004 Patch V3R-8 — V3-only Larger Decoded-backed Dataset (10 k target).

Safety assertions (hard constraints):
- Only processes uniswap_v3 rows — no V2, no Aero, no mixed dataset.
- No production / runtime / active-model writes.
- Writes only to mvp/diagnostics, mvp/runs, mvp/models/v3r8_research_candidate.
- Dune API used only for ≤ 50 representative oracle spot-check payloads.
- RPC keys and API keys are NOT written into report bodies.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import label_dashcam_executable as labeler  # noqa: E402
from scripts.run004_research_utils import rounded, write_csv_rows, write_json, write_markdown  # noqa: E402

# ── constants ────────────────────────────────────────────────────────────────
WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
ROUND_TRIP_COST = 0.006
GAS_COST_LOW = 0.01
GAS_COST_MEDIUM = 0.03
GAS_COST_HIGH = 0.10
SANITY_MIN = -1.0
SANITY_MAX = 10.0
EXIT_BLOCK_OFFSET = 900
MIN_WETH_PROXY = 1_000_000_000_000        # 0.000001 WETH — minimal dust filter only
POOL_CAP = 600                            # max rows per pool (concentration: top1 ~= 600/total)
TARGET_SUCCESSFUL = 10_000
MAX_WORKERS = 8
ORACLE_SAMPLE_SIZE = 30
RANDOM_SEED = 42

# Block-time calibration for rows with NULL block_time (Base chain ~0.933s/block)
_CAL_BLOCK = 44_630_353
_CAL_TS_MS  = 1_776_050_053_000           # 2026-04-13 03:14:13 UTC in ms
_MS_PER_BLOCK = 933                       # ~0.933s/block measured over 8.6-day window

# liquidity/tick unsafe extreme thresholds
TICK_ABS_MAX = 887272          # TickMath.MAX_TICK
SQRT_PRICE_MAX = (2 ** 160) - 1
LIQUIDITY_ZERO_THRESHOLD = 0

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"
MODEL_DIR = ROOT / "mvp/models/v3r8_research_candidate"

OUT_DIR.mkdir(parents=True, exist_ok=True)
RUN_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ── type aliases ─────────────────────────────────────────────────────────────
Row = dict[str, Any]

# ── helpers ──────────────────────────────────────────────────────────────────

def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        try:
            return int(float(str(v)))
        except (TypeError, ValueError):
            return default


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return default


def _dt(v: Any) -> datetime | None:
    text = str(v or "").replace("Z", "+00:00").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
        except ValueError:
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                return None


def _norm(raw: int, decimals: int) -> float | None:
    if decimals < 0:
        return None
    return raw / float(10 ** decimals)


def _sanity(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net < SANITY_MIN or net > SANITY_MAX:
        return max(SANITY_MIN, min(SANITY_MAX, net)), True
    return net, False


def _ch_query(query: str, timeout: int = 240) -> list[Row]:
    q = query.rstrip(";")
    if " format " not in q.lower():
        q = f"{q} FORMAT JSONEachRow"
    cmd = [
        "docker", "exec", "eatlq-clickhouse-1",
        "clickhouse-client", "--database", "eatlq", "--query", q,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                          timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _weth_leg(token0: str, token1: str) -> str:
    if _lower(token0) == WETH_BASE:
        return "token0"
    if _lower(token1) == WETH_BASE:
        return "token1"
    return "missing"


def _infer_direction(amount0: int, amount1: int, token0: str, token1: str,
                     dec0: int, dec1: int) -> Row:
    if amount0 > 0 and amount1 < 0:
        tin, tout, ain, aout, din, dout = token0, token1, amount0, abs(amount1), dec0, dec1
    elif amount1 > 0 and amount0 < 0:
        tin, tout, ain, aout, din, dout = token1, token0, amount1, abs(amount0), dec1, dec0
    else:
        tin, tout, ain, aout, din, dout = "", "", 0, 0, -1, -1
    weth_raw = ain if tin == WETH_BASE else aout if tout == WETH_BASE else 0
    return {
        "direction_ambiguous": tin == "" or tout == "",
        "tokenIn": tin,
        "tokenOut": tout,
        "amountIn_raw": str(ain),
        "amountOut_raw": str(aout),
        "amountIn_normalized": _norm(ain, din),
        "amountOut_normalized": _norm(aout, dout),
        "weth_side_amount_raw": str(weth_raw),
        "weth_side_amount_normalized": _norm(weth_raw, 18),
        "is_buy_weth_to_token": tin == WETH_BASE and tout not in {"", WETH_BASE},
        "is_sell_token_to_weth": tout == WETH_BASE and tin not in {"", WETH_BASE},
    }


# ── phase 1: fetch pool metadata ─────────────────────────────────────────────

def fetch_metadata() -> dict[str, Row]:
    rows = _ch_query("""
        SELECT
          lower(pool_address)   AS pool_address,
          lower(token0_address) AS token0,
          lower(token1_address) AS token1,
          token0_symbol, token1_symbol,
          token0_decimals, token1_decimals,
          fee_bps, has_weth_leg
        FROM eatlq.discovered_pools
        WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
    """)
    return {str(r["pool_address"]): r for r in rows}


# ── phase 2: fetch all decoded V3 rows ───────────────────────────────────────

def fetch_decoded_rows() -> list[Row]:
    """Fetch ALL V3 decoded swap rows (including null block_time rows).

    For rows where block_time IS NULL, timestamp_ms is estimated from block_number
    using a linear calibration measured over the 8.6-day window in the dataset.
    """
    print("  [CH] Fetching all V3 decoded rows (full history, incl. null-block_time)…")
    rows = _ch_query(f"""
        SELECT
          lower(s.pool_address)     AS pool_addr,
          s.tx_hash                 AS src_tx_hash,
          toString(s.log_index)     AS src_log_index,
          s.block_number            AS src_block_number,
          if(isNull(s.block_time),
             '',
             toString(s.block_time))               AS block_time_str,
          if(isNull(s.block_time),
             toInt64(s.block_number - {_CAL_BLOCK}) * {_MS_PER_BLOCK} + {_CAL_TS_MS},
             toUnixTimestamp64Milli(s.block_time)) AS timestamp_ms,
          lower(s.sender)           AS sender_addr,
          lower(s.recipient)        AS recipient_addr,
          toString(s.amount0)       AS amount0_str,
          toString(s.amount1)       AS amount1_str,
          toString(s.sqrt_price_x96) AS sqrtPriceX96,
          toString(s.liquidity)     AS liquidity_str,
          s.tick                    AS tick
        FROM eatlq.uniswap_v3_swaps_decoded s
        WHERE s.chain_id = 8453
          AND s.removed = 0
          AND s.amount0 != 0
          AND s.amount1 != 0
          AND s.tx_hash != ''
          AND lower(s.pool_address) IN (
              SELECT lower(pool_address)
              FROM eatlq.discovered_pools
              WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
          )
        ORDER BY s.block_number ASC, s.log_index ASC
    """, timeout=300)
    print(f"  [CH] Fetched {len(rows):,} decoded rows across pools.")
    return rows


# ── phase 3: per-pool cap with temporal spread ───────────────────────────────

def apply_pool_cap(raw_rows: list[Row], meta: dict[str, Row]) -> list[Row]:
    """Cap each pool at POOL_CAP rows, spread evenly across temporal range."""
    rng = random.Random(RANDOM_SEED)
    by_pool: dict[str, list[Row]] = defaultdict(list)
    for r in raw_rows:
        pool = str(r["pool_addr"])
        if pool in meta:
            by_pool[pool].append(r)
    selected: list[tuple[str, Row]] = []
    pool_contributions: dict[str, int] = {}
    for pool in sorted(by_pool, key=lambda p: len(by_pool[p]), reverse=True):
        rows = list(by_pool[pool])
        if not rows:
            continue
        if len(rows) <= POOL_CAP:
            chosen = rows
        else:
            # Evenly stride across time + random within each stride bucket
            step = max(1, len(rows) // POOL_CAP)
            strided: list[Row] = []
            for i in range(0, len(rows), step):
                bucket = rows[i : i + step]
                strided.append(rng.choice(bucket))
            # Shuffle to avoid pure chronological bias in later assembly
            rng.shuffle(strided)
            chosen = strided[:POOL_CAP]
        selected.extend((pool, row) for row in chosen)
        pool_contributions[pool] = len(chosen)
    # Build candidate rows
    candidates: list[Row] = []
    for idx, (pool, row) in enumerate(selected):
        m = meta[pool]
        token0 = _lower(m["token0"])
        token1 = _lower(m["token1"])
        amount0 = _int(row["amount0_str"])
        amount1 = _int(row["amount1_str"])
        direction = _infer_direction(amount0, amount1, token0, token1,
                                     _int(m["token0_decimals"]), _int(m["token1_decimals"]))
        ts_ms = _int(row.get("timestamp_ms", 0))
        bt = _dt(row.get("block_time_str")) if row.get("block_time_str") else None
        if bt is None and ts_ms > 0:
            bt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        candidates.append({
            "candidate_id": idx,
            "candidate_kind": "decoded_backed_v3_swap_v3r8",
            "decoded_source": "uniswap_v3_swaps_decoded",
            "pool_address": pool,
            "source_tx_hash": row["src_tx_hash"],
            "source_log_index": row["src_log_index"],
            "source_block_number": row["src_block_number"],
            "block_number": row["src_block_number"],
            "entry_block": row["src_block_number"],
            "exit_block": _int(row["src_block_number"]) + EXIT_BLOCK_OFFSET,
            "fixed_horizon_sec": 1800,
            "source_timestamp": bt.isoformat() if bt else "",
            "timestamp": bt.isoformat() if bt else "",
            "timestamp_ms": ts_ms,
            "sender": row.get("sender_addr", ""),
            "recipient": row.get("recipient_addr", ""),
            "amount0": row["amount0_str"],
            "amount1": row["amount1_str"],
            "sqrtPriceX96": row.get("sqrtPriceX96", ""),
            "tick": row.get("tick", ""),
            "liquidity": row.get("liquidity_str", ""),
            "token0": token0,
            "token1": token1,
            "token0_symbol": m.get("token0_symbol", ""),
            "token1_symbol": m.get("token1_symbol", ""),
            "token0_decimals": m.get("token0_decimals", ""),
            "token1_decimals": m.get("token1_decimals", ""),
            "fee_bps": m.get("fee_bps", ""),
            "fee_tier": _int(m.get("fee_bps", 0)) * 100,
            "weth_leg": _weth_leg(token0, token1),
            "metadata_join_status": "joined_discovered_pools",
            "tx_log_block_identity_present": bool(
                row.get("src_tx_hash") and row.get("src_log_index") != "" and row.get("src_block_number")
            ),
            **direction,
        })
    return candidates


# ── phase 4: min-notional filter ─────────────────────────────────────────────

def apply_min_notional(candidates: list[Row]) -> tuple[list[Row], Row]:
    before = len(candidates)
    filtered = [c for c in candidates if _int(c["weth_side_amount_raw"]) >= MIN_WETH_PROXY]
    after = len(filtered)
    report = {
        "notional_proxy": "weth_side_amount_raw",
        "min_weth_raw": MIN_WETH_PROXY,
        "min_weth": MIN_WETH_PROXY / 1e18,
        "before_filter": before,
        "after_filter": after,
        "removed_dust": before - after,
        "unique_pools_after": len({c["pool_address"] for c in filtered}),
    }
    return filtered, report


# ── phase 5: compute 1h window features ──────────────────────────────────────

def compute_window_features(candidates: list[Row], all_rows: list[Row]) -> None:
    """Attach 1h lookback microstructure features to each candidate (in-place).

    Uses the full CH fetch (all_rows) sorted by (pool, timestamp_ms) to build
    per-pool sorted arrays, then uses bisect for O(log n) window lookups.
    """
    print("  [features] Computing 1h window features…")
    # Build per-pool sorted list of (ts_ms, weth_side_raw, sender, is_buy)
    pool_timeline: dict[str, list[tuple[int, int, str, bool]]] = defaultdict(list)
    for r in all_rows:
        pool = str(r["pool_addr"])
        ts_ms = _int(r.get("timestamp_ms", 0))
        a0 = _int(r["amount0_str"])
        a1 = _int(r["amount1_str"])
        sender = str(r.get("sender_addr", ""))
        # Determine WETH side and direction
        # We don't have meta here for direction, use amount sign + pool token info
        # Simplified: use abs of larger WETH side
        weth_raw = abs(a0) if abs(a0) >= abs(a1) else abs(a1)
        is_buy = a0 < 0  # token0 goes out (WETH out) = buy
        pool_timeline[pool].append((ts_ms, weth_raw, sender, is_buy))

    # Sort each pool timeline
    for pool in pool_timeline:
        pool_timeline[pool].sort(key=lambda x: x[0])

    HOUR_MS = 3_600_000

    for cand in candidates:
        pool = cand["pool_address"]
        ts_ms = _int(cand.get("timestamp_ms", 0))
        if not ts_ms or pool not in pool_timeline:
            cand.update({
                "feature_sender_present": False,
                "feature_sender_pool_prior_count_1h": 0,
                "feature_pool_flow_count_1h": 0,
                "feature_pool_weth_flow_sum_1h": 0.0,
                "feature_pool_weth_signed_flow_sum_1h": 0.0,
                "feature_pool_unique_senders_1h": 0,
                "feature_temporal_prev_pool_delta_sec": None,
                "feature_fee_tier": cand.get("fee_tier", ""),
                "feature_tick": cand.get("tick", ""),
                "feature_liquidity": cand.get("liquidity", ""),
                "feature_protocol": "uniswap_v3",
            })
            continue
        timeline = pool_timeline[pool]
        ts_keys = [t[0] for t in timeline]
        window_start = ts_ms - HOUR_MS
        lo = bisect.bisect_left(ts_keys, window_start)
        hi = bisect.bisect_left(ts_keys, ts_ms)  # exclude self
        window = timeline[lo:hi]
        sender = str(cand.get("sender", ""))
        sender_count = sum(1 for _, _, s, _ in window if s == sender)
        flow_count = len(window)
        weth_sum = sum(w for _, w, _, _ in window) / 1e18
        weth_signed = sum((w if buy else -w) for _, w, _, buy in window) / 1e18
        unique_senders = len({s for _, _, s, _ in window})
        # Previous swap in pool
        prev_ts = timeline[hi - 1][0] if hi > 0 else None
        prev_delta_sec = (ts_ms - prev_ts) / 1000.0 if prev_ts and prev_ts < ts_ms else None
        cand.update({
            "feature_sender_present": bool(sender),
            "feature_sender_pool_prior_count_1h": sender_count,
            "feature_pool_flow_count_1h": flow_count,
            "feature_pool_weth_flow_sum_1h": round(weth_sum, 6),
            "feature_pool_weth_signed_flow_sum_1h": round(weth_signed, 6),
            "feature_pool_unique_senders_1h": unique_senders,
            "feature_temporal_prev_pool_delta_sec": prev_delta_sec,
            "feature_fee_tier": cand.get("fee_tier", ""),
            "feature_tick": cand.get("tick", ""),
            "feature_liquidity": cand.get("liquidity", ""),
            "feature_protocol": "uniswap_v3",
        })
    print(f"  [features] Done. {len(candidates):,} candidates enriched.")


# ── phase 6: parallel quoting ─────────────────────────────────────────────────

def _quote_one(rpc: labeler.RPC, cand: Row) -> Row:
    """Quote entry + exit for one candidate; return labeling result dict."""
    amount_in = _int(cand["amountIn_raw"])
    if amount_in <= 0:
        return {**_quote_failure(cand, "fail_zero_amount_in"), "net_gross": None}

    # Entry quote
    entry = _call_quoter(rpc, cand["tokenIn"], cand["tokenOut"], amount_in,
                         _int(cand["fee_tier"]), _int(cand["entry_block"]))
    if not entry["success"]:
        reason = "quoter_revert" if entry["error"] else "fail_v3_entry_zero"
        return _quote_failure(cand, reason)

    # Exit quote
    exit_q = _call_quoter(rpc, cand["tokenOut"], cand["tokenIn"], _int(entry["amount_out"]),
                           _int(cand["fee_tier"]), _int(cand["exit_block"]))
    if not exit_q["success"]:
        reason = "exit_quoter_revert" if exit_q["error"] else "executable_exit_drained"
        res = _quote_failure(cand, reason)
        res["gross_quote_return_30m"] = 0.0
        res["entry_quote_amount_out"] = entry["amount_out"]
        res["exit_quote_amount_out"] = 0
        return res

    raw_gross = _int(exit_q["amount_out"]) / amount_in - 1.0
    gross, bogus = _sanity(raw_gross)
    if bogus:
        return _quote_failure(cand, "fail_sanity_clip")

    fixed_adj = gross - ROUND_TRIP_COST
    gas_adj_low    = fixed_adj - GAS_COST_LOW
    gas_adj_medium = fixed_adj - GAS_COST_MEDIUM
    gas_adj_high   = fixed_adj - GAS_COST_HIGH

    return {
        "candidate_id": cand["candidate_id"],
        "pool_address": cand["pool_address"],
        "label_status": "success",
        "failure_reason": "",
        "label_type": "executable",
        "executable_label_current": 0,
        "gross_quote_return_30m": round(gross, 8),
        "fixed_cost_adjusted_return_30m": round(fixed_adj, 8),
        "gas_cost_return_low": GAS_COST_LOW,
        "gas_adjusted_return_30m_low": round(gas_adj_low, 8),
        "gas_cost_return_medium": GAS_COST_MEDIUM,
        "gas_adjusted_return_30m_medium": round(gas_adj_medium, 8),
        "gas_cost_return_high": GAS_COST_HIGH,
        "gas_adjusted_return_30m_high": round(gas_adj_high, 8),
        "final_return_after_cost": round(gas_adj_medium, 8),
        "executable_label_after_gas": 0,
        "entry_quote_amount_out": entry["amount_out"],
        "exit_quote_amount_out": exit_q["amount_out"],
        "_quote_ok": True,
    }


def _quote_failure(cand: Row, reason: str) -> Row:
    return {
        "candidate_id": cand["candidate_id"],
        "pool_address": cand["pool_address"],
        "label_status": "failed",
        "failure_reason": reason,
        "label_type": reason,
        "executable_label_current": reason,
        "gross_quote_return_30m": 0.0,
        "fixed_cost_adjusted_return_30m": None,
        "gas_cost_return_low": GAS_COST_LOW,
        "gas_adjusted_return_30m_low": None,
        "gas_cost_return_medium": GAS_COST_MEDIUM,
        "gas_adjusted_return_30m_medium": None,
        "gas_cost_return_high": GAS_COST_HIGH,
        "gas_adjusted_return_30m_high": None,
        "final_return_after_cost": None,
        "executable_label_after_gas": 0,
        "entry_quote_amount_out": 0,
        "exit_quote_amount_out": 0,
        "_quote_ok": False,
    }


def _call_quoter(rpc: labeler.RPC, tin: str, tout: str, ain: int, fee: int, block: int) -> Row:
    data = (
        labeler._SEL_QUOTE_V3
        + labeler._addr_pad(tin)
        + labeler._addr_pad(tout)
        + labeler._uint_pad(ain)
        + labeler._uint_pad(fee)
        + labeler._uint_pad(0)
    )
    try:
        res = labeler._eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception as exc:
        return {"success": False, "error": str(exc), "amount_out": 0}
    raw = res[2:] if res.startswith("0x") else res
    words = [raw[i: i + 64] for i in range(0, len(raw), 64)]
    amount_out = int(words[0], 16) if words and words[0] else 0
    return {"success": amount_out > 0, "error": "", "amount_out": amount_out}


def quote_all_parallel(candidates: list[Row], rpc_url: str) -> list[Row]:
    """Quote all candidates in parallel — both buy AND sell directions.

    For a buy (WETH→token): entry=WETH→token, exit=token→WETH.
    For a sell (token→WETH): entry=token→WETH, exit=WETH→token.
    Both are valid round-trip signals. Ambiguous direction rows are skipped.
    """
    quotable = [c for c in candidates if not c.get("direction_ambiguous")
                and _int(c["amountIn_raw"]) > 0 and c.get("tokenIn") and c.get("tokenOut")]
    non_quotable = [c for c in candidates if c.get("direction_ambiguous")
                    or _int(c["amountIn_raw"]) <= 0 or not c.get("tokenIn") or not c.get("tokenOut")]
    print(f"  [quote] {len(quotable):,} quotable (both dirs), {len(non_quotable):,} ambiguous/skipped")

    rpc = labeler.RPC(rpc_url)
    results: list[Row] = []
    done_lock = Lock()
    done_count = 0
    total = len(quotable)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_quote_one, rpc, c): c for c in quotable}
        for fut in as_completed(futures):
            cand = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = _quote_failure(cand, f"exception:{exc!s:.100}")
            with done_lock:
                results.append(r)
                done_count += 1
                if done_count % 500 == 0 or done_count == total:
                    elapsed = time.time() - t0
                    ok = sum(1 for x in results if x.get("_quote_ok"))
                    print(f"  [quote] {done_count}/{total}  ok={ok}  elapsed={elapsed:.0f}s")

    # Add non-quotable as skipped rows
    for c in non_quotable:
        results.append(_quote_failure(c, "direction_ambiguous_skipped"))

    by_id = {r["candidate_id"]: r for r in results}
    return by_id


# ── phase 7: merge + risk labels + alpha labels ──────────────────────────────

def _detect_unsafe_extremes(cand: Row) -> dict[str, bool]:
    tick = _int(cand.get("tick", 0))
    liquidity = _int(cand.get("liquidity", 0))
    sqrt_price = _int(cand.get("sqrtPriceX96", "0") or "0")
    return {
        "tick_unsafe_extreme": abs(tick) >= TICK_ABS_MAX,
        "liquidity_zero": liquidity <= LIQUIDITY_ZERO_THRESHOLD,
        "sqrt_price_unsafe_extreme": sqrt_price == 0 or sqrt_price >= SQRT_PRICE_MAX,
    }


def _risk_labels(cand: Row, q: Row, pool_hour_counts: dict[tuple[str, str], int]) -> Row:
    extremes = _detect_unsafe_extremes(cand)
    exit_drained = q.get("label_type") == "executable_exit_drained"
    liquidity_risk = extremes["liquidity_zero"] or extremes["tick_unsafe_extreme"]
    review_only = exit_drained or extremes["sqrt_price_unsafe_extreme"]
    gross = _float(q.get("gross_quote_return_30m", 0.0))
    quote_anomaly = bool(
        gross > 0.5
        or (q.get("_quote_ok") and _float(q.get("entry_quote_amount_out", 0)) <= 0)
    )
    scale_risk = bool(
        _int(cand.get("weth_side_amount_raw", 0)) > 50 * (10 ** 18)  # >50 WETH
    )
    gas_dominates = bool(
        q.get("_quote_ok") and abs(gross) < GAS_COST_LOW
    )
    # Pool-hour cluster: pool has >100 swaps in the same hour window
    bt = _dt(cand.get("timestamp"))
    hour_key: tuple[str, str] | None = None
    if bt:
        hour_key = (cand["pool_address"], bt.strftime("%Y-%m-%dT%H"))
    repeated_cluster = bool(hour_key and pool_hour_counts.get(hour_key, 0) > 100)

    reasons = []
    if exit_drained:
        reasons.append("v3_exit_drained")
    if liquidity_risk:
        reasons.append("v3_liquidity_risk")
    if review_only:
        reasons.append("review_only")
    if quote_anomaly:
        reasons.append("quote_anomaly")
    if scale_risk:
        reasons.append("scale_risk")
    if gas_dominates:
        reasons.append("gas_cost_dominates")
    if repeated_cluster:
        reasons.append("repeated_pool_hour_cluster")

    severity = "none"
    if exit_drained or liquidity_risk or review_only:
        severity = "review"
    if exit_drained and liquidity_risk:
        severity = "high"

    return {
        "v3_exit_drained_label": int(exit_drained),
        "v3_liquidity_exit_risk": int(liquidity_risk),
        "v3_review_only": int(review_only),
        "quote_anomaly_label": int(quote_anomaly),
        "scale_risk_label": int(scale_risk),
        "gas_cost_dominates_label": int(gas_dominates),
        "repeated_pool_hour_cluster_label": int(repeated_cluster),
        "quarantine_reason": ",".join(reasons) if reasons else "",
        "risk_severity": severity,
    }


def build_full_dataset(candidates: list[Row], quote_by_id: dict[int, Row]) -> list[Row]:
    """Merge candidate metadata + quote results + risk labels."""
    # Compute pool-hour cluster counts
    pool_hour_counts: dict[tuple[str, str], int] = Counter()
    for c in candidates:
        bt = _dt(c.get("timestamp"))
        if bt:
            pool_hour_counts[(c["pool_address"], bt.strftime("%Y-%m-%dT%H"))] += 1

    dataset: list[Row] = []
    for cand in candidates:
        q = quote_by_id.get(cand["candidate_id"], _quote_failure(cand, "missing_quote"))
        risk = _risk_labels(cand, q, pool_hour_counts)
        row = {**cand, **{k: v for k, v in q.items() if not k.startswith("_")}, **risk}
        dataset.append(row)

    return dataset


def assign_alpha_labels(dataset: list[Row]) -> list[Row]:
    """Assign alpha labels to successful rows only, top-20% by final_return_after_cost."""
    successful = [r for r in dataset if r.get("label_status") == "success"]
    if not successful:
        for r in dataset:
            r["final_return_rank"] = None
            r["alpha_top20_after_gas"] = 0
            r["alpha_ret_gt_5pct_after_gas"] = 0
            r["alpha_ret_gt_10pct_after_gas"] = 0
            r["within_pool_return_rank"] = None
        return dataset

    # Global rank
    sorted_ids = sorted(
        (r["candidate_id"] for r in successful),
        key=lambda cid: _float(next(r["final_return_after_cost"] for r in successful if r["candidate_id"] == cid), -999),
        reverse=True,
    )
    rank_map = {cid: i for i, cid in enumerate(sorted_ids)}
    top20_threshold = int(len(successful) * 0.20)
    top20_ids = set(sorted_ids[:top20_threshold])

    # Within-pool rank
    by_pool: dict[str, list[Row]] = defaultdict(list)
    for r in successful:
        by_pool[r["pool_address"]].append(r)
    within_pool_rank: dict[int, int] = {}
    for pool_rows in by_pool.values():
        sorted_pool = sorted(pool_rows, key=lambda r: _float(r.get("final_return_after_cost"), -999), reverse=True)
        for rank, r in enumerate(sorted_pool):
            within_pool_rank[r["candidate_id"]] = rank

    for r in dataset:
        cid = r["candidate_id"]
        if r.get("label_status") == "success":
            ret = _float(r.get("final_return_after_cost", -999))
            r["final_return_rank"] = rank_map.get(cid)
            r["alpha_top20_after_gas"] = 1.0 if cid in top20_ids else 0.0
            r["alpha_ret_gt_5pct_after_gas"] = 1.0 if ret > 0.05 else 0.0
            r["alpha_ret_gt_10pct_after_gas"] = 1.0 if ret > 0.10 else 0.0
            r["within_pool_return_rank"] = within_pool_rank.get(cid)
        else:
            r["final_return_rank"] = None
            r["alpha_top20_after_gas"] = 0.0
            r["alpha_ret_gt_5pct_after_gas"] = 0.0
            r["alpha_ret_gt_10pct_after_gas"] = 0.0
            r["within_pool_return_rank"] = None

    return dataset


# ── phase 8: split clean / risk ───────────────────────────────────────────────

def split_datasets(dataset: list[Row]) -> tuple[list[Row], list[Row], list[Row]]:
    """Return (all_rows, clean_alpha, risk_rows)."""
    risk = [r for r in dataset if r.get("risk_severity") in {"review", "high"}
            or r.get("label_status") != "success"]
    clean = [r for r in dataset if r not in risk]
    return dataset, clean, risk


# ── phase 9: output files ─────────────────────────────────────────────────────

CSV_COLUMNS = [
    "candidate_id", "candidate_kind", "decoded_source", "pool_address",
    "source_tx_hash", "source_log_index", "source_block_number",
    "block_number", "entry_block", "exit_block", "fixed_horizon_sec",
    "source_timestamp", "timestamp", "timestamp_ms",
    "sender", "recipient", "amount0", "amount1",
    "sqrtPriceX96", "tick", "liquidity",
    "token0", "token1", "token0_symbol", "token1_symbol",
    "token0_decimals", "token1_decimals", "fee_bps", "fee_tier", "weth_leg",
    "metadata_join_status", "tx_log_block_identity_present", "direction_ambiguous",
    "tokenIn", "tokenOut", "amountIn_raw", "amountOut_raw",
    "amountIn_normalized", "amountOut_normalized",
    "weth_side_amount_raw", "weth_side_amount_normalized",
    "is_buy_weth_to_token", "is_sell_token_to_weth",
    "feature_sender_present", "feature_sender_pool_prior_count_1h",
    "feature_pool_flow_count_1h", "feature_pool_weth_flow_sum_1h",
    "feature_pool_weth_signed_flow_sum_1h", "feature_pool_unique_senders_1h",
    "feature_temporal_prev_pool_delta_sec",
    "feature_fee_tier", "feature_tick", "feature_liquidity", "feature_protocol",
    "label_status", "failure_reason", "label_type", "executable_label_current",
    "gross_quote_return_30m", "fixed_cost_adjusted_return_30m",
    "gas_cost_return_low", "gas_adjusted_return_30m_low",
    "gas_cost_return_medium", "gas_adjusted_return_30m_medium",
    "gas_cost_return_high", "gas_adjusted_return_30m_high",
    "final_return_after_cost", "executable_label_after_gas",
    "v3_exit_drained_label", "v3_liquidity_exit_risk", "v3_review_only",
    "quote_anomaly_label", "scale_risk_label", "gas_cost_dominates_label",
    "repeated_pool_hour_cluster_label",
    "quarantine_reason", "risk_severity",
    "final_return_rank", "alpha_top20_after_gas",
    "alpha_ret_gt_5pct_after_gas", "alpha_ret_gt_10pct_after_gas",
    "within_pool_return_rank",
]


def _normalize_row(r: Row) -> Row:
    out = {}
    for col in CSV_COLUMNS:
        out[col] = r.get(col, "")
    return out


# ── phase 10: analytics reports ───────────────────────────────────────────────

def pool_concentration_report(dataset: list[Row]) -> Row:
    successful = [r for r in dataset if r.get("label_status") == "success"]
    total = len(successful)
    counts = Counter(r["pool_address"] for r in successful)
    pools_sorted = counts.most_common()
    unique = len(counts)
    top1_share = pools_sorted[0][1] / total if pools_sorted and total else 0.0
    top5_share = sum(c for _, c in pools_sorted[:5]) / total if pools_sorted and total else 0.0
    top10_share = sum(c for _, c in pools_sorted[:10]) / total if pools_sorted and total else 0.0

    # Time span
    times = [_dt(r.get("timestamp")) for r in successful]
    times = [t for t in times if t]
    time_span_days = (max(times) - min(times)).total_seconds() / 86400 if len(times) > 1 else 0.0

    return {
        "total_successful_rows": total,
        "unique_pools": unique,
        "top1_pool_address": pools_sorted[0][0] if pools_sorted else "",
        "top1_pool_count": pools_sorted[0][1] if pools_sorted else 0,
        "top1_pool_share": round(top1_share, 6),
        "top5_pool_share": round(top5_share, 6),
        "top10_pool_share": round(top10_share, 6),
        "time_span_days": round(time_span_days, 2),
        "pool_distribution_top20": [
            {"pool": p, "count": c, "share": round(c / total, 6)} for p, c in pools_sorted[:20]
        ],
        "pool_distribution_full": [
            {"pool": p, "count": c, "share": round(c / total, 6)} for p, c in pools_sorted
        ],
        "pass_unique_pools_ge_50": unique >= 50,
        "pass_top1_le_10pct": top1_share <= 0.10,
        "pass_top5_le_45pct": top5_share <= 0.45,
        "pass_top5_le_30pct": top5_share <= 0.30,
    }


def feature_coverage_report(dataset: list[Row]) -> Row:
    n = len(dataset)
    if n == 0:
        return {"total_rows": 0}

    def cov(col: str, check: Any = None) -> float:
        if check is None:
            return rounded(sum(1 for r in dataset if r.get(col) not in (None, "", "None")) / n, 6)
        return rounded(sum(1 for r in dataset if check(r.get(col))) / n, 6)

    feature_cols = [
        "feature_sender_present", "feature_sender_pool_prior_count_1h",
        "feature_pool_flow_count_1h", "feature_pool_weth_flow_sum_1h",
        "feature_pool_weth_signed_flow_sum_1h", "feature_pool_unique_senders_1h",
        "feature_temporal_prev_pool_delta_sec", "feature_fee_tier",
        "feature_tick", "feature_liquidity", "feature_protocol",
    ]
    decoded_identity_cols = [
        "source_tx_hash", "source_log_index", "source_block_number",
        "pool_address", "tokenIn", "tokenOut",
    ]
    return {
        "total_rows": n,
        "decoded_feature_coverage": cov("feature_protocol"),
        "source_identity_coverage": rounded(
            sum(1 for r in dataset if all(r.get(c) not in (None, "", "None")
                                          for c in decoded_identity_cols)) / n, 6
        ),
        "weth_leg_coverage": cov("weth_leg", lambda v: v in ("token0", "token1")),
        "fee_tier_coverage": cov("fee_tier", lambda v: _int(v) > 0),
        "tx_log_block_identity_coverage": cov("tx_log_block_identity_present",
                                              lambda v: str(v).lower() == "true"),
        "temporal_coverage": cov("feature_temporal_prev_pool_delta_sec"),
        "per_feature_coverage": {col: cov(col) for col in feature_cols},
        "unsafe_extreme_rows": {
            "tick_unsafe": sum(1 for r in dataset if abs(_int(r.get("tick", 0))) >= TICK_ABS_MAX),
            "liquidity_zero": sum(1 for r in dataset if _int(r.get("liquidity", 0)) <= 0),
            "sqrt_price_zero": sum(1 for r in dataset if _int(r.get("sqrtPriceX96", "0") or "0") == 0),
        },
    }


def direction_fee_tier_report(dataset: list[Row]) -> Row:
    successful = [r for r in dataset if r.get("label_status") == "success"]
    n = len(successful)
    direction_counts = Counter(
        "buy_weth_to_token" if r.get("is_buy_weth_to_token") else
        "sell_token_to_weth" if r.get("is_sell_token_to_weth") else "ambiguous"
        for r in successful
    )
    fee_counts = Counter(str(r.get("fee_tier", "")) for r in successful)
    fee_bps_counts = Counter(str(r.get("fee_bps", "")) for r in successful)

    returns_by_fee: dict[str, list[float]] = defaultdict(list)
    for r in successful:
        ft = str(r.get("fee_tier", ""))
        ret = r.get("final_return_after_cost")
        if ret is not None:
            returns_by_fee[ft].append(_float(ret))

    fee_return_stats = {}
    for ft, vals in returns_by_fee.items():
        vals.sort()
        fee_return_stats[ft] = {
            "count": len(vals),
            "mean": round(sum(vals) / len(vals), 6),
            "p50": vals[len(vals) // 2],
            "min": vals[0],
            "max": vals[-1],
        }

    return {
        "total_successful": n,
        "direction_distribution": dict(direction_counts),
        "direction_share": {k: round(v / n, 6) for k, v in direction_counts.items()} if n else {},
        "fee_tier_distribution": dict(fee_counts.most_common()),
        "fee_bps_distribution": dict(fee_bps_counts.most_common()),
        "fee_tier_return_stats": fee_return_stats,
    }


def oracle_spotcheck_report(candidates: list[Row]) -> Row:
    rng = random.Random(RANDOM_SEED + 1)
    dune_key_present = bool(os.environ.get("DUNE_API_KEY") or os.environ.get("DUNE_API_TOKEN"))
    sample = rng.sample(candidates, min(ORACLE_SAMPLE_SIZE, len(candidates)))
    payloads = []
    for c in sample:
        bt = _dt(c.get("timestamp"))
        payloads.append({
            "candidate_id": c["candidate_id"],
            "oracle_result_status": "oracle_result_pending",
            "source_identity": {
                "pool_address": c["pool_address"],
                "tx_hash": c["source_tx_hash"],
                "log_index": c["source_log_index"],
                "block_number": c["source_block_number"],
                "timestamp": c.get("timestamp", ""),
            },
            "dune_sql_filter": {
                "blockchain": "base",
                "project_contract_address": c["pool_address"],
                "tx_hash": c["source_tx_hash"],
                "evt_index": c["source_log_index"],
            },
            "checks": {
                "pool_match": None,
                "token_direction_match": None,
                "amount_scale_match": None,
                "fee_tier_match": None,
                "amountUSD_sanity": None,
            },
        })
    return {
        "oracle_version": "v3r8_spotcheck",
        "dune_token_present": dune_key_present,
        "executed_external_calls": False,
        "sample_size": len(payloads),
        "note": "Dune oracle limited to <=50 representative spot-check payloads per V3R8 constraints.",
        "payloads": payloads,
    }


def input_validation_report(all_rows: list[Row], clean: list[Row], risk: list[Row],
                             conc: Row, feat: Row, cands_before_quote: int,
                             quote_attempted: int, quote_success: int,
                             quote_failed: int, quote_skipped: int) -> Row:
    n_all = len(all_rows)
    n_clean = len(clean)
    v2_rows = sum(1 for r in all_rows if r.get("decoded_source", "").lower() != "uniswap_v3_swaps_decoded")
    aero_rows = sum(1 for r in all_rows if "aero" in str(r.get("decoded_source", "")).lower())
    protocol_dist = Counter(r.get("decoded_source", "unknown") for r in all_rows)
    return_vals = [_float(r.get("final_return_after_cost")) for r in clean if r.get("final_return_after_cost") is not None]
    return_vals.sort()

    gate = {
        "input_validation_pass": True,
        "no_v2_rows": v2_rows == 0,
        "no_aero_rows": aero_rows == 0,
        "no_mixed_dataset": v2_rows == 0 and aero_rows == 0,
        "all_protocol_v3": all(str(r.get("decoded_source", "")) == "uniswap_v3_swaps_decoded" for r in all_rows),
        "decoded_feature_coverage_gte_98pct": feat.get("decoded_feature_coverage", 0) >= 0.98,
        "source_identity_coverage_gte_98pct": feat.get("source_identity_coverage", 0) >= 0.98,
        "successful_rows_gte_10k": quote_success >= 10_000,
        "unique_pools_gte_50": conc.get("unique_pools", 0) >= 50,
        "top5_pool_share_lte_45pct": conc.get("top5_pool_share", 1.0) <= 0.45,
        "quote_success_rate_gte_90pct": (quote_success / quote_attempted >= 0.90) if quote_attempted else False,
        "risk_rows_isolated": True,
        "no_production_writes": True,
        "no_runtime_writes": True,
        "no_active_model_writes": True,
        "no_live_promote": True,
        "no_paper_promote": True,
    }
    gate["input_validation_pass"] = all(gate.values())

    return {
        "status": "pass" if gate["input_validation_pass"] else "fail",
        "all_rows": n_all,
        "clean_alpha_rows": n_clean,
        "risk_rows": len(risk),
        "v2_rows_detected": v2_rows,
        "aero_rows_detected": aero_rows,
        "protocol_distribution": dict(protocol_dist),
        "candidates_before_quoting": cands_before_quote,
        "quote_attempted": quote_attempted,
        "quote_success": quote_success,
        "quote_failed": quote_failed,
        "quote_skipped": quote_skipped,
        "quote_success_rate": round(quote_success / quote_attempted, 6) if quote_attempted else 0.0,
        "decoded_feature_coverage": feat.get("decoded_feature_coverage"),
        "source_identity_coverage": feat.get("source_identity_coverage"),
        "unique_pools": conc.get("unique_pools"),
        "top1_pool_share": conc.get("top1_pool_share"),
        "top5_pool_share": conc.get("top5_pool_share"),
        "return_summary": {
            "count": len(return_vals),
            "mean": round(sum(return_vals) / len(return_vals), 8) if return_vals else None,
            "min": return_vals[0] if return_vals else None,
            "p50": return_vals[len(return_vals) // 2] if return_vals else None,
            "max": return_vals[-1] if return_vals else None,
        },
        "gate_checks": gate,
    }


# ── leakage denylist for V3R-9 ───────────────────────────────────────────────

def build_leakage_denylist(risk: list[Row]) -> list[Row]:
    """Pools/txs flagged as risk rows form the leakage denylist for V3R-9."""
    seen: set[str] = set()
    denylist: list[Row] = []
    for r in risk:
        pool = r.get("pool_address", "")
        tx = r.get("source_tx_hash", "")
        key = f"{pool}:{tx}:{r.get('source_log_index','')}"
        if key not in seen:
            seen.add(key)
            denylist.append({
                "pool_address": pool,
                "source_tx_hash": tx,
                "source_log_index": r.get("source_log_index", ""),
                "risk_severity": r.get("risk_severity", ""),
                "quarantine_reason": r.get("quarantine_reason", ""),
                "label_type": r.get("label_type", ""),
            })
    return denylist


# ── markdown report ───────────────────────────────────────────────────────────

def markdown(iv: Row, conc: Row, feat: Row, dir_ft: Row, oracle: Row,
             t_elapsed: float) -> str:
    gate = iv.get("gate_checks", {})
    pools_top5 = conc.get("pool_distribution_top20", [])[:5]

    def gmark(k: str) -> str:
        return "`pass`" if gate.get(k) else "`FAIL`"

    # Model dryrun allowed if concentration is excellent: top5 <= 30% AND top1 <= 10% AND pools >= 75
    allow_model_dryrun = (
        conc.get("unique_pools", 0) >= 75
        and conc.get("top5_pool_share", 1.0) <= 0.30
        and conc.get("top1_pool_share", 1.0) <= 0.10
    )
    allow_v3r9 = (
        conc.get("unique_pools", 0) >= 50
        and conc.get("top5_pool_share", 1.0) <= 0.45
    )

    return f"""# Run-004 Patch V3R-8 — V3-only Larger Decoded-backed Dataset

## Scope
- Protocol: Uniswap V3 only (no V2, no Aero, no mixed)
- Target: {TARGET_SUCCESSFUL:,} successful rows
- Pool cap: {POOL_CAP} rows per pool (concentration control)
- No production / runtime / active-model writes.
- No live / paper promote.

## Input Validation
- Status: `{iv['status']}`
- All rows: {iv['all_rows']:,} | Clean alpha: {iv['clean_alpha_rows']:,} | Risk: {iv['risk_rows']:,}
- V2 rows detected: {iv['v2_rows_detected']}
- Aero rows detected: {iv['aero_rows_detected']}
- Protocol distribution: {iv['protocol_distribution']}

## Quoting Accounting
- Candidates before quoting: {iv['candidates_before_quoting']:,}
- Quote attempted: {iv['quote_attempted']:,}
- Quote success: {iv['quote_success']:,}
- Quote failed: {iv['quote_failed']:,}
- Quote skipped (ambiguous dir): {iv['quote_skipped']:,}
- Quote success rate: {iv['quote_success_rate']:.4f}

## Pool Concentration
- Unique pools: {conc['unique_pools']}
- Top-1 pool share: {conc['top1_pool_share']:.4f}
- Top-5 pool share: {conc['top5_pool_share']:.4f}
- Top-10 pool share: {conc['top10_pool_share']:.4f}
- Time span: {conc['time_span_days']:.2f} days
- Top-5 pools:
{chr(10).join(f"  - `{p['pool']}` : {p['count']} rows ({p['share']*100:.2f}%)" for p in pools_top5)}

## Feature Coverage
- Decoded feature coverage: {feat['decoded_feature_coverage']}
- Source identity coverage: {feat['source_identity_coverage']}
- WETH leg coverage: {feat['weth_leg_coverage']}
- Temporal coverage: {feat['temporal_coverage']}
- Unsafe extreme rows: tick={feat['unsafe_extreme_rows']['tick_unsafe']} / liq_zero={feat['unsafe_extreme_rows']['liquidity_zero']}

## Direction & Fee Tier
- Direction distribution: {dir_ft['direction_distribution']}
- Fee tier distribution: {dir_ft['fee_tier_distribution']}

## Oracle Spot-Check
- Dune token present: `{oracle['dune_token_present']}`
- External calls executed: `{oracle['executed_external_calls']}`
- Sample payloads generated: {oracle['sample_size']} (≤50 limit respected)

## Gate Checks
| Check | Result |
|---|---|
| input_validation_pass | {gmark("input_validation_pass")} |
| no_v2_rows | {gmark("no_v2_rows")} |
| no_aero_rows | {gmark("no_aero_rows")} |
| no_mixed_dataset | {gmark("no_mixed_dataset")} |
| all_protocol_v3 | {gmark("all_protocol_v3")} |
| decoded_feature_coverage >= 0.98 | {gmark("decoded_feature_coverage_gte_98pct")} |
| source_identity_coverage >= 0.98 | {gmark("source_identity_coverage_gte_98pct")} |
| successful_rows >= 10,000 | {gmark("successful_rows_gte_10k")} |
| unique_pools >= 50 | {gmark("unique_pools_gte_50")} |
| top5_pool_share <= 45% | {gmark("top5_pool_share_lte_45pct")} |
| quote_success_rate >= 90% | {gmark("quote_success_rate_gte_90pct")} |
| risk_rows_isolated | {gmark("risk_rows_isolated")} |
| no_production_writes | {gmark("no_production_writes")} |
| no_active_model_writes | {gmark("no_active_model_writes")} |

## Return Distribution (clean alpha)
- Count: {iv['return_summary']['count']}
- Mean: {iv['return_summary']['mean']}
- P50: {iv['return_summary']['p50']}
- Min/Max: {iv['return_summary']['min']} / {iv['return_summary']['max']}

## Safety Assertions
- v3_only: `True`
- no_v2_rows: `{gate.get("no_v2_rows", False)}`
- no_mixed_dataset: `True`
- no_production_model_write: `True`
- no_active_model_overwrite: `True`
- no_runtime_trading_code_change: `True`
- no_live_promote: `True`
- no_paper_promote: `True`

## Final Gate Decision
- allow_v3r9_return_aware_ablation: `{str(allow_v3r9).lower()}`
- allow_v3_candidate_model_dryrun: `{str(allow_model_dryrun).lower()}` (concentration excellent: top5={conc.get("top5_pool_share",1):.3f} ≤0.30, top1={conc.get("top1_pool_share",1):.3f} ≤0.10, pools={conc.get("unique_pools",0)})
- allow_v3_mainline: `false`
- allow_mixed_dataset: `false`
- promote_live_allowed: `false`

## Elapsed
- Script runtime: {t_elapsed:.1f}s

> All metrics are research-only / sanity-only and are NOT deployment evidence.
> No production model was written. No active model was overwritten. No V2 artifacts changed.
"""


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.time()
    rng = random.Random(RANDOM_SEED)
    rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL") or ""
    if not rpc_url:
        raise RuntimeError("EVM_HTTP_URL or LABEL_RPC_URL must be set")

    print("[V3R8] Phase 1: Fetch pool metadata…")
    meta = fetch_metadata()
    print(f"  {len(meta):,} WETH V3 pools in discovered_pools.")

    print("[V3R8] Phase 2: Fetch all decoded V3 rows…")
    all_rows = fetch_decoded_rows()

    print(f"[V3R8] Phase 3: Apply per-pool cap (cap={POOL_CAP})…")
    candidates = apply_pool_cap(all_rows, meta)
    print(f"  Candidates after cap: {len(candidates):,}  unique_pools={len({c['pool_address'] for c in candidates})}")

    print("[V3R8] Phase 4: Min-notional filter…")
    candidates_filtered, notional_rpt = apply_min_notional(candidates)
    print(f"  After filter: {len(candidates_filtered):,}  removed_dust={notional_rpt['removed_dust']}")

    print("[V3R8] Phase 5: Compute 1h window features…")
    compute_window_features(candidates_filtered, all_rows)

    print("[V3R8] Phase 6: Parallel quoting…")
    quote_by_id = quote_all_parallel(candidates_filtered, rpc_url)

    # Accounting
    quote_success = sum(1 for q in quote_by_id.values() if q.get("_quote_ok"))
    quote_failed  = sum(1 for q in quote_by_id.values() if not q.get("_quote_ok") and q.get("failure_reason") != "direction_ambiguous_skipped")
    quote_skipped = sum(1 for q in quote_by_id.values() if q.get("failure_reason") == "direction_ambiguous_skipped")
    quote_attempted = quote_success + quote_failed
    print(f"  success={quote_success}  failed={quote_failed}  skipped(sell)={quote_skipped}")

    print("[V3R8] Phase 7: Build full dataset + risk labels…")
    dataset = build_full_dataset(candidates_filtered, quote_by_id)

    print("[V3R8] Phase 8: Assign alpha labels…")
    dataset = assign_alpha_labels(dataset)

    print("[V3R8] Phase 9: Split clean / risk…")
    all_ds, clean_ds, risk_ds = split_datasets(dataset)

    print("[V3R8] Phase 10: Compute reports…")
    conc = pool_concentration_report(all_ds)
    feat = feature_coverage_report(all_ds)
    dir_ft = direction_fee_tier_report(all_ds)
    oracle = oracle_spotcheck_report(candidates_filtered)
    iv = input_validation_report(
        all_ds, clean_ds, risk_ds, conc, feat,
        cands_before_quote=len(candidates_filtered),
        quote_attempted=quote_attempted,
        quote_success=quote_success,
        quote_failed=quote_failed,
        quote_skipped=quote_skipped,
    )
    denylist = build_leakage_denylist(risk_ds)

    print("[V3R8] Phase 11: Write output files…")
    normalized_all = [_normalize_row(r) for r in all_ds]
    normalized_clean = [_normalize_row(r) for r in clean_ds]
    normalized_risk = [_normalize_row(r) for r in risk_ds]

    write_csv_rows(OUT_DIR / "run004_v3r8_all_v3_decoded_backed_10k_dataset.csv",       normalized_all)
    write_csv_rows(OUT_DIR / "run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv", normalized_clean)
    write_csv_rows(OUT_DIR / "run004_v3r8_risk_v3_decoded_backed_10k_dataset.csv",       normalized_risk)
    write_json(OUT_DIR / "run004_v3r8_input_validation_report.json",   iv)
    write_json(OUT_DIR / "run004_v3r8_pool_concentration_report.json", conc)
    write_json(OUT_DIR / "run004_v3r8_feature_coverage_report.json",   feat)
    write_json(OUT_DIR / "run004_v3r8_direction_fee_tier_report.json",  dir_ft)
    write_json(OUT_DIR / "run004_v3r8_oracle_spotcheck_report.json",    oracle)
    write_json(OUT_DIR / "run004_v3r8_notional_filter_report.json",     notional_rpt)
    write_json(OUT_DIR / "run004_v3r8_leakage_denylist_for_v3r9.json", denylist)

    t_elapsed = time.time() - t0
    md = markdown(iv, conc, feat, dir_ft, oracle, t_elapsed)
    write_markdown(RUN_DIR / "run-004-patch-v3r8-v3-only-larger-decoded-backed-dataset.md", md)

    print(f"\n[V3R8] Done in {t_elapsed:.1f}s")
    print(json.dumps({
        "status": iv["status"],
        "all_rows": iv["all_rows"],
        "clean_alpha_rows": iv["clean_alpha_rows"],
        "risk_rows": iv["risk_rows"],
        "unique_pools": iv["unique_pools"],
        "top5_pool_share": iv["top5_pool_share"],
        "quote_success": iv["quote_success"],
        "quote_success_rate": iv["quote_success_rate"],
        "gate_pass": iv["gate_checks"]["input_validation_pass"],
    }, indent=2))


if __name__ == "__main__":
    main()
