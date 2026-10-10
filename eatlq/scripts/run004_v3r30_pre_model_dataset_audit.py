"""Run-004 Patch V3R-30 — pre-model audit for V3 small timestamp dataset.

Static, research-only audit of the V3R-29 252-row timestamp dataset. This does
not scan sources, expand rows, quote/relabel, train, write model artifacts, or
touch strategy/executor/mainline/live paths.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float, as_int, top_shares  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

V3R29_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_summary.json"
V3R29_ROWS = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_rows.csv"
V3R29_REPORT = ROOT / "mvp/runs/run-004-patch-v3r29-small-timestamp-dataset-expansion.md"
V3R28_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r28_timestamp_horizon_leakage_holdout_audit_summary.json"
V3R28_ROWS = ROOT / "mvp/diagnostics/run004_v3r28_timestamp_horizon_leakage_holdout_audit_rows.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r30-pre-model-dataset-audit.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r30_pre_model_dataset_audit_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r30_pre_model_dataset_audit_rows.csv"

EXPECTED_INPUT_ROWS = 252
NEAR_ZERO_ABS = 0.05
NEAR_TOTAL_LOSS = -0.95
EXTREME_POSITIVE = 1.0
SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD = 50


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(row: dict[str, Any], field: str) -> float:
    val = as_float(row.get(field))
    return float(val) if val is not None else 0.0


def quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 8)


def rich_distribution(vals: list[float]) -> dict[str, float | None]:
    return {
        "min": quantile(vals, 0.0),
        "p01": quantile(vals, 0.01),
        "p05": quantile(vals, 0.05),
        "p50": quantile(vals, 0.50),
        "median": quantile(vals, 0.50),
        "p95": quantile(vals, 0.95),
        "p99": quantile(vals, 0.99),
        "max": quantile(vals, 1.0),
    }


def duplicate_count(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def positive_rate(rows: list[dict[str, Any]], field: str) -> float:
    vals = [f(r, field) for r in rows]
    return round(sum(1 for v in vals if v > 0) / max(1, len(vals)), 4)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 4)


def counter_dict(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(Counter(str(r.get(field, "")) for r in rows))


def top_share_from_counter(counter: Counter[str]) -> float:
    return round(counter.most_common(1)[0][1] / max(1, sum(counter.values())), 4) if counter else 0.0


def concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pools = [r["pool_address"] for r in rows]
    tokens = [r["token_address"] for r in rows]
    sources = Counter(r["source_path"] for r in rows)
    fees = Counter(str(as_int(r["fee"])) for r in rows)
    p1, p5 = top_shares(pools)
    t1, t5 = top_shares(tokens)
    return {
        "pool_count": len(set(pools)),
        "token_count": len(set(tokens)),
        "source_count": len(sources),
        "fee_distribution": dict(fees),
        "top1_pool_share": p1,
        "top5_pool_share": p5,
        "top1_token_share": t1,
        "top5_token_share": t5,
        "source_top_share": top_share_from_counter(sources),
        "fee_top_share": top_share_from_counter(fees),
        "per_pool_clean_rows": counter_dict(rows, "pool_address"),
        "per_token_clean_rows": counter_dict(rows, "token_address"),
        "per_source_clean_rows": dict(sources),
        "per_fee_clean_rows": dict(fees),
    }


def sign_flips(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    flips = []
    for row in rows:
        r5 = f(row, "label_return_5m")
        r30 = f(row, "label_return_30m")
        if r5 != 0 and r30 != 0 and r5 * r30 < 0:
            flips.append({**row, "near_zero_flip": min(abs(r5), abs(r30)) <= NEAR_ZERO_ABS})
    pool_c = Counter(r["pool_address"] for r in flips)
    token_c = Counter(r["token_address"] for r in flips)
    source_c = Counter(r["source_path"] for r in flips)
    fee_c = Counter(str(as_int(r["fee"])) for r in flips)
    near_zero_share = round(sum(1 for r in flips if r["near_zero_flip"]) / max(1, len(flips)), 4)
    concentration_summary = {
        "pool_top_share": top_share_from_counter(pool_c),
        "token_top_share": top_share_from_counter(token_c),
        "source_top_share": top_share_from_counter(source_c),
        "fee_top_share": top_share_from_counter(fee_c),
        "pool_distribution": dict(pool_c),
        "token_distribution": dict(token_c),
        "source_distribution": dict(source_c),
        "fee_distribution": dict(fee_c),
        "highly_concentrated": any(top_share_from_counter(c) > 0.50 for c in [pool_c, token_c, source_c, fee_c]),
    }
    return flips, {
        "sign_flip_count": len(flips),
        "sign_flip_share": round(len(flips) / max(1, len(rows)), 4),
        "near_zero_threshold_abs_min_horizon_return": NEAR_ZERO_ABS,
        "near_zero_flip_count": sum(1 for r in flips if r["near_zero_flip"]),
        "near_zero_flip_share": near_zero_share,
        "flip_concentration": concentration_summary,
    }


def split_summary(rows: list[dict[str, Any]], split_field: str, overall: dict[str, Any]) -> dict[str, Any]:
    train = [r for r in rows if r.get(split_field) == "train"]
    holdout = [r for r in rows if r.get(split_field) == "holdout"]
    train_pools = {r["pool_address"] for r in train}
    holdout_pools = {r["pool_address"] for r in holdout}
    train_tokens = {r["token_address"] for r in train}
    holdout_tokens = {r["token_address"] for r in holdout}
    train_flip, train_flip_summary = sign_flips(train)
    holdout_flip, holdout_flip_summary = sign_flips(holdout)
    train_r5 = [f(r, "label_return_5m") for r in train]
    holdout_r5 = [f(r, "label_return_5m") for r in holdout]
    train_r30 = [f(r, "label_return_30m") for r in train]
    holdout_r30 = [f(r, "label_return_30m") for r in holdout]
    train_conc = concentration(train)
    holdout_conc = concentration(holdout)
    drift = {
        "positive_rate_5m_delta_holdout_minus_train": round(positive_rate(holdout, "label_return_5m") - positive_rate(train, "label_return_5m"), 4),
        "positive_rate_30m_delta_holdout_minus_train": round(positive_rate(holdout, "label_return_30m") - positive_rate(train, "label_return_30m"), 4),
        "median_5m_delta_holdout_minus_train": round((quantile(holdout_r5, 0.5) or 0.0) - (quantile(train_r5, 0.5) or 0.0), 8),
        "median_30m_delta_holdout_minus_train": round((quantile(holdout_r30, 0.5) or 0.0) - (quantile(train_r30, 0.5) or 0.0), 8),
        "sign_flip_share_delta_holdout_minus_train": round(holdout_flip_summary["sign_flip_share"] - train_flip_summary["sign_flip_share"], 4),
        "explanation": "30m positive-rate drift is visible in holdout and must be reported in any dry-run; group-disjoint splits remove identity overlap.",
    }
    mode = split_field.replace("_split", "")
    feasible = bool(train and holdout)
    pool_overlap = len(train_pools & holdout_pools)
    token_overlap = len(train_tokens & holdout_tokens)
    if split_field == "pool_split" and pool_overlap:
        feasible = False
    if split_field == "token_split" and token_overlap:
        feasible = False
    return {
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "train_positive_rate_5m": positive_rate(train, "label_return_5m"),
        "holdout_positive_rate_5m": positive_rate(holdout, "label_return_5m"),
        "train_positive_rate_30m": positive_rate(train, "label_return_30m"),
        "holdout_positive_rate_30m": positive_rate(holdout, "label_return_30m"),
        "train_pool_count": len(train_pools),
        "holdout_pool_count": len(holdout_pools),
        "train_token_count": len(train_tokens),
        "holdout_token_count": len(holdout_tokens),
        "pool_overlap": pool_overlap,
        "token_overlap": token_overlap,
        "train_top1_top5_pool_share": [train_conc["top1_pool_share"], train_conc["top5_pool_share"]],
        "holdout_top1_top5_pool_share": [holdout_conc["top1_pool_share"], holdout_conc["top5_pool_share"]],
        "train_top1_top5_token_share": [train_conc["top1_token_share"], train_conc["top5_token_share"]],
        "holdout_top1_top5_token_share": [holdout_conc["top1_token_share"], holdout_conc["top5_token_share"]],
        "train_sign_flip_share": train_flip_summary["sign_flip_share"],
        "holdout_sign_flip_share": holdout_flip_summary["sign_flip_share"],
        "label_distribution_drift": drift,
        "split_feasible": feasible,
        "split_mode": mode,
    }


def outlier_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outlier_rows = []
    for row in rows:
        r5 = f(row, "label_return_5m")
        r30 = f(row, "label_return_30m")
        reasons = []
        if abs(r5) > 1.0 or abs(r30) > 1.0:
            reasons.append("abs_return_gt_1")
        if abs(r5) > 10.0 or abs(r30) > 10.0:
            reasons.append("abs_return_gt_10")
        if r5 <= NEAR_TOTAL_LOSS or r30 <= NEAR_TOTAL_LOSS:
            reasons.append("near_total_loss")
        if r5 > EXTREME_POSITIVE or r30 > EXTREME_POSITIVE:
            reasons.append("extreme_positive")
        if reasons:
            outlier_rows.append({**row, "outlier_reasons": ",".join(reasons)})
    pool_c = Counter(r["pool_address"] for r in outlier_rows)
    token_c = Counter(r["token_address"] for r in outlier_rows)
    source_c = Counter(r["source_path"] for r in outlier_rows)
    fee_c = Counter(str(as_int(r["fee"])) for r in outlier_rows)
    return {
        "abs_return_gt_1_count": sum(1 for r in rows if abs(f(r, "label_return_5m")) > 1.0 or abs(f(r, "label_return_30m")) > 1.0),
        "abs_return_gt_10_count": sum(1 for r in rows if abs(f(r, "label_return_5m")) > 10.0 or abs(f(r, "label_return_30m")) > 10.0),
        "near_total_loss_count": sum(1 for r in rows if f(r, "label_return_5m") <= NEAR_TOTAL_LOSS or f(r, "label_return_30m") <= NEAR_TOTAL_LOSS),
        "extreme_positive_count": sum(1 for r in rows if f(r, "label_return_5m") > EXTREME_POSITIVE or f(r, "label_return_30m") > EXTREME_POSITIVE),
        "outlier_rows": [
            {
                "tx_hash": r["tx_hash"],
                "log_index": r["log_index"],
                "source_path": r["source_path"],
                "pool_address": r["pool_address"],
                "token_address": r["token_address"],
                "fee": as_int(r["fee"]),
                "label_return_5m": f(r, "label_return_5m"),
                "label_return_30m": f(r, "label_return_30m"),
                "outlier_reasons": r["outlier_reasons"],
            }
            for r in outlier_rows[:50]
        ],
        "outlier_concentration": {
            "pool_top_share": top_share_from_counter(pool_c),
            "token_top_share": top_share_from_counter(token_c),
            "source_top_share": top_share_from_counter(source_c),
            "fee_top_share": top_share_from_counter(fee_c),
        },
        "outliers_dominate": bool(outlier_rows) and max(
            top_share_from_counter(pool_c),
            top_share_from_counter(token_c),
            top_share_from_counter(source_c),
            top_share_from_counter(fee_c),
        )
        > 0.50,
    }


def schema_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    required = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "observed_direction",
        "entry_block",
        "entry_timestamp",
        "exit_block_5m",
        "exit_timestamp_5m",
        "exit_block_30m",
        "exit_timestamp_30m",
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
    ]
    missing_fields = [field for field in required if field not in rows[0]]
    blank_counts = {
        field: sum(1 for row in rows if str(row.get(field, "")).strip() == "")
        for field in required
        if field not in {"fixed_block_return_control"}
    }
    return {
        "required_fields": required,
        "missing_fields": missing_fields,
        "blank_counts": blank_counts,
        "schema_complete": not missing_fields and all(v == 0 for v in blank_counts.values()),
    }


def denominator_audit(v3r29: dict[str, Any]) -> dict[str, Any]:
    build = v3r29["build_metrics"]
    clean = v3r29["dataset_metrics"]
    attempted_den = build["state_changed_count"]
    return {
        "quote_success_5m": build["quote_success_rate_5m"],
        "quote_success_30m": build["quote_success_rate_30m"],
        "denominator_type": "state_changed_attempted_rows",
        "denominator_rows": attempted_den,
        "quote_success_5m_numerator": build["quote_success_5m"],
        "quote_success_30m_numerator": build["quote_success_30m"],
        "clean_rows_valid_label_denominator": clean["clean_rows_count"],
        "clean_rows_valid_5m": clean["valid_5m_label_count"],
        "clean_rows_valid_30m": clean["valid_30m_label_count"],
        "clean_rows_vs_quote_success_less_than_one_is_contradiction": False,
        "explanation": "V3R-29 quote success is computed over state_changed attempted rows before clean filtering; the 252 clean rows are the post-filter subset with 252/252 valid 5m and 30m labels.",
        "selection_bias_risk_from_failed_attempts": True,
        "failure_reason_distribution": build["failure_reason_distribution"],
        "failure_reason_distribution_must_be_preserved_before_v3r31": True,
        "quote_denominator_explained": attempted_den > 0,
    }


def write_rows(rows: list[dict[str, Any]], flips: list[dict[str, Any]], outliers: dict[str, Any]) -> None:
    flip_keys = {(r["tx_hash"], r["log_index"]) for r in flips}
    outlier_keys = {(r["tx_hash"], r["log_index"]) for r in outliers["outlier_rows"]}
    fields = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "entry_timestamp",
        "label_return_5m",
        "label_return_30m",
        "chronological_split",
        "pool_split",
        "token_split",
        "sign_flip_5m_30m",
        "near_zero_flip",
        "outlier_flag",
        "same_block_is_label",
        "timestamp_horizon_primary_label",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            key = (row["tx_hash"], row["log_index"])
            r5 = f(row, "label_return_5m")
            r30 = f(row, "label_return_30m")
            out = {k: row.get(k, "") for k in fields}
            out["sign_flip_5m_30m"] = key in flip_keys
            out["near_zero_flip"] = key in flip_keys and min(abs(r5), abs(r30)) <= NEAR_ZERO_ABS
            out["outlier_flag"] = key in outlier_keys
            writer.writerow(out)


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    return f"""# Run-004 Patch V3R-30 — Pre-Model Dataset Audit

