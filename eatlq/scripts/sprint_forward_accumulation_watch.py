#!/usr/bin/env python3
"""
Forward accumulation watch — S074_* dry-run state + leaderboard milestones.

Read-only. Appends time series for S074_023 (PF/WR/n/top_pool_share).
Cron: */30 * * * * cd /opt/EATLQ && python3 scripts/sprint_forward_accumulation_watch.py --once
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
STATE_DIRS = [
    REPO / "logs" / "sprint075",
    REPO / "logs" / "sprint077",
    REPO / "logs" / "sprint079",
]
OUT_DIR = REPO / "mvp" / "audits" / "forward_accumulation_watch"
FOCUS_ID = "S074_023"
MILESTONES = (20, 50, 100)
POOL_CONC_WARN = 0.50


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def metrics_from_trades(trades: list[dict]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "forward_n": 0,
            "pf": None,
            "win_rate": None,
            "mean_return": None,
            "max_drawdown": None,
            "top_pool_share": None,
            "unique_pools": 0,
            "top_pools": [],
        }
    rets = [float(t.get("ret_ag") or 0) for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [abs(r) for r in rets if r < 0]
    pf = round(sum(wins) / sum(losses), 6) if losses else None
    pools = [str(t.get("pool") or "") for t in trades if t.get("pool")]
    cnt = Counter(pools)
    top_share = round(max(cnt.values()) / n, 6) if cnt else None
    eq = 100.0
    peak = eq
    max_dd = 0.0
    for r in sorted(rets):
        eq += r * 10.0
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return {
        "forward_n": n,
        "pf": pf,
        "win_rate": round(len(wins) / n, 6),
        "mean_return": round(statistics.mean(rets), 6),
        "max_drawdown": round(max_dd, 6),
        "top_pool_share": top_share,
        "unique_pools": len(cnt),
        "top_pools": [{"pool": p, "n": c, "share": round(c / n, 6)} for p, c in cnt.most_common(5)],
    }


def load_strategy_states() -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for d in STATE_DIRS:
        if not d.exists():
            continue
        for p in d.glob("*_state.json"):
            sid = p.stem.replace("_state", "")
            try:
                st = json.loads(p.read_text())
            except Exception:
                continue
            trades = st.get("trades") or []
            m = metrics_from_trades(trades)
            prev = by_id.get(sid)
            if prev is None or m["forward_n"] >= prev["metrics"]["forward_n"]:
                by_id[sid] = {
                    "strategy_id": sid,
                    "state_path": str(p.relative_to(REPO)),
                    "harness_dir": d.name,
                    "last_run": st.get("last_run"),
                    "metrics": m,
                }
    return by_id


def milestone_status(n: int) -> dict[str, bool]:
    return {str(m): n >= m for m in MILESTONES}


def append_history(focus: dict) -> None:
    hist_path = OUT_DIR / f"{FOCUS_ID}_history.jsonl"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now_iso(), **focus}
    with open(hist_path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_history_tail(n: int = 48) -> list[dict]:
    hist_path = OUT_DIR / f"{FOCUS_ID}_history.jsonl"
    if not hist_path.exists():
        return []
    lines = hist_path.read_text().strip().splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def concentration_trend(history: list[dict]) -> str:
    if len(history) < 2:
        return "INSUFFICIENT_HISTORY"
    shares = [h.get("top_pool_share") for h in history if h.get("top_pool_share") is not None]
    if len(shares) < 2:
        return "UNKNOWN"
    delta = shares[-1] - shares[0]
    if delta < -0.05:
        return "IMPROVING"
    if delta > 0.05:
        return "WORSENING"
    return "STABLE"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    states = load_strategy_states()
    rows = []
    for sid, rec in sorted(states.items()):
        m = rec["metrics"]
        n = m["forward_n"]
        flag = "PLUMBING_ONLY"
        if n >= 100:
            flag = "FORWARD_MATURE_CANDIDATE"
        elif n >= 50:
            flag = "N50_PLUS_WATCH"
        elif n >= 20:
            flag = "EARLY_HINT"
        rows.append(
            {
                "strategy_id": sid,
                "harness": rec["harness_dir"],
                "forward_n": n,
                "pf": m["pf"],
                "win_rate": m["win_rate"],
                "mean_return": m["mean_return"],
                "top_pool_share": m["top_pool_share"],
                "unique_pools": m["unique_pools"],
                "promotion_watch_flag": flag,
                "milestones": milestone_status(n),
                "state_path": rec["state_path"],
            }
        )

    rows.sort(key=lambda r: (-r["forward_n"], -(r["mean_return"] or -999)))
    max_n = max((r["forward_n"] for r in rows), default=0)
    ge100 = [r for r in rows if r["forward_n"] >= 100]

    focus_rec = states.get(FOCUS_ID)
    focus_block: dict[str, Any] = {"strategy_id": FOCUS_ID, "found": focus_rec is not None}
    if focus_rec:
        m = focus_rec["metrics"]
        focus_block.update(
            {
                "state_path": focus_rec["state_path"],
                "harness": focus_rec["harness_dir"],
                "last_run": focus_rec["last_run"],
                **m,
                "milestones": milestone_status(m["forward_n"]),
                "pool_conc_at_warn": m["top_pool_share"] is not None and m["top_pool_share"] >= POOL_CONC_WARN,
                "next_milestone": 50 if m["forward_n"] < 50 else (100 if m["forward_n"] < 100 else None),
                "trades_to_n50": max(0, 50 - m["forward_n"]),
                "trades_to_n100": max(0, 100 - m["forward_n"]),
            }
        )
        append_history(focus_block)
        hist = load_history_tail()
        focus_block["concentration_trend"] = concentration_trend(hist)
        if hist:
            focus_block["top_pool_share_first"] = hist[0].get("top_pool_share")
            focus_block["top_pool_share_latest"] = hist[-1].get("top_pool_share")

    n_acct = {}
    na_path = REPO / "mvp" / "audits" / "execution_readiness" / "n_accounting.json"
    if na_path.exists():
        try:
            n_acct = json.loads(na_path.read_text())
        except Exception:
            pass

    snapshot = {
        "generated_at_utc": utc_now_iso(),
        "policy": {
            "forward_dryrun_auto_accumulate": True,
            "leaderboard_cron": "*/15 scripts/sprint076_forward_leaderboard.py",
            "harness_crons": ["sprint075", "sprint077", "sprint079 every 5m"],
            "milestones": list(MILESTONES),
            "pool_conc_review_threshold": POOL_CONC_WARN,
        },
        "summary": {
            "strategy_count": len(rows),
            "max_forward_n": max_n,
            "count_n_ge_100": len(ge100),
            "count_n_ge_50": sum(1 for r in rows if r["forward_n"] >= 50),
            "count_n_ge_20": sum(1 for r in rows if r["forward_n"] >= 20),
            "true_live_forward_executor_n": n_acct.get("true_live_forward_executor_n"),
        },
        "focus_S074_023": focus_block,
        "all_strategies": rows,
    }

    (OUT_DIR / "forward_accumulation_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, default=str)
    )

    csv_path = OUT_DIR / "forward_accumulation_snapshot.csv"
    if rows:
        fields = list(rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                r2 = dict(r)
                r2["milestones"] = json.dumps(r2.get("milestones"))
                w.writerow(r2)

    md = [
        "# Forward accumulation watch",
        "",
        f"- generated_at_utc: {snapshot['generated_at_utc']}",
        f"- max_forward_n: **{max_n}** (goal 100)",
        f"- strategies n≥50: {snapshot['summary']['count_n_ge_50']}",
        f"- true_live_forward_executor_n: **{n_acct.get('true_live_forward_executor_n', '?')}** (canary pre-req 25, not live)",
        "",
        f"## Focus: {FOCUS_ID}",
        "",
    ]
    if focus_rec:
        fb = focus_block
        md += [
            f"| metric | value |",
            f"|--------|------:|",
            f"| forward_n | {fb.get('forward_n')} |",
            f"| pf | {fb.get('pf')} |",
            f"| win_rate | {fb.get('win_rate')} |",
            f"| top_pool_share | {fb.get('top_pool_share')} |",
            f"| unique_pools | {fb.get('unique_pools')} |",
            f"| trades_to_n50 | {fb.get('trades_to_n50')} |",
            f"| trades_to_n100 | {fb.get('trades_to_n100')} |",
            f"| concentration_trend | {fb.get('concentration_trend')} |",
            f"| pool_conc_at_warn (>{POOL_CONC_WARN}) | {fb.get('pool_conc_at_warn')} |",
            "",
            "### Top pools",
            "",
        ]
        for p in fb.get("top_pools") or []:
            md.append(f"- `{p['pool'][:18]}…` n={p['n']} share={p['share']}")
    else:
        md.append("_S074_023 state not found._")

    md += ["", "## Top 10 by forward_n", "", "| id | n | PF | WR | pool_share | flag |", "|----|--:|---:|---:|-----------:|------|"]
    for r in rows[:10]:
        md.append(
            f"| {r['strategy_id']} | {r['forward_n']} | {r['pf']} | {r['win_rate']} | "
            f"{r['top_pool_share']} | {r['promotion_watch_flag']} |"
        )
    (OUT_DIR / "forward_accumulation_readout.md").write_text("\n".join(md) + "\n")

    print(json.dumps({"ok": True, "max_forward_n": max_n, "S074_023_n": focus_block.get("forward_n")}, indent=2))


if __name__ == "__main__":
    main()
