"""Run-004 Patch V3R-25 — clean prototype validation.

Research-only/read-only validation of the V3R-24R clean prototype rows.
No source expansion, training, promotion, production writes, or live behavior.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.v3_quoterv2_struct import WETH_BASE, quote_exact_input_single_struct  # noqa: E402
from scripts.run004_v3r24r_v3_candidate_source_diversity_repair import (  # noqa: E402
    BudgetedRpc,
    SENSITIVITY_WETH,
    WETH_DECIMALS,
    _rpc_url,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_JSON = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_summary.json"
SOURCE_CSV = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_rows.csv"
SCHEMA_JSON = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_schema.json"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r25-clean-prototype-validation.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r25_clean_prototype_validation_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r25_clean_prototype_validation_rows.csv"

RPC_BUDGET = 500
RECHECK_LIMIT = 20
SENSITIVITY_LIMIT = 20
NEAR_TOTAL_LOSS = -0.95
RETURN_TOLERANCE = 1e-8


def as_bool(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes"}


def as_float(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def as_int(v: Any) -> int:
    if v is None or str(v).strip() == "":
        return 0
    raw = str(v).strip()
    if "e" in raw.lower() or "." in raw:
        return int(Decimal(raw))
    return int(raw)


def weth_to_raw(v: Any) -> int:
    return int(Decimal(str(v).strip()) * Decimal(WETH_DECIMALS))


def quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 8)


def distribution(vals: list[float]) -> dict[str, float | None]:
    return {
        "min": quantile(vals, 0),
        "p5": quantile(vals, 0.05),
        "median": quantile(vals, 0.5),
        "p95": quantile(vals, 0.95),
        "max": quantile(vals, 1),
    }


def top_shares(vals: list[str]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    c = Counter(vals)
    return round(c.most_common(1)[0][1] / len(vals), 4), round(sum(v for _, v in c.most_common(5)) / len(vals), 4)


def load_rows() -> list[dict[str, Any]]:
    with SOURCE_CSV.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def reconstruct_clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for row in rows:
        label_return = as_float(row.get("label_return"))
        if (
            as_bool(row.get("trainable_candidate_flag"))
            and label_return is not None
            and label_return > NEAR_TOTAL_LOSS
            and as_bool(row.get("state_changed"))
            and as_bool(row.get("horizon_exit_quote_ok"))
        ):
            clean.append(row)
    return clean


def clean_metrics(clean: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [as_float(r["label_return"]) for r in clean if as_float(r.get("label_return")) is not None]
    pools = [r["pool_address"].lower() for r in clean]
    tokens = [r["token_address"].lower() for r in clean]
    p1, p5 = top_shares(pools)
    t1, t5 = top_shares(tokens)
    return {
        "clean_rows_count": len(clean),
        "clean_pool_count": len(set(pools)),
        "clean_token_count": len(set(tokens)),
        "clean_fee_distribution": dict(Counter(str(as_int(r["fee"])) for r in clean)),
        "clean_source_file_distribution": dict(Counter(r["source_path"] for r in clean)),
        "clean_return_distribution": distribution([x for x in returns if x is not None]),
        "clean_near_total_loss_share": round(sum(1 for x in returns if x is not None and x <= NEAR_TOTAL_LOSS) / max(1, len(returns)), 4),
        "clean_top1_pool_share": p1,
        "clean_top5_pool_share": p5,
        "clean_top1_token_share": t1,
        "clean_top5_token_share": t5,
    }


def quote_pair(rpc: BudgetedRpc, row: dict[str, Any], amount_in: int) -> dict[str, Any]:
    token = row["token_address"].lower()
    fee = as_int(row["fee"])
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, amount_in, fee, block_identifier=as_int(row["entry_block"]))
    if not entry["ok"]:
        return {"ok": False, "reason": "entry_" + (entry.get("reason") or "quote_fail")}
    horizon = quote_exact_input_single_struct(
        rpc, token, WETH_BASE, int(entry["amount_out"]), fee, block_identifier=as_int(row["exit_block"])
    )
    if not horizon["ok"]:
        return {"ok": False, "entry_amount_out": int(entry["amount_out"]), "reason": "horizon_" + (horizon.get("reason") or "quote_fail")}
    ret = round(int(horizon["amount_out"]) / amount_in - 1.0, 8)
    return {
        "ok": True,
        "entry_amount_out": int(entry["amount_out"]),
        "horizon_exit_amount_out_weth": int(horizon["amount_out"]),
        "label_return": ret,
    }


def recheck(rpc: BudgetedRpc, clean: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    details: dict[tuple[str, str], dict[str, Any]] = {}
    exact = 0
    tolerance = 0
    mismatch = 0
    reasons: Counter[str] = Counter()
    for row in clean[:RECHECK_LIMIT]:
        amount = weth_to_raw(row["selected_notional_weth"])
        q = quote_pair(rpc, row, amount)
        key = (row["tx_hash"], row["log_index"])
        if not q["ok"]:
            mismatch += 1
            reasons[q["reason"]] += 1
            details[key] = {"recheck_ok": False, "recheck_reason": q["reason"]}
            continue
        entry_match = q["entry_amount_out"] == as_int(row["entry_amount_out"])
        horizon_match = q["horizon_exit_amount_out_weth"] == as_int(row["horizon_exit_amount_out_weth"])
        return_delta = abs(float(q["label_return"]) - float(row["label_return"]))
        exact_match = entry_match and horizon_match and return_delta == 0
        tolerance_match = entry_match and horizon_match and return_delta <= RETURN_TOLERANCE
        exact += int(exact_match)
        tolerance += int(tolerance_match)
        if not tolerance_match:
            mismatch += 1
            reasons["quote_value_mismatch"] += 1
        details[key] = {
            "recheck_ok": tolerance_match,
            "recheck_entry_amount_out": q["entry_amount_out"],
            "recheck_horizon_exit_amount_out_weth": q["horizon_exit_amount_out_weth"],
            "recheck_label_return": q["label_return"],
            "recheck_return_delta": round(return_delta, 12),
        }
    checked = min(RECHECK_LIMIT, len(clean))
    return {
        "rechecked_rows": checked,
        "exact_match_count": exact,
        "tolerance_match_count": tolerance,
        "mismatch_count": mismatch,
        "tolerance_match_rate": round(tolerance / max(1, checked), 4),
        "mismatch_reason_distribution": dict(reasons),
        "prototype_unstable": tolerance / max(1, checked) < 0.95,
    }, details


def sensitivity(rpc: BudgetedRpc, clean: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    details: dict[tuple[str, str], dict[str, Any]] = {}
    stable = 0
    near_total_loss = 0
    sign_flip = 0
    checked = 0
    for row in clean[:SENSITIVITY_LIMIT]:
        vals = []
        reasons = []
        for weth in SENSITIVITY_WETH:
            q = quote_pair(rpc, row, int(weth * WETH_DECIMALS))
            vals.append(q.get("label_return") if q["ok"] else None)
            reasons.append("" if q["ok"] else q["reason"])
        good = [float(v) for v in vals if v is not None]
        checked += 1
        row_stable = len(good) == len(vals) and max(good) - min(good) <= 0.25
        row_near = any(v is not None and float(v) <= NEAR_TOTAL_LOSS for v in vals)
        signs = {1 if v > 0 else -1 if v < 0 else 0 for v in good}
        row_flip = len({s for s in signs if s != 0}) > 1
        stable += int(row_stable)
        near_total_loss += int(row_near)
        sign_flip += int(row_flip)
        details[(row["tx_hash"], row["log_index"])] = {
            "sensitivity_returns": vals,
            "sensitivity_reasons": reasons,
            "sensitivity_stable": row_stable,
            "sensitivity_near_total_loss": row_near,
            "sensitivity_sign_flip": row_flip,
        }
    return {
        "sensitivity_checked_count": checked,
        "stable_count": stable,
        "near_total_loss_count": near_total_loss,
        "sign_flip_count": sign_flip,
        "selected_notional_policy_ok": near_total_loss == 0 and sign_flip / max(1, checked) <= 0.10,
    }, details


def duplicate_conflicts(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def artifact_sanity(clean: list[dict[str, Any]]) -> dict[str, Any]:
    source_dist = Counter(r["source_path"] for r in clean)
    fee_dist = Counter(str(as_int(r["fee"])) for r in clean)
    return {
        "duplicate_conflict_count": duplicate_conflicts(clean),
        "single_source_file": len(source_dist) == 1,
        "single_fee_tier": len(fee_dist) == 1,
        "same_block_sanity_not_label": all(not as_bool(r.get("same_block_is_label")) for r in clean),
        "fixed_block_pilot_explicit": all(r.get("exit_block_policy") == "fixed_block_pilot" for r in clean),
        "timestamp_missing": any(not str(r.get("timestamp") or "").strip() for r in clean),
        "source_file_distribution": dict(source_dist),
        "fee_distribution": dict(fee_dist),
    }


def write_rows(clean: list[dict[str, Any]], recheck_details: dict[tuple[str, str], dict[str, Any]], sens_details: dict[tuple[str, str], dict[str, Any]]) -> None:
    fields = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "exit_block",
        "selected_notional_weth",
        "label_return",
        "same_block_sanity_return",
        "same_block_is_label",
        "exit_block_policy",
        "recheck_ok",
        "recheck_label_return",
        "recheck_return_delta",
        "sensitivity_stable",
        "sensitivity_near_total_loss",
        "sensitivity_sign_flip",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in clean[:100]:
            key = (row["tx_hash"], row["log_index"])
            merged = {k: row.get(k) for k in fields}
            merged.update(recheck_details.get(key, {}))
            merged.update(sens_details.get(key, {}))
            writer.writerow({k: merged.get(k) for k in fields})


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    return f"""# Run-004 Patch V3R-25 — Clean Prototype Validation

