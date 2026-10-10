#!/usr/bin/env python3
"""Mechanically integrate the independent GL3 grade into the GL1 ledger."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
BASE = D / "base_gl1_golden_truth_ledger_gl2_20260715.json"
RESULT = D / "base_gl3_gl12_alpha_beta_20260715.json"
REVIEW = D / "base_gl3_independent_review_20260715.json"
OUT = D / "base_gl1_golden_truth_ledger_gl3_20260715.json"
REPORT = ROOT / "reports/GL1_golden_truth_ledger_GL3_20260715.md"
PLAIN = ROOT / "reports/GL3_incremental_plain_language_summary_20260715.md"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fmt(v: float) -> str:
    return f"{100*v:+.2f}%"


def main() -> None:
    base = json.loads(BASE.read_text())
    result = json.loads(RESULT.read_text())
    review = json.loads(REVIEW.read_text())
    if review["final_grade"] not in {"IRON", "PINNED", "OPEN"}:
        raise RuntimeError("independent reviewer did not provide an allowed final grade")
    if review["go_decision"] != "NOT_MADE" or review["substantive_blocker"]:
        raise RuntimeError("GL3 integration requires reviewed NOT_MADE/no-blocker posture")
    data = deepcopy(base)
    claim = next(x for x in data["claims"] if x["id"] == "GL1-12")
    summaries = result["summaries"]
    claim.update({
        "grade": review["final_grade"],
        "scope": "all 431 same-age candidates; KEYSTONE is the frozen 116-row subset; 0.001 WETH; entry_block+1; trigger_block+1/+5/+15; residuals bounded without default-loss imputation",
        "answer": {
            "complete_unconditional_ev_identification": "PARTIALLY_IDENTIFIED_WITH_SIGN_PINNED",
            "full_universe_beta_interval": {w: summaries[w]["alpha_beta"]["beta_full_universe_interval"] for w in ("1", "5", "15")},
            "keystone_interval": {w: summaries[w]["alpha_beta"]["keystone_interval"] for w in ("1", "5", "15")},
            "selection_alpha_interval": {w: summaries[w]["alpha_beta"]["selection_alpha_interval"] for w in ("1", "5", "15")},
            "remaining_full_universe_unresolved": {w: summaries[w]["full_universe"]["n_unresolved"] for w in ("1", "5", "15")},
            "remaining_keystone_unresolved": {w: summaries[w]["keystone_subset"]["n_unresolved"] for w in ("1", "5", "15")},
            "confirmed_continuously_unsellable": {w: summaries[w]["full_universe"]["n_confirmed_continuously_unsellable"] for w in ("1", "5", "15")},
            "interpretation": "full-universe beta is strictly negative while KEYSTONE is strictly positive; the historical conditional alpha lower bound remains >83pp in every frozen exit window",
        },
        "anchors": [
            {"path": "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md", "sha256": result["preregistration"]["sha256"]},
            {"path": "mvp/diagnostics/base_gl3_gl12_alpha_beta_20260715.json", "sha256": sha(RESULT)},
            {"path": "mvp/diagnostics/base_gl3_event_inventory_20260715.json", "sha256": sha(D / "base_gl3_event_inventory_20260715.json")},
            {"path": "mvp/diagnostics/base_gl3_pool_interval_evidence_20260715.json", "sha256": sha(D / "base_gl3_pool_interval_evidence_20260715.json")},
            {"path": "mvp/diagnostics/base_gl3_rpc_full_ledger_20260715.jsonl", "sha256": sha(D / "base_gl3_rpc_full_ledger_20260715.jsonl")},
            {"path": "mvp/diagnostics/base_gl3_independent_review_20260715.json", "sha256": sha(REVIEW)},
        ],
        "open_resolution": None,
        "gl3_supersedes_gl2_answer": True,
        "gl3_review_residual": review["limitations"],
        "flip_root": "GL2 fixed the estimand but left E4 at a cutoff snapshot; GL3 pre-froze trigger-aligned full-interval event/recovery rules and valid residual bounds",
    })
    data["generated_utc"] = datetime.now(timezone.utc).isoformat()
    data["gl3_integration"] = {
        "base_ledger": str(BASE.relative_to(ROOT)), "base_ledger_sha256": sha(BASE),
        "independent_review": str(REVIEW.relative_to(ROOT)), "independent_review_sha256": sha(REVIEW),
        "regraded_claims": {"GL1-12": review["final_grade"]},
        "safety": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "go_decision": "NOT_MADE",
    }
    data["go_decision"] = "NOT_MADE"
    OUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    counts = dict(sorted(Counter(x["grade"] for x in data["claims"]).items()))
    report_lines = [
        "---", "sop_applied: true", "alpha_id: AR-GL1-GOLDEN-TRUTH-GL3", "family: F0/F1/F4/F7",
        "next_gate: PRESERVE_GL1_12_HISTORICAL_BOUND_AND_RESOLVE_OTHER_OPEN_ITEMS", "decision_flags:",
        "  training_allowed: false", "  live_allowed: false", "  self_go_allowed: false", "---", "",
        "# GL1 黄金事实账本 · GL3 增量更新", "",
        "本文件不覆盖原 GL1/GL2；它只按独立审查结果机械替换 GL1-12。独立审查最终定级 `GL1-12=IRON`；`GO=NOT_MADE`。", "",
        "|主张|GL3 最终分级|冻结历史答案|残余与限制|", "|---|---|---|---|",
    ]
    beta = claim["answer"]["full_universe_beta_interval"]
    alpha = claim["answer"]["selection_alpha_interval"]
    report_lines.append(
        "|GL1-12|IRON|全宇宙 beta +1/+5/+15="
        + "/".join(f"[{fmt(beta[w][0])},{fmt(beta[w][1])}]" for w in ("1", "5", "15"))
        + "；条件 alpha=" + "/".join(f"[{fmt(alpha[w][0])},{fmt(alpha[w][1])}]" for w in ("1", "5", "15"))
        + "|仍有 full 15/14/14、KEYSTONE 2/1/1 个未决；CH Mint/Burn 解码不完整，RPC ledger 元数据不完整；历史报价不是实盘成交，未判 GO。|"
    )
    report_lines += ["", f"全账本分级计数：`{counts}`。GL2 基线 SHA256=`{sha(BASE)}`；GL3 独立审查 SHA256=`{sha(REVIEW)}`。", "", "完整逐行理由见 `reports/GL3_independent_line_review_20260715.md`；机器账本见 `mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。", ""]
    REPORT.write_text("\n".join(report_lines))
    PLAIN.write_text("\n".join([
        "---", "sop_applied: true", "alpha_id: AR-GL3-CLOSE-GL12", "family: F1/F4/F7",
        "next_gate: PRESERVE_HISTORICAL_IRON_WITHOUT_GO", "decision_flags:", "  training_allowed: false",
        "  live_allowed: false", "  self_go_allowed: false", "---", "", "# GL3 人话版增量摘要", "",
        "这次把 GL1-12 真正收口了：在完全同源的 entry+1、trigger+1/+5/+15 秒级口径下，瞎买 431 个同龄候选的完整宇宙 EV 即使用对剩余未知最乐观的主赋值，三窗仍约为 -30% 到 -31%；KEYSTONE 冻结 116 个子集则约为 +52% 到 +57%，所以它不是搭了正 beta 的顺风车，而是在负 beta 背景上留下至少 +83pp 的条件 alpha。全区间事件与真储备重核把 411 个残余确认成持续不可卖，另有 43 个 cell 因数据或严格失败证据不足继续保持 unresolved，没有默认全损。独立审查最终给 `GL1-12=IRON`，但这只钉住冻结历史有界事实：ClickHouse 的 Mint/Burn 解码覆盖和 RPC ledger 元数据仍有限，历史原子报价也不是真实成交；因此 `GO=NOT_MADE`，不训练、不进 canary/live。", "",
    ]))
    print(json.dumps({"final_grade": review["final_grade"], "grade_counts": counts, "ledger": str(OUT.relative_to(ROOT)), "report": str(REPORT.relative_to(ROOT)), "plain": str(PLAIN.relative_to(ROOT))}, indent=2))


if __name__ == "__main__":
    main()
