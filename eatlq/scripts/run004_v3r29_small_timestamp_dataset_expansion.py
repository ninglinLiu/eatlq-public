"""Run-004 Patch V3R-29 — small timestamp dataset expansion.

Research-only/read-only bounded expansion from local V3 candidate sources.
Target: 150-300 clean timestamp-horizon rows. No training, production labeler,
mainline, executor/runtime, promotion, or live behavior changes.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.v3_quoterv2_struct import STRUCT_SELECTOR, WETH_BASE, quote_exact_input_single_struct  # noqa: E402
from scripts.run004_v3r24r_v3_candidate_source_diversity_repair import (  # noqa: E402
    BudgetedRpc,
    _lower,
    _rpc_url,
    changes,
    choose_notional,
    field_availability,
    get_value,
    read_state,
    scan_sources,
    source_inventory,
)
from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float, as_int, distribution, top_shares  # noqa: E402
from scripts.run004_v3r28_timestamp_horizon_leakage_holdout_audit import (  # noqa: E402
    chronological_split,
    group_split,
    sign_flip_audit,
    temporal_adjacency,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

V3R28_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r28_timestamp_horizon_leakage_holdout_audit_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r29-small-timestamp-dataset-expansion.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_rows.csv"

SOURCE_RUN = "run004_v3r29_small_timestamp_dataset_expansion"
MIN_CLEAN_ROWS = 150
MAX_CLEAN_ROWS = 300
PRESELECT_LIMIT = 720
RPC_BUDGET = 9000
NEAR_TOTAL_LOSS = -0.95
FEE_ORDER = [100, 500, 3000, 10000]
WETH_DECIMALS = 10**18
ERROR_SECONDS_RISK_THRESHOLD = 4


def block_timestamp(rpc: BudgetedRpc, cache: dict[int, int], block: int) -> int:
    if block in cache:
        return cache[block]
    payload = rpc.call("eth_getBlockByNumber", [hex(block), False], timeout=25)
    if not payload or "timestamp" not in payload:
        raise RuntimeError(f"missing_block_timestamp:{block}")
    ts = int(str(payload["timestamp"]), 16)
    cache[block] = ts
    return ts


def resolve_timestamp_horizons(rpc: BudgetedRpc, cache: dict[int, int], entry_block: int) -> dict[str, Any]:
    entry_ts = block_timestamp(rpc, cache, entry_block)
    target_5m = entry_ts + 300
    target_30m = entry_ts + 1800

    def direct_or_advance(start_block: int, target_ts: int) -> tuple[int, int]:
        block = start_block
        ts = block_timestamp(rpc, cache, block)
        # Base normally lands exactly at +150/+900 blocks; this bounded repair
        # avoids binary search unless a sparse/missing block breaks that invariant.
        for _ in range(8):
            if ts >= target_ts:
                return block, ts
            block += 1
            ts = block_timestamp(rpc, cache, block)
        return block, ts

    exit_5m, ts_5m = direct_or_advance(entry_block + 150, target_5m)
    exit_30m, ts_30m = direct_or_advance(entry_block + 900, target_30m)
    return {
        "entry_timestamp": entry_ts,
        "target_timestamp_5m": target_5m,
        "exit_block_5m": exit_5m,
        "exit_timestamp_5m": ts_5m,
        "error_seconds_5m": ts_5m - target_5m,
        "target_timestamp_30m": target_30m,
        "exit_block_30m": exit_30m,
        "exit_timestamp_30m": ts_30m,
        "error_seconds_30m": ts_30m - target_30m,
    }


def cap_stratify(rows: list[dict[str, Any]], limit: int, cap: int) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(int(row["_fee"]), []).append(row)
    selected: list[dict[str, Any]] = []
    pool_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    while len(selected) < limit:
        progressed = False
        for fee in FEE_ORDER:
            bucket = buckets.get(fee, [])
            while bucket:
                row = bucket.pop(0)
                pool = _lower(row["pool_address"])
                token = row["_token_address"]
                if pool_counts[pool] >= cap or token_counts[token] >= cap:
                    continue
                selected.append(row)
                pool_counts[pool] += 1
                token_counts[token] += 1
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def preselect(buys: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    best: list[dict[str, Any]] = []
    cap_used = 0
    for cap in [3, 4, 5, 6, 8, 10, 12]:
        selected = cap_stratify(buys, PRESELECT_LIMIT, cap)
        if len(selected) > len(best):
            best = selected
            cap_used = cap
        if len(selected) >= PRESELECT_LIMIT:
            break
    pools = [_lower(r["pool_address"]) for r in best]
    tokens = [r["_token_address"] for r in best]
    p1, p5 = top_shares(pools)
    t1, t5 = top_shares(tokens)
    return best, {
        "preselected_rows": len(best),
        "preselected_pool_count": len(set(pools)),
        "preselected_token_count": len(set(tokens)),
        "preselected_fee_distribution": dict(Counter(str(r["_fee"]) for r in best)),
        "pool_token_cap_used": cap_used,
        "top1_pool_share": p1,
        "top5_pool_share": p5,
        "top1_token_share": t1,
        "top5_token_share": t5,
    }


def same_block_return(rpc: BudgetedRpc, token: str, amount_token: int, fee: int, entry_block: int, amount_in: int) -> float | None:
    q = quote_exact_input_single_struct(rpc, token, WETH_BASE, amount_token, fee, block_identifier=entry_block)
    if not q["ok"]:
        return None
    return round(int(q["amount_out"]) / amount_in - 1.0, 8)


def quote_timestamp_row(rpc: BudgetedRpc, row: dict[str, Any], notional: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    token = row["_token_address"]
    fee = int(row["_fee"])
    amount_in = int(notional["selected_notional_raw"])
    out: dict[str, Any] = {
        "timestamp_quote_entry_ok": False,
        "timestamp_quote_5m_ok": False,
        "timestamp_quote_30m_ok": False,
        "same_block_is_label": False,
        "failure_reason_5m": "",
        "failure_reason_30m": "",
    }
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, amount_in, fee, block_identifier=row["_entry_block"])
    if not entry["ok"]:
        reason = "entry_" + (entry.get("reason") or "quote_fail")
        out["failure_reason_5m"] = reason
        out["failure_reason_30m"] = reason
        return out
    entry_amount = int(entry["amount_out"])
    out["timestamp_quote_entry_ok"] = True
    out["timestamp_entry_amount_out"] = entry_amount
    out["same_block_sanity_return"] = same_block_return(rpc, token, entry_amount, fee, row["_entry_block"], amount_in)
    for label, block_field in [("5m", "exit_block_5m"), ("30m", "exit_block_30m")]:
        q = quote_exact_input_single_struct(
            rpc,
            token,
            WETH_BASE,
            entry_amount,
            fee,
            block_identifier=as_int(mapping[block_field]),
        )
        if q["ok"]:
            ret = round(int(q["amount_out"]) / amount_in - 1.0, 8)
            out[f"timestamp_quote_{label}_ok"] = True
            out[f"label_return_{label}"] = ret
            out[f"exit_amount_out_weth_{label}"] = int(q["amount_out"])
        else:
            out[f"failure_reason_{label}"] = label + "_" + (q.get("reason") or "quote_fail")
    return out


def build_clean_rows(rpc: BudgetedRpc, selected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    timestamp_cache: dict[int, int] = {}
    state_counts: Counter[str] = Counter()
    fail_reasons: Counter[str] = Counter()
    checked = 0
    state_changed_count = 0
    mapped_count = 0
    q5 = 0
    q30 = 0
    stop_reason = ""

    for row in selected:
        if len(clean) >= MAX_CLEAN_ROWS:
            stop_reason = "target_clean_row_cap_reached"
            break
        if rpc.calls >= rpc.budget:
            stop_reason = "rpc_budget_exhausted"
            break
        checked += 1
        try:
            notional = choose_notional(row)
            mapping = resolve_timestamp_horizons(rpc, timestamp_cache, as_int(row["_entry_block"]))
            mapped_count += 1
            s0 = read_state(rpc, row, as_int(row["_entry_block"]))
            s1 = read_state(rpc, row, as_int(mapping["exit_block_30m"]))
            ch = changes(s0, s1)
            for k, v in ch.items():
                if v:
                    state_counts[k] += 1
            state_changed = any(ch.values())
            if not state_changed:
                fail_reasons["no_state_change"] += 1
                continue
            state_changed_count += 1
            quote = quote_timestamp_row(rpc, row, notional, mapping)
        except Exception as exc:
            reason = "rpc_budget_exhausted" if "rpc_budget" in str(exc) else str(exc)[:80]
            fail_reasons[reason] += 1
            stop_reason = reason if reason == "rpc_budget_exhausted" else stop_reason
            if reason == "rpc_budget_exhausted":
                break
            continue

        if as_bool(quote.get("timestamp_quote_5m_ok")):
            q5 += 1
        if as_bool(quote.get("timestamp_quote_30m_ok")):
            q30 += 1
        r5 = as_float(quote.get("label_return_5m"))
        r30 = as_float(quote.get("label_return_30m"))
        ok_clean = (
            as_bool(quote.get("timestamp_quote_entry_ok"))
            and as_bool(quote.get("timestamp_quote_5m_ok"))
            and as_bool(quote.get("timestamp_quote_30m_ok"))
            and r5 is not None
            and r30 is not None
            and r5 > NEAR_TOTAL_LOSS
            and r30 > NEAR_TOTAL_LOSS
            and mapping["exit_timestamp_5m"] >= mapping["target_timestamp_5m"]
            and mapping["exit_timestamp_30m"] >= mapping["target_timestamp_30m"]
        )
        if not ok_clean:
            reason = quote.get("failure_reason_5m") or quote.get("failure_reason_30m") or "near_total_loss_or_mapping"
            fail_reasons[str(reason)] += 1
            continue
        clean.append(
            {
                "source_run": SOURCE_RUN,
                "source_path": row["_source_path"],
                "tx_hash": row["_tx_hash"],
                "log_index": row["_log_index"],
                "block_number": row["_entry_block"],
                "pool_address": _lower(row["pool_address"]),
                "token_address": row["_token_address"],
                "token0": _lower(get_value(row, "token0")),
                "token1": _lower(get_value(row, "token1")),
                "fee": row["_fee"],
                "amount0": get_value(row, "amount0"),
                "amount1": get_value(row, "amount1"),
                "token_in": _lower(get_value(row, "tokenIn", "token_in")),
                "token_out": _lower(get_value(row, "tokenOut", "token_out")),
                "observed_direction": "WETH_TO_TOKEN",
                **notional,
                "entry_block": row["_entry_block"],
                **mapping,
                "fixed_block_return_control": "",
                "state_changed": True,
                "state_change_fields": json.dumps(ch, sort_keys=True),
                **quote,
                "timestamp_horizon_primary_label": True,
                "trainable_candidate_flag": False,
                "research_clean_timestamp_candidate": True,
                "quote_selector": STRUCT_SELECTOR,
            }
        )
    return clean, {
        "candidate_checked_count": checked,
        "timestamp_mapped_count": mapped_count,
        "state_changed_count": state_changed_count,
        "state_changed_share": round(state_changed_count / max(1, checked), 4),
        "state_change_fields_distribution": dict(state_counts),
        "quote_success_5m": q5,
        "quote_success_30m": q30,
        "quote_success_rate_5m": round(q5 / max(1, state_changed_count), 4),
        "quote_success_rate_30m": round(q30 / max(1, state_changed_count), 4),
        "failure_reason_distribution": dict(fail_reasons),
        "timestamp_cache_blocks": len(timestamp_cache),
        "budget_stop_reason": stop_reason or rpc.stop_reason or "",
    }


def duplicate_count(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def share(vals: list[float], pred: Any) -> float:
    if not vals:
        return 0.0
    return round(sum(1 for v in vals if pred(v)) / len(vals), 4)


def direction_consistency(a_vals: list[float], b_vals: list[float]) -> tuple[float, int]:
    checked = same = flips = 0
    for a, b in zip(a_vals, b_vals):
        if a == 0 or b == 0:
            continue
        checked += 1
        same += int(a * b > 0)
        flips += int(a * b < 0)
    return round(same / max(1, checked), 4), flips


def dataset_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ret5 = [float(as_float(r["label_return_5m"]) or 0.0) for r in rows]
    ret30 = [float(as_float(r["label_return_30m"]) or 0.0) for r in rows]
    pools = [r["pool_address"] for r in rows]
    tokens = [r["token_address"] for r in rows]
    p1, p5 = top_shares(pools)
    t1, t5 = top_shares(tokens)
    consistency, flips = direction_consistency(ret5, ret30)
    err5 = [float(r["error_seconds_5m"]) for r in rows]
    err30 = [float(r["error_seconds_30m"]) for r in rows]
    return {
        "clean_rows_count": len(rows),
        "valid_5m_label_count": sum(1 for r in rows if as_bool(r["timestamp_quote_5m_ok"]) and as_float(r["label_return_5m"]) is not None),
        "valid_30m_label_count": sum(1 for r in rows if as_bool(r["timestamp_quote_30m_ok"]) and as_float(r["label_return_30m"]) is not None),
        "pool_count": len(set(pools)),
        "token_count": len(set(tokens)),
        "fee_distribution": dict(Counter(str(as_int(r["fee"])) for r in rows)),
        "source_file_distribution": dict(Counter(r["source_path"] for r in rows)),
        "top1_pool_share": p1,
        "top5_pool_share": p5,
        "top1_token_share": t1,
        "top5_token_share": t5,
        "return_5m_distribution": distribution(ret5),
        "return_30m_distribution": distribution(ret30),
        "near_total_loss_share_5m": share(ret5, lambda x: x <= NEAR_TOTAL_LOSS),
        "near_total_loss_share_30m": share(ret30, lambda x: x <= NEAR_TOTAL_LOSS),
        "positive_return_share_5m": share(ret5, lambda x: x > 0),
        "positive_return_share_30m": share(ret30, lambda x: x > 0),
        "5m_vs_30m_direction_consistency": consistency,
        "sign_flip_count_5m_vs_30m": flips,
        "duplicate_conflict_count": duplicate_count(rows),
        "timestamp_error_seconds_5m_distribution": distribution(err5),
        "timestamp_error_seconds_30m_distribution": distribution(err30),
        "timestamp_error_seconds_risk": max(err5 + err30, default=999999) > ERROR_SECONDS_RISK_THRESHOLD,
    }


def leakage_and_holdout(rows: list[dict[str, Any]]) -> dict[str, Any]:
    chronological = chronological_split(rows)
    pool_split = group_split(rows, "pool_address", "group_by_pool")
    token_split = group_split(rows, "token_address", "group_by_token")
    source_dist = Counter(r["source_path"] for r in rows)
    fee_dist = Counter(str(as_int(r["fee"])) for r in rows)
    labels_ok = all(as_bool(r["timestamp_quote_5m_ok"]) and as_bool(r["timestamp_quote_30m_ok"]) for r in rows)
    order_ok = all(as_int(r["entry_block"]) < as_int(r["exit_block_5m"]) < as_int(r["exit_block_30m"]) for r in rows)
    timestamp_ok = all(as_int(r["entry_timestamp"]) > 0 and as_int(r["exit_timestamp_5m"]) >= as_int(r["target_timestamp_5m"]) and as_int(r["exit_timestamp_30m"]) >= as_int(r["target_timestamp_30m"]) for r in rows)
    leakage_passed = (
        labels_ok
        and order_ok
        and timestamp_ok
        and duplicate_count(rows) == 0
        and (max(source_dist.values()) / max(1, len(rows))) <= 0.75
        and (max(fee_dist.values()) / max(1, len(rows))) <= 0.75
    )
    return {
        "same_block_sanity_not_label": True,
        "fixed_block_not_primary_label": True,
        "timestamp_horizon_primary_label": True,
        "labels_from_timestamp_horizon": labels_ok,
        "entry_before_5m_before_30m": order_ok,
        "timestamp_missing_or_mapping_fallback": not timestamp_ok,
        "source_top_share": round(max(source_dist.values()) / max(1, len(rows)), 4) if rows else 0.0,
        "fee_top_share": round(max(fee_dist.values()) / max(1, len(rows)), 4) if rows else 0.0,
        "temporal_adjacency": temporal_adjacency(rows),
        "leakage_audit_passed": leakage_passed,
        "holdout_splits": {
            "chronological": chronological,
            "group_by_pool": pool_split,
            "group_by_token": token_split,
        },
        "holdout_audit_passed": leakage_passed and chronological["split_feasible"] and pool_split["split_feasible"] and token_split["split_feasible"],
    }


def assign_splits(rows: list[dict[str, Any]], holdout: dict[str, Any]) -> list[dict[str, Any]]:
    chronological = holdout["holdout_splits"]["chronological"]
    pool_split = holdout["holdout_splits"]["group_by_pool"]
    token_split = holdout["holdout_splits"]["group_by_token"]
    ordered = sorted(rows, key=lambda r: (as_int(r["entry_timestamp"]), as_int(r["entry_block"])))
    chronological_holdout = {(r["tx_hash"], r["log_index"]) for r in ordered[-chronological["holdout_rows"]:]}
    pool_holdout = set(pool_split.get("holdout_groups", []))
    token_holdout = set(token_split.get("holdout_groups", []))
    out = []
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        r5 = as_float(row["label_return_5m"]) or 0.0
        r30 = as_float(row["label_return_30m"]) or 0.0
        out.append(
            {
                **row,
                "chronological_split": "holdout" if key in chronological_holdout else "train",
                "pool_split": "holdout" if row["pool_address"] in pool_holdout else "train",
                "token_split": "holdout" if row["token_address"] in token_holdout else "train",
                "sign_flip_5m_30m": bool(r5 and r30 and r5 * r30 < 0),
            }
        )
    return out


def write_rows(rows: list[dict[str, Any]]) -> None:
    fields = [
        "source_run",
        "source_path",
        "tx_hash",
        "log_index",
        "pool_address",
        "token_address",
        "token0",
        "token1",
        "fee",
        "observed_direction",
        "selected_notional_weth",
        "entry_block",
        "entry_timestamp",
        "exit_block_5m",
        "exit_timestamp_5m",
        "exit_block_30m",
        "exit_timestamp_30m",
        "state_changed",
        "same_block_sanity_return",
        "same_block_is_label",
        "timestamp_horizon_primary_label",
        "timestamp_quote_5m_ok",
        "label_return_5m",
        "timestamp_quote_30m_ok",
        "label_return_30m",
        "fixed_block_return_control",
        "chronological_split",
        "pool_split",
        "token_split",
        "sign_flip_5m_30m",
        "trainable_candidate_flag",
        "research_clean_timestamp_candidate",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:MAX_CLEAN_ROWS]:
            writer.writerow({k: row.get(k, "") for k in fields})


def render_md(summary: dict[str, Any]) -> str:
    flags = summary["decision_flags"]
    m = summary["dataset_metrics"]
    h = summary["leakage_holdout"]
    return f"""# Run-004 Patch V3R-29 — Small Timestamp Dataset Expansion

