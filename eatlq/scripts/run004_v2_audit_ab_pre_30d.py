"""Static Audit A/B on 7D pilot rows — no RPC. Writes summary JSON only."""
from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROWS = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rows.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v2_audit_ab_pre_30d_summary.json"

FROM_TS = 1777095455
DAY_SEC = 86400


def day_idx(entry_ts_str: str) -> int | None:
    if not (entry_ts_str or "").strip():
        return None
    try:
        t = int(entry_ts_str)
    except ValueError:
        return None
    return max(0, (t - FROM_TS) // DAY_SEC)


def parse_float(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def top_share(counter: Counter[str], n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    top1 = counter.most_common(1)[0][1] / n
    top5 = sum(v for _, v in counter.most_common(5)) / n
    return top1, top5


def main() -> None:
    rows = []
    with ROWS.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r["_day"] = day_idx(r.get("entry_timestamp", ""))
            rows.append(r)

    day2 = [r for r in rows if r["_day"] == 2]
    day2_n = len(day2)
    pool_d2 = Counter(r["pool_address"].lower() for r in day2)
    top_pool, top_pool_n = pool_d2.most_common(1)[0] if day2_n else ("", 0)
    max_pool_share = top_pool_n / day2_n if day2_n else 0.0

    dom_rows = [r for r in day2 if r["pool_address"].lower() == top_pool]
    extreme_dom = sum(1 for r in dom_rows if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine")
    clean_dom = sum(1 for r in dom_rows if (r.get("is_clean") or "").strip() == "True")
    pos_dom = neg_dom = 0
    for r in dom_rows:
        nr = parse_float(r.get("net_return"))
        if nr is None:
            continue
        if nr > 0:
            pos_dom += 1
        elif nr < 0:
            neg_dom += 1

    extreme_day2 = [r for r in day2 if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine"]
    clean_day2 = [r for r in day2 if (r.get("is_clean") or "").strip() == "True"]

    extreme_all = [r for r in rows if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine"]
    n_ext = len(extreme_all)
    pool_e = Counter(r["pool_address"].lower() for r in extreme_all)
    tok_e = Counter(r["token_address"].lower() for r in extreme_all)
    t1p, t5p = top_share(pool_e, n_ext)
    t1t, t5t = top_share(tok_e, n_ext)

    day_e = Counter()
    for r in extreme_all:
        d = r["_day"]
        day_e[d if d is not None else -1] += 1

    clean_all = [r for r in rows if (r.get("is_clean") or "").strip() == "True"]
    clean_overlap_ext = sum(
        1 for r in clean_all if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine"
    )

    all_nr = [r for r in rows if parse_float(r.get("net_return")) is not None]
    pos_rows = [r for r in all_nr if parse_float(r["net_return"]) > 0]
    pos_pools = Counter(r["pool_address"].lower() for r in pos_rows)
    top_ext_pool = pool_e.most_common(1)[0][0] if pool_e else None
    top_pos_pool = pos_pools.most_common(1)[0][0] if pos_pools else None
    share_pos_top_ext = (
        sum(1 for r in pos_rows if r["pool_address"].lower() == top_ext_pool) / len(pos_rows)
        if pos_rows and top_ext_pool
        else 0.0
    )

    # Extreme: positive vs negative net_return (labeled failures still have net_return)
    pos_ext, neg_ext = 0, 0
    for r in extreme_all:
        nr = parse_float(r.get("net_return"))
        if nr is None:
            continue
        if nr > 0:
            pos_ext += 1
        elif nr < 0:
            neg_ext += 1
    share_extreme_positive = pos_ext / n_ext if n_ext else 0.0

    # Gates (user-specified)
    audit_a_status = "FAIL" if max_pool_share >= 0.50 else ("WARN" if max_pool_share >= 0.35 else "PASS")
    audit_b_pool_hard_block = t1p >= 0.35 or t5p >= 0.70

    positive_rows_total = len(pos_rows)
    extreme_positive = pos_ext
    share_positive_that_are_extreme = extreme_positive / positive_rows_total if positive_rows_total else 0.0
    # Extreme rows dominating *all* positive net_return rows (not pool concentration inside extreme set)
    extreme_dominates_positive = share_positive_that_are_extreme >= 0.35

    top_positive_count = pos_pools.most_common(1)[0][1] if pos_pools else 0
    share_positive_from_top_positive_pool = top_positive_count / positive_rows_total if positive_rows_total else 0.0

    dominates_positive_via_extreme_pool = share_pos_top_ext >= 0.35
    audit_b_status = "FAIL" if audit_b_pool_hard_block or extreme_dominates_positive or dominates_positive_via_extreme_pool else "PASS"
    if audit_b_status == "PASS" and (t1p >= 0.15 or t5p >= 0.40):
        audit_b_status = "WARN"

    block_30d = max_pool_share >= 0.50 or audit_b_pool_hard_block or extreme_dominates_positive or dominates_positive_via_extreme_pool
    # Soft policy: day2 WARN or any Audit B WARN suggests explicit caps / documentation in 30D script
    quarantine_policy = max_pool_share >= 0.35 or audit_b_status in ("WARN", "FAIL") or share_positive_from_top_positive_pool >= 0.50

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "patch": "run-004-v2-audit-ab-pre-30d",
        "inputs": {
            "rows_csv": str(ROWS.relative_to(ROOT)),
            "from_timestamp_7d": FROM_TS,
            "day_index_2_calendar": "2026-04-27 (UTC) per 7d daily.csv day_start_iso",
        },
        "audit_a_day2_pool_concentration": {
            "status": audit_a_status,
            "day2_total_rows": day2_n,
            "max_pool_address": top_pool,
            "max_pool_row_count": top_pool_n,
            "max_pool_share": max_pool_share,
            "exceeds_0_35": max_pool_share >= 0.35,
            "exceeds_0_50": max_pool_share >= 0.50,
            "dominant_pool_extreme_row_count": extreme_dom,
            "dominant_pool_clean_row_count": clean_dom,
            "dominant_pool_positive_net_return_count": pos_dom,
            "dominant_pool_negative_net_return_count": neg_dom,
            "day2_extreme_total": len(extreme_day2),
            "day2_clean_total": len(clean_day2),
            "interpretation": (
                "Dominant pool row mix: extreme vs clean vs signed net_return distinguishes pump/sandwich noise "
                "from broad liquidity."
            ),
        },
        "audit_b_extreme_quarantine": {
            "status": audit_b_status,
            "extreme_return_quarantine_total": n_ext,
            "top1_pool_share": t1p,
            "top5_pool_share": t5p,
            "top1_token_share": t1t,
            "top5_token_share": t5t,
            "by_day_index": {str(k): v for k, v in sorted(day_e.items())},
            "extreme_rows_with_positive_net_return": pos_ext,
            "extreme_rows_with_negative_net_return": neg_ext,
            "share_extreme_rows_positive_net_return": share_extreme_positive,
            "risk_flag_distribution_top": dict(Counter((r.get("risk_flag") or "").strip() for r in extreme_all).most_common(3)),
            "failure_reason_distribution_top": dict(Counter((r.get("failure_reason") or "").strip() for r in extreme_all).most_common(3)),
            "gates": {
                "block_if_top1_pool_share_ge_0_35": t1p >= 0.35,
                "block_if_top5_pool_share_ge_0_70": t5p >= 0.70,
                "both_false_in_this_run": not audit_b_pool_hard_block,
            },
            "positive_return_cross_cut": {
                "positive_net_return_rows_total": positive_rows_total,
                "extreme_rows_with_positive_net_return": extreme_positive,
                "share_positive_rows_that_are_extreme_quarantine": share_positive_that_are_extreme,
                "block_if_extreme_dominates_positive_return_ge_0_35": extreme_dominates_positive,
            },
        },
        "cross_cut_positive_return_dominance": {
            "positive_net_return_rows_total": positive_rows_total,
            "top_positive_pool": top_pos_pool,
            "top_extreme_pool": top_ext_pool,
            "same_pool_top_extreme_and_top_positive": top_ext_pool == top_pos_pool,
            "share_positive_rows_in_top_extreme_pool": share_pos_top_ext,
            "block_if_share_positive_from_top_extreme_pool_ge_0_35": dominates_positive_via_extreme_pool,
            "top_positive_pool_row_count": top_positive_count,
            "share_positive_rows_in_top_positive_pool": share_positive_from_top_positive_pool,
            "top3_positive_pools_counts": pos_pools.most_common(3),
        },
        "clean_distribution_after_isolating_extreme": {
            "clean_rows_total": len(clean_all),
            "clean_rows_also_flagged_extreme": clean_overlap_ext,
            "isolating_extreme_failed_rows_preserves_clean_count": clean_overlap_ext == 0,
            "interpretation": (
                "extreme_return_quarantine rows are failed/censored labels, not clean training rows. "
                "Removing them from analysis of failures does not shrink clean_rows; 7D clean distribution remains valid."
            ),
        },
        "decision_flags": {
            "audit_a_day2_pool_concentration_passed": audit_a_status != "FAIL",
            "audit_a_soft_warn_day2_pool_share_ge_0_35": audit_a_status == "WARN",
            "audit_b_extreme_quarantine_concentration_passed": audit_b_status != "FAIL",
            "quarantine_policy_required": quarantine_policy,
            "allow_30d_bounded_script_prep": not block_30d,
            "allow_30d_bounded_run": False,
            "allow_training": False,
            "allow_model_research": False,
            "allow_live": False,
            "allow_shadow": False,
            "allow_promote": False,
            "block_30d_bounded_pilot_per_user_gates": block_30d,
        },
        "outputs": {
            "summary_json": str(OUT_JSON.relative_to(ROOT)),
            "markdown": "mvp/runs/run-004-v2-audit-ab-pre-30d.md",
            "repro_script": "scripts/run004_v2_audit_ab_pre_30d.py",
        },
        "overall": {
            "audit_a_verdict": audit_a_status,
            "audit_b_verdict": audit_b_status,
            "user_gate_30d_block": block_30d,
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(str(OUT_JSON))


if __name__ == "__main__":
    main()
