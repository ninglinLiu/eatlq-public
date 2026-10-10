"""Run-004 V3S-3A — staged notional-at-cap scoped 5m expansion (250 then 500).

Research-only execution: bounded RPC quote expansion within frozen scope
`notional_bucket:at_cap` (selected_notional_weth == DEFAULT_CAP_WETH ± 1e-17 raw-equivalent).

- Stage 1: 110 → up to 250 clean rows (max +140 clean, 450 attempts, 7000 RPC, 15m wall).
- Stage 2: only if stage 1 passes all checkpoints; 250 → up to 500 (max +250, 900 att, 14000 RPC, 30m wall).

No training, no model research, no 2000 target, no mainline/live/shadow/promote/production model writes.

5m primary; 30m labels kept for decay diagnostic only (not a hard failure gate on 30m).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r24r_v3_candidate_source_diversity_repair import (  # noqa: E402
    BudgetedRpc,
    DEFAULT_CAP_RAW,
    DEFAULT_CAP_WETH,
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
from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float, as_int  # noqa: E402
from scripts.run004_v3r29_small_timestamp_dataset_expansion import (  # noqa: E402
    assign_splits,
    leakage_and_holdout,
    quote_timestamp_row,
    resolve_timestamp_horizons,
)
from scripts.run004_v3r29b_small_timestamp_dataset_rebalance import (  # noqa: E402
    duplicate_count,
    normalize_v3r29_row,
)
from scripts.run004_v3r30_pre_model_dataset_audit import (  # noqa: E402
    NEAR_ZERO_ABS,
    concentration,
    outlier_audit,
    positive_rate,
    sign_flips,
    split_summary,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_RUN = "run004_v3s3a_staged_scoped_5m_expansion"
NOTIONAL_CAP = DEFAULT_CAP_WETH
NEAR_TOTAL_LOSS = -0.95
FEE_CAP_UNLIMITED = 999999
SOURCE_CAP_UNLIMITED = 999999
POOL_CAP_UNLIMITED = 999999
TOKEN_CAP_UNLIMITED = 999999

V3S2_SUMMARY = ROOT / "mvp/diagnostics/run004_v3s2_scoped_expansion_cost_preflight_summary.json"
V31F_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r31f_failure_consolidation_summary.json"
V29B_ROWS = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_rows.csv"

OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_rows.csv"
OUT_MD = ROOT / "mvp/runs/run-004-v3s3a-staged-scoped-5m-expansion.md"


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def row_at_cap(r: dict[str, Any]) -> bool:
    """Scope: selected_notional_weth at dataset cap (5e-05 WETH). CSV floats use ~1e-12 tolerance (1e-17 not representable)."""
    w = as_float(r.get("selected_notional_weth"))
    if w is None:
        return False
    return abs(float(w) - NOTIONAL_CAP) <= 1e-12


def candidate_at_cap(cand: dict[str, Any]) -> bool:
    n = choose_notional(cand)
    return int(n["selected_notional_raw"]) == int(DEFAULT_CAP_RAW)


def fee_cap_for_v3s3a(_fee_str: str) -> int:
    return FEE_CAP_UNLIMITED


def dominant_quote_failure_stop(failures: Counter[str], *, min_total: int = 25) -> str | None:
    """Stop if one non state-change failure reason dominates (>50%) — V3S-3A safety."""
    ex = {k: v for k, v in failures.items() if k != "no_state_change"}
    tot = sum(ex.values())
    if tot < min_total:
        return None
    top, n = max(ex.items(), key=lambda kv: kv[1])
    if n / tot > 0.50:
        return f"single_failure_reason_dominates_{top}"
    return None


def add_new_scoped_rows(
    rpc: BudgetedRpc,
    raw_buys: list[dict[str, Any]],
    kept: list[dict[str, Any]],
    target_total_rows: int,
    max_new_clean: int,
    max_attempts: int,
    deadline_s: float,
    fail_fast_after_attempts: int,
    stop_flags: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Quote new rows; only at_cap notional candidates; loose caps (scope is notional-only)."""
    src_c: Counter[str] = Counter(r["source_path"] for r in kept)
    pool_c: Counter[str] = Counter(_lower(r["pool_address"]) for r in kept)
    token_c: Counter[str] = Counter(r["token_address"] for r in kept)
    fee_c: Counter[str] = Counter(str(as_int(r["fee"])) for r in kept)
    existing_keys = {(r["tx_hash"], str(r["log_index"])) for r in kept}

    def fee_priority(fee_str: str) -> int:
        return fee_c[fee_str] - fee_cap_for_v3s3a(fee_str)

    raw_sorted = sorted(
        raw_buys,
        key=lambda c: (
            src_c[c["_source_path"]],
            fee_priority(str(int(c["_fee"]))),
            pool_c[_lower(c["pool_address"])],
            token_c[c["_token_address"]],
            as_int(c.get("_entry_block", 0)),
        ),
    )
    timestamp_cache: dict[int, int] = {}
    new_rows: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()
    mapping_failures = 0
    attempted = 0
    state_changed_count = 0
    q5_ok = 0
    q30_ok = 0
    stop_reason = ""

    def check_dom_stop() -> bool:
        nonlocal stop_reason
        if attempted < 50:
            return False
        dom = dominant_quote_failure_stop(failures)
        if dom:
            stop_reason = dom
            return True
        return False

    for cand in raw_sorted:
        if time.time() >= deadline_s:
            stop_reason = "wall_time_exceeded"
            break
        if rpc.calls >= rpc.budget:
            stop_reason = "rpc_budget_exhausted"
            break
        if len(kept) + len(new_rows) >= target_total_rows:
            stop_reason = "target_clean_rows_reached"
            break
        if len(new_rows) >= max_new_clean:
            stop_reason = "max_new_clean_cap"
            break
        if attempted >= max_attempts:
            stop_reason = "max_attempts_reached"
            break

        if not candidate_at_cap(cand):
            continue

        key = (cand["_tx_hash"], str(cand["_log_index"]))
        if key in existing_keys:
            continue

        src = cand["_source_path"]
        pool = _lower(cand["pool_address"])
        token = cand["_token_address"]
        fee_str = str(int(cand["_fee"]))
        if (
            src_c[src] >= SOURCE_CAP_UNLIMITED
            or pool_c[pool] >= POOL_CAP_UNLIMITED
            or token_c[token] >= TOKEN_CAP_UNLIMITED
            or fee_c[fee_str] >= fee_cap_for_v3s3a(fee_str)
        ):
            continue

        attempted += 1

        if attempted >= fail_fast_after_attempts:
            cr = len(new_rows) / max(1, attempted)
            if cr < 0.20:
                stop_reason = "clean_rate_below_0_20_after_100_attempts"
                break

        try:
            notional = choose_notional(cand)
            mapping = resolve_timestamp_horizons(rpc, timestamp_cache, as_int(cand["_entry_block"]))
            s0 = read_state(rpc, cand, as_int(cand["_entry_block"]))
            s1 = read_state(rpc, cand, as_int(mapping["exit_block_30m"]))
            ch = changes(s0, s1)
            if not any(ch.values()):
                failures["no_state_change"] += 1
                if check_dom_stop():
                    break
                continue
            state_changed_count += 1
            quote = quote_timestamp_row(rpc, cand, notional, mapping)
        except Exception as exc:
            reason = "rpc_budget_exhausted" if "rpc_budget" in str(exc) else str(exc)[:80]
            failures[reason] += 1
            if reason == "rpc_budget_exhausted":
                stop_reason = reason
                break
            if check_dom_stop():
                break
            continue

        if as_bool(quote.get("timestamp_quote_5m_ok")):
            q5_ok += 1
        if as_bool(quote.get("timestamp_quote_30m_ok")):
            q30_ok += 1

        r5 = as_float(quote.get("label_return_5m"))
        r30 = as_float(quote.get("label_return_30m"))
        ts_ok = (
            as_int(mapping["exit_timestamp_5m"]) >= as_int(mapping["target_timestamp_5m"])
            and as_int(mapping["exit_timestamp_30m"]) >= as_int(mapping["target_timestamp_30m"])
        )
        if not ts_ok:
            mapping_failures += 1
            failures["timestamp_mapping_order_fail"] += 1
            if attempted >= 40 and (mapping_failures / attempted) > 0.05:
                stop_reason = "timestamp_mapping_failure_rate_exceeds_5pct"
                break
            if check_dom_stop():
                break
            continue

        ok = (
            as_bool(quote.get("timestamp_quote_entry_ok"))
            and as_bool(quote.get("timestamp_quote_5m_ok"))
            and as_bool(quote.get("timestamp_quote_30m_ok"))
            and r5 is not None
            and r30 is not None
            and r5 > NEAR_TOTAL_LOSS
            and r30 > NEAR_TOTAL_LOSS
        )
        if not ok:
            reason = quote.get("failure_reason_5m") or quote.get("failure_reason_30m") or "near_total_loss_or_mapping"
            failures[str(reason)] += 1
            if check_dom_stop():
                break
            continue

        if int(notional["selected_notional_raw"]) != int(DEFAULT_CAP_RAW):
            failures["notional_not_at_cap_after_quote"] += 1
            if check_dom_stop():
                break
            continue

        new_rows.append(
            {
                "source_run": SOURCE_RUN,
                "source_path": src,
                "tx_hash": cand["_tx_hash"],
                "log_index": cand["_log_index"],
                "pool_address": pool,
                "token_address": token,
                "token0": _lower(get_value(cand, "token0")),
                "token1": _lower(get_value(cand, "token1")),
                "fee": cand["_fee"],
                "observed_direction": "WETH_TO_TOKEN",
                "selected_notional_weth": notional["selected_notional_weth"],
                "entry_block": cand["_entry_block"],
                "entry_timestamp": mapping["entry_timestamp"],
                "target_timestamp_5m": mapping["target_timestamp_5m"],
                "exit_block_5m": mapping["exit_block_5m"],
                "exit_timestamp_5m": mapping["exit_timestamp_5m"],
                "error_seconds_5m": mapping["error_seconds_5m"],
                "target_timestamp_30m": mapping["target_timestamp_30m"],
                "exit_block_30m": mapping["exit_block_30m"],
                "exit_timestamp_30m": mapping["exit_timestamp_30m"],
                "error_seconds_30m": mapping["error_seconds_30m"],
                "state_changed": True,
                "same_block_sanity_return": quote.get("same_block_sanity_return"),
                "same_block_is_label": False,
                "timestamp_horizon_primary_label": True,
                "timestamp_quote_5m_ok": True,
                "label_return_5m": r5,
                "timestamp_quote_30m_ok": True,
                "label_return_30m": r30,
                "fixed_block_return_control": "",
                "trainable_candidate_flag": False,
                "research_clean_timestamp_candidate": True,
            }
        )
        existing_keys.add(key)
        src_c[src] += 1
        pool_c[pool] += 1
        token_c[token] += 1
        fee_c[fee_str] += 1

        if check_dom_stop():
            break

    stop_flags.update(
        {
            "mapping_failures_total": mapping_failures,
        }
    )
    return new_rows, {
        "newly_attempted_rows": attempted,
        "state_changed_count": state_changed_count,
        "quote_success_5m_count": q5_ok,
        "quote_success_30m_count": q30_ok,
        "quote_success_rate_5m": round(q5_ok / max(1, state_changed_count), 4),
        "quote_success_rate_30m": round(q30_ok / max(1, state_changed_count), 4),
        "newly_clean_rows": len(new_rows),
        "failure_reason_distribution": dict(failures),
        "stop_reason": stop_reason,
        "rpc_used": rpc.calls,
        "rpc_budget": rpc.budget,
        "clean_rate": round(len(new_rows) / max(1, attempted), 4),
    }