Status: **{'PASSED' if flags['v3r29_small_timestamp_expansion_passed'] else 'FAILED'}**. Research-only/read-only. No training, model promotion, mainline, executor/runtime, or live changes.

## Dataset

- clean rows: **{m['clean_rows_count']}**
- valid 5m / 30m labels: **{m['valid_5m_label_count']} / {m['valid_30m_label_count']}**
- pool/token count: **{m['pool_count']} / {m['token_count']}**
- top1/top5 pool: **{m['top1_pool_share']} / {m['top5_pool_share']}**
- top1/top5 token: **{m['top1_token_share']} / {m['top5_token_share']}**
- source top share / fee top share: **{h['source_top_share']} / {h['fee_top_share']}**

## Holdout

- chronological: `{h['holdout_splits']['chronological']}`
- group-by-pool: `{h['holdout_splits']['group_by_pool']}`
- group-by-token: `{h['holdout_splits']['group_by_token']}`

## Decision Flags

- `v3r29_completed`: `{str(flags['v3r29_completed']).lower()}`
- `small_timestamp_dataset_expansion_done`: `{str(flags['small_timestamp_dataset_expansion_done']).lower()}`
- `clean_rows_count`: `{flags['clean_rows_count']}`
- `target_clean_rows_met`: `{str(flags['target_clean_rows_met']).lower()}`
- `quote_success_rate_5m`: `{flags['quote_success_rate_5m']}`
- `quote_success_rate_30m`: `{flags['quote_success_rate_30m']}`
- `diversity_gate_passed`: `{str(flags['diversity_gate_passed']).lower()}`
- `duplicate_conflict_count`: `{flags['duplicate_conflict_count']}`
- `same_block_sanity_not_label`: `{str(flags['same_block_sanity_not_label']).lower()}`
- `timestamp_horizon_primary_label`: `{str(flags['timestamp_horizon_primary_label']).lower()}`
- `chronological_split_feasible`: `{str(flags['chronological_split_feasible']).lower()}`
- `pool_disjoint_split_feasible`: `{str(flags['pool_disjoint_split_feasible']).lower()}`
- `token_disjoint_split_feasible`: `{str(flags['token_disjoint_split_feasible']).lower()}`
- `v3r29_small_timestamp_expansion_passed`: `{str(flags['v3r29_small_timestamp_expansion_passed']).lower()}`
- `allow_model_research`: `{str(flags['allow_model_research']).lower()}`
- `allow_larger_expansion`: `{str(flags['allow_larger_expansion']).lower()}`
- `allow_mainline`: `{str(flags['allow_mainline']).lower()}`
- `allow_live`: `{str(flags['allow_live']).lower()}`
"""


def main() -> None:
    started = time.time()
    prior = json.loads(V3R28_SUMMARY.read_text(encoding="utf-8"))
    if not prior["decision_flags"]["allow_v3r29_small_timestamp_dataset_expansion"]:
        raise RuntimeError("V3R-28 did not allow V3R-29 small timestamp dataset expansion")

    inv = source_inventory()
    fa = field_availability(inv)
    buys, scan = scan_sources(inv) if fa["required_fields_available"] else ([], {})
    selected, pre = preselect(buys)
    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    rpc.inner.delay_s = 0.01
    clean_rows, build = build_clean_rows(rpc, selected)
    metrics = dataset_metrics(clean_rows)
    holdout = leakage_and_holdout(clean_rows)
    stability = sign_flip_audit(clean_rows)

    diversity_gate = (
        metrics["clean_rows_count"] >= MIN_CLEAN_ROWS
        and metrics["clean_rows_count"] <= MAX_CLEAN_ROWS
        and metrics["valid_5m_label_count"] == metrics["clean_rows_count"]
        and metrics["valid_30m_label_count"] == metrics["clean_rows_count"]
        and metrics["near_total_loss_share_5m"] <= 0.10
        and metrics["near_total_loss_share_30m"] <= 0.10
        and metrics["top1_pool_share"] <= 0.20
        and metrics["top5_pool_share"] <= 0.55
        and metrics["top1_token_share"] <= 0.20
        and metrics["top5_token_share"] <= 0.55
        and metrics["duplicate_conflict_count"] == 0
        and not metrics["timestamp_error_seconds_risk"]
    )
    flags = {
        "v3r29_completed": True,
        "small_timestamp_dataset_expansion_done": True,
        "clean_rows_count": metrics["clean_rows_count"],
        "target_clean_rows_met": MIN_CLEAN_ROWS <= metrics["clean_rows_count"] <= MAX_CLEAN_ROWS,
        "valid_5m_label_count": metrics["valid_5m_label_count"],
        "valid_30m_label_count": metrics["valid_30m_label_count"],
        "quote_success_rate_5m": build["quote_success_rate_5m"],
        "quote_success_rate_30m": build["quote_success_rate_30m"],
        "diversity_gate_passed": diversity_gate,
        "duplicate_conflict_count": metrics["duplicate_conflict_count"],
        "same_block_sanity_not_label": holdout["same_block_sanity_not_label"],
        "fixed_block_not_primary_label": holdout["fixed_block_not_primary_label"],
        "timestamp_horizon_primary_label": holdout["timestamp_horizon_primary_label"],
        "leakage_audit_passed": holdout["leakage_audit_passed"],
        "chronological_split_feasible": holdout["holdout_splits"]["chronological"]["split_feasible"],
        "pool_disjoint_split_feasible": holdout["holdout_splits"]["group_by_pool"]["split_feasible"],
        "token_disjoint_split_feasible": holdout["holdout_splits"]["group_by_token"]["split_feasible"],
        "label_stability_passed": stability["label_stability_passed"],
        "sign_flip_count_5m_30m": stability["sign_flip_count"],
        "sign_flip_share_5m_30m": stability["sign_flip_share"],
        "v3r29_small_timestamp_expansion_passed": diversity_gate and holdout["holdout_audit_passed"] and rpc.calls <= rpc.budget,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    annotated = assign_splits(clean_rows, holdout)
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r28_summary": str(V3R28_SUMMARY.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
            "source_builder": "scripts/run004_v3r24r_v3_candidate_source_diversity_repair.py",
            "holdout_audit": "scripts/run004_v3r28_timestamp_horizon_leakage_holdout_audit.py",
        },
        "source_inventory": inv,
        "field_availability": fa,
        "expanded_source_scan": scan,
        "preselection": pre,
        "build_metrics": build,
        "dataset_metrics": metrics,
        "leakage_holdout": holdout,
        "label_stability": stability,
        "rpc_budget": {
            "rpc_calls_total": rpc.calls,
            "rpc_budget": rpc.budget,
            "rpc_budget_respected": rpc.calls <= rpc.budget,
            "budget_stop_reason": build["budget_stop_reason"],
        },
        "output_row_cap": {
            "output_rows": len(annotated),
            "row_cap": MAX_CLEAN_ROWS,
            "output_row_cap_respected": len(annotated) <= MAX_CLEAN_ROWS,
        },
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - started, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(annotated)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        {
            "clean": metrics["clean_rows_count"],
            "checked": build["candidate_checked_count"],
            "q5": build["quote_success_rate_5m"],
            "q30": build["quote_success_rate_30m"],
            "rpc": rpc.calls,
            "passed": flags["v3r29_small_timestamp_expansion_passed"],
        }
    )


if __name__ == "__main__":
    main()
