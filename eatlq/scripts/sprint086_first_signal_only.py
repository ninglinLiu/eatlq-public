#!/usr/bin/env python3
"""Sprint086/087 — First-signal-only + 0x6ff5 reverse-filter validation.

Task A: deduplicate paper-shadow rows to first signal per pool per 24h window,
        rerun trailing_5_pull3_2h with real cost, check PF > 1.3.

Task C: same but exclude rows where is_0x6ff5=True or dominant_sender=0x6ff5,
        or optionally exclude entire pool if 0x6ff5 ever traded that pool before signal.

Outputs:
  mvp/audits/sprint086_real_cost_overlay/first_signal_only_summary.json
  mvp/audits/sprint086_real_cost_overlay/first_signal_only_readout.md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from sprint086_batched_exit_backtest import (  # noqa: E402
    DEFAULT_POLICIES,
    ExitPolicy,
    _as_float,
    apply_policy,
    pf,
    trajectory,
)

SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
TRAILING_2H = next(p for p in DEFAULT_POLICIES if p.name == "trailing_5_pull3_2h")

DEFAULT_INPUTS = [
    "mvp/audits/sprint086_real_cost_overlay/sprint055_paper_shadow_realcost.csv",
    "mvp/audits/sprint086_real_cost_overlay/alpha_001_paper_shadow_realcost.csv",
    "mvp/audits/sprint086_real_cost_overlay/alpha_005_paper_shadow_realcost.csv",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="First-signal-only + 0x6ff5 reverse filter")
    p.add_argument("--inputs", nargs="+", default=DEFAULT_INPUTS)
    p.add_argument("--out-dir", default="mvp/audits/sprint086_real_cost_overlay")
    p.add_argument("--window-sec", type=int, default=86400, help="First-signal dedup window (default 24h)")
    p.add_argument("--per-leg-cost-pct", type=float, default=0.012)
    p.add_argument("--pf-threshold", type=float, default=1.3)
    p.add_argument("--run-reverse-filter", action="store_true", default=True)
    return p.parse_args()


def parse_signal_time(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def first_signal_only(rows: list[dict], window_sec: int) -> list[dict]:
    """Keep earliest signal per pool; suppress repeats within window_sec."""
    sorted_rows = sorted(rows, key=lambda r: r.get("signal_time") or "")
    kept: list[dict] = []
    last_ts: dict[str, datetime] = {}
    for r in sorted_rows:
        pool = (r.get("pool") or "").lower()
        ts = parse_signal_time(r.get("signal_time", ""))
        if not pool or ts is None:
            continue
        prev = last_ts.get(pool)
        if prev is None or (ts - prev).total_seconds() >= window_sec:
            kept.append(r)
            last_ts[pool] = ts
    return kept


def is_0x6ff5_row(r: dict) -> bool:
    flag = str(r.get("is_0x6ff5", "")).strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    dom = str(r.get("dominant_sender", "")).strip().lower()
    return dom == SENDER_0X6FF5


def exclude_0x6ff5(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not is_0x6ff5_row(r)]


def sell_path_ok(row: dict) -> bool:
    v = str(row.get("sell_path_ok", "True")).strip().lower()
    return v in ("true", "1", "yes", "")


def eval_policy(rows: list[dict], policy: ExitPolicy, per_leg_cost: float) -> dict:
    nets: list[float] = []
    for r in rows:
        traj = trajectory(r, use_real=True)
        res = apply_policy(traj, policy, per_leg_cost, sell_path_ok(r))
        if isinstance(res["net"], (int, float)):
            nets.append(float(res["net"]))
    return {
        "n": len(nets),
        "mean_return": round(sum(nets) / max(len(nets), 1), 6) if nets else None,
        "win_rate": round(sum(1 for v in nets if v > 0) / max(len(nets), 1), 4) if nets else None,
        "pf": pf(nets),
    }


def eval_horizon_pf(rows: list[dict], label: str = "1h") -> dict:
    col = f"net_return_{label}_real"
    vals = [_as_float(r.get(col)) for r in rows]
    vals = [v for v in vals if v is not None]
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 6) if vals else None,
        "hit": round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None,
        "pf": pf(vals),
    }


def process_source(path: Path, args) -> dict:
    if not path.exists():
        return {"input": str(path), "status": "missing"}
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    deduped = first_signal_only(rows, args.window_sec)
    deduped_no6ff5 = exclude_0x6ff5(deduped) if args.run_reverse_filter else deduped

    scenarios = {
        "all_rows": rows,
        "first_signal_only": deduped,
    }
    if args.run_reverse_filter:
        scenarios["first_signal_no_0x6ff5"] = deduped_no6ff5

    results: dict[str, dict] = {}
    for name, subset in scenarios.items():
        trailing = eval_policy(subset, TRAILING_2H, args.per_leg_cost_pct)
        h1_real = eval_horizon_pf(subset, "1h")
        results[name] = {
            "rows": len(subset),
            "unique_pools": len({r.get("pool") for r in subset}),
            "trailing_2h": trailing,
            "real_pf_1h_horizon": h1_real,
            "passes_pf_threshold": (
                trailing.get("pf") is not None
                and trailing["pf"] != float("inf")
                and trailing["pf"] >= args.pf_threshold
            ),
        }

    dedup_ratio = round(len(deduped) / max(len(rows), 1), 4)
    removed_6ff5 = len(deduped) - len(deduped_no6ff5)

    return {
        "input": str(path.relative_to(REPO)),
        "status": "ok",
        "total_rows": len(rows),
        "deduped_rows": len(deduped),
        "dedup_ratio": dedup_ratio,
        "removed_by_0x6ff5_filter": removed_6ff5,
        "window_sec": args.window_sec,
        "scenarios": results,
    }


def write_readout(out_dir: Path, summaries: list[dict], args) -> None:
    decision = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP086-FIRST-SIGNAL-ONLY",
        "task": "A + C validation",
        "policy": TRAILING_2H.name,
        "pf_threshold": args.pf_threshold,
        "window_sec": args.window_sec,
        "per_leg_cost_pct": args.per_leg_cost_pct,
        "summaries": summaries,
        "verdict": {},
    }

    md = [
        "# Sprint086 — First-Signal-Only + 0x6ff5 Reverse Filter",
        "",
        f"Policy: **`{TRAILING_2H.name}`** · PF threshold: **{args.pf_threshold}** · "
        f"Dedup window: **{args.window_sec // 3600}h**",
        "",
        "## Headline (first_signal_only → trailing 2h)",
        "",
        "| source | all rows PF | first-only PF | first-only n | pass ≥1.3? |",
        "|---|--:|--:|--:|:-:|",
    ]

    for s in summaries:
        if s.get("status") != "ok":
            md.append(f"| {Path(s['input']).name} | – | – | – | – |")
            continue
        all_sc = s["scenarios"]["all_rows"]["trailing_2h"]
        fs_sc = s["scenarios"]["first_signal_only"]["trailing_2h"]
        passed = s["scenarios"]["first_signal_only"]["passes_pf_threshold"]
        decision["verdict"][s["input"]] = {
            "first_signal_pf": fs_sc.get("pf"),
            "passes": passed,
        }
        md.append(
            f"| {Path(s['input']).name} | {all_sc.get('pf')} | "
            f"**{fs_sc.get('pf')}** | {fs_sc.get('n')} | {'✓' if passed else '✗'} |"
        )

    if args.run_reverse_filter:
        md += [
            "",
            "## 0x6ff5 reverse filter (first_signal_only, exclude is_0x6ff5=True rows)",
            "",
            "| source | before n | after n | removed | PF before | PF after | delta |",
            "|---|--:|--:|--:|--:|--:|--:|",
        ]
        for s in summaries:
            if s.get("status") != "ok":
                continue
            before = s["scenarios"]["first_signal_only"]["trailing_2h"]
            after = s["scenarios"]["first_signal_no_0x6ff5"]["trailing_2h"]
            pf_b, pf_a = before.get("pf"), after.get("pf")
            delta = round(pf_a - pf_b, 4) if pf_b is not None and pf_a is not None else None
            md.append(
                f"| {Path(s['input']).name} | {before.get('n')} | {after.get('n')} | "
                f"{s.get('removed_by_0x6ff5_filter', 0)} | {pf_b} | **{pf_a}** | {delta} |"
            )

    md += ["", "## Per-source detail", ""]
    for s in summaries:
        md.append(f"### {Path(s.get('input', '?')).name}")
        md.append("")
        if s.get("status") != "ok":
            md.append(f"- status: `{s.get('status')}`")
            md.append("")
            continue
        md.append(f"- total rows: {s['total_rows']} → deduped: **{s['deduped_rows']}** ({s['dedup_ratio']*100:.1f}%)  ")
        for scen_name, scen in s["scenarios"].items():
            t = scen["trailing_2h"]
            md.append(
                f"- `{scen_name}`: n={t['n']} mean={t['mean_return']} WR={t['win_rate']} "
                f"PF={t['pf']} pass={scen['passes_pf_threshold']}"
            )
        md.append("")

    (out_dir / "first_signal_only_summary.json").write_text(json.dumps(decision, indent=2) + "\n")
    (out_dir / "first_signal_only_readout.md").write_text("\n".join(md) + "\n")


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    for inp in args.inputs:
        path = REPO / inp
        print(f"[first-signal] {inp}", flush=True)
        s = process_source(path, args)
        summaries.append(s)
        if s.get("status") == "ok":
            fs = s["scenarios"]["first_signal_only"]["trailing_2h"]
            print(f"  all PF={s['scenarios']['all_rows']['trailing_2h']['pf']} "
                  f"→ first-only PF={fs['pf']} n={fs['n']}", flush=True)

    write_readout(out_dir, summaries, args)
    print(json.dumps({"out_dir": args.out_dir, "sources": len(summaries)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