Status: **research-only / read-only**. No LIVE, promote, production signals, runtime changes, H19, retrain, larger expansion, or training merge.

## Clean Prototype Reconstruction

- clean rows: **{summary['clean_prototype']['clean_rows_count']}**
- pool/token count: **{summary['clean_prototype']['clean_pool_count']} / {summary['clean_prototype']['clean_token_count']}**
- top1/top5 pool: **{summary['clean_prototype']['clean_top1_pool_share']} / {summary['clean_prototype']['clean_top5_pool_share']}**
- top1/top5 token: **{summary['clean_prototype']['clean_top1_token_share']} / {summary['clean_prototype']['clean_top5_token_share']}**
- fee distribution: `{summary['clean_prototype']['clean_fee_distribution']}`
- source distribution: `{summary['clean_prototype']['clean_source_file_distribution']}`

## Recheck / Sensitivity

- rechecked rows: **{summary['recheck']['rechecked_rows']}**
- tolerance match rate: **{summary['recheck']['tolerance_match_rate']}**
- mismatch count: **{summary['recheck']['mismatch_count']}**
- sensitivity checked: **{summary['sensitivity']['sensitivity_checked_count']}**
- stable / near-total-loss / sign-flip: **{summary['sensitivity']['stable_count']} / {summary['sensitivity']['near_total_loss_count']} / {summary['sensitivity']['sign_flip_count']}**

