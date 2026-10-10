#!/usr/bin/env python3
"""Sprint083 SOP reality audit.

Read-only analysis that consolidates the next decision blockers without opening
another sprint namespace:

1. 0x6ff5 True/False counterfactual on the same sprint055 label table.
2. Rolling 7d toxic-pool rate from completed paper-shadow labels.
3. Forward-definition alignment across sprint075, sprint079, sprint083.

No RPC, Redis, ClickHouse, or live trading side effects.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CSV_055 = REPO / "logs/sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp/audits/sprint083_fee500_paper_shadow"
S075_SCOREBOARD = REPO / "mvp/audits/sprint075_forward_harness/scoreboard.json"
S079_SCOREBOARD = REPO / "mvp/audits/sprint079_forward_harness/scoreboard.json"
S076_LEADERBOARD = REPO / "mvp/audits/sprint076_forward_race/sprint076_forward_leaderboard.json"
N_ACCOUNTING = REPO / "mvp/audits/execution_readiness/n_accounting.json"

HORIZONS = ("30m", "1h", "2h", "6h", "12h")
TOXIC_LOSS_THRESHOLD = -0.15
CRASH_LOSS_THRESHOLD = -0.25


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fnum(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_csv_rows() -> list[dict[str, Any]]:
    with CSV_055.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_dt"] = parse_dt(r.get("signal_time", ""))
    return [r for r in rows if r["_dt"] is not None]


def profit_factor(vals: list[float]) -> float | None:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses > 0:
        return round(wins / losses, 6)
    if wins > 0:
        return None
    return 0.0


def metric_block(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    key = f"net_return_{horizon}_ag"
    vals = [fnum(r.get(key)) for r in rows if r.get("entry_quote_ok") == "True"]
    vals = [v for v in vals if v is not None]
    pools = Counter((r.get("pool") or "").lower() for r in rows if fnum(r.get(key)) is not None)
    senders = Counter((r.get("dominant_sender") or "").lower() for r in rows if r.get("dominant_sender"))
    return {
        "n": len(vals),
        "pf": profit_factor(vals) if vals else None,
        "wr": round(sum(1 for v in vals if v > 0) / len(vals), 6) if vals else None,
        "mean": round(statistics.mean(vals), 6) if vals else None,
        "median": round(statistics.median(vals), 6) if vals else None,
        "top_pool_share": round(max(pools.values()) / sum(pools.values()), 6) if pools else None,
        "top_sender_share": round(max(senders.values()) / sum(senders.values()), 6) if senders else None,
    }


def counterfactual_6ff5(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [r for r in rows if r.get("entry_quote_ok") == "True" and r.get("is_0x6ff5") in ("True", "False")]
    by_flag = {
        "is_0x6ff5_true": [r for r in eligible if r.get("is_0x6ff5") == "True"],
        "is_0x6ff5_false": [r for r in eligible if r.get("is_0x6ff5") == "False"],
    }
    blocks = {
        label: {h: metric_block(group, h) for h in HORIZONS}
        for label, group in by_flag.items()
    }
    differential = {}
    for h in HORIZONS:
        t = blocks["is_0x6ff5_true"][h]
        f = blocks["is_0x6ff5_false"][h]
        differential[h] = {
            "pf_gap_true_minus_false": round((t["pf"] or 0) - (f["pf"] or 0), 6)
            if t["pf"] is not None and f["pf"] is not None else None,
            "mean_gap_true_minus_false": round((t["mean"] or 0) - (f["mean"] or 0), 6)
            if t["mean"] is not None and f["mean"] is not None else None,
            "n_true": t["n"],
            "n_false": f["n"],
        }
    verdict = "COUNTERFACTUAL_READY_NOT_CAUSAL"
    if any(v["n_false"] < 20 for v in differential.values()):
        verdict = "COUNTERFACTUAL_UNDERPOWERED_FALSE_GROUP"
    return {
        "sample_policy": "same sprint055 paper-shadow rows; grouped only by enriched is_0x6ff5 flag",
        "by_group": blocks,
        "differential": differential,
        "verdict": verdict,
        "interpretation": "A positive gap is attribution evidence only; promotion still requires live-forward N and concentration gates.",
    }


def is_toxic(row: dict[str, Any]) -> tuple[bool, list[str]]:
    flags: list[str] = []
    r12 = fnum(row.get("net_return_12h_ag"))
    if r12 is not None and r12 <= TOXIC_LOSS_THRESHOLD:
        flags.append("12h_loss_le_15pct")
    for h in HORIZONS:
        v = fnum(row.get(f"net_return_{h}_ag"))
        if v is not None and v <= CRASH_LOSS_THRESHOLD:
            flags.append(f"{h}_loss_le_25pct")
    return bool(flags), flags


def rolling_toxic(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    start = now - timedelta(days=7)
    label_ready = [
        r for r in rows
        if r["_dt"] >= start
        and r.get("entry_quote_ok") == "True"
        and fnum(r.get("net_return_12h_ag")) is not None
    ]
    toxic_rows = []
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_fee: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_sender_flag: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pool_toxic: dict[str, list[bool]] = defaultdict(list)
    for r in label_ready:
        toxic, _ = is_toxic(r)
        if toxic:
            toxic_rows.append(r)
        day = r["_dt"].date().isoformat()
        by_day[day].append(r)
        by_fee[str(r.get("fee_bps") or "unknown")].append(r)
        by_sender_flag[str(r.get("is_0x6ff5") or "unknown")].append(r)
        pool_toxic[(r.get("pool") or "").lower()].append(toxic)

    def rate(group: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(group)
        tox_n = sum(1 for x in group if is_toxic(x)[0])
        return {
            "n": n,
            "toxic_n": tox_n,
            "toxic_rate": round(tox_n / n, 6) if n else None,
        }

    pool_rows = []
    for pool, vals in pool_toxic.items():
        pool_rows.append({
            "pool": pool,
            "n": len(vals),
            "toxic_n": sum(1 for v in vals if v),
            "toxic_rate": round(sum(1 for v in vals if v) / len(vals), 6) if vals else None,
        })
    pool_rows.sort(key=lambda x: (-(x["toxic_rate"] or 0), -x["n"], x["pool"]))

    overall = rate(label_ready)
    status = "TOXIC_RATE_OK"
    if overall["toxic_rate"] is not None and overall["toxic_rate"] > 0.15:
        status = "TOXIC_RATE_GT_15PCT_DOWNGRADE_REQUIRED"
    return {
        "window_start_utc": start.isoformat(),
        "window_end_utc": now.isoformat(),
        "label_policy": "requires entry_quote_ok=True and completed 12h label",
        "overall": overall,
        "by_day": {k: rate(v) for k, v in sorted(by_day.items())},
        "by_fee_bps": {k: rate(v) for k, v in sorted(by_fee.items())},
        "by_is_0x6ff5": {k: rate(v) for k, v in sorted(by_sender_flag.items())},
        "top_toxic_pools": pool_rows[:10],
        "status": status,
    }


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def forward_alignment() -> dict[str, Any]:
    s075 = read_json(S075_SCOREBOARD, [])
    s079 = read_json(S079_SCOREBOARD, {})
    s076 = read_json(S076_LEADERBOARD, {})
    n_acc = read_json(N_ACCOUNTING, {})

    s075_max_n = max((int(x.get("fwd_n") or 0) for x in s075), default=0)
    s075_bad_conc = sum(1 for x in s075 if fnum(x.get("fwd_top_pool_share")) and fnum(x.get("fwd_top_pool_share")) > 0.50)
    s079_strats = s079.get("strategies", [])
    s079_6ff5 = [x for x in s079_strats if x.get("sender_0x6ff5_only")]
    s076_forward = ((s076.get("views") or {}).get("forward_only") or [])
    downgraded = [
        x for x in s076_forward
        if x.get("sop_review_status") in ("REGIME_SPECIFIC_ALPHA", "SUBJECT_CONDITIONED_ALPHA")
    ]

    rows = [
        {
            "source": "sprint075_forward_harness",
            "artifact": str(S075_SCOREBOARD),
            "evidence_class": "HARNESS_FORWARD_LABELS_NOT_EXECUTOR_LIVE",
            "n": s075_max_n,
            "primary_gate_issue": "top_pool_concentration",
            "downgraded_count": s075_bad_conc,
            "can_count_for_canary": False,
        },
        {
            "source": "sprint079_forward_harness",
            "artifact": str(S079_SCOREBOARD),
            "evidence_class": "ARCHIVE_BACKFILL_OOS_NOT_CRON_LIVE_FORWARD",
            "n": max((int(x.get("forward_n") or 0) for x in s079_strats), default=0),
            "primary_gate_issue": "archive_replay_and_0x6ff5_subject_conditioning",
            "sender_0x6ff5_strategy_count": len(s079_6ff5),
            "can_count_for_canary": False,
        },
        {
            "source": "sprint083_accumulation_monitor",
            "artifact": str(N_ACCOUNTING),
            "evidence_class": "EXECUTOR_ACCOUNTING",
            "n": int(n_acc.get("true_live_forward_executor_n") or 0),
            "primary_gate_issue": "signal_supply_and_live_n_below_25",
            "source_status": n_acc.get("source_status"),
            "can_count_for_canary": True,
        },
        {
            "source": "sprint076_forward_leaderboard",
            "artifact": str(S076_LEADERBOARD),
            "evidence_class": "SOP_REVIEW_VIEW",
            "n": len(s076_forward),
            "primary_gate_issue": "concentration_downgrades_are_policy_not_performance_metric",
            "downgraded_count": len(downgraded),
            "can_count_for_canary": False,
        },
    ]
    return {
        "canonical_canary_source": "sprint083_accumulation_monitor.true_live_forward_executor_n",
        "canonical_canary_n": int(n_acc.get("true_live_forward_executor_n") or 0),
        "rows": rows,
        "verdict": "DO_NOT_MIX_FORWARD_DEFINITIONS",
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    cf = report["counterfactual_6ff5"]
    toxic = report["rolling_toxic_7d"]
    align = report["forward_definition_alignment"]
    lines = [
        "# Sprint083 SOP Reality Audit",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "## Decision",
        "",
        f"- 0x6ff5 counterfactual: `{cf['verdict']}`",
        f"- Rolling toxic status: `{toxic['status']}`",
        f"- Forward definition status: `{align['verdict']}`",
        f"- Canonical canary N: `{align['canonical_canary_n']}` from `true_live_forward_executor_n`",
        "",
        "## 0x6ff5 Differential",
        "",
        "| horizon | n_true | n_false | pf_gap_true_minus_false | mean_gap_true_minus_false |",
        "|---|---:|---:|---:|---:|",
    ]
    for h, d in cf["differential"].items():
        lines.append(
            f"| {h} | {d['n_true']} | {d['n_false']} | {d['pf_gap_true_minus_false']} | {d['mean_gap_true_minus_false']} |"
        )
    lines += [
        "",
        "## Rolling Toxic",
        "",
        f"- Overall: `{toxic['overall']}`",
        f"- By fee: `{toxic['by_fee_bps']}`",
        f"- By is_0x6ff5: `{toxic['by_is_0x6ff5']}`",
        "",
        "## Forward Definitions",
        "",
        "| source | evidence_class | n | can_count_for_canary | primary_gate_issue |",
        "|---|---|---:|---|---|",
    ]
    for r in align["rows"]:
        lines.append(
            f"| {r['source']} | {r['evidence_class']} | {r['n']} | {r['can_count_for_canary']} | {r['primary_gate_issue']} |"
        )
    lines.append("")
    path.write_text("\n".join(lines))


def main() -> None:
    now = utc_now()
    rows = load_csv_rows()
    report = {
        "generated_at_utc": now.isoformat(),
        "source_csv": str(CSV_055),
        "counterfactual_6ff5": counterfactual_6ff5(rows),
        "rolling_toxic_7d": rolling_toxic(rows, now),
        "forward_definition_alignment": forward_alignment(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "sop_reality_audit.json"
    md_path = OUT_DIR / "sop_reality_audit.md"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    write_markdown(report, md_path)
    print(json.dumps({
        "ok": True,
        "json": str(json_path),
        "markdown": str(md_path),
        "counterfactual_verdict": report["counterfactual_6ff5"]["verdict"],
        "toxic_status": report["rolling_toxic_7d"]["status"],
        "canonical_canary_n": report["forward_definition_alignment"]["canonical_canary_n"],
    }, indent=2))


if __name__ == "__main__":
    main()