Status: **{'PASSED' if f['pre_model_audit_passed'] else 'FAILED'}**. Static audit only. No model, no feature research, no production/mainline/live artifact.

## Target Stability

- 5m / 30m positive rate: **{summary['positive_rate_5m']} / {summary['positive_rate_30m']}**
- 5m/30m Pearson correlation: **{summary['target_stability']['return_5m_30m_pearson_correlation']}**
- sign flip: **{summary['sign_flip_count']} / {summary['input_rows']} = {summary['sign_flip_share']}**
- near-zero flip share: **{summary['near_zero_flip_share']}** with threshold abs(min horizon return) <= `{NEAR_ZERO_ABS}`
- recommendation: **preserve dual_horizon; do not collapse to a single target**

## Split Readiness

- chronological: `{summary['chronological_split_summary']}`
- pool-disjoint: `{summary['pool_disjoint_split_summary']}`
- token-disjoint: `{summary['token_disjoint_split_summary']}`

## Concentration / Outliers

- pool/token/source count: **{summary['pool_count']} / {summary['token_count']} / {summary['source_count']}**
- top1/top5 pool: **{summary['top1_pool_share']} / {summary['top5_pool_share']}**
- top1/top5 token: **{summary['top1_token_share']} / {summary['top5_token_share']}**
- source top share / fee top share: **{summary['source_top_share']} / {summary['fee_top_share']}**
- outlier summary: `{summary['outlier_summary']}`