## Leakage / Artifact Sanity

- duplicate conflicts: **{summary['artifact_sanity']['duplicate_conflict_count']}**
- same-block sanity not label: **{summary['artifact_sanity']['same_block_sanity_not_label']}**
- fixed-block pilot explicit: **{summary['artifact_sanity']['fixed_block_pilot_explicit']}**
- timestamp missing: **{summary['artifact_sanity']['timestamp_missing']}**

## Decision Flags

- `v3r25_completed`: `{str(f['v3r25_completed']).lower()}`
- `clean_prototype_reconstructed`: `{str(f['clean_prototype_reconstructed']).lower()}`
- `clean_rows_count`: `{f['clean_rows_count']}`
- `clean_diversity_still_passed`: `{str(f['clean_diversity_still_passed']).lower()}`
- `recheck_passed`: `{str(f['recheck_passed']).lower()}`
- `sensitivity_passed`: `{str(f['sensitivity_passed']).lower()}`
- `duplicate_conflict_count`: `{f['duplicate_conflict_count']}`
- `same_block_sanity_not_label`: `{str(f['same_block_sanity_not_label']).lower()}`
- `fixed_block_pilot_explicit`: `{str(f['fixed_block_pilot_explicit']).lower()}`
- `timestamp_missing`: `{str(f['timestamp_missing']).lower()}`
- `prototype_validation_passed`: `{str(f['prototype_validation_passed']).lower()}`
- `require_timestamp_block_mapping`: `{str(f['require_timestamp_block_mapping']).lower()}`
- `allow_v3r26_timestamp_mapping_repair`: `{str(f['allow_v3r26_timestamp_mapping_repair']).lower()}`
- `allow_larger_expansion`: `{str(f['allow_larger_expansion']).lower()}`
- `allow_model_research`: `{str(f['allow_model_research']).lower()}`
- `allow_mainline`: `{str(f['allow_mainline']).lower()}`
- `allow_live`: `{str(f['allow_live']).lower()}`
"""


def main() -> None:
    started = time.time()
    source_summary = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    _ = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    rows = load_rows()
    clean = reconstruct_clean(rows)
    cm = clean_metrics(clean)
    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    recheck_metrics, recheck_details = recheck(rpc, clean)
    sensitivity_metrics, sensitivity_details = sensitivity(rpc, clean)
    sanity = artifact_sanity(clean)
    diversity_pass = (
        cm["clean_rows_count"] >= 20
        and cm["clean_top1_pool_share"] <= 0.20
        and cm["clean_top5_pool_share"] <= 0.55
        and cm["clean_top1_token_share"] <= 0.20
        and cm["clean_top5_token_share"] <= 0.55
        and sanity["duplicate_conflict_count"] == 0
        and cm["clean_near_total_loss_share"] == 0
    )
    recheck_passed = recheck_metrics["tolerance_match_rate"] >= 0.95
    sensitivity_passed = bool(sensitivity_metrics["selected_notional_policy_ok"])
    prototype_passed = (
        diversity_pass
        and recheck_passed
        and sensitivity_passed
        and sanity["same_block_sanity_not_label"]
        and sanity["fixed_block_pilot_explicit"]
        and rpc.calls <= RPC_BUDGET
    )
    flags = {
        "v3r25_completed": True,
        "clean_prototype_reconstructed": True,
        "clean_rows_count": cm["clean_rows_count"],
        "clean_diversity_still_passed": diversity_pass,
        "recheck_passed": recheck_passed,
        "sensitivity_passed": sensitivity_passed,
        "duplicate_conflict_count": sanity["duplicate_conflict_count"],
        "same_block_sanity_not_label": sanity["same_block_sanity_not_label"],
        "fixed_block_pilot_explicit": sanity["fixed_block_pilot_explicit"],
        "timestamp_missing": sanity["timestamp_missing"],
        "prototype_validation_passed": prototype_passed,
        "require_timestamp_block_mapping": True,
        "allow_v3r26_timestamp_mapping_repair": prototype_passed,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": "run004_v3r25_clean_prototype_validation",
        "research_only": True,
        "inputs": {
            "v3r24r_summary": str(SOURCE_JSON.relative_to(ROOT)),
            "v3r24r_rows": str(SOURCE_CSV.relative_to(ROOT)),
            "schema": str(SCHEMA_JSON.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "source_scan_not_expanded": True,
        "v3r24r_reference_flags": source_summary.get("decision_flags", {}),
        "clean_prototype": cm,
        "recheck": recheck_metrics,
        "sensitivity": sensitivity_metrics,
        "artifact_sanity": sanity,
        "rpc_budget": {
            "eth_call_rpc_calls": rpc.calls,
            "rpc_budget": RPC_BUDGET,
            "rpc_budget_respected": rpc.calls <= RPC_BUDGET,
        },
        "output_row_cap": {
            "output_rows": min(len(clean), 100),
            "row_cap": 100,
            "output_row_cap_respected": len(clean) <= 100,
        },
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - started, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(clean, recheck_details, sensitivity_details)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        {
            "clean_rows": cm["clean_rows_count"],
            "recheck_match_rate": recheck_metrics["tolerance_match_rate"],
            "sensitivity_stable": sensitivity_metrics["stable_count"],
            "rpc": rpc.calls,
            "prototype_validation_passed": prototype_passed,
        }
    )


if __name__ == "__main__":
    main()
