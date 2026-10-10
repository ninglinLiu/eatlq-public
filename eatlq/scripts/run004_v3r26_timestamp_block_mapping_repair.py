"""Run-004 Patch V3R-26 — timestamp/block mapping repair.

Research-only/read-only. Uses only the V3R-25 clean prototype rows and maps
their fixed-block pilot observations to deterministic 5m/30m timestamp horizons.
No source expansion, training, production writes, mainline/runtime changes, or live behavior.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.v3_quoterv2_struct import WETH_BASE, quote_exact_input_single_struct  # noqa: E402
from scripts.run004_v3r24r_v3_candidate_source_diversity_repair import BudgetedRpc, _rpc_url  # noqa: E402
from scripts.run004_v3r25_clean_prototype_validation import (  # noqa: E402
    as_bool,
    as_float,
    as_int,
    distribution,
    weth_to_raw,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_ROWS = ROOT / "mvp/diagnostics/run004_v3r25_clean_prototype_validation_rows.csv"
SOURCE_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r25_clean_prototype_validation_summary.json"
SOURCE_MD = ROOT / "mvp/runs/run-004-patch-v3r25-clean-prototype-validation.md"
OUT_SCRIPT = "scripts/run004_v3r26_timestamp_block_mapping_repair.py"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r26_timestamp_block_mapping_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r26_timestamp_block_mapping_rows.csv"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r26-timestamp-block-mapping-repair.md"

CHAIN_ID = 8453
RPC_BUDGET = 1500
QUOTE_LIMIT = 20
ERROR_SECONDS_RISK_THRESHOLD = 30
QUOTE_PASS_THRESHOLD = 0.90


def load_rows() -> list[dict[str, Any]]:
    with SOURCE_ROWS.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_prior_cache() -> dict[str, int]:
    if not OUT_JSON.exists():
        return {}
    try:
        data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.get("checkpoint", {}).get("block_timestamp_cache", {}).items()}
    except Exception:
        return {}


def save_checkpoint(cache: dict[str, int], rows_done: int, rpc_calls: int) -> None:
    payload = {
        "run": "run004_v3r26_timestamp_block_mapping_repair",
        "checkpoint": {
            "rows_done": rows_done,
            "rpc_calls": rpc_calls,
            "block_timestamp_cache": cache,
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def block_timestamp(rpc: BudgetedRpc, cache: dict[str, int], block: int) -> int:
    key = str(block)
    if key in cache:
        return cache[key]
    payload = rpc.call("eth_getBlockByNumber", [hex(block), False], timeout=25)
    if not payload or "timestamp" not in payload:
        raise RuntimeError(f"missing_block_timestamp:{block}")
    ts = int(str(payload["timestamp"]), 16)
    cache[key] = ts
    return ts


def first_block_at_or_after(rpc: BudgetedRpc, cache: dict[str, int], entry_block: int, entry_ts: int, target_ts: int) -> tuple[int, int]:
    low = entry_block
    high = entry_block + max(1, (target_ts - entry_ts) // 2)
    high_ts = block_timestamp(rpc, cache, high)
    step = 64
    while high_ts < target_ts:
        low = high
        high += step
        step *= 2
        high_ts = block_timestamp(rpc, cache, high)
    while low + 1 < high:
        mid = (low + high) // 2
        mid_ts = block_timestamp(rpc, cache, mid)
        if mid_ts >= target_ts:
            high = mid
            high_ts = mid_ts
        else:
            low = mid
    return high, high_ts


def map_rows(rpc: BudgetedRpc, rows: list[dict[str, Any]], cache: dict[str, int]) -> list[dict[str, Any]]:
    out = []
    for idx, row in enumerate(rows, start=1):
        entry_block = as_int(row["entry_block"])
        entry_ts = block_timestamp(rpc, cache, entry_block)
        target_5m = entry_ts + 300
        target_30m = entry_ts + 1800
        exit_5m, exit_ts_5m = first_block_at_or_after(rpc, cache, entry_block, entry_ts, target_5m)
        exit_30m, exit_ts_30m = first_block_at_or_after(rpc, cache, entry_block, entry_ts, target_30m)
        out.append(
            {
                **row,
                "entry_timestamp": entry_ts,
                "target_timestamp_5m": target_5m,
                "exit_block_5m": exit_5m,
                "exit_timestamp_5m": exit_ts_5m,
                "error_seconds_5m": exit_ts_5m - target_5m,
                "target_timestamp_30m": target_30m,
                "exit_block_30m": exit_30m,
                "exit_timestamp_30m": exit_ts_30m,
                "error_seconds_30m": exit_ts_30m - target_30m,
            }
        )
        if idx % 10 == 0:
            save_checkpoint(cache, idx, rpc.calls)
    return out


def quote_timestamp_horizons(rpc: BudgetedRpc, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    details: dict[tuple[str, str], dict[str, Any]] = {}
    attempted = min(QUOTE_LIMIT, len(rows))
    success_5m = 0
    success_30m = 0
    fail_reasons: Counter[str] = Counter()
    direction_same_5m = 0
    direction_same_30m = 0
    direction_checked_5m = 0
    direction_checked_30m = 0
    for row in rows[:QUOTE_LIMIT]:
        key = (row["tx_hash"], row["log_index"])
        token = row["token_address"].lower()
        amount_in = weth_to_raw(row["selected_notional_weth"])
        fee = as_int(row["fee"])
        fixed_return = as_float(row.get("label_return"))
        detail: dict[str, Any] = {}
        entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, amount_in, fee, block_identifier=as_int(row["entry_block"]))
        if not entry["ok"]:
            reason = "entry_" + (entry.get("reason") or "quote_fail")
            fail_reasons[reason] += 2
            details[key] = {"timestamp_quote_entry_ok": False, "timestamp_quote_failure": reason}
            continue
        entry_amount = int(entry["amount_out"])
        detail["timestamp_quote_entry_ok"] = True
        detail["timestamp_entry_amount_out"] = entry_amount
        for label, block_field in [("5m", "exit_block_5m"), ("30m", "exit_block_30m")]:
            q = quote_exact_input_single_struct(
                rpc,
                token,
                WETH_BASE,
                entry_amount,
                fee,
                block_identifier=as_int(row[block_field]),
            )
            if q["ok"]:
                ret = round(int(q["amount_out"]) / amount_in - 1.0, 8)
                detail[f"timestamp_quote_{label}_ok"] = True
                detail[f"return_{label}"] = ret
                detail[f"exit_amount_out_weth_{label}"] = int(q["amount_out"])
                if label == "5m":
                    success_5m += 1
                    direction_checked_5m += int(fixed_return is not None and fixed_return != 0 and ret != 0)
                    direction_same_5m += int(fixed_return is not None and fixed_return * ret > 0)
                else:
                    success_30m += 1
                    direction_checked_30m += int(fixed_return is not None and fixed_return != 0 and ret != 0)
                    direction_same_30m += int(fixed_return is not None and fixed_return * ret > 0)
            else:
                reason = f"{label}_" + (q.get("reason") or "quote_fail")
                detail[f"timestamp_quote_{label}_ok"] = False
                detail[f"timestamp_quote_{label}_failure"] = reason
                fail_reasons[reason] += 1
        details[key] = detail
    rate_5m = round(success_5m / max(1, attempted), 4)
    rate_30m = round(success_30m / max(1, attempted), 4)
    return {
        "quote_attempted_rows": attempted,
        "quote_success_5m": success_5m,
        "quote_success_30m": success_30m,
        "quote_failed_5m": attempted - success_5m,
        "quote_failed_30m": attempted - success_30m,
        "quote_success_rate_5m": rate_5m,
        "quote_success_rate_30m": rate_30m,
        "failure_reason_distribution": dict(fail_reasons),
        "fixed_vs_5m_direction_consistency": round(direction_same_5m / max(1, direction_checked_5m), 4),
        "fixed_vs_30m_direction_consistency": round(direction_same_30m / max(1, direction_checked_30m), 4),
        "timestamp_horizon_quote_passed": rate_5m >= QUOTE_PASS_THRESHOLD and rate_30m >= QUOTE_PASS_THRESHOLD,
    }, details


def duplicate_conflicts(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def resolution_metrics(rows: list[dict[str, Any]], source_count: int) -> dict[str, Any]:
    err_5m = [float(r["error_seconds_5m"]) for r in rows]
    err_30m = [float(r["error_seconds_30m"]) for r in rows]
    return {
        "input_rows": source_count,
        "mapped_rows": len(rows),
        "entry_timestamp_resolution_success_rate": round(sum(1 for r in rows if r.get("entry_timestamp")) / max(1, source_count), 4),
        "horizon_5m_resolution_success_rate": round(sum(1 for r in rows if r.get("exit_block_5m") and r["exit_timestamp_5m"] >= r["target_timestamp_5m"]) / max(1, source_count), 4),
        "horizon_30m_resolution_success_rate": round(sum(1 for r in rows if r.get("exit_block_30m") and r["exit_timestamp_30m"] >= r["target_timestamp_30m"]) / max(1, source_count), 4),
        "error_seconds_5m_distribution": distribution(err_5m),
        "error_seconds_30m_distribution": distribution(err_30m),
        "error_seconds_risk": max(err_5m + err_30m, default=999999) > ERROR_SECONDS_RISK_THRESHOLD,
    }


def write_rows(rows: list[dict[str, Any]], quote_details: dict[tuple[str, str], dict[str, Any]]) -> None:
    fields = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "entry_timestamp",
        "target_timestamp_5m",
        "exit_block_5m",
        "exit_timestamp_5m",
        "error_seconds_5m",
        "target_timestamp_30m",
        "exit_block_30m",
        "exit_timestamp_30m",
        "error_seconds_30m",
        "exit_block",
        "label_return",
        "same_block_sanity_return",
        "timestamp_quote_entry_ok",
        "timestamp_entry_amount_out",
        "timestamp_quote_5m_ok",
        "return_5m",
        "exit_amount_out_weth_5m",
        "timestamp_quote_30m_ok",
        "return_30m",
        "exit_amount_out_weth_30m",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            key = (row["tx_hash"], row["log_index"])
            merged = {**row, **quote_details.get(key, {})}
            writer.writerow({k: merged.get(k, "") for k in fields})


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    result = "PASSED_TIMESTAMP_MAPPING" if f["timestamp_mapping_passed"] and f["timestamp_horizon_quote_passed"] else (
        "FAILED_TIMESTAMP_MAPPING" if not f["timestamp_mapping_passed"] else "FAILED_TIMESTAMP_HORIZON_QUOTE"
    )
    return f"""# Run-004 Patch V3R-26 — Timestamp/Block Mapping Repair