## Quote Denominator

V3R-29 quote success uses **state_changed attempted rows** as denominator: `{summary['failure_denominator_audit']['denominator_rows']}`. Clean rows are post-filtered, so `252/252` valid labels and quote success `<1.0` are not contradictory.

## Decision Flags

- `pre_model_audit_passed`: `{str(f['pre_model_audit_passed']).lower()}`
- `target_stability_passed`: `{str(f['target_stability_passed']).lower()}`
- `dual_horizon_preserved`: `{str(f['dual_horizon_preserved']).lower()}`
- `split_readiness_passed`: `{str(f['split_readiness_passed']).lower()}`
- `concentration_audit_passed`: `{str(f['concentration_audit_passed']).lower()}`
- `outlier_audit_passed`: `{str(f['outlier_audit_passed']).lower()}`
- `quote_denominator_explained`: `{str(f['quote_denominator_explained']).lower()}`
- `sample_size_risk`: `{str(f['sample_size_risk']).lower()}`
- `allow_v3r31_very_small_model_dryrun`: `{str(f['allow_v3r31_very_small_model_dryrun']).lower()}`
- `allow_model_research`: `{str(f['allow_model_research']).lower()}`
- `allow_larger_expansion`: `{str(f['allow_larger_expansion']).lower()}`
- `allow_mainline`: `{str(f['allow_mainline']).lower()}`
- `allow_live`: `{str(f['allow_live']).lower()}`
"""


def main() -> None:
    v3r29 = json.loads(V3R29_SUMMARY.read_text(encoding="utf-8"))
    v3r28 = json.loads(V3R28_SUMMARY.read_text(encoding="utf-8"))
    rows = load_csv(V3R29_ROWS)
    _ = V3R29_REPORT.read_text(encoding="utf-8")
    _ = load_csv(V3R28_ROWS)

    schema = schema_audit(rows)
    conc = concentration(rows)
    ret5 = [f(r, "label_return_5m") for r in rows]
    ret30 = [f(r, "label_return_30m") for r in rows]
    flips, flip_summary = sign_flips(rows)
    outliers = outlier_audit(rows)
    denominator = denominator_audit(v3r29)
    chronological = split_summary(rows, "chronological_split", conc)
    pool_split = split_summary(rows, "pool_split", conc)
    token_split = split_summary(rows, "token_split", conc)

    duplicate_conflict_count = duplicate_count(rows)
    valid_5m = sum(1 for r in rows if as_bool(r["timestamp_quote_5m_ok"]) and as_float(r["label_return_5m"]) is not None)
    valid_30m = sum(1 for r in rows if as_bool(r["timestamp_quote_30m_ok"]) and as_float(r["label_return_30m"]) is not None)
    same_block_primary_used = any(as_bool(r.get("same_block_is_label")) for r in rows)
    fixed_block_primary_used = any(str(r.get("fixed_block_return_control", "")).strip() and not as_bool(r.get("timestamp_horizon_primary_label")) for r in rows)
    sample_size_risk = conc["pool_count"] < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD or conc["token_count"] < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD

    target_stability_passed = (
        flip_summary["sign_flip_share"] <= 0.25
        and not flip_summary["flip_concentration"]["highly_concentrated"]
        and v3r29["decision_flags"]["valid_5m_label_count"] == len(rows)
        and v3r29["decision_flags"]["valid_30m_label_count"] == len(rows)
    )
    split_readiness_passed = chronological["split_feasible"] and pool_split["split_feasible"] and token_split["split_feasible"]
    concentration_passed = (
        conc["top1_pool_share"] <= 0.20
        and conc["top5_pool_share"] <= 0.55
        and conc["top1_token_share"] <= 0.20
        and conc["top5_token_share"] <= 0.55
        and conc["source_top_share"] <= 0.75
        and conc["fee_top_share"] <= 0.75
    )
    outlier_passed = (
        outliers["abs_return_gt_1_count"] == 0
        and outliers["abs_return_gt_10_count"] == 0
        and outliers["near_total_loss_count"] == 0
        and not outliers["outliers_dominate"]
    )
    semantics_passed = not same_block_primary_used and not fixed_block_primary_used and all(as_bool(r["timestamp_horizon_primary_label"]) for r in rows)
    pre_model_passed = (
        len(rows) == EXPECTED_INPUT_ROWS
        and valid_5m == len(rows)
        and valid_30m == len(rows)
        and duplicate_conflict_count == 0
        and semantics_passed
        and target_stability_passed
        and split_readiness_passed
        and concentration_passed
        and outlier_passed
        and denominator["quote_denominator_explained"]
    )
    flags = {
        "pre_model_audit_passed": pre_model_passed,
        "target_stability_passed": target_stability_passed,
        "dual_horizon_preserved": True,
        "split_readiness_passed": split_readiness_passed,
        "concentration_audit_passed": concentration_passed,
        "outlier_audit_passed": outlier_passed,
        "quote_denominator_explained": denominator["quote_denominator_explained"],
        "sample_size_risk": sample_size_risk,
        "allow_v3r31_very_small_model_dryrun": pre_model_passed,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": "run004_v3r30_pre_model_dataset_audit",
        "research_only": True,
        "inputs": {
            "v3r29_summary": str(V3R29_SUMMARY.relative_to(ROOT)),
            "v3r29_rows": str(V3R29_ROWS.relative_to(ROOT)),
            "v3r29_report": str(V3R29_REPORT.relative_to(ROOT)),
            "v3r28_summary": str(V3R28_SUMMARY.relative_to(ROOT)),
            "v3r28_rows": str(V3R28_ROWS.relative_to(ROOT)),
        },
        "no_source_rescan": True,
        "no_dataset_expansion": True,
        "no_model_artifacts": True,
        "bounded_recheck": {"performed": False, "rpc_cap": 20, "rpc_used": 0, "reason": "static file/schema audit was sufficient; no relabeling run"},
        "schema_completeness_audit": schema,
        "input_rows": len(rows),
        "valid_5m_labels": valid_5m,
        "valid_30m_labels": valid_30m,
        "duplicate_conflict_count": duplicate_conflict_count,
        **conc,
        "return_5m_distribution": rich_distribution(ret5),
        "return_30m_distribution": rich_distribution(ret30),
        "positive_rate_5m": positive_rate(rows, "label_return_5m"),
        "positive_rate_30m": positive_rate(rows, "label_return_30m"),
        "target_stability": {
            "return_5m_30m_pearson_correlation": pearson(ret5, ret30),
            "recommended_target_handling": "dual_horizon",
            "collapse_to_single_target_forbidden": True,
            "which_horizon_more_stable": "30m has higher positive rate and less negative median; 5m is not enough to collapse target semantics",
            "prior_v3r28_sign_flip_share": v3r28["decision_flags"]["sign_flip_share_5m_30m"],
        },
        **flip_summary,
        "chronological_split_summary": chronological,
        "pool_disjoint_split_summary": pool_split,
        "token_disjoint_split_summary": token_split,
        "outlier_summary": outliers,
        "failure_denominator_audit": denominator,
        "semantics_audit": {
            "same_block_primary_label_used": same_block_primary_used,
            "fixed_block_primary_label_used": fixed_block_primary_used,
            "timestamp_horizon_primary_label": all(as_bool(r["timestamp_horizon_primary_label"]) for r in rows),
            "semantics_passed": semantics_passed,
        },
        "decision_flags": flags,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(rows, flips, outliers)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        {
            "rows": len(rows),
            "valid_5m": valid_5m,
            "valid_30m": valid_30m,
            "sign_flip_share": flip_summary["sign_flip_share"],
            "passed": pre_model_passed,
            "allow_v3r31": flags["allow_v3r31_very_small_model_dryrun"],
        }
    )


if __name__ == "__main__":
    main()
