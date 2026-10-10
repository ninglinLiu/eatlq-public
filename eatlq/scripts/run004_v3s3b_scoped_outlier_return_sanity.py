"""Run-004 V3S-3B — static 5m/30m return & outlier sanity audit on V3S-3A dataset.

Research-only: read CSV/summary JSON, no quote, no expansion, no training, no label changes.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_RUN = "run004_v3s3b_scoped_outlier_return_sanity"
V3S3A_JSON = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_summary.json"
V3S3A_CSV = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_rows.csv"
V3S3A_MD = ROOT / "mvp/runs/run-004-v3s3a-staged-scoped-5m-expansion.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s3b_scoped_outlier_return_sanity_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v3s3b-scoped-outlier-return-sanity.md"

EXTREME_POSITIVE = 1.0
NEAR_TOTAL_LOSS = -0.95
ABS_TIER_05 = 0.5
ABS_TIER_10 = 1.0
ABS_TIER_20 = 2.0
# Gates (documented in summary)
MEAN_LEVERAGE_FAIL = 0.35  # |sum(r) abs>1| / max(|sum(all r)|, eps)
L1_SHARE_FAIL = 0.45  # sum(|r| for abs>1) / sum(|r|) above → dominance concern
TOPK_POSITIVE_FRAGILE = 0.55  # top 5 rows' share of sum(max(0,r))
HOLDOUT_POSITIVE_FROM_EXTREME = 0.60  # holdout max(0,r) from |r|>1 rows
OUTLIER_ENTITY_CONCENTRATION = 0.50  # single pool|token|source|fee share among outlier rows
POS_RATE_STABILITY_MAX_DROP = 0.10  # after drop top-|r| outliers, pr drop <= this (absolute)


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 10)


def return_distribution(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    vals: list[float] = []
    for r in rows:
        v = as_float(r.get(field))
        if v is None:
            continue
        vals.append(float(v))
    n = len(vals)
    if not n:
        return {"count": 0}
    abs_vals = [abs(v) for v in vals]
    pos_mask = [v > 0 for v in vals]
    return {
        "count": n,
        "min": quantile(vals, 0.0),
        "p01": quantile(vals, 0.01),
        "p05": quantile(vals, 0.05),
        "p25": quantile(vals, 0.25),
        "p50": quantile(vals, 0.50),
        "median": quantile(vals, 0.50),
        "p75": quantile(vals, 0.75),
        "p95": quantile(vals, 0.95),
        "p99": quantile(vals, 0.99),
        "max": quantile(vals, 1.0),
        "mean": round(sum(vals) / n, 10),
        "abs_return_gt_0_5_count": sum(1 for v in vals if abs(v) > ABS_TIER_05),
        "abs_return_gt_1_0_count": sum(1 for v in vals if abs(v) > ABS_TIER_10),
        "abs_return_gt_2_0_count": sum(1 for v in vals if abs(v) > ABS_TIER_20),
        "extreme_positive_count": sum(1 for v in vals if v > EXTREME_POSITIVE),
        "near_total_loss_count": sum(1 for v in vals if v <= NEAR_TOTAL_LOSS),
        "positive_rate": round(sum(pos_mask) / n, 4),
        "sum_positive_returns": round(sum(v for v in vals if v > 0), 10),
        "sum_abs_returns": round(sum(abs_vals), 10),
    }


def r5(r: dict[str, Any]) -> float:
    v = as_float(r.get("label_return_5m"))
    return float(v) if v is not None else 0.0


def r30(r: dict[str, Any]) -> float:
    v = as_float(r.get("label_return_30m"))
    return float(v) if v is not None else 0.0


def is_extreme_abs_5m(r: dict[str, Any]) -> bool:
    return abs(r5(r)) > ABS_TIER_10


def is_outlier_row_any(r: dict[str, Any]) -> bool:
    return bool(as_bool(r.get("outlier_flag"))) or abs(r5(r)) > ABS_TIER_10 or abs(r30(r)) > ABS_TIER_10


def top_k_indices_by_abs(rows: list[dict[str, Any]], k: int) -> set[int]:
    idx_val = sorted(range(len(rows)), key=lambda i: abs(r5(rows[i])), reverse=True)
    return set(idx_val[: min(k, len(rows))])


def subset_without_indices(rows: list[dict[str, Any]], drop: set[int]) -> list[dict[str, Any]]:
    return [r for i, r in enumerate(rows) if i not in drop]


def mean_leverage_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [r5(r) for r in rows]
    s_all = sum(vals)
    abs_idx = [i for i, v in enumerate(vals) if abs(v) > ABS_TIER_10]
    s_ext = sum(vals[i] for i in abs_idx)
    sum_abs_all = sum(abs(v) for v in vals)
    sum_abs_ext = sum(abs(vals[i]) for i in abs_idx)
    eps = 1e-12
    return {
        "sum_returns_all": round(s_all, 10),
        "sum_returns_abs_gt_1": round(s_ext, 10),
        "mean_leverage_ratio_abs_num": round(abs(s_ext) / max(abs(s_all), eps), 4),
        "l1_share_abs_gt_1": round(sum_abs_ext / max(sum_abs_all, eps), 4),
        "rows_abs_gt_1_count": len(abs_idx),
    }


def topk_positive_mass(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    enriched: list[tuple[int, dict[str, Any], float]] = [
        (i, r, max(0.0, r5(r))) for i, r in enumerate(rows)
    ]
    enriched.sort(key=lambda t: t[2], reverse=True)
    top = enriched[:k]
    total_pos = sum(max(0.0, r5(r)) for r in rows)
    top_sum = sum(t[2] for t in top)
    eps = 1e-12
    return {
        "top_k": k,
        "share_of_sum_positive_returns": round(top_sum / max(total_pos, eps), 4),
        "top_rows": [
            {
                "tx_hash": t[1].get("tx_hash"),
                "log_index": t[1].get("log_index"),
                "label_return_5m": r5(t[1]),
                "positive_part": round(t[2], 8),
                "pool_address": t[1].get("pool_address"),
                "token_address": t[1].get("token_address"),
                "source_path": t[1].get("source_path"),
                "fee": t[1].get("fee"),
            }
            for t in top
        ],
    }


def outlier_concentration(out_rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(out_rows)
    if not n:
        return {"outlier_row_count": 0}
    pc = Counter(str(r.get("pool_address", "")) for r in out_rows)
    tc = Counter(str(r.get("token_address", "")) for r in out_rows)
    sc = Counter(str(r.get("source_path", "")) for r in out_rows)
    fc = Counter(str(r.get("fee", "")) for r in out_rows)

    def top_share(cnt: Counter) -> tuple[str, int, float]:
        k, v = cnt.most_common(1)[0]
        return k, v, round(v / n, 4)

    tp, tpn, tps = top_share(pc)
    tt, ttn, tts = top_share(tc)
    ts, tsn, tss = top_share(sc)
    tf, tfn, tfs = top_share(fc)
    return {
        "outlier_row_count": n,
        "pool_top": {"key": tp, "count": tpn, "share": tps},
        "token_top": {"key": tt, "count": ttn, "share": tts},
        "source_top": {"key": ts, "count": tsn, "share": tss},
        "fee_top": {"key": tf, "count": tfn, "share": tfs},
        "any_entity_share_exceeds_gate": max(tps, tts, tss, tfs) >= OUTLIER_ENTITY_CONCENTRATION,
    }


def extreme_outlier_rows_detail(rows: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    cand = [r for r in rows if abs(r5(r)) > ABS_TIER_10 or abs(r30(r)) > EXTREME_POSITIVE or r5(r) > EXTREME_POSITIVE]
    cand.sort(key=lambda r: abs(r5(r)), reverse=True)
    out = []
    for r in cand[:limit]:
        out.append(
            {
                "tx_hash": r.get("tx_hash"),
                "log_index": r.get("log_index"),
                "label_return_5m": r5(r),
                "label_return_30m": r30(r),
                "pool_address": r.get("pool_address"),
                "token_address": r.get("token_address"),
                "source_path": r.get("source_path"),
                "fee": r.get("fee"),
                "outlier_flag": bool(as_bool(r.get("outlier_flag"))),
            }
        )
    return out


def row_soft_outlier_train_hold(r: dict[str, Any]) -> bool:
    return abs(r5(r)) > ABS_TIER_05 or bool(as_bool(r.get("outlier_flag")))


def chronological_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    train = [r for r in rows if r.get("chronological_split") == "train"]
    hold = [r for r in rows if r.get("chronological_split") == "holdout"]
    tr = [r5(r) for r in train]
    ho = [r5(r) for r in hold]

    def outlier_count(sub: list[dict[str, Any]]) -> int:
        return sum(1 for r in sub if is_outlier_row_any(r))

    hold_pos = sum(max(0.0, r5(r)) for r in hold)
    hold_pos_ext = sum(max(0.0, r5(r)) for r in hold if is_extreme_abs_5m(r))
    eps = 1e-12
    share_ext = round(hold_pos_ext / max(hold_pos, eps), 4)
    return {
        "train_rows": len(train),
        "holdout_rows": len(hold),
        "train_outlier_count": outlier_count(train),
        "holdout_outlier_count": outlier_count(hold),
        "train_soft_outlier_count_abs_gt_0_5_or_flag": sum(1 for r in train if row_soft_outlier_train_hold(r)),
        "holdout_soft_outlier_count_abs_gt_0_5_or_flag": sum(1 for r in hold if row_soft_outlier_train_hold(r)),
        "train_positive_rate_5m": round(sum(1 for v in tr if v > 0) / max(1, len(tr)), 4),
        "holdout_positive_rate_5m": round(sum(1 for v in ho if v > 0) / max(1, len(ho)), 4),
        "train_median_return_5m": quantile(tr, 0.5),
        "holdout_median_return_5m": quantile(ho, 0.5),
        "train_mean_return_5m": round(sum(tr) / max(1, len(tr)), 8) if tr else None,
        "holdout_mean_return_5m": round(sum(ho) / max(1, len(ho)), 8) if ho else None,
        "holdout_positive_return_mass_from_abs_gt_1_rows": share_ext,
        "holdout_positive_mass_from_extreme_abs_gt_1_failed_gate": share_ext >= HOLDOUT_POSITIVE_FROM_EXTREME,
    }


def top_decile_return_bucket_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """No model score: sort by 5m return (desc). Top decile (~50 rows @ N=500) outlier diagnostic only."""
    n = len(rows)
    if not n:
        return {"note": "empty"}
    dec = max(1, math.ceil(0.10 * n))
    ordered = sorted(range(n), key=lambda i: r5(rows[i]), reverse=True)
    top_ix = set(ordered[:dec])
    ext_in_top = sum(1 for i in top_ix if is_extreme_abs_5m(rows[i]))
    flag_in_top = sum(1 for i in top_ix if as_bool(rows[i].get("outlier_flag")))
    return {
        "decile_row_count": dec,
        "top_decile_extreme_abs_gt_1_count": ext_in_top,
        "top_decile_outlier_flag_count": flag_in_top,
        "top_decile_extreme_share": round(ext_in_top / dec, 4),
        "top_bucket_return_dominated_by_extremes": (ext_in_top / dec) >= 0.20,
        "disclaimer": "Return-sorted diagnostic only — not a model score or claimed alpha.",
    }


def robustness_after_dropping_top_abs(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    full_vals = [r5(r) for r in rows]
    base_pr = round(sum(1 for v in full_vals if v > 0) / max(1, len(full_vals)), 4)
    base_med = quantile(full_vals, 0.5)
    base_mean = round(sum(full_vals) / max(1, len(full_vals)), 8)
    out: dict[str, Any] = {}
    for k in ks:
        drop = top_k_indices_by_abs(rows, k)
        rest = [v for i, v in enumerate(full_vals) if i not in drop]
        if not rest:
            out[f"drop_top_{k}"] = {"error": "no_rows_left"}
            continue
        pr = round(sum(1 for v in rest if v > 0) / len(rest), 4)
        out[f"drop_top_{k}"] = {
            "remaining_rows": len(rest),
            "positive_rate_5m": pr,
            "median_return_5m": quantile(rest, 0.5),
            "mean_return_5m": round(sum(rest) / len(rest), 8),
            "positive_rate_delta_vs_full": round(pr - base_pr, 4),
            "median_delta_vs_full": None
            if base_med is None or quantile(rest, 0.5) is None
            else round(float(quantile(rest, 0.5) or 0) - float(base_med or 0), 10),
            "mean_delta_vs_full": round(sum(rest) / len(rest) - base_mean, 10),
        }
    out["full_baseline"] = {
        "positive_rate_5m": base_pr,
        "median_return_5m": base_med,
        "mean_return_5m": base_mean,
    }
    return out


def evaluate_gates(
    ml: dict[str, Any],
    top5_pos: dict[str, Any],
    top10_pos: dict[str, Any],
    conc: dict[str, Any],
    chrono: dict[str, Any],
    robust: dict[str, Any],
    dist5: dict[str, Any],
) -> dict[str, Any]:
    mean_fail = (
        ml["mean_leverage_ratio_abs_num"] >= MEAN_LEVERAGE_FAIL
        or ml["l1_share_abs_gt_1"] >= L1_SHARE_FAIL
    )
    fragile = (
        top5_pos["share_of_sum_positive_returns"] >= TOPK_POSITIVE_FRAGILE
        or top10_pos["share_of_sum_positive_returns"] >= max(TOPK_POSITIVE_FRAGILE, 0.65)
    )
    entity_conc = conc.get("any_entity_share_exceeds_gate", False)
    holdout_extreme = chrono.get("holdout_positive_mass_from_extreme_abs_gt_1_failed_gate", False)

    base_pr = robust["full_baseline"]["positive_rate_5m"]
    drop5 = robust.get("drop_top_5", {})
    drop10 = robust.get("drop_top_10", {})
    pr_after_5 = drop5.get("positive_rate_5m") if isinstance(drop5, dict) else None
    pr_after_10 = drop10.get("positive_rate_5m") if isinstance(drop10, dict) else None
    stable_drop5 = (
        isinstance(pr_after_5, float)
        and base_pr - pr_after_5 <= POS_RATE_STABILITY_MAX_DROP
        and pr_after_5 >= 0.25
    )
    stable_drop10 = (
        isinstance(pr_after_10, float)
        and base_pr - pr_after_10 <= POS_RATE_STABILITY_MAX_DROP + 0.03
        and pr_after_10 >= 0.22
    )
    signal_persists_after_drop = stable_drop5 or stable_drop10

    outliers_dominate_v3s3a_reason = (
        f"abs_gt_1 rows n={dist5.get('abs_return_gt_1_0_count')} extreme_positive n={dist5.get('extreme_positive_count')}; "
        f"mean_leverage |sum(r,|r|>1)|/|sum(r)|={ml['mean_leverage_ratio_abs_num']}, "
        f"L1-share |r|>1 / sum|r|={ml['l1_share_abs_gt_1']}"
    )

    outlier_dominance_failed = mean_fail
    fragile_return_distribution = fragile

    concentration_blocks_v3s4 = entity_conc

    chronological_oos_return_sane = (
        not holdout_extreme
        and chrono.get("holdout_outlier_count", 999)
        <= max(30, int(math.ceil(0.35 * chrono.get("holdout_rows", 1))))
    )

    structural_audit_ok = (
        not outlier_dominance_failed
        and not fragile_return_distribution
        and not concentration_blocks_v3s4
        and not holdout_extreme
        and chronological_oos_return_sane
    )

    allow_v3s4 = structural_audit_ok and signal_persists_after_drop

    return_distribution_sane = (
        dist5.get("abs_return_gt_1_0_count", 999) <= 20
        and dist5.get("near_total_loss_count", 999) == 0
        and not mean_fail
    )

    if allow_v3s4:
        next_step = "proceed_V3S-4_tiny_scoped_model_feasibility_precheck"
    elif not structural_audit_ok:
        if concentration_blocks_v3s4 and outlier_dominance_failed:
            next_step = "V3S-3C_outlier_policy_repair_entity_concentration_and_mean_leverage"
        elif concentration_blocks_v3s4:
            next_step = "V3S-3C_outlier_policy_repair_entity_concentration"
        elif holdout_extreme:
            next_step = "V3S-3C_outlier_policy_repair_holdout_positive_mass"
        elif outlier_dominance_failed or fragile_return_distribution:
            next_step = "V3S-3C_outlier_policy_repair_return_mass"
        elif not chronological_oos_return_sane:
            next_step = "V3S-3C_outlier_policy_repair_chrono_holdout_shape"
        else:
            next_step = "V3S-3C_outlier_policy_repair"
    elif not signal_persists_after_drop:
        next_step = "V3S-3C_outlier_policy_repair_robustness"
    else:
        next_step = "V3S-3C_outlier_policy_repair"

    blockers_ordered: list[str] = []
    if outlier_dominance_failed:
        blockers_ordered.append("mean_leverage_abs_gt_1_vs_total_sum")
    if fragile_return_distribution:
        blockers_ordered.append("fragile_topk_positive_mass")
    if concentration_blocks_v3s4:
        blockers_ordered.append("scoped_outlier_entity_concentration")
    if holdout_extreme:
        blockers_ordered.append("holdout_positive_mass_from_abs_gt_1")
    if not chronological_oos_return_sane:
        blockers_ordered.append("chronological_oos_return_shape")
    if structural_audit_ok and not signal_persists_after_drop:
        blockers_ordered.append("robustness_after_drop_top_failed")

    return {
        "structural_audit_ok": structural_audit_ok,
        "outlier_dominance_failed": outlier_dominance_failed,
        "fragile_return_distribution": fragile_return_distribution,
        "chronological_oos_return_sane": chronological_oos_return_sane,
        "concentration_blocks_v3s4": concentration_blocks_v3s4,
        "holdout_extreme_positive_gate_failed": holdout_extreme,
        "signal_persists_after_drop_top_outliers": signal_persists_after_drop,
        "robustness_drop_top_5_stable": stable_drop5,
        "robustness_drop_top_10_stable": stable_drop10,
        "return_distribution_sane": return_distribution_sane,
        "allow_v3s4_tiny_scoped_model_feasibility": allow_v3s4,
        "v3s3b_outlier_audit_passed": structural_audit_ok,
        "next_step_recommendation": next_step,
        "blockers_ordered": blockers_ordered,
        "v3s3a_outliers_dominate_explanation": outliers_dominate_v3s3a_reason,
    }


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    qa = summary.get("final_qa", [])
    lines = [
        "# Run-004 V3S-3B — Scoped 5m outlier / return sanity (static)",
        "",
        "Research-only. No quote, no expansion, no label edits.",
        "",
        f"**Rows:** {summary['row_count']} (expected 500-at-cap slice from V3S-3A).",
        "",
        "## Final Q&A",
        "",
    ]
    for i, q in enumerate(qa, 1):
        lines.append(f"{i}. {q}")
    lines.extend(
        [
            "",
            "## Decision flags",
            "",
            "```json",
            json.dumps(f, indent=2, ensure_ascii=False),
            "```",
            "",
            "## Active blockers (gates_evaluated)",
            "",
            json.dumps(summary.get("gates_evaluated", {}).get("blockers_ordered", []), ensure_ascii=False),
            "",
            "## Gate thresholds (this run)",
            "",
            f"- `MEAN_LEVERAGE_FAIL` (|sum(r,|r|>1)| / |sum(r)|): **{MEAN_LEVERAGE_FAIL}**",
            f"- `L1_SHARE_FAIL` sum(|r| for |r|>1)/sum(|r|): **{L1_SHARE_FAIL}**",
            f"- `TOPK_POSITIVE_FRAGILE` top-5 share of sum(max(0,r)): **{TOPK_POSITIVE_FRAGILE}**",
            f"- `HOLDOUT_POSITIVE_FROM_EXTREME`: **{HOLDOUT_POSITIVE_FROM_EXTREME}**",
            f"- `OUTLIER_ENTITY_CONCENTRATION`: **{OUTLIER_ENTITY_CONCENTRATION}**",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    if not V3S3A_CSV.exists():
        raise SystemExit(f"missing {V3S3A_CSV}")
    rows = load_csv(V3S3A_CSV)
    n = len(rows)
    v3s3a_summary = json.loads(V3S3A_JSON.read_text(encoding="utf-8")) if V3S3A_JSON.exists() else {}
    _ = V3S3A_MD.exists()

    dist5 = return_distribution(rows, "label_return_5m")
    dist30 = return_distribution(rows, "label_return_30m")
    ml = mean_leverage_metrics(rows)
    top5 = topk_positive_mass(rows, 5)
    top10 = topk_positive_mass(rows, 10)
    out_rows = [r for r in rows if is_outlier_row_any(r)]
    conc = outlier_concentration(out_rows)
    chrono = chronological_metrics(rows)
    bucket = top_decile_return_bucket_diagnostic(rows)
    robust = robustness_after_dropping_top_abs(rows, [1, 5, 10])
    detail = extreme_outlier_rows_detail(rows, 25)

    gates = evaluate_gates(ml, top5, top10, conc, chrono, robust, dist5)

    decision_flags = {
        "v3s3b_outlier_audit_passed": gates["v3s3b_outlier_audit_passed"],
        "return_distribution_sane": gates["return_distribution_sane"],
        "outlier_dominance_failed": gates["outlier_dominance_failed"],
        "fragile_return_distribution": gates["fragile_return_distribution"],
        "chronological_oos_return_sane": gates["chronological_oos_return_sane"],
        "allow_v3s4_tiny_scoped_model_feasibility": gates["allow_v3s4_tiny_scoped_model_feasibility"],
        "allow_training": False,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }

    v3s3a_prior_allow = (
        v3s3a_summary.get("decision_flags", {}).get("allow_v3s4_tiny_scoped_model_feasibility")
    )

    final_qa = [
        f"（1）500 行 scoped 5m return 分布是否正常？**{'基本正常' if gates['return_distribution_sane'] else '偏Heavy-tail / 极端值偏厚'}** "
        f"— 中位 {dist5.get('median')}，|r|>1 共 **{dist5.get('abs_return_gt_1_0_count')}**，极端正收益 **{dist5.get('extreme_positive_count')}**。",
        f"（2）V3S-3A `outliers_dominate=true` 的原因：**{gates['v3s3a_outliers_dominate_explanation']}**；"
        f"top-5 占 sum(max(0,r)) **{top5['share_of_sum_positive_returns']}**，top-10 **{top10['share_of_sum_positive_returns']}**。",
        f"（3）极端收益是否集中在少数 pool/token/source/fee？**{'是' if conc.get('any_entity_share_exceeds_gate') else '否（未过 50% 单实体集中度门槛）'}** "
        f"— outlier 行 pool_top_share={conc.get('pool_top', {}).get('share')}, token={conc.get('token_top', {}).get('share')}, "
        f"source={conc.get('source_top', {}).get('share')}, fee={conc.get('fee_top', {}).get('share')}。",
        f"（4）去掉 top-|r| 后 5m 信号是否还在？drop_top_5 PR={robust.get('drop_top_5', {}).get('positive_rate_5m')} "
        f"(Δ {robust.get('drop_top_5', {}).get('positive_rate_delta_vs_full')})；"
        f"drop_top_10 PR={robust.get('drop_top_10', {}).get('positive_rate_5m')}；"
        f"persist_flag=**{gates['signal_persists_after_drop_top_outliers']}**。",
        f"（5）chronological holdout 是否主要靠 outlier 撑阳性质量？**{'是（危险）' if gates['holdout_extreme_positive_gate_failed'] else '否'}** "
        f"— holdout |r|>1 贡献 positive mass 比例 **{chrono.get('holdout_positive_return_mass_from_abs_gt_1_rows')}**；"
        f"holdout outlier 行 **{chrono.get('holdout_outlier_count')}** / {chrono.get('holdout_rows')}。",
        f"（6）是否允许进入 V3S-4？**{'允许' if gates['allow_v3s4_tiny_scoped_model_feasibility'] else '不允许'}**"
        f"（V3S-3A 曾设 allow_v3s4={v3s3a_prior_allow}；本审计覆盖该 flag）。",
        f"（7）若不允许：**暂停 V3S-4**；下一步 **{gates['next_step_recommendation']}**；"
        f"阻塞项（有序）**{gates.get('blockers_ordered', [])}** — 非静默删样本，需显式 outlier policy。",
    ]

    summary = {
        "run": SOURCE_RUN,
        "generator_script": "scripts/run004_v3s3b_scoped_outlier_return_sanity.py",
        "research_only": True,
        "no_quote": True,
        "no_expansion": True,
        "inputs": {
            "v3s3a_summary": str(V3S3A_JSON.relative_to(ROOT)),
            "v3s3a_rows": str(V3S3A_CSV.relative_to(ROOT)),
            "v3s3a_md": str(V3S3A_MD.relative_to(ROOT)),
        },
        "row_count": n,
        "v3s3a_prior_allow_v3s4": v3s3a_prior_allow,
        "return_5m_distribution": dist5,
        "return_30m_diagnostic_distribution": dist30,
        "mean_and_mass_leverage_abs_gt_1": ml,
        "top_positive_mass": {"top_5": top5, "top_10": top10},
        "outlier_row_concentration": conc,
        "extreme_outlier_rows_sample": detail,
        "chronological_oos_return_audit": chrono,
        "top_decile_return_sort_diagnostic": bucket,
        "robustness_after_drop_top_abs": robust,
        "gates_evaluated": gates,
        "decision_flags": decision_flags,
        "final_qa": final_qa,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(json.dumps({"rows": n, "flags": decision_flags}, ensure_ascii=False))


if __name__ == "__main__":
    main()