Status: **{result}**. Research-only/read-only. No source expansion, training, mainline, production runtime, or live.

## Timestamp Mapping

- input rows: **{summary['mapping']['input_rows']}**
- mapped rows: **{summary['mapping']['mapped_rows']}**
- entry timestamp success: **{summary['mapping']['entry_timestamp_resolution_success_rate']}**
- 5m / 30m resolution success: **{summary['mapping']['horizon_5m_resolution_success_rate']} / {summary['mapping']['horizon_30m_resolution_success_rate']}**
- 5m error seconds: `{summary['mapping']['error_seconds_5m_distribution']}`
- 30m error seconds: `{summary['mapping']['error_seconds_30m_distribution']}`
- error_seconds_risk: **{summary['mapping']['error_seconds_risk']}**

## Timestamp Horizon Quote

- quote attempted rows: **{summary['timestamp_horizon_quote']['quote_attempted_rows']}**
- 5m success/failed: **{summary['timestamp_horizon_quote']['quote_success_5m']} / {summary['timestamp_horizon_quote']['quote_failed_5m']}**
- 30m success/failed: **{summary['timestamp_horizon_quote']['quote_success_30m']} / {summary['timestamp_horizon_quote']['quote_failed_30m']}**
- fixed vs 5m/30m direction consistency: **{summary['timestamp_horizon_quote']['fixed_vs_5m_direction_consistency']} / {summary['timestamp_horizon_quote']['fixed_vs_30m_direction_consistency']}**

