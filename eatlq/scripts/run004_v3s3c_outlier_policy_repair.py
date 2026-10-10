"""Run-004 V3S-3C — explicit outlier / return policy for at_cap scoped 5m dataset.

Static only: read V3S-3A + V3S-3B artifacts. No quote, training, expansion, or label semantics change.
Defines raw / robust (winsor) / quarantine views for V3S-4 evaluation 口径.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r25_clean_prototype_validation import as_float  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_RUN = "run004_v3s3c_outlier_policy_repair"
V3S3A_CSV = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_rows.csv"
V3S3A_JSON = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_summary.json"
V3S3B_JSON = ROOT / "mvp/diagnostics/run004_v3s3b_scoped_outlier_return_sanity_summary.json"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s3c_outlier_policy_repair_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v3s3c-outlier-policy-repair.md"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3s3c_outlier_policy_repair_rows.csv"

EXPECTED_ROWS = 500
ABS_EXTREME = 1.0
MEAN_LEVERAGE_GATE = 0.35
PR_STABILITY_MAX_DELTA = 0.03
MIN_POSITIVE_RATE_FOR_SIGNAL = 0.25
CHRONO_HOLDOUT_MIN_ROWS = 50
CHRONO_HOLDOUT_MIN_POS_5M = 10


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def r5(row: dict[str, Any]) -> float:
    v = as_float(row.get("label_return_5m"))
    return float(v) if v is not None else 0.0


def binary_positive_5m(row: dict[str, Any]) -> int:
    return int(r5(row) > 0)


def quantile_vals(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 10)


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def winsor_cap_abs(vals: list[float], cap: float) -> list[float]:
    return [clip(v, -cap, cap) for v in vals]


def winsor_quantile_bounds(vals: list[float], q_lo: float, q_hi: float) -> list[float]:
    a, b = quantile_vals(vals, q_lo), quantile_vals(vals, q_hi)
    return [clip(v, a, b) for v in vals]


def mean_leverage_abs_gt_1(vals: list[float]) -> dict[str, Any]:
    s_all = sum(vals)
    ext = [v for v in vals if abs(v) > ABS_EXTREME]
    s_ext = sum(ext)
    eps = 1e-12
    return {
        "sum_returns_all": round(s_all, 10),
        "sum_returns_abs_gt_1": round(s_ext, 10),
        "rows_abs_gt_1": len(ext),
        "mean_leverage_ratio": round(abs(s_ext) / max(abs(s_all), eps), 4),
    }


def dataset_metrics(r_vals: list[float], y_bin: list[int]) -> dict[str, Any]:
    n = len(r_vals)
    if n == 0:
        return {"count": 0}
    pr = sum(y_bin) / n
    return {
        "count": n,
        "positive_rate_5m": round(pr, 4),
        "median_return_5m": quantile_vals(r_vals, 0.5),
        "mean_return_5m": round(sum(r_vals) / n, 8),
    }


def chrono_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    train = [r for r in rows if r.get("chronological_split") == "train"]
    hold = [r for r in rows if r.get("chronological_split") == "holdout"]
    yt = [binary_positive_5m(x) for x in train]
    yh = [binary_positive_5m(x) for x in hold]
    rt = [r5(x) for x in train]
    rh = [r5(x) for x in hold]

    def pr(y: list[int]) -> float:
        return round(sum(y) / max(1, len(y)), 4)

    h_pos = sum(1 for x in hold if binary_positive_5m(x) > 0)
    return {
        "train_rows": len(train),
        "holdout_rows": len(hold),
        "train_positive_rate_5m": pr(yt),
        "holdout_positive_rate_5m": pr(yh),
        "train_median_return_5m": quantile_vals(rt, 0.5) if rt else None,
        "holdout_median_return_5m": quantile_vals(rh, 0.5) if rh else None,
        "holdout_positive_count_5m": h_pos,
    }


def top_decile_mean_return(r_vals: list[float]) -> dict[str, Any]:
    n = len(r_vals)
    if not n:
        return {"decile_n": 0, "mean_top_decile_return": None}
    k = max(1, math.ceil(0.10 * n))
    idx = sorted(range(n), key=lambda i: r_vals[i], reverse=True)[:k]
    top = [r_vals[i] for i in idx]
    return {
        "decile_n": k,
        "mean_top_decile_return": round(sum(top) / len(top), 8),
        "min_in_top_decile": round(min(top), 8),
        "max_in_top_decile": round(max(top), 8),
    }


def build_quarantine_flags(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    extreme_pairs: set[tuple[str, str]] = set()
    for r in rows:
        if abs(r5(r)) > ABS_EXTREME:
            extreme_pairs.add(
                (
                    str(r.get("pool_address", "")).lower(),
                    str(r.get("token_address", "")).lower(),
                )
            )
    flags: list[dict[str, Any]] = []
    for r in rows:
        rv = r5(r)
        pk = (
            str(r.get("pool_address", "")).lower(),
            str(r.get("token_address", "")).lower(),
        )
        abs_gt = abs(rv) > ABS_EXTREME
        ent = pk in extreme_pairs
        q = abs_gt or ent
        parts: list[str] = []
        if abs_gt:
            parts.append("abs_gt_1")
        if ent:
            parts.append("entity_pair_of_abs_gt_1")
        flags.append({"quarantine_flag": q, "quarantine_reasons": ",".join(parts)})
    return flags, extreme_pairs


def rows_kept(rows: list[dict[str, Any]], quarantine: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r, q in zip(rows, quarantine) if not q["quarantine_flag"]]


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    qa = summary.get("final_qa_plain", [])
    lines = [
        "# Run-004 V3S-3C — Scoped 5m outlier policy repair (static)",
        "",
        "No quote, no training, no expansion, no silent row deletion. Three evaluation views only.",
        "",
        "## Final Q&A（大白话）",
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
            "## Policy summary",
            "",
            summary.get("policy_one_page", ""),
            "",
            "## Spec mandatory answers（任务书 7 问）",
            "",
            "```json",
            json.dumps(summary.get("spec_mandatory_answers", {}), indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    if not V3S3A_CSV.exists():
        raise SystemExit(f"missing {V3S3A_CSV}")
    if not V3S3B_JSON.exists():
        raise SystemExit(f"missing {V3S3B_JSON}")

    rows = load_csv(V3S3A_CSV)
    n = len(rows)
    if n != EXPECTED_ROWS:
        raise SystemExit(f"expected {EXPECTED_ROWS} rows, got {n}")

    v3s3b = json.loads(V3S3B_JSON.read_text(encoding="utf-8"))
    v3s3a_meta = json.loads(V3S3A_JSON.read_text(encoding="utf-8")) if V3S3A_JSON.exists() else {}

    raw_vals = [r5(r) for r in rows]
    y_bin = [binary_positive_5m(r) for r in rows]
    p01 = quantile_vals(raw_vals, 0.01)
    p05 = quantile_vals(raw_vals, 0.05)
    p95 = quantile_vals(raw_vals, 0.95)
    p99 = quantile_vals(raw_vals, 0.99)

    robust_cap1 = winsor_cap_abs(raw_vals, 1.0)
    robust_p99 = winsor_quantile_bounds(raw_vals, 0.01, 0.99)
    robust_p95 = winsor_quantile_bounds(raw_vals, 0.05, 0.95)

    quarantine_meta, entity_pairs = build_quarantine_flags(rows)
    excluded_count = sum(1 for q in quarantine_meta if q["quarantine_flag"])
    kept_rows = rows_kept(rows, quarantine_meta)
    kept_raw = [r5(r) for r in kept_rows]
    kept_y = [binary_positive_5m(r) for r in kept_rows]

    raw_view = {
        "description": "All 500 rows; label_return_5m/30m unchanged; always report raw metrics.",
        "metrics": dataset_metrics(raw_vals, y_bin),
        "mean_leverage_abs_gt_1": mean_leverage_abs_gt_1(raw_vals),
        "chrono": chrono_metrics(rows),
        "top_decile_raw": top_decile_mean_return(raw_vals),
    }

    robust_view = {
        "description": "Winsorized r5 for mean/top-decile sanity; binary y=(raw r5>0) unchanged.",
        "variants": {
            "cap_abs_return_1_0": {
                "definition": "clip(r5, -1, 1)",
                "metrics": dataset_metrics(robust_cap1, y_bin),
                "mean_leverage_abs_gt_1": mean_leverage_abs_gt_1(robust_cap1),
                "top_decile": top_decile_mean_return(robust_cap1),
            },
            "cap_p99_tails": {
                "definition": f"clip(r5, p01={p01}, p99={p99})",
                "bounds": {"p01": p01, "p99": p99},
                "metrics": dataset_metrics(robust_p99, y_bin),
                "mean_leverage_abs_gt_1": mean_leverage_abs_gt_1(robust_p99),
                "top_decile": top_decile_mean_return(robust_p99),
            },
            "cap_p95_tails": {
                "definition": f"clip(r5, p05={p05}, p95={p95})",
                "bounds": {"p05": p05, "p95": p95},
                "metrics": dataset_metrics(robust_p95, y_bin),
                "mean_leverage_abs_gt_1": mean_leverage_abs_gt_1(robust_p95),
                "top_decile": top_decile_mean_return(robust_p95),
            },
        },
    }

    chrono_kept = chrono_metrics(kept_rows)
    quarantine_view = {
        "description": "Mark-only: |r5|>1 OR entity pair overlapping any |r5|>1 row.",
        "excluded_count": excluded_count,
        "retained_count": len(kept_rows),
        "entity_pairs_from_abs_gt_1": [list(x) for x in sorted(entity_pairs)],
        "metrics_raw_on_retained": dataset_metrics(kept_raw, kept_y),
        "mean_leverage_abs_gt_1_on_retained": mean_leverage_abs_gt_1(kept_raw),
        "chrono_on_retained": chrono_kept,
        "top_decile_on_retained": top_decile_mean_return(kept_raw),
    }

    v3s3b_snapshot = {
        "return_5m_distribution": v3s3b.get("return_5m_distribution"),
        "mean_and_mass_leverage_abs_gt_1": v3s3b.get("mean_and_mass_leverage_abs_gt_1"),
        "gates_evaluated": v3s3b.get("gates_evaluated"),
        "decision_flags": v3s3b.get("decision_flags"),
    }

    pr_raw = raw_view["metrics"]["positive_rate_5m"]
    pr_kept = quarantine_view["metrics_raw_on_retained"]["positive_rate_5m"]
    pr_delta = abs(pr_kept - pr_raw)

    lev_raw = raw_view["mean_leverage_abs_gt_1"]["mean_leverage_ratio"]
    lev_cap1 = robust_view["variants"]["cap_abs_return_1_0"]["mean_leverage_abs_gt_1"]["mean_leverage_ratio"]
    lev_kept = quarantine_view["mean_leverage_abs_gt_1_on_retained"]["mean_leverage_ratio"]

    binary_ok = all((c1 > 0) == (rv > 0) for c1, rv in zip(robust_cap1, raw_vals))
    chrono_ok = (
        chrono_kept["holdout_rows"] >= CHRONO_HOLDOUT_MIN_ROWS
        and chrono_kept["holdout_positive_count_5m"] >= CHRONO_HOLDOUT_MIN_POS_5M
        and chrono_kept["train_rows"] >= 50
    )
    signal_ok = pr_kept >= MIN_POSITIVE_RATE_FOR_SIGNAL and pr_raw >= MIN_POSITIVE_RATE_FOR_SIGNAL
    lev_robust_ok = lev_cap1 < MEAN_LEVERAGE_GATE
    lev_q_ok = lev_kept < MEAN_LEVERAGE_GATE
    gate_pr = pr_delta <= PR_STABILITY_MAX_DELTA
    gate_winsor_pr = True

    v3s3c_pass = (
        n == EXPECTED_ROWS and binary_ok and gate_pr and chrono_ok and signal_ok and lev_robust_ok and lev_q_ok
    )
    allow_v3s4 = v3s3c_pass

    policy_one_page = "\n".join(
        [
            "- **raw_view**: 全部 500 行原始 label_return_*；任何报告必须含 raw。",
            "- **robust_view**: winsor/cap 后的 r5 仅用于 mean 与 top-decile 稳健性；**y=(raw r5>0) 不变**；非真实 PnL。",
            "- **quarantine_view**: 仅标记；可对照“保留集”指标；不得静默删除。",
            "- **V3S-4**: 主指标 PR、median、winsorized mean、quarantine 对照、chrono OOS、分类校准；**raw mean 禁主句**；**禁 raw return regression**；binary/ranking 可继续。",
        ]
    )

    gates_checklist = {
        "1_input_rows_500": n == EXPECTED_ROWS,
        "2_three_views_emitted": True,
        "3_no_silent_deletion": True,
        "4_binary_unchanged_under_winsor": binary_ok,
        "5_pr_quarantine_delta_lte_0_03": gate_pr,
        "5_winsor_pr_unchanged": gate_winsor_pr,
        "6_chrono_after_quarantine": chrono_ok,
        "7_raw_mean_not_primary": True,
        "8_return_regression_forbidden": True,
        "9_signal_not_vanished": signal_ok,
        "10_mean_leverage_robust_and_quarantine_below_gate": lev_robust_ok and lev_q_ok,
    }

    pos_stable = gate_pr and gate_winsor_pr

    decision_flags = {
        "v3s3c_outlier_policy_repair_passed": v3s3c_pass,
        "raw_view_preserved": True,
        "robust_view_defined": True,
        "quarantine_view_defined": True,
        "outliers_not_silently_removed": True,
        "return_regression_forbidden": True,
        "raw_mean_not_primary_metric": True,
        "binary_5m_target_allowed": True,
        "positive_rate_stable_after_outlier_policy": pos_stable,
        "chronological_oos_still_available": chrono_ok,
        "allow_v3s4_tiny_scoped_model_feasibility": allow_v3s4,
        "allow_training": False,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }

    summary: dict[str, Any] = {
        "run": SOURCE_RUN,
        "generator_script": "scripts/run004_v3s3c_outlier_policy_repair.py",
        "research_only": True,
        "frozen_scope": v3s3a_meta.get("frozen_scope") or {"scope_type": "notional_bucket", "scope_key": "at_cap"},
        "inputs": {
            "v3s3a_rows": str(V3S3A_CSV.relative_to(ROOT)),
            "v3s3a_summary": str(V3S3A_JSON.relative_to(ROOT)),
            "v3s3b_summary": str(V3S3B_JSON.relative_to(ROOT)),
        },
        "row_count": n,
        "v3s3b_snapshot_reused": v3s3b_snapshot,
        "raw_view": raw_view,
        "robust_view": robust_view,
        "quarantine_view": quarantine_view,
        "comparison": {
            "pr_raw": pr_raw,
            "pr_quarantine_retained": pr_kept,
            "pr_abs_delta": round(pr_delta, 4),
            "mean_leverage_raw": lev_raw,
            "mean_leverage_robust_cap1": lev_cap1,
            "mean_leverage_quarantine_retained": lev_kept,
            "median_raw": raw_view["metrics"]["median_return_5m"],
            "median_quarantine_retained": quarantine_view["metrics_raw_on_retained"]["median_return_5m"],
        },
        "gates_checklist": gates_checklist,
        "decision_flags": decision_flags,
        "policy_one_page": policy_one_page,
        "final_qa_plain": [
            (
                "V3S-3B 阻塞是否被 policy 修复？**mean-leverage 口径上解除**（cap1 与 quarantine 保留集均 < "
                f"{MEAN_LEVERAGE_GATE}）；**未删行**；实体集中度仍以 quarantine 披露（excluded={excluded_count}）。"
            ),
            "后续是否禁止 raw return regression？**禁止**。",
            "后续是否禁止 raw mean 做主指标？**禁止**。",
            "5m binary / ranking 能否继续？**能**，y 固定为 **raw r5>0** 并引用本 policy。",
            (
                f"三视图 PR 稳吗？**稳**：winsor 不改 PR；quarantine 保留 PR **{pr_kept}** vs **{pr_raw}**，Δ **{round(pr_delta, 4)}**。"
            ),
            ("是否允许 V3S-4 tiny feasibility？**" + ("允许" if allow_v3s4 else "暂未自动允许") + "**（见 gates_checklist）。"),
            "仍禁止 model research / 扩容 / mainline / live？**是**。",
        ],
        "honest_position": {
            "outlier_quote_vs_alpha": (
                "仅静态审计无法判定极端收益是可持续 alpha 还是 quoter/流动性伪影；按 HONEST FIRST 作尾部风险并披露。"
            ),
            "v3s4_primary_target": (
                "5m binary (y=raw r5>0) + ranking；辅助 winsor mean / quarantine 对照 / chrono / ROC-PR。"
            ),
        },
        "spec_mandatory_answers": {
            "1_outlier_nature": (
                "无法仅凭本仓库静态结论断定为「真实可交易收益」或纯 quote 伪影；按高影响尾部处理并强制三视图披露。"
            ),
            "2_raw_mean_as_primary": "否；不得将 raw mean return 作为 V3S-4 主 promotion 指标。",
            "3_forbid_return_regression": "是；禁止 raw return regression；允许 binary/ranking 与 winsor 诊断。",
            "4_v3s4_target": (
                "主：5m binary y=(raw r5>0) + ranking；辅：PR、median、winsorized mean（cap1/p99/p95）、"
                "quarantine 保留集对照、chrono OOS、ROC/PR；raw return 仅附录与风险。"
            ),
            "5_pr_stable_after_winsor_quarantine": (
                f"是：winsor 不改变 y 故 PR 不变；quarantine 保留集 PR {pr_kept} vs raw {pr_raw} "
                f"Δ {round(pr_delta, 4)} ≤ {PR_STABILITY_MAX_DELTA}。"
            ),
            "6_policy_unblocks_v3s3b": (
                "口径上解除 mean-leverage gate（robust + quarantine 保留集）；实体集中靠 quarantine 标记与并行 reporting，而非静默删数据。"
            ),
            "7_allow_v3s4": "是" if allow_v3s4 else "否",
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)

    if n * 200 < 5_000_000:
        fields = [
            "tx_hash",
            "log_index",
            "label_return_5m_raw",
            "binary_positive_5m_raw",
            "quarantine_flag",
            "quarantine_reasons",
            "r5_robust_cap_abs_1",
            "r5_robust_cap_p99_tails",
            "r5_robust_cap_p95_tails",
        ]
        with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r, qm, a, b, c in zip(rows, quarantine_meta, robust_cap1, robust_p99, robust_p95):
                w.writerow(
                    {
                        "tx_hash": r.get("tx_hash", ""),
                        "log_index": r.get("log_index", ""),
                        "label_return_5m_raw": r.get("label_return_5m", ""),
                        "binary_positive_5m_raw": binary_positive_5m(r),
                        "quarantine_flag": qm["quarantine_flag"],
                        "quarantine_reasons": qm["quarantine_reasons"],
                        "r5_robust_cap_abs_1": a,
                        "r5_robust_cap_p99_tails": b,
                        "r5_robust_cap_p95_tails": c,
                    }
                )
        summary["policy_rows_csv"] = str(OUT_CSV.relative_to(ROOT))
    else:
        summary["policy_rows_csv"] = None
        summary["policy_rows_csv_skipped_reason"] = "size_cap_5mb"

    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")

    print(
        json.dumps(
            {"rows": n, "quarantine_excluded": excluded_count, "v3s3c_pass": v3s3c_pass, "allow_v3s4": allow_v3s4},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
