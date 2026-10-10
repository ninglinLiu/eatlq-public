"""Run-004 Patch V3R-27 — bounded timestamp-horizon validation.

Research-only/read-only. Validates all V3R-26 mapped clean rows with 5m/30m
timestamp-horizon quotes. No source expansion, training, promotion, mainline,
production labeler, executor/runtime, or live behavior changes.
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
    top_shares,
    weth_to_raw,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r26_timestamp_block_mapping_summary.json"
SOURCE_ROWS = ROOT / "mvp/diagnostics/run004_v3r26_timestamp_block_mapping_rows.csv"
V3R24R_ROWS = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_rows.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r27-bounded-timestamp-horizon-validation.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r27_bounded_timestamp_horizon_validation_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r27_bounded_timestamp_horizon_validation_rows.csv"

RPC_BUDGET = 1500
NEAR_TOTAL_LOSS = -0.95


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def duplicate_count(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def quote_row(rpc: BudgetedRpc, row: dict[str, Any]) -> dict[str, Any]:
    token = row["token_address"].lower()
    amount_in = weth_to_raw(row["selected_notional_weth"])
    fee = as_int(row["fee"])
    out: dict[str, Any] = {
        "timestamp_quote_entry_ok": False,
        "timestamp_quote_5m_ok": False,
        "timestamp_quote_30m_ok": False,
        "label_return_5m": None,
        "label_return_30m": None,
        "failure_reason_5m": "",
        "failure_reason_30m": "",
    }
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, amount_in, fee, block_identifier=as_int(row["entry_block"]))
    if not entry["ok"]:
        reason = "entry_" + (entry.get("reason") or "quote_fail")
        out["failure_reason_5m"] = reason
        out["failure_reason_30m"] = reason
        return out
    entry_amount = int(entry["amount_out"])
    out["timestamp_quote_entry_ok"] = True
    out["timestamp_entry_amount_out"] = entry_amount
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
            out[f"timestamp_quote_{label}_ok"] = True
            out[f"label_return_{label}"] = ret
            out[f"exit_amount_out_weth_{label}"] = int(q["amount_out"])
        else:
            out[f"failure_reason_{label}"] = label + "_" + (q.get("reason") or "quote_fail")
    return out


def validate_quotes(rpc: BudgetedRpc, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if rpc.calls >= rpc.budget:
            break
        quoted = quote_row(rpc, row)
        out.append({**row, **quoted})
    return out


def enrich_from_v3r24r(rows: list[dict[str, Any]], v3r24r: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for row in rows:
        prior = v3r24r.get((row["tx_hash"], row["log_index"]), {})
        merged = dict(row)
        for field in ["selected_notional_weth", "same_block_is_label", "entry_amount_out"]:
            if field not in merged or str(merged.get(field) or "").strip() == "":
                merged[field] = prior.get(field, "")
        enriched.append(merged)
    return enriched


def share(vals: list[float], predicate: Any) -> float:
    if not vals:
        return 0.0
    return round(sum(1 for v in vals if predicate(v)) / len(vals), 4)


def direction_consistency(a_vals: list[float], b_vals: list[float]) -> tuple[float, int]:
    checked = 0
    same = 0
    flips = 0
    for a, b in zip(a_vals, b_vals):
        if a == 0 or b == 0:
            continue
        checked += 1
        same += int(a * b > 0)
        flips += int(a * b < 0)
    return round(same / max(1, checked), 4), flips


def metrics(rows: list[dict[str, Any]], v3r24r: dict[tuple[str, str], dict[str, Any]], rpc: BudgetedRpc, budget_stop_reason: str) -> dict[str, Any]:
    ret_5m = [as_float(r.get("label_return_5m")) for r in rows if as_bool(r.get("timestamp_quote_5m_ok"))]
    ret_30m = [as_float(r.get("label_return_30m")) for r in rows if as_bool(r.get("timestamp_quote_30m_ok"))]
    ret_5m_f = [float(v) for v in ret_5m if v is not None]
    ret_30m_f = [float(v) for v in ret_30m if v is not None]
    fixed = [float(as_float(r["label_return"]) or 0.0) for r in rows if as_bool(r.get("timestamp_quote_5m_ok")) and as_bool(r.get("timestamp_quote_30m_ok"))]
    paired_5m = [float(as_float(r["label_return_5m"]) or 0.0) for r in rows if as_bool(r.get("timestamp_quote_5m_ok")) and as_bool(r.get("timestamp_quote_30m_ok"))]
    paired_30m = [float(as_float(r["label_return_30m"]) or 0.0) for r in rows if as_bool(r.get("timestamp_quote_5m_ok")) and as_bool(r.get("timestamp_quote_30m_ok"))]
    fixed_5m, _ = direction_consistency(fixed, paired_5m)
    fixed_30m, _ = direction_consistency(fixed, paired_30m)
    h5_h30, sign_flip = direction_consistency(paired_5m, paired_30m)
    pools = [r["pool_address"].lower() for r in rows]
    tokens = [r["token_address"].lower() for r in rows]
    p1, p5 = top_shares(pools)
    t1, t5 = top_shares(tokens)
    same_block_ok = all(not as_bool(v3r24r.get((r["tx_hash"], r["log_index"]), {}).get("same_block_is_label")) for r in rows)
    total = len(rows)
    q5 = sum(1 for r in rows if as_bool(r.get("timestamp_quote_5m_ok")))
    q30 = sum(1 for r in rows if as_bool(r.get("timestamp_quote_30m_ok")))
    quote_metrics = {
        "total_rows": total,
        "quoted_5m_count": q5,
        "quoted_30m_count": q30,
        "quote_success_rate_5m": round(q5 / max(1, total), 4),
        "quote_success_rate_30m": round(q30 / max(1, total), 4),
        "failure_reason_distribution_5m": dict(Counter(r["failure_reason_5m"] for r in rows if r.get("failure_reason_5m"))),
        "failure_reason_distribution_30m": dict(Counter(r["failure_reason_30m"] for r in rows if r.get("failure_reason_30m"))),
    }
    return_metrics = {
        "return_5m_distribution": distribution(ret_5m_f),
        "return_30m_distribution": distribution(ret_30m_f),
        "near_total_loss_share_5m": share(ret_5m_f, lambda x: x <= NEAR_TOTAL_LOSS),
        "near_total_loss_share_30m": share(ret_30m_f, lambda x: x <= NEAR_TOTAL_LOSS),
        "positive_return_share_5m": share(ret_5m_f, lambda x: x > 0),
        "positive_return_share_30m": share(ret_30m_f, lambda x: x > 0),
    }
    consistency = {
        "fixed_vs_5m_direction_consistency": fixed_5m,
        "fixed_vs_30m_direction_consistency": fixed_30m,
        "5m_vs_30m_direction_consistency": h5_h30,
        "sign_flip_count_5m_vs_30m": sign_flip,
        "explain_5m_consistency_0_8_from_V3R26": "V3R-26 used the first 20-row bounded quote subset; several fixed-block labels were 30m-equivalent while 5m exits captured earlier intermediate pool states, causing short-horizon sign differences.",
    }
    sanity = {
        "top1_pool_share": p1,
        "top5_pool_share": p5,
        "top1_token_share": t1,
        "top5_token_share": t5,
        "fee_distribution": dict(Counter(str(as_int(r["fee"])) for r in rows)),
        "source_file_distribution": dict(Counter(r["source_path"] for r in rows)),
        "tx_hash_log_index_duplicate_count": duplicate_count(rows),
        "duplicate_conflict_count": duplicate_count(rows),
        "same_block_sanity_not_label": same_block_ok,
        "timestamp_horizon_primary_label": True,
    }
    diversity_gate = p1 <= 0.20 and p5 <= 0.55 and t1 <= 0.20 and t5 <= 0.55
    timestamp_validation_passed = (
        quote_metrics["quote_success_rate_5m"] >= 0.90
        and quote_metrics["quote_success_rate_30m"] >= 0.90
        and return_metrics["near_total_loss_share_5m"] <= 0.10
        and return_metrics["near_total_loss_share_30m"] <= 0.10
        and diversity_gate
        and sanity["duplicate_conflict_count"] == 0
        and same_block_ok
        and sanity["timestamp_horizon_primary_label"]
        and rpc.calls <= rpc.budget
    )
    flags = {
        "v3r27_completed": True,
        "timestamp_horizon_validation_done": True,
        "quote_success_rate_5m": quote_metrics["quote_success_rate_5m"],
        "quote_success_rate_30m": quote_metrics["quote_success_rate_30m"],
        "near_total_loss_share_5m": return_metrics["near_total_loss_share_5m"],
        "near_total_loss_share_30m": return_metrics["near_total_loss_share_30m"],
        "diversity_gate_passed": diversity_gate,
        "duplicate_conflict_count": sanity["duplicate_conflict_count"],
        "same_block_sanity_not_label": same_block_ok,
        "timestamp_horizon_primary_label": True,
        "timestamp_validation_passed": timestamp_validation_passed,
        "allow_v3r28_leakage_holdout_audit": timestamp_validation_passed,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    return {
        "timestamp_horizon_quote_validation": quote_metrics,
        "return_distribution": return_metrics,
        "consistency": consistency,
        "diversity_leakage_sanity": sanity,
        "rpc_budget": {
            "eth_call_rpc_calls": rpc.calls,
            "rpc_budget": rpc.budget,
            "rpc_budget_respected": rpc.calls <= rpc.budget,
            "budget_stop_reason": budget_stop_reason,
        },
        "output_row_cap": {
            "output_rows": len(rows),
            "row_cap": 100,
            "output_row_cap_respected": len(rows) <= 100,
        },
        "decision_flags": flags,
    }


def write_rows(rows: list[dict[str, Any]]) -> None:
    fields = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "entry_timestamp",
        "exit_block_5m",
        "exit_timestamp_5m",
        "exit_block_30m",
        "exit_timestamp_30m",
        "fixed_block_return_control",
        "same_block_sanity_return",
        "timestamp_quote_entry_ok",
        "timestamp_quote_5m_ok",
        "label_return_5m",
        "failure_reason_5m",
        "timestamp_quote_30m_ok",
        "label_return_30m",
        "failure_reason_30m",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:100]:
            out = {k: row.get(k, "") for k in fields}
            out["fixed_block_return_control"] = row.get("label_return")
            writer.writerow(out)


def render_md(summary: dict[str, Any]) -> str:
    q = summary["timestamp_horizon_quote_validation"]
    r = summary["return_distribution"]
    c = summary["consistency"]
    s = summary["diversity_leakage_sanity"]
    f = summary["decision_flags"]
    return f"""# Run-004 Patch V3R-27 — Bounded Timestamp-Horizon Validation

