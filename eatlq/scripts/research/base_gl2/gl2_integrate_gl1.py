#!/usr/bin/env python3
"""Mechanically integrate the independent GL2 review into a superseding GL1 ledger."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DIAG = ROOT / "mvp/diagnostics"
REPORTS = ROOT / "reports"
ORIGINAL = DIAG / "base_gl1_golden_truth_ledger_20260715.json"
REVIEW = DIAG / "base_gl2_independent_review_20260715.json"
OUT = DIAG / "base_gl1_golden_truth_ledger_gl2_20260715.json"
REPORT = REPORTS / "GL1_golden_truth_ledger_GL2_20260715.md"
SUMMARY = DIAG / "base_gl2_three_opens_20260715.json"
PLAIN = REPORTS / "GL2_incremental_plain_language_summary_20260715.md"
GENERATED = "2026-07-15T14:17:00Z"


def load(path: Path):
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def main() -> None:
    original = load(ORIGINAL)
    review = load(REVIEW)
    claims = review["claims"]
    updated = json.loads(json.dumps(original))
    for claim in updated["claims"]:
        if claim["id"] not in claims:
            continue
        verdict = claims[claim["id"]]
        claim["grade"] = verdict["grade"]
        claim["answer"] = verdict["answer"]
        claim["anchors"] = verdict["anchors"]
        claim["open_resolution"] = verdict["open_resolution"]
        claim["gl2_review_residual"] = verdict["residual"]
        claim["gl2_supersedes_prior_answer"] = True
    updated["generated_utc"] = GENERATED
    updated["go_decision"] = "NOT_MADE"
    updated["research_only"] = True
    updated["gl2_integration"] = {
        "independent_review": str(REVIEW.relative_to(ROOT)),
        "independent_review_sha256": sha(REVIEW),
        "original_ledger": str(ORIGINAL.relative_to(ROOT)),
        "original_ledger_sha256": sha(ORIGINAL),
        "regraded_claims": {key: claims[key]["grade"] for key in sorted(claims)},
        "safety": {"live_allowed": False, "self_go_allowed": False, "training_allowed": False},
    }
    write_json(OUT, updated)

    grade_counts = Counter(c["grade"] for c in updated["claims"])
    report_lines = [
        "---",
        "sop_applied: true",
        "alpha_id: AR-GL1-GOLDEN-TRUTH-GL2",
        "family: F0/F1/F3/F4/F6/F7",
        "next_gate: RESOLVE_REMAINING_OPEN_ITEMS_UNDER_FROZEN_RULES",
        "decision_flags:",
        "  training_allowed: false",
        "  live_allowed: false",
        "  self_go_allowed: false",
        "---",
        "",
        "# GL1 黄金事实账本 · GL2 增量更新",
        "",
        "本文件不覆盖原 GL1；它以独立逐行终审为准，机械替换 GL1-12/18/19 的答案、锚点和残余。三条均维持 OPEN，GO 未作出。",
        "",
        "|主张|GL2 分级|本轮可固定事实|仍缺什么|",
        "|---|---|---|---|",
    ]
    short = {
        "GL1-12": "resolved-only 三窗 beta=3.31%/3.51%/3.94%，条件 alpha=56.53/57.66/58.23pp；不能外推完整 431",
        "GL1-18": "5 门中 2 个进入 OOS，0 个稳定胜；其中 1 个无功效，3 个未测试",
        "GL1-19": "EOA 0/859 mismatch、target/WETH golden 20/20；前向 4 条全来自左截断钱包，merit 未测试",
    }
    for key in ("GL1-12", "GL1-18", "GL1-19"):
        item = claims[key]
        report_lines.append(f"|{key}|{item['grade']}|{short[key]}|{item['open_resolution']}|")
    report_lines += [
        "",
        f"全账本分级计数：`{dict(sorted(grade_counts.items()))}`。原账本 SHA256=`{sha(ORIGINAL)}`；独立审查 SHA256=`{sha(REVIEW)}`。",
        "",
        "完整逐行理由见 `reports/GL2_independent_line_review_20260715.md`；机器账本见 `mvp/diagnostics/base_gl1_golden_truth_ledger_gl2_20260715.json`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。",
    ]
    REPORT.write_text("\n".join(report_lines) + "\n")

    summary = {
        "alpha_id": "AR-GL2-THREE-OPENS",
        "generated_utc": GENERATED,
        "grades": {key: claims[key]["grade"] for key in sorted(claims)},
        "gl1_12": claims["GL1-12"]["answer"],
        "gl1_18": claims["GL1-18"]["answer"],
        "gl1_19": claims["GL1-19"]["answer"],
        "independent_review_sha256": sha(REVIEW),
        "updated_gl1_ledger": str(OUT.relative_to(ROOT)),
        "research_only": True,
        "go_decision": "NOT_MADE",
        "decision_flags": {"live_allowed": False, "self_go_allowed": False, "training_allowed": False},
    }
    write_json(SUMMARY, summary)

    plain = f"""---
sop_applied: true
alpha_id: AR-GL2-THREE-OPENS
family: F1/F3/F4/F6/F7
next_gate: KEEP_ALL_THREE_OPEN_UNTIL_RESIDUALS_CLOSE
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2 人话版增量摘要

这次没有把三条 OPEN 硬关掉，反而把“为什么还不能关”钉得更清楚。

- A：能定价的同龄币里，大盘顺风约占 KEYSTONE 均值的 5%–6%，选币差约占 94%。但 431 个币仍有 149–153 个不知道中间是否恢复过，完整市场平均收益依然算不出来；所以“+56% 里究竟多少是真 alpha”只能对已定价子集说，不能对全宇宙说。
- B：账户归因与持仓/现金腿整数对账已经能做。问题出在资格：前向出现的 4 次买入全来自两个历史库存不完整的钱包，真正库存完整的三个钱包一次都没买。因此这 4 笔负回报不能证明聪明钱有 edge，也不能证明没有 edge。
- C：真正有数据且有基本功效的天气门只有 ETH 波动这一项，它没有稳定胜过随机门；另一个门一次都没开，三个门没有合格历史数据。现在只能说“已测门没找到稳定胜者”，不能说“五类天气门全部没用”。

独立审查最终给出：`GL1-12=OPEN`、`GL1-18=OPEN`、`GL1-19=OPEN`。没有 GO，不训练，不进 canary/live，也没有改 Redis、signals 或执行器。
"""
    PLAIN.write_text(plain)


if __name__ == "__main__":
    main()