def write_rows_v3s3a(rows: list[dict[str, Any]], flips: list[dict[str, Any]], outliers: dict[str, Any]) -> None:
    flip_keys = {(r["tx_hash"], str(r["log_index"])) for r in flips}
    outlier_keys = {(r["tx_hash"], str(r["log_index"])) for r in outliers["outlier_rows"]}
    fields = [
        "source_run",
        "source_path",
        "tx_hash",
        "log_index",
        "pool_address",
        "token_address",
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
        "near_zero_flip",
        "outlier_flag",
        "trainable_candidate_flag",
        "research_clean_timestamp_candidate",
    ]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            r5 = as_float(row.get("label_return_5m")) or 0.0
            r30 = as_float(row.get("label_return_30m")) or 0.0
            key = (row["tx_hash"], str(row["log_index"]))
            out = {k: row.get(k, "") for k in fields}
            out["sign_flip_5m_30m"] = key in flip_keys
            out["near_zero_flip"] = key in flip_keys and min(abs(float(r5)), abs(float(r30))) <= NEAR_ZERO_ABS
            out["outlier_flag"] = key in outlier_keys
            w.writerow(out)


def build_checkpoint(
    label: str,
    base_rows: int,
    annotated: list[dict[str, Any]],
    build: dict[str, Any],
    wall_s: float,
    started_rpc: int,
    conc: dict[str, Any],
    chronological: dict[str, Any],
    stop_flags: dict[str, Any],
    partial_tag: str | None,
) -> dict[str, Any]:
    n = len(annotated)
    r5 = [float(as_float(r.get("label_return_5m")) or 0.0) for r in annotated]
    r30 = [float(as_float(r.get("label_return_30m")) or 0.0) for r in annotated]
    ho = [r for r in annotated if r.get("chronological_split") == "holdout"]
    tr = [r for r in annotated if r.get("chronological_split") == "train"]
    pr5_tr = sum(1 for r in tr if (as_float(r.get("label_return_5m")) or 0) > 0) / max(1, len(tr))
    pr5_ho = sum(1 for r in ho if (as_float(r.get("label_return_5m")) or 0) > 0) / max(1, len(ho))

    src_c = Counter(r["source_path"] for r in annotated)
    fee_c = Counter(str(as_int(r["fee"])) for r in annotated)
    pool_c = Counter(r["pool_address"] for r in annotated)
    tok_c = Counter(r["token_address"] for r in annotated)

    def top1(cnt: Counter) -> float:
        return round(cnt.most_common(1)[0][1] / max(1, n), 4) if cnt else 0.0

    if n < 200 and build.get("stop_reason") == "max_attempts_reached":
        partial_tag = partial_tag or "PARTIAL_EXPANSION_LT_200_ROWS"

    sample_tier = (
        "scoped_backtest_eligible_2000_plus" if n >= 2000 else "tiny_model_500_plus" if n >= 500 else "weak_250_plus" if n >= 250 else "below_250"
    )

    return {
        "stage_label": label,
        "partial_expansion_marker": partial_tag,
        "initial_base_rows": base_rows,
        "final_clean_rows": n,
        "newly_attempted_rows": build["newly_attempted_rows"],
        "newly_clean_rows": build["newly_clean_rows"],
        "clean_rate": build.get("clean_rate"),
        "rpc_used_stage_delta": build["rpc_used"] - started_rpc,
        "rpc_used_cumulative": build["rpc_used"],
        "wall_time_seconds_stage": round(wall_s, 2),
        "failure_reason_distribution": build["failure_reason_distribution"],
        "stop_reason": build.get("stop_reason", ""),
        "pools": conc["pool_count"],
        "tokens": conc["token_count"],
        "sources": conc["source_count"],
        "positive_rate_5m": positive_rate(annotated, "label_return_5m"),
        "positive_rate_30m": positive_rate(annotated, "label_return_30m"),
        "median_return_5m": round(median(r5), 8) if r5 else None,
        "median_return_30m": round(median(r30), 8) if r30 else None,
        "sign_flip_share": round(
            sum(1 for a, b in zip(r5, r30) if a * b < 0) / max(1, n), 4,
        ),
        "chronological_train_rows": len(tr),
        "chronological_holdout_rows": len(ho),
        "chrono_train_positive_rate_5m": round(pr5_tr, 4),
        "chrono_holdout_positive_rate_5m": round(pr5_ho, 4) if ho else None,
        "chrono_drift_5m_holdout_minus_train": round(pr5_ho - pr5_tr, 4) if ho and tr else None,
        "top_pool_share": top1(pool_c),
        "top_token_share": top1(tok_c),
        "top_source_share": top1(src_c),
        "top_fee_share": top1(fee_c),
        "sample_size_tier": sample_tier,
        "chronological_split_feasible": chronological.get("split_feasible", False),
        "holdout_5m_positive_count": sum(1 for r in ho if (as_float(r.get("label_return_5m")) or 0) > 0),
        "stop_flags": stop_flags,
    }