Status: **{'PASSED' if f['timestamp_validation_passed'] else 'FAILED'}**. Research-only/read-only. Fixed-block return is control only, timestamp horizon is primary label.

## 5m / 30m Quote Metrics

- total rows: **{q['total_rows']}**
- quoted 5m / 30m: **{q['quoted_5m_count']} / {q['quoted_30m_count']}**
- success rate 5m / 30m: **{q['quote_success_rate_5m']} / {q['quote_success_rate_30m']}**
- failures 5m / 30m: `{q['failure_reason_distribution_5m']}` / `{q['failure_reason_distribution_30m']}`

## Return Distribution

- 5m: `{r['return_5m_distribution']}`
- 30m: `{r['return_30m_distribution']}`
- near-total-loss 5m / 30m: **{r['near_total_loss_share_5m']} / {r['near_total_loss_share_30m']}**
- positive return 5m / 30m: **{r['positive_return_share_5m']} / {r['positive_return_share_30m']}**

## Consistency / Sanity

- fixed vs 5m / 30m direction consistency: **{c['fixed_vs_5m_direction_consistency']} / {c['fixed_vs_30m_direction_consistency']}**
- 5m vs 30m direction consistency: **{c['5m_vs_30m_direction_consistency']}**
- sign flips 5m vs 30m: **{c['sign_flip_count_5m_vs_30m']}**
- top1/top5 pool: **{s['top1_pool_share']} / {s['top5_pool_share']}**
- top1/top5 token: **{s['top1_token_share']} / {s['top5_token_share']}**
- duplicate conflicts: **{s['duplicate_conflict_count']}**

