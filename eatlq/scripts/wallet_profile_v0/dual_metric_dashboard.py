#!/usr/bin/env python3
"""Dual metric dashboard — every PF reported as single-tick + robust pair."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated"
OUT_JSON = OUT_DIR / "dual_metric_dashboard.json"
OUT_MD = OUT_DIR / "dual_metric_dashboard.md"


def run_backtest(days: int, single_tick: bool) -> dict:
    out = OUT_DIR / f"_dual_tmp_{days}d_{'single' if single_tick else 'robust'}.json"
    cmd = [
        sys.executable,
        str(REPO / "scripts/sprint091_sender_gated_paper.py"),
        "--backtest-days", str(days),
        "--no-readout",
        "--output-json", str(out),
    ]
    if single_tick:
        cmd.append("--single-tick")
    subprocess.run(cmd, cwd=str(REPO), check=True, capture_output=True, text=True)
    return json.loads(out.read_text())


def pf_cell(pf) -> str:
    if pf is None:
        return "—"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.3f}"


def fmt_ret(v) -> str:
    return f"{v:.4f}" if v is not None else "—"


def main() -> None:
    windows = (7, 14, 21, 30)
    rows = []
    for days in windows:
        print(f"=== {days}d single-tick ===", file=sys.stderr)
        single = run_backtest(days, single_tick=True)
        print(f"=== {days}d robust ===", file=sys.stderr)
        robust = run_backtest(days, single_tick=False)
        rows.append({
            "backtest_days": days,
            "single_tick": {
                "n": single.get("gated_signals"),
                "pf": single.get("pf_gated_30m_tick"),
                "mean_ret": single.get("mean_return_gated"),
                "top_pool_share": single.get("top_pool_share"),
            },
            "robust_pm60s": {
                "n": robust.get("gated_signals"),
                "pf": robust.get("pf_gated_30m_tick"),
                "mean_ret": robust.get("mean_return_gated"),
                "top_pool_share": robust.get("top_pool_share"),
            },
            "passes_both_pf_1.5_n_15": (
                (single.get("pf_gated_30m_tick") or 0) > 1.5
                and (single.get("gated_signals") or 0) >= 15
                and (robust.get("pf_gated_30m_tick") or 0) > 1.5
                and (robust.get("gated_signals") or 0) >= 15
            ),
        })

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": "All PF claims must cite both single_tick and robust_pm60s columns",
        "pass_threshold": {"pf": 1.5, "n": 15},
        "windows": rows,
        "RESEARCH_ONLY": True,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2))

    md_lines = [
        "# Dual Metric Dashboard — Sender-Gated v0",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "**Rule:** Every PF number must be read as a pair (single-tick | robust ±60s).",
        "",
        "| window | n (single\\|robust) | PF single-tick | PF robust ±60s | mean ret single | mean ret robust | both pass? |",
        "|--------|-------------------:|---------------:|---------------:|----------------:|----------------:|:----------:|",
    ]
    for r in rows:
        s, rb = r["single_tick"], r["robust_pm60s"]
        md_lines.append(
            f"| {r['backtest_days']}d "
            f"| {s['n']}\\|{rb['n']} "
            f"| {pf_cell(s['pf'])} "
            f"| {pf_cell(rb['pf'])} "
            f"| {fmt_ret(s['mean_ret'])} "
            f"| {fmt_ret(rb['mean_ret'])} "
            f"| {'✅' if r['passes_both_pf_1.5_n_15'] else '❌'} |"
        )
    md_lines += ["", "Guard NOT lifted. Research only.", ""]
    OUT_MD.write_text("\n".join(md_lines))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