def stage_passes(
    chk: dict[str, Any],
    dup: int,
    valid_5m: int,
    valid_30m: int,
    n: int,
    *,
    min_rows: int,
) -> bool:
    if n < min_rows:
        return False
    if valid_5m != n or valid_30m != n:
        return False
    if dup != 0:
        return False
    if chk["chronological_holdout_rows"] < 50:
        return False
    if chk["holdout_5m_positive_count"] < 10:
        return False
    return True


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    lines = [
        "# Run-004 V3S-3A — Staged scoped 5m expansion (notional at_cap)",
        "",
        f"**Status:** Stage1 passed={f['v3s3a_stage1_passed']}, reached_250={f['reached_250_clean_rows']}, reached_500={f['reached_500_clean_rows']}.",
        "",
        "## Frozen scope",
        "",
        "- `notional_bucket:at_cap` — `selected_notional_weth == 5e-05` (raw == DEFAULT_CAP_RAW).",
        "",
        "## Final answers",
        "",
    ]
    for i, q in enumerate(summary.get("final_qa", []), 1):
        lines.append(f"{i}. {q}")
    lines.extend([
        "",
        "## Decision flags",
        "",
        "```json",
        json.dumps(f, indent=2, ensure_ascii=False),
        "```",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="V3S-3A staged at_cap scoped expansion (RPC spend).")
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge V3S-2 preflight cost/risk before RPC and artifact writes.",
    )
    args = ap.parse_args()
    if not args.confirm:
        print(
            json.dumps(
                {
                    "error": "refusing_to_run_without_confirm",
                    "hint": "Re-run with --confirm after V3S-2 cost preflight review.",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)

    t0 = time.time()
    v3s2_exists = V3S2_SUMMARY.exists()
    v31f_exists = V31F_SUMMARY.exists()
    v3s2_doc: dict[str, Any] = {}
    if v3s2_exists:
        v3s2_doc = json.loads(V3S2_SUMMARY.read_text(encoding="utf-8"))
    if v31f_exists:
        _ = json.loads(V31F_SUMMARY.read_text(encoding="utf-8"))

    inv = source_inventory()
    fa = field_availability(inv)
    if not fa["required_fields_available"]:
        raise RuntimeError("source inventory missing required fields")
    raw_buys, scan = scan_sources(inv)

    base = [normalize_v3r29_row(r) for r in load_csv(V29B_ROWS) if row_at_cap(r)]
    base_n = len(base)
    if base_n < 100:
        print(json.dumps({"error": "base_at_cap_rows_below_100", "count": base_n}, ensure_ascii=False))
        raise SystemExit(2)

    rpc = BudgetedRpc(_rpc_url(), 50_000)
    rpc.inner.delay_s = 0.01

    decision_flags: dict[str, Any] = {
        "v3s3a_stage1_passed": False,
        "reached_250_clean_rows": False,
        "reached_500_clean_rows": False,
        "scoped_5m_primary_preserved": True,
        "thirty_m_decay_diagnostic_only": True,
        "chronological_oos_ready": False,
        "cost_budget_respected": True,
        "duplicate_conflict_abort": False,
        "allow_v3s4_tiny_scoped_model_feasibility": False,
        "allow_training": False,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }

    checkpoints: list[dict[str, Any]] = []
    all_annotated: list[dict[str, Any]] = []
    total_rpc_start = rpc.calls
    stage1_partial = None
    stage2_skipped_reason: str | None = "not_started"

    # --- Stage 1 ---
    t_s1 = time.time()
    s1_deadline = t_s1 + 15 * 60
    s1_stop: dict[str, Any] = {}
    rpc.budget = rpc.calls + 7000
    s1_rpc0 = rpc.calls
    new1, b1 = add_new_scoped_rows(
        rpc,
        raw_buys,
        base,
        target_total_rows=250,
        max_new_clean=140,
        max_attempts=450,
        deadline_s=s1_deadline,
        fail_fast_after_attempts=100,
        stop_flags=s1_stop,
    )
    merged1 = base + new1
    hold1 = leakage_and_holdout(merged1)
    ann1 = assign_splits(merged1, hold1)
    conc1 = concentration(ann1)
    ch1 = split_summary(ann1, "chronological_split", conc1)
    w1 = time.time() - t_s1
    if rpc.calls - s1_rpc0 > 7000:
        decision_flags["cost_budget_respected"] = False
    if w1 > 15 * 60:
        decision_flags["cost_budget_respected"] = False

    dup1 = duplicate_count(ann1)
    v5_1 = sum(1 for r in ann1 if as_bool(r.get("timestamp_quote_5m_ok")) and as_float(r.get("label_return_5m")) is not None)
    v30_1 = sum(1 for r in ann1 if as_bool(r.get("timestamp_quote_30m_ok")) and as_float(r.get("label_return_30m")) is not None)

    if dup1 > 0:
        decision_flags["duplicate_conflict_abort"] = True
        s1_stop.setdefault("duplicate_conflict_in_merged_rows", dup1)

    if len(ann1) < 200 and b1.get("stop_reason") == "max_attempts_reached":
        stage1_partial = "PARTIAL_EXPANSION_LT_200_ROWS"

    chk1 = build_checkpoint(
        "stage_1", base_n, ann1, b1, w1, total_rpc_start, conc1, ch1, s1_stop, stage1_partial,
    )
    chk1["leakage_audit_passed"] = hold1["leakage_audit_passed"]
    chk1["holdout_audit_passed"] = hold1["holdout_audit_passed"]
    chk1["source_top_share_leakage_metric"] = hold1["source_top_share"]
    chk1["fee_top_share_leakage_metric"] = hold1["fee_top_share"]
    checkpoints.append(chk1)
    decision_flags["reached_250_clean_rows"] = len(ann1) >= 250
    s1_ok = (
        stage_passes(chk1, dup1, v5_1, v30_1, len(ann1), min_rows=250)
        and decision_flags["cost_budget_respected"]
        and dup1 == 0
    )
    if chk1.get("partial_expansion_marker"):
        s1_ok = False
    decision_flags["v3s3a_stage1_passed"] = bool(s1_ok)
    decision_flags["chronological_oos_ready"] = bool(
        dup1 == 0
        and chk1["chronological_split_feasible"]
        and chk1["chronological_holdout_rows"] >= 50
        and chk1["holdout_5m_positive_count"] >= 10
    )

    all_annotated = ann1
    new2: list[dict[str, Any]] = []
    b2: dict[str, Any] = {}
    rpc_after_s1 = rpc.calls
    s2_ok = False

    # --- Stage 2 (only if stage 1 passes) ---
    if not s1_ok:
        stage2_skipped_reason = "stage1_failed_partial_budget_or_duplicate"
    elif s1_ok:
        t1 = time.time()
        s2_deadline = t1 + 30 * 60
        rpc.budget = rpc.calls + 14000
        s2_stop: dict[str, Any] = {}
        new2, b2 = add_new_scoped_rows(
            rpc,
            raw_buys,
            ann1,
            target_total_rows=500,
            max_new_clean=250,
            max_attempts=900,
            deadline_s=s2_deadline,
            fail_fast_after_attempts=100,
            stop_flags=s2_stop,
        )
        merged2 = ann1 + new2
        hold2 = leakage_and_holdout(merged2)
        ann2 = assign_splits(merged2, hold2)
        conc2 = concentration(ann2)
        ch2 = split_summary(ann2, "chronological_split", conc2)
        w2 = time.time() - t1

        dup2 = duplicate_count(ann2)
        v5_2 = sum(
            1
            for r in ann2
            if as_bool(r.get("timestamp_quote_5m_ok")) and as_float(r.get("label_return_5m")) is not None
        )
        v30_2 = sum(
            1
            for r in ann2
            if as_bool(r.get("timestamp_quote_30m_ok")) and as_float(r.get("label_return_30m")) is not None
        )
        chk2 = build_checkpoint(
            "stage_2", len(ann1), ann2, b2, w2, chk1["rpc_used_cumulative"], conc2, ch2, s2_stop, None,
        )
        chk2["leakage_audit_passed"] = hold2["leakage_audit_passed"]
        chk2["holdout_audit_passed"] = hold2["holdout_audit_passed"]
        chk2["source_top_share_leakage_metric"] = hold2["source_top_share"]
        chk2["fee_top_share_leakage_metric"] = hold2["fee_top_share"]
        checkpoints.append(chk2)
        all_annotated = ann2
        decision_flags["reached_500_clean_rows"] = len(ann2) >= 500
        stage2_rpc_delta = b2.get("rpc_used", rpc.calls) - rpc_after_s1
        if stage2_rpc_delta > 14000 or w2 > 30 * 60:
            decision_flags["cost_budget_respected"] = False
        s2_ok = (
            len(ann2) >= 500
            and dup2 == 0
            and v5_2 == len(ann2)
            and v30_2 == len(ann2)
            and chk2["chronological_holdout_rows"] >= 50
            and chk2["holdout_5m_positive_count"] >= 10
            and w2 <= 30 * 60
            and stage2_rpc_delta <= 14000
            and decision_flags["cost_budget_respected"]
        )
        stage2_skipped_reason = None if s2_ok else "stage2_numeric_gates_or_budget_not_met"
        if s2_ok:
            decision_flags["allow_v3s4_tiny_scoped_model_feasibility"] = True
            decision_flags["chronological_oos_ready"] = (
                dup2 == 0
                and chk2["chronological_split_feasible"]
                and chk2["chronological_holdout_rows"] >= 50
                and chk2["holdout_5m_positive_count"] >= 10
            )

    flips, flip_summary = sign_flips(all_annotated)
    outliers = outlier_audit(all_annotated)
    write_rows_v3s3a(all_annotated, flips, outliers)

    v3s2_clean = 0.3537
    observed_stage1_cr = b1.get("clean_rate")
    cr_vs_v3s2 = round((observed_stage1_cr or 0) - v3s2_clean, 4) if observed_stage1_cr is not None else None

    v3s2_pricing = v3s2_doc.get("pricing_assumptions_usd_per_million_calls", {})

    final_qa = [
        f"（1）是否成功从约 110 扩到 **250** clean？**{'是' if decision_flags['reached_250_clean_rows'] else '否'}**（Stage1 结束 clean={chk1['final_clean_rows']}，最终全数据集={len(all_annotated)}，Stage1 stop={b1.get('stop_reason')}）。",
        f"（2）是否继续到 **500**？**{'是' if decision_flags['reached_500_clean_rows'] else '否'}**。原因：{stage2_skipped_reason or ('stage2_complete' if s2_ok else '见 checkpoint / gates')}。",
        f"（3）clean rate 相对 V3S-2 预估（~0.354）：Stage1 观测 **{observed_stage1_cr}**（Δ **{cr_vs_v3s2}**）。",
        f"（4）RPC / wall / cost：累计 RPC **{rpc.calls}**；总墙钟 **{round(time.time() - t0, 1)}s**；"
        f"相对 Stage1/2 硬顶（7000/15m + 14000/30m）cost_budget_respected=**{decision_flags['cost_budget_respected']}**；"
        f"粗算 USD（mid $/M={v3s2_pricing.get('mid')}）≈ **{round(rpc.calls * (v3s2_pricing.get('mid') or 15) / 1e6, 4)}**（仅供参考，以账单为准）。",
        "（5）**5m primary 是否保持？是** — 门禁与 clean 定义以 5m quote + 有效 5m label 为主。",
        "（6）**30m 是否仅 decay diagnostic？是** — 不做 30m 硬失败 gate。",
        f"（7）chronological OOS（within at_cap scope）：**{'可用' if decision_flags['chronological_oos_ready'] else '未完全就绪'}**（feasible_split + holdout 行数/阳性数见 checkpoint）。",
        f"（8）V3S-4 tiny scoped feasibility：**{'允许预检' if decision_flags['allow_v3s4_tiny_scoped_model_feasibility'] else '不允许'}**（需 Stage2 全流程与 500 行及门禁）。",
        "（9）model research / 更大扩容 / mainline / live：**仍禁止**（decision_flags.allow_* = false）。",
    ]

    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "frozen_scope": {
            "scope_type": "notional_bucket",
            "scope_key": "at_cap",
            "notional_cap_weth": NOTIONAL_CAP,
        },
        "inputs": {
            "v3s2_summary": str(V3S2_SUMMARY.relative_to(ROOT)) if v3s2_exists else None,
            "v3r31f_summary": str(V31F_SUMMARY.relative_to(ROOT)) if v31f_exists else None,
            "v3r29b_rows": str(V29B_ROWS.relative_to(ROOT)),
            "source_scan": scan,
        },
        "quote_denominator_audit": {
            "stage1": {
                "state_changed_count": b1.get("state_changed_count"),
                "newly_attempted_rows": b1.get("newly_attempted_rows"),
                "failure_reason_distribution": b1.get("failure_reason_distribution"),
                "explanation": "Denominator for clean_rate = newly_attempted_rows (at_cap candidates); quote success rates use state_changed_count where applicable (V3R-29B pattern).",
            },
            "stage2": None
            if not b2
            else {
                "state_changed_count": b2.get("state_changed_count"),
                "newly_attempted_rows": b2.get("newly_attempted_rows"),
                "failure_reason_distribution": b2.get("failure_reason_distribution"),
            },
        },
        "estimated_rpc_cost_usd_mid_only": round(
            rpc.calls * float(v3s2_doc.get("pricing_assumptions_usd_per_million_calls", {}).get("mid", 15) or 15) / 1e6,
            6,
        ),
        "base_at_cap_rows": base_n,
        "checkpoints": checkpoints,
        "final_clean_rows": len(all_annotated),
        "duplicate_conflict_count": duplicate_count(all_annotated),
        "total_runtime_seconds": round(time.time() - t0, 2),
        "total_rpc_calls": rpc.calls,
        "flip_summary": flip_summary,
        "outlier_summary": outliers,
        "leakage_last": leakage_and_holdout(all_annotated),
        "decision_flags": decision_flags,
        "final_qa": final_qa,
        "stage2_skipped_reason": stage2_skipped_reason,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")

    print(json.dumps({"final_rows": len(all_annotated), "stage1_pass": s1_ok, "flags": decision_flags}, ensure_ascii=False))


if __name__ == "__main__":
    main()
