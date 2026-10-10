#!/usr/bin/env python3
"""Mechanically integrate the independent GL4 grades into the GL1 ledger."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
BASE = D / "base_gl1_golden_truth_ledger_gl3_20260715.json"
TRUTH = D / "base_gl4_live_truth_ledger_20260716.json"
REVIEW = D / "base_gl4_independent_review_20260716.json"
REPORT = ROOT / "reports/GL4_live_track_record_reconciliation_20260716.md"
PREREG = ROOT / "configs/research/base_gl4_live_reconciliation_preregistration_20260716.json"
OUT = D / "base_gl1_golden_truth_ledger_gl4_20260716.json"
OUT_MD = ROOT / "reports/GL1_golden_truth_ledger_GL4_20260716.md"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def main() -> None:
    base = json.loads(BASE.read_text())
    truth = json.loads(TRUTH.read_text())
    review = json.loads(REVIEW.read_text())
    final = review["final_adjudication"]
    recalc = review["independent_recalculation"]
    if final["substantive_blocker"] or final["go_decision"] != "NOT_MADE":
        raise RuntimeError("independent GL4 review is not integrable")
    if final["GL1_02"]["outcome"] != "FAIL_AT_OBSERVED_TINY_LIVE_SCALE":
        raise RuntimeError("unexpected GL1-02 outcome")
    data = deepcopy(base)
    claims = {x["id"]: x for x in data["claims"]}
    anchors = [
        {"path": str(PREREG.relative_to(ROOT)), "sha256": sha(PREREG), "note": "pre-outcome frozen classification and attribution"},
        {"path": str(TRUTH.relative_to(ROOT)), "sha256": sha(TRUTH), "note": "receipt-first machine ledger"},
        {"path": str(REPORT.relative_to(ROOT)), "sha256": sha(REPORT), "note": "human tables and adjudication"},
        {"path": str(REVIEW.relative_to(ROOT)), "sha256": sha(REVIEW), "note": "independent final line grade"},
    ]
    a = truth["aggregate"]
    claims["GL1-02"].update({
        "grade": final["GL1_02"]["final_grade"],
        "scope": final["GL1_02"]["scope"],
        "answer": {
            "verdict": final["GL1_02"]["outcome"],
            "successful_swap_legs": a["successful_swap_fill_legs"],
            "buy_fills": a["successful_buy_fills"],
            "sell_fills": a["successful_sell_fills"],
            "cumulative_invested_weth": a["cumulative_invested_weth"],
            "cumulative_invested_weth_exact_decimal": recalc["cumulative_buy_principal_weth_decimal"],
            "cumulative_invested_usd_at_buy_oracle": a["cumulative_invested_usd_at_buy_oracle"],
            "strict_receipt_closed_realized_pnl_weth": a["closed_realized_pnl_weth_all_associated"],
            "strict_receipt_closed_realized_pnl_weth_exact_decimal": recalc["strict_closed_realized_pnl_weth_decimal"],
            "strict_receipt_closed_realized_pnl_usd": a["closed_realized_pnl_usd_receipt_time_cashflow"],
            "strict_closed_win_rate": a["closed_win_rate"],
            "unsold_positions": a["confirmed_stranded_positions"],
            "supplemental_receipt_plus_end_mark_economic_pnl_weth": a["receipt_plus_end_executable_mark_economic_pnl_weth"],
            "supplemental_receipt_plus_end_mark_economic_pnl_weth_exact_decimal": recalc["receipt_plus_end_executable_mark_economic_pnl_weth_decimal"],
            "supplemental_receipt_plus_end_mark_economic_pnl_usd": a["receipt_time_oracle_economic_pnl_usd"],
            "wording_guard": "The supplemental end-window executable mark is not realized sell PnL.",
            "residual": final["GL1_02"]["residual"],
        },
        "anchors": anchors,
        "open_resolution": None,
        "flip_root": "the project had already run two real wallets for a month; dry-run-only RM1 was not the complete live record",
        "gl4_supersedes_prior_open_answer": True,
    })
    claims["GL1-01"]["gl4_scope_enforcement"] = final["GL1_01"]["scope"]
    claims["GL1-12"]["gl4_scope_enforcement"] = final["GL1_12"]["scope"]
    new_claim = {
        "id": "GL1-24", "claim": "实盘真实战绩", "grade": final["GL1_24_recommended_new_entry"]["final_grade"],
        "scope": final["GL1_24_recommended_new_entry"]["scope"],
        "answer": {
            "wallets": truth["wallets"], "window": truth["window"],
            "successful_swap_legs": a["successful_swap_fill_legs"], "buy_fills": a["successful_buy_fills"], "sell_fills": a["successful_sell_fills"],
            "cumulative_invested_weth": a["cumulative_invested_weth"],
            "cumulative_invested_weth_exact_decimal": recalc["cumulative_buy_principal_weth_decimal"],
            "strict_receipt_closed_realized_pnl_weth": a["closed_realized_pnl_weth_all_associated"],
            "strict_receipt_closed_realized_pnl_weth_exact_decimal": recalc["strict_closed_realized_pnl_weth_decimal"],
            "strict_closed_wins": a["closed_wins"], "strict_closed_roundtrips": a["closed_roundtrips"],
            "unsold_positions_cash_realized_pnl": None,
            "supplemental_end_mark_economic_pnl_weth": a["receipt_plus_end_executable_mark_economic_pnl_weth"],
            "supplemental_end_mark_economic_pnl_weth_exact_decimal": recalc["receipt_plus_end_executable_mark_economic_pnl_weth_decimal"],
            "supplemental_end_mark_economic_pnl_usd": a["receipt_time_oracle_economic_pnl_usd"],
            "required_wording": final["GL1_24_recommended_new_entry"]["required_wording"],
        },
        "anchors": anchors, "open_resolution": None,
        "flip_root": "prior GL1 systematically omitted the existing live month and treated RM1 dry-run as if no real execution existed",
    }
    if "GL1-24" in claims:
        raise RuntimeError("GL1-24 already exists")
    data["claims"].append(new_claim)
    data["claims"].sort(key=lambda x: int(x["id"].split("-")[1]))
    data["historical_claims"].append({
        "claim": "receipt-verified real-money track record", "date": "2026-06-18..2026-06-29 fills",
        "scope": "two evidenced Base wallets; strict realized and unsold economic mark separated",
        "source": str(TRUTH.relative_to(ROOT)),
        "value": "8 buys / 3 sells; strict closed -0.000011451891750166 WETH; supplemental economic -0.160235862229391189 WETH",
    })
    data["generated_utc"] = "2026-07-16T00:00:00Z"
    data["gl4_integration"] = {
        "base_ledger": str(BASE.relative_to(ROOT)), "base_ledger_sha256": sha(BASE),
        "truth_ledger": str(TRUTH.relative_to(ROOT)), "truth_ledger_sha256": sha(TRUTH),
        "independent_review": str(REVIEW.relative_to(ROOT)), "independent_review_sha256": sha(REVIEW),
        "regraded_claims": {"GL1-02": final["GL1_02"]["final_grade"]},
        "added_claims": {"GL1-24": final["GL1_24_recommended_new_entry"]["final_grade"]},
        "retained_scope_enforced": {"GL1-01": final["GL1_01"]["final_grade"], "GL1-12": final["GL1_12"]["final_grade"]},
        "plus56_verdict": final["plus56_verdict"], "aggressive_207_rug_verdict": final["aggressive_207_rug_verdict"],
        "safety": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "go_decision": "NOT_MADE",
    }
    data["go_decision"] = "NOT_MADE"
    data["grading_notice"] = "GL4 grades were mechanically integrated from the independent line review; human acceptance remains required."
    write_json(OUT, data)

    counts = dict(sorted(Counter(x["grade"] for x in data["claims"]).items()))
    md = [
        "---", "sop_applied: true", "alpha_id: AR-GL1-GOLDEN-TRUTH-GL4", "family: F0/F1/F4/F7",
        "next_gate: HUMAN_ACCEPTANCE_ONLY", "decision_flags:", "  training_allowed: false", "  live_allowed: false",
        "  self_go_allowed: false", "---", "", "# GL1 黄金事实账本 · GL4 实盘增量", "",
        "独立终审已落：`GL1-02=PINNED / FAIL_AT_OBSERVED_TINY_LIVE_SCALE`，新增 `GL1-24=PINNED`；`GL1-01=PINNED` 与 `GL1-12=IRON` 保留但强制限定为历史、非实盘、非前瞻。`GO=NOT_MADE`。", "",
        "|条目|最终 grade|答案|严格边界|", "|---|---|---|---|",
        f"|GL1-02 · KEYSTONE 真钱净 edge|{claims['GL1-02']['grade']} / FAIL|3 个 receipt 闭环严格 realized `{recalc['strict_closed_realized_pnl_weth_decimal']}` WETH，0/3 胜|实际配置/规模与 .001 WETH GL3 estimand 不同；不同不能把负实盘改判 PASS|",
        f"|GL1-24 · 实盘真实战绩|{new_claim['grade']}|11 成交腿；8 买本金 `{recalc['cumulative_buy_principal_weth_decimal']}` WETH；5 未卖仓末窗 mark 后总经济 `{recalc['receipt_plus_end_executable_mark_economic_pnl_weth_decimal']}` WETH|未卖仓 cash-realized=null，economic mark 不得叫 realized|",
        f"|GL1-01 · +56 frontier|{claims['GL1-01']['grade']} 保留|冻结历史 selected frontier|quote/guarded atomic，非 live realized|",
        f"|GL1-12 · 历史 alpha/beta 区间|{claims['GL1-12']['grade']} 保留|431/116 冻结区间与符号分离|不是 +56 已真钱实现，不是 forward guarantee|", "",
        f"全账本分级计数：`{counts}`。GL3 基线 SHA256=`{sha(BASE)}`；GL4 truth SHA256=`{sha(TRUTH)}`；独立审查 SHA256=`{sha(REVIEW)}`。", "",
        "最终对质：+56 没有被证明为历史算术错误，但真钱/forward 外推已失败；aggressive 207-rug 只保留样本内算术，forward/live=`OPEN / NOT_GO_EVIDENCE`。", "",
        "机器账本：`mvp/diagnostics/base_gl1_golden_truth_ledger_gl4_20260716.json`。独立审查：`reports/GL4_independent_line_review_20260716.md`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。", "",
    ]
    OUT_MD.write_text("\n".join(md))
    print(json.dumps({"ledger": str(OUT.relative_to(ROOT)), "ledger_sha256": sha(OUT), "report": str(OUT_MD.relative_to(ROOT)), "report_sha256": sha(OUT_MD), "grade_counts": counts}, indent=2))


if __name__ == "__main__":
    main()