## Required Answers

1. timestamp missing fixed: **{f['timestamp_mapping_passed']}**.
2. all 54 clean rows mapped entry/5m/30m: **{f['entry_timestamp_resolved'] and f['horizon_5m_resolved'] and f['horizon_30m_resolved']}**.
3. error seconds are reported above.
4. timestamp horizon quote passed: **{f['timestamp_horizon_quote_passed']}**.
5. fixed-block and timestamp-horizon returns are separate columns; direction consistency reported above.
6. allow V3R-27 bounded timestamp-horizon prototype expansion: **{f['allow_v3r27_bounded_expansion']}**.
7. model/mainline/live remain forbidden: **False / False / False**.

## Decision Flags

- `timestamp_mapping_passed`: `{str(f['timestamp_mapping_passed']).lower()}`
- `entry_timestamp_resolved`: `{str(f['entry_timestamp_resolved']).lower()}`
- `horizon_5m_resolved`: `{str(f['horizon_5m_resolved']).lower()}`
- `horizon_30m_resolved`: `{str(f['horizon_30m_resolved']).lower()}`
- `timestamp_horizon_quote_passed`: `{str(f['timestamp_horizon_quote_passed']).lower()}`
- `fixed_block_no_longer_primary_label`: `{str(f['fixed_block_no_longer_primary_label']).lower()}`
- `allow_v3r27_bounded_expansion`: `{str(f['allow_v3r27_bounded_expansion']).lower()}`
- `allow_model_research`: `{str(f['allow_model_research']).lower()}`
- `allow_mainline`: `{str(f['allow_mainline']).lower()}`
- `allow_live`: `{str(f['allow_live']).lower()}`
"""


def main() -> None:
    started = time.time()
    prior_summary = json.loads(SOURCE_SUMMARY.read_text(encoding="utf-8"))
    _ = SOURCE_MD.read_text(encoding="utf-8")
    rows = load_rows()
    expected = int(prior_summary["decision_flags"]["clean_rows_count"])
    cache = load_prior_cache()
    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    mapped = map_rows(rpc, rows, cache)
    quote_metrics, quote_details = quote_timestamp_horizons(rpc, mapped)
    mapping = resolution_metrics(mapped, len(rows))
    dup = duplicate_conflicts(rows)
    no_expansion = (
        len({r["pool_address"] for r in rows}) == prior_summary["clean_prototype"]["clean_pool_count"]
        and len({r["token_address"] for r in rows}) == prior_summary["clean_prototype"]["clean_token_count"]
        and Counter(r["source_path"] for r in rows) == Counter(prior_summary["clean_prototype"]["clean_source_file_distribution"])
    )
    entry_ok = mapping["input_rows"] == expected and mapping["entry_timestamp_resolution_success_rate"] == 1.0
    h5_ok = mapping["horizon_5m_resolution_success_rate"] == 1.0
    h30_ok = mapping["horizon_30m_resolution_success_rate"] == 1.0
    mapping_passed = (
        entry_ok
        and h5_ok
        and h30_ok
        and not mapping["error_seconds_risk"]
        and dup == 0
        and no_expansion
        and len(rows) <= 100
    )
    timestamp_quote_passed = bool(quote_metrics["timestamp_horizon_quote_passed"])
    flags = {
        "timestamp_mapping_passed": mapping_passed,
        "entry_timestamp_resolved": entry_ok,
        "horizon_5m_resolved": h5_ok,
        "horizon_30m_resolved": h30_ok,
        "timestamp_horizon_quote_passed": timestamp_quote_passed,
        "fixed_block_no_longer_primary_label": mapping_passed and timestamp_quote_passed,
        "allow_v3r27_bounded_expansion": mapping_passed and timestamp_quote_passed,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": "run004_v3r26_timestamp_block_mapping_repair",
        "research_only": True,
        "chain_id": CHAIN_ID,
        "inputs": {
            "v3r25_rows": str(SOURCE_ROWS.relative_to(ROOT)),
            "v3r25_summary": str(SOURCE_SUMMARY.relative_to(ROOT)),
            "v3r25_report": str(SOURCE_MD.relative_to(ROOT)),
        },
        "artifacts": {
            "script": OUT_SCRIPT,
            "summary": str(OUT_JSON.relative_to(ROOT)),
            "rows": str(OUT_CSV.relative_to(ROOT)),
            "report": str(OUT_MD.relative_to(ROOT)),
        },
        "source_scan_not_expanded": True,
        "no_new_pool_token_source_expansion": no_expansion,
        "duplicate_conflict_count": dup,
        "mapping": mapping,
        "timestamp_horizon_quote": quote_metrics,
        "fixed_block_return_kept_as_control": True,
        "rpc_budget": {
            "rpc_calls": rpc.calls,
            "rpc_budget": RPC_BUDGET,
            "rpc_budget_respected": rpc.calls <= RPC_BUDGET,
            "methods": ["eth_getBlockByNumber", "eth_call"],
        },
        "output_row_cap": {
            "output_rows": len(mapped),
            "row_cap": 100,
            "output_row_cap_respected": len(mapped) <= 100,
        },
        "decision_flags": flags,
        "checkpoint": {
            "block_timestamp_cache_count": len(cache),
            "block_timestamp_cache": cache,
        },
        "runtime_seconds": round(time.time() - started, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(mapped, quote_details)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    result = "PASSED_TIMESTAMP_MAPPING" if flags["timestamp_mapping_passed"] and flags["timestamp_horizon_quote_passed"] else (
        "FAILED_TIMESTAMP_MAPPING" if not flags["timestamp_mapping_passed"] else "FAILED_TIMESTAMP_HORIZON_QUOTE"
    )
    print(
        {
            "result": result,
            "input_rows": len(rows),
            "mapped_rows": len(mapped),
            "quote_5m": quote_metrics["quote_success_rate_5m"],
            "quote_30m": quote_metrics["quote_success_rate_30m"],
            "rpc": rpc.calls,
        }
    )


if __name__ == "__main__":
    main()
