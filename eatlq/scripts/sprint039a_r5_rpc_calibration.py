#!/usr/bin/env python3
"""Sprint039A — empirical eth_call histogram for R5 (--run) vs CH candidate counts.

Runs sprint037b with SPRINT037B_RPC_INSTRUMENT=1 on one or two representative weeks,
writes per-week rpc_instrument_*.json under --work-dir, aggregates ratios, and plans a
greedy smallest-CH-first subset under RPC_CAP using a padded max observed ratio.

No RPC URLs or secrets are printed or logged by this script.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION_WEEKS = ("2025-10-20", "2026-04-20")
DEFAULT_PREFLIGHT = REPO / "mvp/audits/sprint039a/regime_discovery_preflight.json"
DEFAULT_OUT_JSON = REPO / "mvp/audits/sprint039a/rpc_calibration_histogram.json"
DEFAULT_RPC_CAP = 60_000


def week_end_exclusive(ws: str) -> str:
    t0 = datetime.strptime(ws, "%Y-%m-%d")
    return (t0 + timedelta(days=7)).strftime("%Y-%m-%d")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--weeks",
        default=",".join(DEFAULT_CALIBRATION_WEEKS),
        help="Comma-separated ISO Monday week_starts (default: small + medium CH dry-run weeks)",
    )
    ap.add_argument(
        "--work-dir",
        default=str(REPO / "mvp/audits/sprint039a/rpc_calibration_workdir"),
        help="037b --out-dir; receives candidates_*.csv and rpc_instrument_*.json",
    )
    ap.add_argument("--out-json", default=str(DEFAULT_OUT_JSON))
    ap.add_argument("--preflight-json", default=str(DEFAULT_PREFLIGHT))
    ap.add_argument("--rpc-cap", type=int, default=DEFAULT_RPC_CAP)
    ap.add_argument(
        "--safety-pad",
        type=float,
        default=1.05,
        help="recommended_multiplier = max(observed ratio) × pad",
    )
    args = ap.parse_args()

    weeks = [w.strip() for w in args.weeks.split(",") if w.strip()]
    if len(weeks) < 1:
        print("[calib] ERROR: empty --weeks", flush=True)
        sys.exit(2)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    reg_path = work_dir / "calibration_regimes.json"
    reg_objs = [
        {
            "week_start": w,
            "week_end": week_end_exclusive(w),
            "role": "positive",
            "label": f"Sprint039A RPC calib {w}",
        }
        for w in weeks
    ]
    reg_path.write_text(json.dumps(reg_objs, indent=2), encoding="utf-8")

    env = {**os.environ, "SPRINT037B_RPC_INSTRUMENT": "1"}
    cmd = [
        sys.executable,
        str(REPO / "scripts/sprint037b_r5_label_expansion.py"),
        "--run",
        "--regimes-json",
        str(reg_path),
        "--out-dir",
        str(work_dir),
        "--no-global-artifacts",
    ]
    print("[calib] running sprint037b (instrumented) …", flush=True)
    pr = subprocess.run(cmd, cwd=str(REPO), env=env, timeout=3 * 3600)
    if pr.returncode != 0:
        print("[calib] ERROR sprint037b exit", pr.returncode, flush=True)
        sys.exit(pr.returncode)

    per_week: list[dict] = []
    ratios: list[float] = []
    for w in weeks:
        jp = work_dir / f"rpc_instrument_{w}.json"
        if not jp.is_file():
            print("[calib] ERROR missing instrument file:", jp, flush=True)
            sys.exit(4)
        row = json.loads(jp.read_text(encoding="utf-8"))
        per_week.append(row)
        ratios.append(float(row["ratio_eth_calls_per_ch_candidate"]))

    max_ratio = max(ratios)
    mean_ratio = sum(ratios) / len(ratios)
    recommended = round(max_ratio * args.safety_pad, 4)

    implied_legacy = []
    for row in per_week:
        leg = int(row["assumed_multiplier_legacy_18_implied_calls"])
        act = int(row["eth_calls_total"])
        implied_legacy.append(
            {
                "week": row["regime_week"],
                "actual_eth_calls": act,
                "legacy_x18_implied_calls": leg,
                "legacy_overestimate_factor": round(leg / max(act, 1), 4),
            }
        )

    preflight_path = Path(args.preflight_json)
    estimates: list[dict[str, object]] = []
    if preflight_path.is_file():
        pre = json.loads(preflight_path.read_text(encoding="utf-8"))
        for r in pre.get("dry_run_estimates_by_week", []):
            ws = str(r.get("week_start") or "")
            n = int(r.get("ch_candidates_dry_run") or 0)
            if ws and n > 0:
                estimates.append({"week_start": ws, "ch_candidates_dry_run": n})
    estimates.sort(key=lambda x: int(x["ch_candidates_dry_run"]))  # type: ignore[arg-type]

    subset_weeks: list[str] = []
    total_rpc_est = 0
    for e in estimates:
        ws = str(e["week_start"])
        n = int(e["ch_candidates_dry_run"])
        add = int(n * recommended)
        if total_rpc_est + add <= args.rpc_cap:
            subset_weeks.append(ws)
            total_rpc_est += add

    rel_out = Path(args.out_json)
    try:
        invoke_mult = str(rel_out.relative_to(REPO))
    except ValueError:
        invoke_mult = str(rel_out)

    phase1_hint = ""
    if subset_weeks:
        phase1_hint = (
            f"python scripts/sprint039a_regime_discovery_r5_expansion.py "
            f"--rpc-multiplier-json {invoke_mult} "
            f"--phase1-weeks {','.join(subset_weeks)}"
        )

    out_doc = {
        "kind": "sprint039a_r5_rpc_calibration",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "calibration_weeks": weeks,
        "work_dir": str(work_dir),
        "per_week_rpc_instrument": per_week,
        "ratio_eth_calls_per_ch_candidate_max": round(max_ratio, 6),
        "ratio_eth_calls_per_ch_candidate_mean": round(mean_ratio, 6),
        "recommended_multiplier_budget_conservative": recommended,
        "safety_pad_applied": args.safety_pad,
        "legacy_multiplier_18_vs_actual": implied_legacy,
        "subset_under_rpc_cap": {
            "rpc_cap": args.rpc_cap,
            "preflight_source": str(preflight_path),
            "greedy_smallest_ch_weeks_first": subset_weeks,
            "estimated_rpc_calls_at_recommended_multiplier": total_rpc_est,
            "n_weeks_in_subset": len(subset_weeks),
        },
        "sprint039a_invoke_hint": phase1_hint or None,
    }

    outp = Path(args.out_json)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out_doc, indent=2), encoding="utf-8")
    print("[calib] wrote", outp, "recommended_multiplier=", recommended, flush=True)


if __name__ == "__main__":
    main()
