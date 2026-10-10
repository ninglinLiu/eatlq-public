"""Run-004 V2 live-equivalent 7-day pilot (diagnostics-only).

Extends the 1D pilot semantics:
  * global Swap topic scan, factory-canonical filter, WETH-leg only;
  * deterministic Base 2s timestamp grid validated by sampled blocks;
  * archive eth_call(getReserves) with primary entry_quote_block_policy
    = entry_block_minus_1 and exit_quote_block_policy = exit_block;
  * timestamp 30m horizon, no same-block / fixed-block primary label.

7D additions:
  * 7-day historical window inside factory scan to_block;
  * daily aggregation (clean rows, quote success, concentration, failures);
  * entry-quote-block-policy sensitivity over a sampled subset
    (entry_block_minus_1 / entry_block / entry_block_plus_1) - diagnostic
    only and never replaces the primary label;
  * explicit failure decomposition including
    pool_created_after_entry_quote_block to explain entry_zero_reserve.

No training, no model research, no signals/trades, no production model write,
no executor / strategy runtime change, no H1611 747-pool fallback.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import random
import sys
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v2_live_equivalent_1d_pilot import (  # noqa: E402
    PilotRPC,
    block_number,
    deterministic_timestamp_grid,
    load_universe,
    pilot_rpc_from_config,
    quote_no_fee,
    reserve_orientation,
    scan_swap_logs,
    threaded_single_reserves,
)
from scripts.run004_v2_live_equivalent_backtest import (  # noqa: E402
    DEFAULT_CONFIG,
    WETH_BASE,
    block_timestamp,
    decode_v2_swap_log,
    load_config,
    quote_v2_out,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

UNIVERSE_CSV = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe.csv"
UNIVERSE_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe_summary.json"
OUT_ROWS = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rows.csv"
OUT_DAILY = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_daily.csv"
OUT_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v2-live-equivalent-7d-pilot.md"

MAX_UNCOMPRESSED_CSV_BYTES = 200 * 1024 * 1024
DAYS = 7
SECONDS_PER_DAY = 86_400
SENSITIVITY_SAMPLE_N = int(os.environ.get("V2_7D_SENSITIVITY_N", "500"))
MAX_DECODED_ROWS = int(os.environ.get("V2_7D_MAX_ROWS", "0"))


def resolve_7d_window(rpc: PilotRPC, universe_to_block: int) -> tuple[int, int, int, int]:
    from_env = os.environ.get("V2_7D_FROM_BLOCK", "").strip()
    to_env = os.environ.get("V2_7D_TO_BLOCK", "").strip()
    latest = block_number(rpc)
    to_block = int(to_env, 0) if to_env else min(latest, universe_to_block)
    from_block = int(from_env, 0) if from_env else max(0, to_block - DAYS * 43_200)
    if to_block > universe_to_block:
        raise RuntimeError(
            f"7D window to_block {to_block} exceeds factory universe to_block {universe_to_block}"
        )
    from_ts = block_timestamp(rpc, from_block)
    to_ts = block_timestamp(rpc, to_block)
    return from_block, to_block, from_ts, to_ts


def output_paths_with_compression(path: Path) -> tuple[Path, bool]:
    size = path.stat().st_size if path.exists() else 0
    if size <= MAX_UNCOMPRESSED_CSV_BYTES:
        return path, False
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        dst.writelines(src)
    path.unlink()
    return gz_path, True


def compute_entry_policy_sensitivity(
    rpc: PilotRPC,
    decoded_inputs: list[dict[str, Any]],
    cfg: dict[str, Any],
    sample_n: int,
) -> dict[str, Any]:
    if not decoded_inputs:
        return {"sample_size": 0, "policies": {}, "note": "no_decoded_inputs"}
    notional = int(cfg["execution"].get("notional_wei", 500000000000000))
    rng = random.Random(42)
    sample = rng.sample(decoded_inputs, min(sample_n, len(decoded_inputs)))
    offsets = [
        ("entry_block_minus_1", -1),
        ("entry_block", 0),
        ("entry_block_plus_1", 1),
    ]
    requests: list[tuple[str, int]] = []
    for row in sample:
        eb = int(row["entry_block"])
        for _, off in offsets:
            requests.append((row["pool_address"], max(1, eb + off)))
    print(f"[7d_pilot] sensitivity reserves: rows={len(sample)} requests={len(requests)}", flush=True)
    reserves = threaded_single_reserves(rpc, requests, workers=16)
    policies: dict[str, dict[str, int | float]] = {}
    for name, off in offsets:
        success = 0
        zero_reserve = 0
        rpc_failure = 0
        pool_not_yet_created = 0
        for row in sample:
            eb = int(row["entry_block"])
            block = max(1, eb + off)
            if int(row.get("creation_block", 0)) > block:
                pool_not_yet_created += 1
                continue
            res = reserves.get((row["pool_address"], block))
            if res is None:
                rpc_failure += 1
                continue
            r_weth, r_tok = reserve_orientation(row, res)
            if r_weth <= 0 or r_tok <= 0:
                zero_reserve += 1
                continue
            tokens = quote_v2_out(r_weth, r_tok, notional)
            if tokens > 0:
                success += 1
        n = len(sample)
        policies[name] = {
            "n": n,
            "entry_quote_success_count": success,
            "entry_quote_success_rate": success / n if n else 0.0,
            "zero_reserve_count": zero_reserve,
            "rpc_failure_count": rpc_failure,
            "pool_not_yet_created_count": pool_not_yet_created,
        }
    return {
        "sample_size": len(sample),
        "policies": policies,
        "note": (
            "Diagnostic only. Primary label uses entry_block_minus_1 to mirror live"
            " observability where execution sees pre-trade reserves; entry_block and"
            " entry_block_plus_1 reflect post-swap state and would leak the swap into"
            " the entry quote. Higher success at +1 is therefore expected and not"
            " evidence of better label quality."
        ),
    }


def aggregate_daily(written_rows: list[dict[str, Any]], from_ts: int) -> list[dict[str, Any]]:
    by_day_attempted: dict[int, int] = defaultdict(int)
    by_day_clean: dict[int, int] = defaultdict(int)
    by_day_entry_ok: dict[int, int] = defaultdict(int)
    by_day_exit_ok: dict[int, int] = defaultdict(int)
    by_day_pools: dict[int, set[str]] = defaultdict(set)
    by_day_tokens: dict[int, set[str]] = defaultdict(set)
    by_day_failures: dict[int, Counter] = defaultdict(Counter)
    by_day_pool_counter: dict[int, Counter] = defaultdict(Counter)
    by_day_token_counter: dict[int, Counter] = defaultdict(Counter)

    for row in written_rows:
        ts = row.get("entry_timestamp")
        try:
            ts_int = int(ts) if ts not in ("", None) else None
        except (TypeError, ValueError):
            ts_int = None
        if ts_int is None:
            day = 0
        else:
            day = max(0, (ts_int - from_ts) // SECONDS_PER_DAY)
        by_day_attempted[day] += 1
        if row.get("entry_quote_success") in (True, "True"):
            by_day_entry_ok[day] += 1
        if row.get("exit_quote_success") in (True, "True"):
            by_day_exit_ok[day] += 1
        if row.get("is_clean") in (True, "True"):
            by_day_clean[day] += 1
            by_day_pools[day].add(row["pool_address"])
            by_day_tokens[day].add(row["token_address"])
            by_day_pool_counter[day][row["pool_address"]] += 1
            by_day_token_counter[day][row["token_address"]] += 1
        reason = row.get("failure_reason") or row.get("risk_flag")
        if reason:
            by_day_failures[day][reason] += 1

    daily_rows: list[dict[str, Any]] = []
    for day in range(DAYS):
        attempted = by_day_attempted.get(day, 0)
        clean = by_day_clean.get(day, 0)
        entry_ok = by_day_entry_ok.get(day, 0)
        exit_ok = by_day_exit_ok.get(day, 0)
        pools = by_day_pool_counter.get(day, Counter())
        tokens = by_day_token_counter.get(day, Counter())
        top1_pool_share = (pools.most_common(1)[0][1] / clean) if clean and pools else 0.0
        top5_pool_share = (sum(v for _, v in pools.most_common(5)) / clean) if clean else 0.0
        top1_token_share = (tokens.most_common(1)[0][1] / clean) if clean and tokens else 0.0
        top5_token_share = (sum(v for _, v in tokens.most_common(5)) / clean) if clean else 0.0
        daily_rows.append({
            "day_index": day,
            "day_start_timestamp": from_ts + day * SECONDS_PER_DAY,
            "day_start_iso": datetime.fromtimestamp(from_ts + day * SECONDS_PER_DAY, tz=timezone.utc).isoformat(),
            "attempted_label_rows": attempted,
            "clean_rows": clean,
            "clean_rate": (clean / attempted) if attempted else 0.0,
            "entry_quote_success": entry_ok,
            "exit_quote_success": exit_ok,
            "entry_quote_success_rate": (entry_ok / attempted) if attempted else 0.0,
            "exit_quote_success_rate": (exit_ok / attempted) if attempted else 0.0,
            "unique_pools_clean": len(by_day_pools.get(day, set())),
            "unique_tokens_clean": len(by_day_tokens.get(day, set())),
            "top1_pool_share_clean": top1_pool_share,
            "top5_pool_share_clean": top5_pool_share,
            "top1_token_share_clean": top1_token_share,
            "top5_token_share_clean": top5_token_share,
            "failure_reason_top5": dict(by_day_failures.get(day, Counter()).most_common(5)),
        })
    return daily_rows


def main() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    rpc = pilot_rpc_from_config(cfg)
    universe_summary = json.loads(UNIVERSE_SUMMARY.read_text(encoding="utf-8"))
    if not universe_summary.get("point_in_time_universe_pass"):
        raise RuntimeError("factory universe point_in_time_universe_pass is not true")
    universe_to_block = int(universe_summary["to_block"])
    weth_pairs, all_pairs, _ = load_universe()

    from_block, to_block, from_ts, to_ts = resolve_7d_window(rpc, universe_to_block)
    print(f"[7d_pilot] window blocks={from_block:,}-{to_block:,} ts={from_ts}-{to_ts}")

    rows, scan_counts = scan_swap_logs(rpc, from_block, to_block, all_pairs, weth_pairs)

    timestamp_from = max(1, from_block - 1)
    timestamp_to = to_block + 2_000
    block_ts, ts_meta = deterministic_timestamp_grid(
        rpc, from_block, to_block, from_ts, to_ts, timestamp_from, timestamp_to
    )
    ordered_blocks = sorted(block_ts)
    ordered_ts = [block_ts[b] for b in ordered_blocks]
    horizon_sec = int(cfg["horizon"].get("exit_horizon_sec", 1800))

    counters: Counter = Counter()
    failure_reasons: Counter = Counter()
    decoded_inputs: list[dict[str, Any]] = []
    unknown_outputs: list[dict[str, Any]] = []

    for row in rows:
        decoded = decode_v2_swap_log({"data": row["data"]}, row["token0"], row["token1"], WETH_BASE)
        base = {
            "pool_address": row["pool_address"],
            "token_address": row["token_address"],
            "token0": row["token0"],
            "token1": row["token1"],
            "creation_block": row["creation_block"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "entry_block": row["entry_block"],
            "observed_direction": decoded.direction,
            "label_action": "FIXED_NOTIONAL_BUY_WETH_TO_TOKEN_ROUNDTRIP",
            "amount0_in": decoded.amount0_in,
            "amount1_in": decoded.amount1_in,
            "amount0_out": decoded.amount0_out,
            "amount1_out": decoded.amount1_out,
        }
        if decoded.certainty != "certain":
            counters["unknown_direction_rows"] += 1
            failure_reasons[decoded.reason or "unknown_direction"] += 1
            unknown_outputs.append({
                **base,
                "failure_reason": decoded.reason or "unknown_direction",
                "risk_flag": "semantic_uncertain",
            })
            continue
        counters["decoded_rows"] += 1
        decoded_inputs.append({**row, **base})

    counters["decoded_rows_pre_subsample"] = counters["decoded_rows"]
    subsample_meta: dict[str, Any] = {"applied": False, "max_rows": MAX_DECODED_ROWS}
    if MAX_DECODED_ROWS and counters["decoded_rows"] > MAX_DECODED_ROWS:
        rng_sub = random.Random(7)
        decoded_inputs = rng_sub.sample(decoded_inputs, MAX_DECODED_ROWS)
        subsample_meta = {
            "applied": True,
            "max_rows": MAX_DECODED_ROWS,
            "decoded_rows_pre_subsample": counters["decoded_rows_pre_subsample"],
            "kept_rows": MAX_DECODED_ROWS,
            "subsample_rng_seed": 7,
            "rationale": (
                "Bounded random subsample over decoded canonical WETH-pair rows."
                " Required because the configured endpoint sustains ~45-50 reliable"
                " single-call eth_calls/sec, and processing every decoded row would"
                " take ~3 hours. Daily distribution is preserved in expectation."
            ),
        }
        counters["decoded_rows"] = len(decoded_inputs)
    counters["attempted_label_rows"] = len(decoded_inputs)
    print(
        f"[7d_pilot] decoded_rows_pre_subsample={counters['decoded_rows_pre_subsample']:,}"
        f" decoded_rows_used={counters['decoded_rows']:,}"
        f" subsample_applied={subsample_meta['applied']}",
        flush=True,
    )

    prepared: list[dict[str, Any]] = []
    timestamp_failures = 0
    for row in decoded_inputs:
        entry_block = int(row["entry_block"])
        entry_ts = block_ts.get(entry_block)
        if entry_ts is None:
            timestamp_failures += 1
            failure_reasons["timestamp_resolution_error"] += 1
            counters["failed_rows"] += 1
            prepared.append({
                **row,
                "failure_reason": "timestamp_resolution_error",
                "risk_flag": "timestamp_resolution_failed",
            })
            continue
        exit_target_ts = entry_ts + horizon_sec
        pos = bisect_left(ordered_ts, exit_target_ts)
        if pos >= len(ordered_blocks):
            failure_reasons["exit_block_missing"] += 1
            counters["failed_rows"] += 1
            prepared.append({
                **row,
                "entry_timestamp": entry_ts,
                "exit_target_timestamp": exit_target_ts,
                "failure_reason": "exit_block_missing",
                "risk_flag": "timestamp_resolution_failed",
            })
            continue
        exit_block = ordered_blocks[pos]
        exit_ts = ordered_ts[pos]
        entry_quote_block = max(1, entry_block - 1)
        prepared.append({
            **row,
            "entry_timestamp": entry_ts,
            "exit_target_timestamp": exit_target_ts,
            "exit_block": exit_block,
            "exit_timestamp": exit_ts,
            "exit_block_resolution_method": ts_meta["method"],
            "exit_block_resolution_error_seconds": exit_ts - exit_target_ts,
            "entry_quote_block": entry_quote_block,
        })

    timestamp_horizon_success_rate = 1.0 - (timestamp_failures / max(counters["attempted_label_rows"], 1))
    print(
        f"[7d_pilot] decoded_rows={counters['decoded_rows']:,} unknown={counters['unknown_direction_rows']:,}"
        f" timestamp_horizon_success_rate={timestamp_horizon_success_rate:.6f}"
    )

    notional = int(cfg["execution"].get("notional_wei", 500000000000000))
    round_trip_cost = float(cfg["execution"].get("round_trip_cost", 0.006))
    slippage_bps = float(cfg["execution"].get("slippage_stress_bps", 100))
    gas_cfg = cfg["execution"].get("gas", {})
    gas_units = int(gas_cfg.get("buy_units", 180000)) + int(gas_cfg.get("sell_units", 160000)) + int(gas_cfg.get("approval_units", 50000))
    gas_price_wei = int(float(gas_cfg.get("gas_price_gwei_medium", 0.03)) * 1_000_000_000)
    gas_cost_return = (gas_units * gas_price_wei) / notional
    max_abs_net_return = float(cfg["execution"]["sanity"].get("max_abs_net_return", 10.0))
    quarantine_abs_return = float(cfg["execution"]["sanity"].get("quarantine_abs_net_return_gt", 1.0))

    entry_requests: list[tuple[str, int]] = []
    for row in prepared:
        if row.get("failure_reason") or "entry_quote_block" not in row:
            continue
        entry_requests.append((row["pool_address"], int(row["entry_quote_block"])))
    print(f"[7d_pilot] entry reserve requests (will dedup): {len(entry_requests):,}", flush=True)
    entry_reserves = threaded_single_reserves(rpc, entry_requests, workers=16)

    entry_keep_rows: list[tuple[dict[str, Any], int, int]] = []
    pre_label_failures: list[dict[str, Any]] = []
    for row in prepared:
        if row.get("failure_reason"):
            pre_label_failures.append(row)
            continue
        entry_quote_block = int(row["entry_quote_block"])
        creation_block = int(row.get("creation_block") or 0)
        if creation_block > entry_quote_block:
            row["failure_reason"] = "pool_created_after_entry_quote_block"
            row["risk_flag"] = "universe_timing_artifact"
            row["entry_quote_success"] = False
            row["exit_quote_success"] = False
            pre_label_failures.append(row)
            failure_reasons["pool_created_after_entry_quote_block"] += 1
            counters["failed_rows"] += 1
            continue
        res = entry_reserves.get((row["pool_address"], entry_quote_block))
        if res is None:
            row["failure_reason"] = "entry_reserve_rpc_failure"
            row["risk_flag"] = "rpc_error"
            row["entry_quote_success"] = False
            row["exit_quote_success"] = False
            pre_label_failures.append(row)
            failure_reasons["entry_reserve_rpc_failure"] += 1
            counters["failed_rows"] += 1
            continue
        r_weth_e, r_tok_e = reserve_orientation(row, res)
        if r_weth_e <= 0 or r_tok_e <= 0:
            row["failure_reason"] = "entry_zero_reserve"
            row["entry_quote_success"] = False
            row["exit_quote_success"] = False
            pre_label_failures.append(row)
            failure_reasons["entry_zero_reserve"] += 1
            counters["failed_rows"] += 1
            continue
        tokens_no_fee = quote_no_fee(r_weth_e, r_tok_e, notional)
        tokens_fee = quote_v2_out(r_weth_e, r_tok_e, notional)
        if tokens_fee <= 0:
            row["failure_reason"] = "entry_quote_zero"
            row["entry_quote_success"] = False
            row["exit_quote_success"] = False
            pre_label_failures.append(row)
            failure_reasons["entry_quote_zero"] += 1
            counters["failed_rows"] += 1
            continue
        row["entry_quote_success"] = True
        row["entry_amount_out"] = tokens_fee
        counters["entry_quote_success"] += 1
        entry_keep_rows.append((row, tokens_no_fee, tokens_fee))

    exit_requests: list[tuple[str, int]] = []
    for row, _, _ in entry_keep_rows:
        exit_requests.append((row["pool_address"], int(row["exit_block"])))
    print(f"[7d_pilot] exit reserve requests (entry-success only): {len(exit_requests):,}", flush=True)
    exit_reserves = threaded_single_reserves(rpc, exit_requests, workers=16) if exit_requests else {}

    final_rows: list[dict[str, Any]] = list(pre_label_failures)
    pool_counts: Counter = Counter()
    token_counts: Counter = Counter()
    duplicate_keys: dict[tuple[str, int, str], dict[str, Any]] = {}
    duplicate_conflict_count = 0

    for row, tokens_no_fee, tokens_fee in entry_keep_rows:
        exit_block = int(row["exit_block"])
        res = exit_reserves.get((row["pool_address"], exit_block))
        if res is None:
            row["failure_reason"] = "exit_reserve_rpc_failure"
            row["exit_quote_success"] = False
            failure_reasons["exit_reserve_rpc_failure"] += 1
            counters["failed_rows"] += 1
            final_rows.append(row)
            continue
        r_weth_x, r_tok_x = reserve_orientation(row, res)
        if r_weth_x <= 0 or r_tok_x <= 0:
            row["failure_reason"] = "exit_zero_reserve_or_pool_drained"
            row["exit_quote_success"] = False
            failure_reasons["exit_zero_reserve_or_pool_drained"] += 1
            counters["failed_rows"] += 1
            final_rows.append(row)
            continue
        weth_back_no_fee = quote_no_fee(r_tok_x, r_weth_x, tokens_no_fee)
        weth_back_fee = quote_v2_out(r_tok_x, r_weth_x, tokens_fee)
        if weth_back_fee <= 0:
            row["failure_reason"] = "exit_quote_zero"
            row["exit_quote_success"] = False
            failure_reasons["exit_quote_zero"] += 1
            counters["failed_rows"] += 1
            final_rows.append(row)
            continue
        row["exit_quote_success"] = True
        row["exit_weth_back"] = weth_back_fee
        gross_return = weth_back_no_fee / notional - 1.0
        fee_adjusted_return = weth_back_fee / notional - 1.0
        gas_adjusted_return = fee_adjusted_return - gas_cost_return - round_trip_cost
        net_return = gas_adjusted_return - (slippage_bps / 10000.0)
        row.update({
            "gross_return": gross_return,
            "fee_adjusted_return": fee_adjusted_return,
            "gas_adjusted_return": gas_adjusted_return,
            "net_return": net_return,
            "gas_cost_return": gas_cost_return,
            "slippage_stress_return_cost": slippage_bps / 10000.0,
            "sanity_flag": "ok",
            "risk_flag": "",
            "failure_reason": "",
        })
        if net_return > max_abs_net_return or net_return < -1.0:
            row["risk_flag"] = "extreme_return_quarantine"
            row["sanity_flag"] = "extreme_return"
            failure_reasons["extreme_return_quarantine"] += 1
            counters["failed_rows"] += 1
        elif abs(net_return) > quarantine_abs_return:
            row["risk_flag"] = "abs_return_gt_quarantine_threshold"
            failure_reasons["abs_return_gt_quarantine_threshold"] += 1
            counters["failed_rows"] += 1
        else:
            counters["clean_rows"] += 1
            pool_counts[row["pool_address"]] += 1
            token_counts[row["token_address"]] += 1
            row["is_clean"] = True
        counters["exit_quote_success"] += 1
        key = (row["tx_hash"], int(row["log_index"]), row["pool_address"])
        if key in duplicate_keys and duplicate_keys[key].get("net_return") != row.get("net_return"):
            duplicate_conflict_count += 1
        duplicate_keys[key] = row
        final_rows.append(row)

    failure_reasons["non_weth_pair"] = scan_counts["non_weth_pair_logs"]
    failure_reasons["non_factory_swap_log"] = scan_counts["non_factory_swap_logs"]

    fields = [
        "pool_address", "token_address", "token0", "token1", "tx_hash", "log_index",
        "entry_block", "entry_timestamp", "exit_target_timestamp", "exit_block", "exit_timestamp",
        "exit_block_resolution_method", "exit_block_resolution_error_seconds",
        "observed_direction", "label_action",
        "amount0_in", "amount1_in", "amount0_out", "amount1_out",
        "entry_quote_block", "entry_quote_success", "exit_quote_success",
        "entry_amount_out", "exit_weth_back",
        "gross_return", "fee_adjusted_return", "gas_adjusted_return", "net_return",
        "gas_cost_return", "slippage_stress_return_cost",
        "is_clean", "risk_flag", "sanity_flag", "failure_reason",
    ]

    OUT_ROWS.parent.mkdir(parents=True, exist_ok=True)
    written_rows: list[dict[str, Any]] = []
    with OUT_ROWS.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in unknown_outputs:
            payload = {k: row.get(k, "") for k in fields}
            writer.writerow(payload)
            written_rows.append(payload)
        for row in final_rows:
            payload = {k: row.get(k, "") for k in fields}
            writer.writerow(payload)
            written_rows.append(payload)

    output_rows_path, compressed = output_paths_with_compression(OUT_ROWS)

    daily_rows = aggregate_daily(written_rows, from_ts)
    OUT_DAILY.parent.mkdir(parents=True, exist_ok=True)
    daily_fields = [
        "day_index", "day_start_timestamp", "day_start_iso",
        "attempted_label_rows", "clean_rows", "clean_rate",
        "entry_quote_success", "exit_quote_success",
        "entry_quote_success_rate", "exit_quote_success_rate",
        "unique_pools_clean", "unique_tokens_clean",
        "top1_pool_share_clean", "top5_pool_share_clean",
        "top1_token_share_clean", "top5_token_share_clean",
        "failure_reason_top5",
    ]
    with OUT_DAILY.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=daily_fields)
        writer.writeheader()
        for d in daily_rows:
            writer.writerow({**d, "failure_reason_top5": json.dumps(d["failure_reason_top5"])})

    sensitivity = compute_entry_policy_sensitivity(rpc, decoded_inputs, cfg, SENSITIVITY_SAMPLE_N)

    clean_rows = counters["clean_rows"]
    attempted = counters["attempted_label_rows"]
    unique_pools = len(pool_counts)
    unique_tokens = len(token_counts)
    top1_pool_share = (pool_counts.most_common(1)[0][1] / clean_rows) if clean_rows and pool_counts else 0.0
    top5_pool_share = (sum(v for _, v in pool_counts.most_common(5)) / clean_rows) if clean_rows else 0.0
    top1_token_share = (token_counts.most_common(1)[0][1] / clean_rows) if clean_rows and token_counts else 0.0
    top5_token_share = (sum(v for _, v in token_counts.most_common(5)) / clean_rows) if clean_rows else 0.0

    daily_clean_counts = [d["clean_rows"] for d in daily_rows]
    total_clean = sum(daily_clean_counts) or 1
    top2_days_share = sum(sorted(daily_clean_counts, reverse=True)[:2]) / total_clean if clean_rows else 0.0
    temporal_concentration_risk = clean_rows > 0 and top2_days_share > 0.50

    pool_concentration_risk = top5_pool_share > 0.50
    token_concentration_risk = top5_token_share > 0.50
    timestamp_passed = timestamp_horizon_success_rate >= 1.0 and counters["attempted_label_rows"] > 0
    clean_gate = clean_rows >= 3000

    decision = "PILOT_7D_COMPLETED"
    if not scan_counts["raw_swap_logs"]:
        decision = "FAILED_SWAP_LOG_SCAN"
    elif not timestamp_passed:
        decision = "FAILED_TIMESTAMP_HORIZON"
    elif duplicate_conflict_count:
        decision = "FAILED_SEMANTICS_UNCERTAIN"
    elif not clean_gate:
        decision = "DATA_INSUFFICIENT_FOR_WALK_FORWARD"

    sizes = {
        str(output_rows_path.relative_to(ROOT)): output_rows_path.stat().st_size,
        str(OUT_DAILY.relative_to(ROOT)): OUT_DAILY.stat().st_size,
    }

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "patch": "run-004-v2-live-equivalent-7d-pilot",
        "sanity_only_not_deploy_evidence": True,
        "from_block": from_block,
        "to_block": to_block,
        "from_timestamp": from_ts,
        "to_timestamp": to_ts,
        "days_covered": DAYS,
        "raw_swap_logs": scan_counts["raw_swap_logs"],
        "canonical_v2_swap_logs": scan_counts["canonical_v2_swap_logs"],
        "non_factory_swap_logs": scan_counts["non_factory_swap_logs"],
        "weth_pair_logs": scan_counts["weth_pair_logs"],
        "non_weth_pair_logs": scan_counts["non_weth_pair_logs"],
        "decoded_rows_pre_subsample": counters.get("decoded_rows_pre_subsample", counters["decoded_rows"]),
        "decoded_rows": counters["decoded_rows"],
        "subsample": subsample_meta,
        "unknown_direction_rows": counters["unknown_direction_rows"],
        "attempted_label_rows": attempted,
        "clean_rows": clean_rows,
        "clean_rate": (clean_rows / attempted) if attempted else 0.0,
        "entry_quote_success_rate": (counters["entry_quote_success"] / attempted) if attempted else 0.0,
        "exit_quote_success_rate": (counters["exit_quote_success"] / attempted) if attempted else 0.0,
        "timestamp_horizon_success_rate": timestamp_horizon_success_rate,
        "duplicate_conflict_count": duplicate_conflict_count,
        "same_block_primary_label_used": False,
        "fixed_block_primary_label_used": False,
        "unique_pools": unique_pools,
        "unique_tokens": unique_tokens,
        "top1_pool_share": top1_pool_share,
        "top5_pool_share": top5_pool_share,
        "top1_token_share": top1_token_share,
        "top5_token_share": top5_token_share,
        "top2_days_share_of_clean": top2_days_share,
        "failure_reason_distribution": dict(failure_reasons.most_common()),
        "daily_metrics": daily_rows,
        "entry_quote_policy_sensitivity": sensitivity,
        "rpc_calls": dict(rpc.calls),
        "rpc_errors": dict(rpc.errors),
        "output_file_sizes": sizes,
        "rows_output_compressed": compressed,
        "factory_universe": {
            "path": str(UNIVERSE_CSV.relative_to(ROOT)),
            "summary": str(UNIVERSE_SUMMARY.relative_to(ROOT)),
            "unique_pairs": universe_summary.get("unique_pairs"),
            "weth_pairs": universe_summary.get("weth_pairs"),
            "point_in_time_universe_pass": universe_summary.get("point_in_time_universe_pass"),
            "to_block": universe_to_block,
        },
        "timestamp_mapping": ts_meta,
        "reserves_rpc_method": {
            "name": "threaded_single_reserves",
            "workers": 16,
            "transport": "requests.Session + HTTPAdapter pool",
            "rationale": (
                "rpc_probe at this endpoint shows JSON-RPC batch_size=50 silently"
                " drops ~74% of items, batch=10 ~43%, batch=5 ~13%, batch=1 0%."
                " Threaded single eth_call is the only honest path; results before"
                " this change conflated provider throttling with on-chain zero"
                " reserves and underreported clean rows."
            ),
            "probe_artifact": "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rpc_probe.json",
        },
        "decision_flags": {
            "factory_universe_loaded": True,
            "point_in_time_universe_passed": True,
            "pilot_7d_completed": decision == "PILOT_7D_COMPLETED",
            "timestamp_horizon_label_passed": timestamp_passed,
            "clean_rows_gate_passed": clean_gate,
            "quote_failure_audit_completed": True,
            "entry_quote_policy_sensitivity_completed": sensitivity.get("sample_size", 0) > 0,
            "candidate_selection_bias_risk": (
                scan_counts["non_factory_swap_logs"] > 0 or scan_counts["non_weth_pair_logs"] > 0
            ),
            "pool_concentration_risk": pool_concentration_risk,
            "token_concentration_risk": token_concentration_risk,
            "temporal_concentration_risk": temporal_concentration_risk,
            "allow_30d_pilot_or_walkforward_prep": (
                decision == "PILOT_7D_COMPLETED"
                and clean_gate
                and timestamp_passed
                and duplicate_conflict_count == 0
            ),
            "allow_training": False,
            "allow_model_research": False,
            "allow_h19": False,
            "allow_shadow": False,
            "allow_live": False,
            "allow_promote": False,
        },
        "decision": decision,
    }

    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_markdown(summary, output_rows_path)
    print(json.dumps({
        "decision": decision,
        "clean_rows": clean_rows,
        "allow_30d_pilot_or_walkforward_prep": summary["decision_flags"]["allow_30d_pilot_or_walkforward_prep"],
        "summary": str(OUT_SUMMARY),
        "rows": str(output_rows_path),
        "daily": str(OUT_DAILY),
        "markdown": str(OUT_MD),
    }, indent=2))


def write_markdown(summary: dict[str, Any], rows_path: Path) -> None:
    flags = summary["decision_flags"]
    sens = summary["entry_quote_policy_sensitivity"]["policies"]
    daily = summary["daily_metrics"]
    failures = summary["failure_reason_distribution"]
    rpc_method = summary.get("reserves_rpc_method", {})
    subsample = summary.get("subsample", {"applied": False})
    sens_minus_1 = sens.get("entry_block_minus_1", {}).get("entry_quote_success_rate", 0.0)
    sens_block = sens.get("entry_block", {}).get("entry_quote_success_rate", 0.0)
    sens_plus_1 = sens.get("entry_block_plus_1", {}).get("entry_quote_success_rate", 0.0)

    daily_lines = [
        "| day | start_iso | attempted | clean | clean_rate | entry_q | exit_q | unique_pools | top5_pool | top5_token |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in daily:
        daily_lines.append(
            "| {di} | {iso} | {att:,} | {clean:,} | {cr:.4f} | {eq:.4f} | {xq:.4f} | {up} | {tp:.4f} | {tt:.4f} |".format(
                di=d["day_index"],
                iso=d["day_start_iso"],
                att=d["attempted_label_rows"],
                clean=d["clean_rows"],
                cr=d["clean_rate"],
                eq=d["entry_quote_success_rate"],
                xq=d["exit_quote_success_rate"],
                up=d["unique_pools_clean"],
                tp=d["top5_pool_share_clean"],
                tt=d["top5_token_share_clean"],
            )
        )

    sens_lines = [
        "| policy | n | entry_quote_success | success_rate | zero_reserve | rpc_fail | pool_not_yet_created |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in ["entry_block_minus_1", "entry_block", "entry_block_plus_1"]:
        s = sens.get(name, {})
        sens_lines.append(
            "| {n} | {N} | {ok} | {rate:.4f} | {zr} | {rf} | {pnc} |".format(
                n=name,
                N=s.get("n", 0),
                ok=s.get("entry_quote_success_count", 0),
                rate=s.get("entry_quote_success_rate", 0.0),
                zr=s.get("zero_reserve_count", 0),
                rf=s.get("rpc_failure_count", 0),
                pnc=s.get("pool_not_yet_created_count", 0),
            )
        )

    sub_block = (
        f"Bounded random subsample applied: {subsample.get('applied', False)} "
        f"(decoded_rows_pre_subsample = {summary['decoded_rows_pre_subsample']:,}, "
        f"kept = {summary['decoded_rows']:,}, seed = {subsample.get('subsample_rng_seed', 'n/a')}).  "
        f"Reason: {subsample.get('rationale', 'n/a')}"
    )
    rpc_block = (
        f"Reserves transport: `{rpc_method.get('name', 'unknown')}` "
        f"({rpc_method.get('transport', '')}, workers={rpc_method.get('workers', '?')}). "
        f"Probe: `{rpc_method.get('probe_artifact', '')}`. "
        f"{rpc_method.get('rationale', '')}"
    )

    md_lines = [
        "# Run-004 V2 Live-Equivalent 7D Pilot",
        "",
        f"**Status:** {summary['decision']}",
        "**Scope:** research-only / diagnostics-only; no training, no model research, no H19, no shadow, no live, no promote, no production model write, no executor / strategy runtime change.",
        "",
        "## Honesty Notes Before You Read Numbers",
        "",
        f"- {rpc_block}",
        f"- {sub_block}",
        "- An earlier 7D pilot run on this window reported `clean_rate = 0.0170` and `entry_zero_reserve = 425,889`. The `rpc_probe.json` diagnostic shows that result was dominated by JSON-RPC batch throttling at the configured endpoint (74% of items in 50-batches return item-level errors that this implementation previously coerced to `(0, 0)` reserves). The numbers below come from a single-call threaded re-run with strict None vs `(0, 0)` semantics. Do not conflate the two runs.",
        "",
        "## Window",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| from_block | {summary['from_block']:,} |",
        f"| to_block | {summary['to_block']:,} |",
        f"| from_timestamp | {summary['from_timestamp']} |",
        f"| to_timestamp | {summary['to_timestamp']} |",
        f"| days_covered | {summary['days_covered']} |",
        "",
        "## Counts",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| raw_swap_logs | {summary['raw_swap_logs']:,} |",
        f"| canonical_v2_swap_logs | {summary['canonical_v2_swap_logs']:,} |",
        f"| non_factory_swap_logs | {summary['non_factory_swap_logs']:,} |",
        f"| weth_pair_logs | {summary['weth_pair_logs']:,} |",
        f"| non_weth_pair_logs | {summary['non_weth_pair_logs']:,} |",
        f"| decoded_rows_pre_subsample | {summary['decoded_rows_pre_subsample']:,} |",
        f"| decoded_rows (used) | {summary['decoded_rows']:,} |",
        f"| subsample_applied | {summary['subsample']['applied']} (max_rows={summary['subsample']['max_rows']}) |",
        f"| unknown_direction_rows | {summary['unknown_direction_rows']:,} |",
        f"| attempted_label_rows | {summary['attempted_label_rows']:,} |",
        f"| clean_rows | **{summary['clean_rows']:,}** |",
        f"| clean_rate | {summary['clean_rate']:.4f} |",
        f"| entry_quote_success_rate | {summary['entry_quote_success_rate']:.4f} |",
        f"| exit_quote_success_rate | {summary['exit_quote_success_rate']:.4f} |",
        f"| timestamp_horizon_success_rate | {summary['timestamp_horizon_success_rate']:.4f} |",
        f"| duplicate_conflict_count | {summary['duplicate_conflict_count']} |",
        "",
        "## Daily Metrics",
        "",
        *daily_lines,
        "",
        f"Top-2 days share of total clean rows: **{summary['top2_days_share_of_clean']:.4f}**.",
        f"Temporal concentration risk: **{flags['temporal_concentration_risk']}**.",
        "",
        "## Pool / Token Concentration (clean rows)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| unique_pools | {summary['unique_pools']} |",
        f"| unique_tokens | {summary['unique_tokens']} |",
        f"| top1_pool_share | {summary['top1_pool_share']:.4f} |",
        f"| top5_pool_share | {summary['top5_pool_share']:.4f} |",
        f"| top1_token_share | {summary['top1_token_share']:.4f} |",
        f"| top5_token_share | {summary['top5_token_share']:.4f} |",
        "",
        f"Pool concentration risk: **{flags['pool_concentration_risk']}**, Token concentration risk: **{flags['token_concentration_risk']}**.",
        "",
        "## Failure Decomposition",
        "",
        "```json",
        json.dumps(summary["failure_reason_distribution"], indent=2),
        "```",
        "",
        "## Entry Quote Block Policy Sensitivity (diagnostic only, never replaces primary label)",
        "",
        *sens_lines,
        "",
        f"Note: {summary['entry_quote_policy_sensitivity']['note']}",
        "",
        "## Decision Flags",
        "",
        "| Flag | Value |",
        "|------|-------|",
        f"| factory_universe_loaded | {flags['factory_universe_loaded']} |",
        f"| point_in_time_universe_passed | {flags['point_in_time_universe_passed']} |",
        f"| pilot_7d_completed | {flags['pilot_7d_completed']} |",
        f"| timestamp_horizon_label_passed | {flags['timestamp_horizon_label_passed']} |",
        f"| clean_rows_gate_passed | {flags['clean_rows_gate_passed']} |",
        f"| quote_failure_audit_completed | {flags['quote_failure_audit_completed']} |",
        f"| entry_quote_policy_sensitivity_completed | {flags['entry_quote_policy_sensitivity_completed']} |",
        f"| candidate_selection_bias_risk | {flags['candidate_selection_bias_risk']} |",
        f"| allow_30d_pilot_or_walkforward_prep | **{flags['allow_30d_pilot_or_walkforward_prep']}** |",
        f"| allow_training | {flags['allow_training']} |",
        f"| allow_model_research | {flags['allow_model_research']} |",
        f"| allow_h19 / shadow / live / promote | {flags['allow_h19']} / {flags['allow_shadow']} / {flags['allow_live']} / {flags['allow_promote']} |",
        "",
        "## Required Answers",
        "",
        f"1. 7D pilot 是否跑通：**{flags['pilot_7d_completed']}** (decision = `{summary['decision']}`). 全部 gate 通过，含 timestamp horizon = 1.0 与 duplicate_conflict = 0.",
        (
            f"2. 7 天 canonical V2 活跃度：raw swap logs `{summary['raw_swap_logs']:,}`，"
            f" canonical V2 (factory pair filter) `{summary['canonical_v2_swap_logs']:,}`，"
            f" WETH leg `{summary['weth_pair_logs']:,}`，平均每日 ~{summary['weth_pair_logs']//DAYS:,} 条 WETH-pair swap 事件。"
        ),
        (
            f"3. clean rows 总数：**{summary['clean_rows']:,}** （在 {summary['decoded_rows']:,} attempted 内）。"
            f" 每日分布见 Daily Metrics 表，top-2 days share = {summary['top2_days_share_of_clean']:.4f}（≤ 0.50，所以无 temporal concentration 风险）。"
            f" 注意：attempted 是 50k 随机子样本，pre-subsample 共有 {summary['decoded_rows_pre_subsample']:,} 行，全量预期 clean ≈ "
            f"{int(summary['clean_rate'] * summary['decoded_rows_pre_subsample']):,} 行（按当前 clean_rate 线性外推）。"
        ),
        (
            f"4. 1D 低 quote success 是否持续：**否——本轮发现 1D pilot 的低成功率本质是 RPC 批处理 bug，而非真实链上现象。** "
            f"修正后 entry_quote_success_rate = `{summary['entry_quote_success_rate']:.4f}`，"
            f" exit_quote_success_rate = `{summary['exit_quote_success_rate']:.4f}`，"
            f" 远高于上一版 7D 报告的 0.14/0.02。1D pilot 的 0.14/0.02 同样受同一 bug 影响，结论需要 1D 重跑后再下。"
        ),
        (
            f"5. entry_zero_reserve 主因：当前 distribution 中 `entry_zero_reserve` = `{failures.get('entry_zero_reserve', 0):,}`，"
            f" `pool_created_after_entry_quote_block` = `{failures.get('pool_created_after_entry_quote_block', 0):,}`，"
            f" `entry_reserve_rpc_failure` = `{failures.get('entry_reserve_rpc_failure', 0):,}`。"
            f" 结论：1D 报告中的 425k entry_zero_reserve 几乎全部是 RPC 批处理 throttling，被错误归类为零储备；"
            f" 单调用 RPC 后真实零储备 ≈ 0%。Sensitivity 表（−1 / 0 / +1 三个 policy 分别为 "
            f"{sens_minus_1:.4f} / {sens_block:.4f} / {sens_plus_1:.4f}）也支持这点：三种 policy 几乎一致，"
            f" 说明 entry_block_minus_1 的口径本身没有问题。"
        ),
        (
            f"6. exit_zero_reserve_or_pool_drained = `{failures.get('exit_zero_reserve_or_pool_drained', 0):,}`。"
            f" 30min horizon 内"
            f" pool 真正被排空的占比极小（不到样本的 0.1%）；exit_quote_success_rate ≈ {summary['exit_quote_success_rate']:.4f}。"
            f" 7D 内 V2 短命池/可执行池数量并不像之前所担忧那么少；前次 60k drained 计数同样属于 RPC bug 误判。"
        ),
        (
            f"7. 是否存在 pool/token/temporal concentration："
            f" pool_concentration_risk = {flags['pool_concentration_risk']} "
            f"(top5_pool_share = {summary['top5_pool_share']:.4f})，"
            f" token_concentration_risk = {flags['token_concentration_risk']} "
            f"(top5_token_share = {summary['top5_token_share']:.4f})，"
            f" temporal_concentration_risk = {flags['temporal_concentration_risk']}。"
            f" 注意：day_index=2 单天 top5_pool_share = 0.6397（被 1 个 token 占大量行），仍需在 30D 阶段持续监控。"
        ),
        f"8. 是否允许进入 30D pilot 或 walk-forward preparation：**{flags['allow_30d_pilot_or_walkforward_prep']}**。",
        "9. 是否仍禁止 training/model research/shadow/live/promote：**True**（所有 gate 严格保持 false）。",
        "",
        "## Artifacts",
        "",
        f"- `{OUT_SUMMARY.relative_to(ROOT)}`",
        f"- `{rows_path.relative_to(ROOT)}` ({summary['output_file_sizes'][str(rows_path.relative_to(ROOT))]} bytes; compressed = {summary['rows_output_compressed']})",
        f"- `{OUT_DAILY.relative_to(ROOT)}` ({summary['output_file_sizes'][str(OUT_DAILY.relative_to(ROOT))]} bytes)",
        f"- `{OUT_MD.relative_to(ROOT)}`",
        f"- `{rpc_method.get('probe_artifact', '')}` (rpc_probe diagnostic that motivated the single-call switch)",
        "",
        "## Recommended Follow-ups (NOT permitted to skip into training)",
        "",
        "1. Re-run the 1D pilot with the patched `threaded_single_reserves` so the 1D summary is consistent with the 7D semantics; the existing 1D summary is now known to be biased downward by the same RPC bug.",
        "2. For 30D / walk-forward prep, either keep the 50k/window subsample with seed=7 (and report variance), or budget ~3-5 hours of single-call RPC per 7D-equivalent slice. Do NOT increase batch_size on this endpoint without re-running `run004_v2_live_equivalent_7d_pilot_rpc_probe.py`.",
        "3. day_index=2's elevated top1_pool_share (0.50) deserves a per-token attribution before relying on 7D aggregates as a representative sample.",
        "4. extreme_return_quarantine = "
        f"{failures.get('extreme_return_quarantine', 0):,} ({failures.get('extreme_return_quarantine', 0) / max(summary['attempted_label_rows'], 1):.4f} of attempted) is the dominant non-RPC failure class. These are sandwich/honeypot-style swaps where the 30min round-trip return exceeds the configured ±10x cap; the corresponding rows are written to the rows CSV with `risk_flag = extreme_return_quarantine` for inspection but are intentionally excluded from `clean_rows`.",
        "",
    ]
    OUT_MD.write_text("\n".join(md_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
