"""Emit ACTOR_FLOW_FINAL.json + markdown review package."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .common import OUT, ROOT, load_freeze, save_json, utcnow


def write_final(gate: dict, scorecard: pd.DataFrame | None, summary: dict) -> dict:
    freeze = load_freeze()
    if not gate.get("passed"):
        final = {
            "phase": freeze["phase"],
            "information_verdict": "ACTOR_RESOLUTION_INSUFFICIENT",
            "copy_verdict": "ACTOR_RESOLUTION_INSUFFICIENT",
            "deploy_allowed": False,
            "gate": gate,
            "best_arm": None,
            "note": "Foundation gate failed — arms not scored for merit claims.",
            "generated_at": utcnow(),
        }
    else:
        final = {
            "phase": freeze["phase"],
            "information_verdict": summary["information_verdict"],
            "copy_verdict": summary["copy_verdict"],
            "deploy_allowed": False,
            "best_arm": summary.get("best_arm"),
            "best_incremental_vs_pool_only_weth": summary.get("best_incremental_vs_pool_only_weth"),
            "pool_only_pnl_weth": summary.get("pool_only_pnl_weth"),
            "gate": {
                "passed": gate.get("passed"),
                "sampled_error_rate": gate.get("sampled_error_rate"),
                "unresolved_coverage": gate.get("unresolved_coverage"),
                "resolvable_scorable_coverage": gate.get("resolvable_scorable_coverage"),
                "audit_mode": gate.get("audit_mode"),
            },
            "scorecard_path": str(OUT / "oos" / "scorecard.csv"),
            "generated_at": utcnow(),
            "hypothesis": (
                "Can resolvable Base V3 economic-actor behavior, after controlling for "
                "pool structure, add incremental, post-latency, capturable information?"
            ),
            "hard_bans_honored": freeze["hard_bans"],
        }
    save_json(OUT / "ACTOR_FLOW_FINAL.json", final)
    _write_review_md(final, scorecard, gate)
    return final


def _write_review_md(final: dict, scorecard: pd.DataFrame, gate: dict) -> None:
    lines = []
    lines.append("# Base V3 Actor-Flow OOS Harness — 审查包")
    lines.append("")
    lines.append(f"> 生成时间：{final.get('generated_at')}")
    lines.append(f"> 项目根：`{ROOT}`")
    lines.append(f"> 阶段：`{final.get('phase')}`")
    lines.append(f"> **Information 裁决：`{final.get('information_verdict')}`**")
    lines.append(f"> **Copy 裁决：`{final.get('copy_verdict')}`**")
    lines.append(f"> `deploy_allowed={final.get('deploy_allowed')}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. 人话结论")
    lines.append("")
    info = final.get("information_verdict")
    copy = final.get("copy_verdict")
    if info == "ACTOR_RESOLUTION_INSUFFICIENT":
        lines.append(
            "主体解析地基未过关（`resolved_economic_actor` 抽样错误率或可评分覆盖不足）。"
            "按章程**停止**对六臂做 merit 裁决，不得写成「没有 edge」。"
        )
    elif info == "INFORMATION_EDGE_GO" and copy == "COPY_EDGE_GO":
        lines.append(
            f"身份特征相对 `POOL_ONLY` 有增量，且在 $1,000 固定本金约束下可复制。"
            f"最佳臂：`{final.get('best_arm')}`。仍禁止实盘；仅可进 prospective shadow。"
        )
    elif info == "PROSPECTIVE_CONFIRMATION_REQUIRED" and copy == "COPY_EDGE_GO":
        lines.append(
            f"最佳臂 `{final.get('best_arm')}` 相对 `POOL_ONLY` 有正增量且通过复制侧约束，"
            "但信息侧未过全部稳健门（fold/置换/LOO 等）→ `PROSPECTIVE_CONFIRMATION_REQUIRED`。"
            "禁止实盘；仅可进 prospective shadow / paper-forward。"
        )
    elif info == "INFORMATION_EDGE_GO":
        lines.append(
            f"身份特征相对 `POOL_ONLY` 有增量信息（`{final.get('best_arm')}`），"
            "但未能通过 COPY_EDGE（容量/卖出/月度现金流）。适合作为风险评分或池级特征，不适合直接跟单。"
        )
    elif info == "PROSPECTIVE_CONFIRMATION_REQUIRED":
        lines.append(
            f"出现正向迹象（`{final.get('best_arm')}`），需 sealed/前向确认；`deploy_allowed=false`。"
        )
    elif info == "POOL_STRUCTURE_SUFFICES":
        lines.append(
            "表现最好的身份臂并未显著优于 `POOL_ONLY`。"
            "真正的 alpha 更像「它喜欢买什么样的池」，应直接使用池特征，不必承担身份工程成本。"
        )
    else:
        lines.append(
            "在本轮 walk-forward 与对照下，未确认可解析经济账户行为能提供延迟后仍可捕获的增量信息。"
            "旧 WONTFIX（wallet-ranking/copy pipeline）维持；本假说本轮未通过。"
        )
    lines.append("")
    lines.append("## 2. 地基门（Phase 1）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| passed | {gate.get('passed')} |")
    lines.append(f"| sampled_error_rate | {gate.get('sampled_error_rate')} |")
    lines.append(f"| unresolved_coverage | {gate.get('unresolved_coverage')} |")
    lines.append(f"| resolvable_scorable_coverage | {gate.get('resolvable_scorable_coverage')} |")
    lines.append(f"| audit_mode | {gate.get('audit_mode')} |")
    lines.append("")
    lines.append("## 3. 六臂记分卡")
    lines.append("")
    if scorecard is not None and len(scorecard):
        cols = [
            "arm", "information_verdict", "copy_verdict", "selected_trades",
            "total_wallet_pnl_weth", "incremental_vs_pool_only_weth",
            "bootstrap_p05_weth", "pct_months_hit_1000_usd", "ruin",
        ]
        cols = [c for c in cols if c in scorecard.columns]
        sc = scorecard[cols]
        # Avoid pandas.to_markdown (requires optional tabulate)
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"
        lines.append(header)
        lines.append(sep)
        for row in sc.itertuples(index=False):
            cells = []
            for v in row:
                if isinstance(v, float):
                    cells.append(f"{v:.6g}")
                else:
                    cells.append(str(v))
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("_无 scorecard（地基门失败或未跑完）。_")
    lines.append("")
    lines.append("## 4. 纪律边界")
    lines.append("")
    lines.append("- 未重启 abs-PnL / winrate top-N / winner-only / EOA-only / MFE 主目标 / 触发价 trailing 证明")
    lines.append("- 双裁决分离：INFORMATION vs COPY")
    lines.append("- `deploy_allowed=false`")
    lines.append("- 失败如实结案，不扩大搜索硬凑赢家")
    lines.append("")
    lines.append("## 5. 路径")
    lines.append("")
    lines.append("```bash")
    lines.append(f"{ROOT}/.venv/bin/python -m src.run_all 2>&1 | tee logs/RUN.log")
    lines.append("```")
    lines.append("")
    lines.append(f"- 终裁：`{OUT / 'ACTOR_FLOW_FINAL.json'}`")
    lines.append(f"- 记分卡：`{OUT / 'oos' / 'scorecard.csv'}`")
    lines.append(f"- 地基门：`{OUT / 'phase1' / 'RESOLUTION_GATE.json'}`")
    lines.append("")
    # ASCII filename for IDE compatibility; keep Chinese name as a copy
    path = ROOT / "ACTOR_FLOW_REVIEW_PACK.md"
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (ROOT / "ACTOR_FLOW_审查包_完整合并.md").write_text(text, encoding="utf-8")
    print(f"[finalize] wrote {path}", flush=True)