## Decision Flags

- `v3r27_completed`: `{str(f['v3r27_completed']).lower()}`
- `timestamp_horizon_validation_done`: `{str(f['timestamp_horizon_validation_done']).lower()}`
- `quote_success_rate_5m`: `{f['quote_success_rate_5m']}`
- `quote_success_rate_30m`: `{f['quote_success_rate_30m']}`
- `near_total_loss_share_5m`: `{f['near_total_loss_share_5m']}`
- `near_total_loss_share_30m`: `{f['near_total_loss_share_30m']}`
- `diversity_gate_passed`: `{str(f['diversity_gate_passed']).lower()}`
- `duplicate_conflict_count`: `{f['duplicate_conflict_count']}`
- `same_block_sanity_not_label`: `{str(f['same_block_sanity_not_label']).lower()}`
- `timestamp_horizon_primary_label`: `{str(f['timestamp_horizon_primary_label']).lower()}`
- `timestamp_validation_passed`: `{str(f['timestamp_validation_passed']).lower()}`
- `allow_v3r28_leakage_holdout_audit`: `{str(f['allow_v3r28_leakage_holdout_audit']).lower()}`
- `allow_larger_expansion`: `{str(f['allow_larger_expansion']).lower()}`
- `allow_model_research`: `{str(f['allow_model_research']).lower()}`
- `allow_mainline`: `{str(f['allow_mainline']).lower()}`
- `allow_live`: `{str(f['allow_live']).lower()}`
"""


def main() -> None:
    started = time.time()
    source_summary = json.loads(SOURCE_SUMMARY.read_text(encoding="utf-8"))
    if not source_summary["decision_flags"]["allow_v3r27_bounded_expansion"]:
        raise RuntimeError("V3R-26 did not allow V3R-27 bounded expansion")
    v3r24r_rows = {(r["tx_hash"], r["log_index"]): r for r in load_csv(V3R24R_ROWS)}
    source_rows = enrich_from_v3r24r(load_csv(SOURCE_ROWS), v3r24r_rows)
    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    budget_stop_reason = ""
    quoted_rows = validate_quotes(rpc, source_rows)
    if len(quoted_rows) < len(source_rows):
        budget_stop_reason = "rpc_budget_exhausted_before_all_rows"
    result = metrics(quoted_rows, v3r24r_rows, rpc, budget_stop_reason)
    summary = {
        "run": "run004_v3r27_bounded_timestamp_horizon_validation",
        "research_only": True,
        "inputs": {
            "v3r26_summary": str(SOURCE_SUMMARY.relative_to(ROOT)),
            "v3r26_rows": str(SOURCE_ROWS.relative_to(ROOT)),
            "v3r24r_rows": str(V3R24R_ROWS.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "source_scan_not_expanded": True,
        "fixed_block_return_control_only": True,
        "timestamp_horizon_primary_label": True,
        **result,
        "runtime_seconds": round(time.time() - started, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(quoted_rows)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        {
            "rows": len(quoted_rows),
            "quote_5m": result["timestamp_horizon_quote_validation"]["quote_success_rate_5m"],
            "quote_30m": result["timestamp_horizon_quote_validation"]["quote_success_rate_30m"],
            "near_loss_5m": result["return_distribution"]["near_total_loss_share_5m"],
            "near_loss_30m": result["return_distribution"]["near_total_loss_share_30m"],
            "passed": result["decision_flags"]["timestamp_validation_passed"],
            "rpc": rpc.calls,
        }
    )


if __name__ == "__main__":
    main()
