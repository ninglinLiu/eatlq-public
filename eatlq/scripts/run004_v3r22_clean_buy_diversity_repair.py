"""Run-004 Patch V3R-22 — clean buy diversity repair.

Research-only/read-only. Reconstructs V3R-21 clean buy labels and tests
diversity-aware sampling strategies. No expansion, training, promotion, or
runtime changes.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

V3R21_JSON = ROOT / "mvp/diagnostics/run004_v3r21_v3_buy_source_label_quality_validation_summary.json"
V3R21_CSV = ROOT / "mvp/diagnostics/run004_v3r21_v3_buy_source_label_quality_validation_rows.csv"
V3R20_JSON = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_summary.json"
V3R20_CSV = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_rows.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r22-clean-buy-diversity-repair.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r22_clean_buy_diversity_repair_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r22_clean_buy_diversity_repair_rows.csv"

SOURCE_RUN = "run004_v3r22_clean_buy_diversity_repair"
NEAR_TOTAL_LOSS = -0.95


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def load_rows() -> list[dict[str, Any]]:
    rows = []
    for row in csv.DictReader(V3R21_CSV.open("r", encoding="utf-8")):
        r = dict(row)
        r["fee_int"] = int(r["fee"])
        r["label_return_float"] = float(r["label_return"]) if r.get("label_return") not in ("", None) else None
        r["pool_address"] = str(r["pool_address"]).lower()
        r["token_address"] = str(r["token_address"]).lower()
        rows.append(r)
    return rows


def field_audit() -> dict[str, Any]:
    header = read_header(V3R21_CSV)
    required = [
        "pool_address",
        "token_address",
        "fee",
        "state_changed",
        "policy_attempted",
        "horizon_quote_ok",
        "label_return",
        "toxicity_class",
    ]
    return {
        "v3r21_rows": str(V3R21_CSV.relative_to(ROOT)),
        "v3r21_header": header,
        "required_fields_present": {f: f in header for f in required},
        "fields_sufficient": all(f in header for f in required),
    }


def is_clean(row: dict[str, Any]) -> bool:
    ret = row["label_return_float"]
    return (
        row.get("toxicity_class") == "clean_label"
        and str(row.get("state_changed", "")).lower() == "true"
        and str(row.get("horizon_quote_ok", "")).lower() == "true"
        and ret is not None
        and ret > NEAR_TOTAL_LOSS
    )


def quantile(values: list[float], q: float) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    idx = (len(vals) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    frac = idx - lo
    return round(vals[lo] * (1 - frac) + vals[hi] * frac, 8)


def distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "min": quantile(values, 0),
        "p5": quantile(values, 0.05),
        "median": quantile(values, 0.50),
        "p95": quantile(values, 0.95),
        "max": quantile(values, 1),
    }


def share(values: list[str]) -> dict[str, float]:
    if not values:
        return {"top1": 0.0, "top5": 0.0}
    counts = Counter(values)
    return {
        "top1": round(counts.most_common(1)[0][1] / len(values), 4),
        "top5": round(sum(v for _, v in counts.most_common(5)) / len(values), 4),
    }


def universe_audit(clean: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(r["label_return_float"]) for r in clean]
    pool_s = share([r["pool_address"] for r in clean])
    token_s = share([r["token_address"] for r in clean])
    return {
        "clean_candidate_count": len(clean),
        "clean_candidate_pool_count": len({r["pool_address"] for r in clean}),
        "clean_candidate_token_count": len({r["token_address"] for r in clean}),
        "clean_candidate_fee_distribution": dict(Counter(str(r["fee_int"]) for r in clean)),
        "clean_return_distribution": distribution(returns),
        "clean_top1_pool_share": pool_s["top1"],
        "clean_top5_pool_share": pool_s["top5"],
        "clean_top1_token_share": token_s["top1"],
        "clean_top5_token_share": token_s["top5"],
    }


def duplicate_conflict_count(rows: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = f"{r['pool_address']}|{r['token_address']}|{r.get('label_return')}"
        grouped.setdefault(key, []).append(r)
    return sum(1 for v in grouped.values() if len(v) > 1 and len({x.get("toxicity_class") for x in v}) > 1)


def evaluate_strategy(name: str, rows: list[dict[str, Any]], failure_reason_hint: str = "") -> dict[str, Any]:
    returns = [float(r["label_return_float"]) for r in rows if r["label_return_float"] is not None]
    pool_s = share([r["pool_address"] for r in rows])
    token_s = share([r["token_address"] for r in rows])
    near_share = round(sum(1 for x in returns if x <= NEAR_TOTAL_LOSS) / max(1, len(returns)), 4)
    dup = duplicate_conflict_count(rows)
    pass_gate = (
        len(rows) >= 20
        and pool_s["top1"] <= 0.20
        and pool_s["top5"] <= 0.55
        and token_s["top1"] <= 0.20
        and token_s["top5"] <= 0.55
        and near_share <= 0.10
        and dup == 0
    )
    failures = []
    if len(rows) < 20:
        failures.append("selected_rows_count_lt_20")
    if pool_s["top1"] > 0.20:
        failures.append("top1_pool_share_gt_0.20")
    if pool_s["top5"] > 0.55:
        failures.append("top5_pool_share_gt_0.55")
    if token_s["top1"] > 0.20:
        failures.append("top1_token_share_gt_0.20")
    if token_s["top5"] > 0.55:
        failures.append("top5_token_share_gt_0.55")
    if near_share > 0.10:
        failures.append("near_total_loss_share_gt_0.10")
    if dup != 0:
        failures.append("duplicate_conflict_count_nonzero")
    if failure_reason_hint:
        failures.append(failure_reason_hint)
    dist = distribution(returns)
    return {
        "strategy_name": name,
        "selected_rows_count": len(rows),
        "selected_pool_count": len({r["pool_address"] for r in rows}),
        "selected_token_count": len({r["token_address"] for r in rows}),
        "top1_pool_share": pool_s["top1"],
        "top5_pool_share": pool_s["top5"],
        "top1_token_share": token_s["top1"],
        "top5_token_share": token_s["top5"],
        "fee_distribution": dict(Counter(str(r["fee_int"]) for r in rows)),
        "return_distribution": dist,
        "near_total_loss_share": near_share,
        "median_return": dist["median"],
        "p95_return": dist["p95"],
        "max_return": dist["max"],
        "duplicate_conflict_count": dup,
        "pass_diversity_gate": pass_gate,
        "failure_reason": ", ".join(failures) if failures else "",
        "rows": rows,
    }


def greedy_cap(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    selected = []
    pool_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    for r in rows:
        if pool_counts[r["pool_address"]] >= cap or token_counts[r["token_address"]] >= cap:
            continue
        selected.append(r)
        pool_counts[r["pool_address"]] += 1
        token_counts[r["token_address"]] += 1
    return selected


def downweight(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool_total = Counter(r["pool_address"] for r in rows)
    token_total = Counter(r["token_address"] for r in rows)
    ordered = sorted(
        rows,
        key=lambda r: (
            pool_total[r["pool_address"]] + token_total[r["token_address"]],
            abs(float(r["label_return_float"] or 0)),
        ),
    )
    # Keep every clean row, but low-frequency rows are selected first. This tests
    # whether simple reordering alone can repair concentration.
    return ordered


def fee_balanced(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["fee_int"], []).append(r)
    selected = []
    while True:
        progressed = False
        for fee in sorted(groups):
            if groups[fee]:
                selected.append(groups[fee].pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def strategy_audit(clean: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        evaluate_strategy("pool_cap_1", greedy_cap(clean, 1)),
        evaluate_strategy("pool_cap_2", greedy_cap(clean, 2)),
        evaluate_strategy("top_pool_downweight", downweight(clean), "reordering_only_does_not_reduce_full_set_concentration"),
        evaluate_strategy("fee_aware_balanced", fee_balanced(clean), "fee10000_is_only_clean_fee_tier" if {r["fee_int"] for r in clean} == {10000} else ""),
    ]


def choose_best(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    passing = [s for s in strategies if s["pass_diversity_gate"]]
    if passing:
        return max(passing, key=lambda s: (s["selected_rows_count"], -s["top5_pool_share"]))
    return max(
        strategies,
        key=lambda s: (
            s["selected_rows_count"] >= 20,
            -s["top5_pool_share"],
            -s["top1_pool_share"],
            s["selected_rows_count"],
        ),
    )


def write_csv(best: dict[str, Any]) -> None:
    fields = [
        "strategy_name",
        "pool_address",
        "token_address",
        "fee",
        "selected_notional_weth",
        "label_return",
        "toxicity_class",
        "source_run",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in best["rows"][:100]:
            writer.writerow(
                {
                    "strategy_name": best["strategy_name"],
                    "pool_address": r["pool_address"],
                    "token_address": r["token_address"],
                    "fee": r["fee"],
                    "selected_notional_weth": r.get("selected_notional_weth"),
                    "label_return": r.get("label_return"),
                    "toxicity_class": r.get("toxicity_class"),
                    "source_run": SOURCE_RUN,
                }
            )


def render_markdown(summary: dict[str, Any]) -> str:
    u = summary["clean_candidate_universe"]
    best = summary["best_strategy"]
    flags = summary["decision_flags"]
    strategy_lines = "\n".join(
        f"- `{s['strategy_name']}`: rows={s['selected_rows_count']}, pools={s['selected_pool_count']}, "
        f"top1/top5 pool={s['top1_pool_share']}/{s['top5_pool_share']}, "
        f"pass={s['pass_diversity_gate']}, reason=`{s['failure_reason']}`"
        for s in summary["strategies"]
    )
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-22 — Clean Buy Diversity Repair

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Clean Candidate Universe

- clean_candidate_count: **{u['clean_candidate_count']}**
- clean_candidate_pool_count: **{u['clean_candidate_pool_count']}**
- clean_candidate_token_count: **{u['clean_candidate_token_count']}**
- clean_candidate_fee_distribution: `{u['clean_candidate_fee_distribution']}`
- clean_return_distribution: `{u['clean_return_distribution']}`
- clean top1/top5 pool share: **{u['clean_top1_pool_share']} / {u['clean_top5_pool_share']}**
- clean top1/top5 token share: **{u['clean_top1_token_share']} / {u['clean_top5_token_share']}**

## 2. Diversity Repair Strategies

{strategy_lines}

## 3. Best Strategy

- best_strategy_name: **{best['strategy_name']}**
- best_selected_rows_count: **{best['selected_rows_count']}**
- best top1/top5 pool share: **{best['top1_pool_share']} / {best['top5_pool_share']}**
- best top1/top5 token share: **{best['top1_token_share']} / {best['top5_token_share']}**
- best near_total_loss_share: **{best['near_total_loss_share']}**
- diversity gate passed: **{best['pass_diversity_gate']}**
- failure reason: `{best['failure_reason']}`

## 4. Source Feasibility Judgment

Current V3R-12 corpus can produce clean labels, but cannot produce a >=20-row diversified clean subset under the required pool/token concentration gate. The main blocker is pool/token concentration, not near-total-loss.

Recommended new source rules:
- only WETH_TO_TOKEN buy observations
- require horizon state_changed
- adaptive notional
- pool/token cap at candidate construction time
- fee-tier stratified sampling
- tx/log_index dedupe
- never reverse sell observations into long-entry labels

## 5. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = __import__("time").time()
    fields = field_audit()
    rows = load_rows()
    clean = [r for r in rows if is_clean(r)]
    universe = universe_audit(clean)
    strategies = strategy_audit(clean)
    best = choose_best(strategies)
    require_new_source = (
        universe["clean_candidate_count"] < 20
        or not best["pass_diversity_gate"]
    )
    flags = {
        "v3r22_completed": True,
        "clean_candidate_universe_reconstructed": bool(fields["fields_sufficient"]),
        "clean_candidate_count": universe["clean_candidate_count"],
        "diversified_subset_available": bool(best["pass_diversity_gate"]),
        "best_strategy_name": best["strategy_name"],
        "best_selected_rows_count": best["selected_rows_count"],
        "best_top1_pool_share": best["top1_pool_share"],
        "best_top5_pool_share": best["top5_pool_share"],
        "best_top1_token_share": best["top1_token_share"],
        "best_top5_token_share": best["top5_token_share"],
        "best_near_total_loss_share": best["near_total_loss_share"],
        "diversity_gate_passed": bool(best["pass_diversity_gate"]),
        "require_new_candidate_source": bool(require_new_source),
        "allow_v3r23_new_candidate_builder_design": bool(require_new_source),
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r21_summary": str(V3R21_JSON.relative_to(ROOT)),
            "v3r21_rows": str(V3R21_CSV.relative_to(ROOT)),
            "v3r20_summary": str(V3R20_JSON.relative_to(ROOT)),
            "v3r20_rows": str(V3R20_CSV.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
        },
        "field_audit": fields,
        "clean_candidate_universe": universe,
        "strategies": [{k: v for k, v in s.items() if k != "rows"} for s in strategies],
        "best_strategy": {k: v for k, v in best.items() if k != "rows"},
        "decision_flags": flags,
        "runtime_seconds": round(__import__("time").time() - t0, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(best)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] clean={len(clean)} best={best['strategy_name']} rows={best['selected_rows_count']}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
