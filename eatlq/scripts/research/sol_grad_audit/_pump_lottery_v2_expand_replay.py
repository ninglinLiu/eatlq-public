#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lottery v2 分阶段数据扩展编排器（Phase 1: 14 天 → Phase 2: 30 天 gate）。

用法:
  --status   覆盖缺口 inventory（零 RPC）
  --collect  缺日采集（sample → label → earlypath → smart_buyers）
  --replay   frozen audit T+15→T+90 replay
  --analyze  capital compare + gate 判定
  --gate     仅 gate（需已有输出）
  --all      collect + replay + analyze
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
SOL_DIR = OUT_DIR.parent / "sol"
DIAG = Path("/opt/EATLQ/mvp/diagnostics/sol")
CGROUP = OUT_DIR.parent / "base_v4" / "run_in_cgroup.sh"

# ── Phase 1: 14 days ──
PHASE1_EXISTING = ["06-11", "06-12", "06-13", "06-14", "06-15"]
PHASE1_NEW = ["06-06", "06-07", "06-08", "06-09", "06-10", "06-16", "06-17", "06-18", "06-19", "06-20"]
PHASE1_DAYS = sorted(set(PHASE1_EXISTING + PHASE1_NEW))

TARGET_N = 1000
P300_TH = 3.0
MEGA_DAY_SHARE_MAX = 0.60
YEAR = 2026

SMART_BUYERS_EXPAND = DIAG / "smart_buyers_expand_p1.jsonl"
GATE_OUT = OUT_DIR / "_pump_lottery_v2_expand_gate.json"
AUDIT_OUT = OUT_DIR / "_pump_lottery_v2_frozen_audit.json"
CAPITAL_OUT = OUT_DIR / "_pump_lottery_v2_frozen_capital_compare.json"

# frozen rules (must match frozen_audit)
CLUSTER_MIN = 0.95
DEV_MAX = 50.0


def day_of(ms: int | float) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d")


def day_unix_window(day: str, year: int = YEAR) -> tuple[int, int]:
    month, dom = map(int, day.split("-"))
    lo = datetime(year, month, dom, tzinfo=timezone.utc)
    hi = lo + timedelta(days=1)
    return int(lo.timestamp()), int(hi.timestamp())


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def smart_buyers_by_day() -> Counter:
    c: Counter = Counter()
    for f in sorted(glob.glob(str(DIAG / "smart_buyers*.jsonl"))):
        for r in load_jsonl(Path(f)):
            d = r.get("day")
            if not d and r.get("created_ms"):
                d = day_of(r["created_ms"])
            if d:
                c[d] += 1
    return c


def labeled_by_day() -> Counter:
    c: Counter = Counter()
    for r in load_jsonl(DIAG / "creates_labeled.jsonl"):
        if r.get("created_ms"):
            c[day_of(r["created_ms"])] += 1
    return c


def replay_by_day(audit_path: Path = AUDIT_OUT) -> tuple[Counter, Counter]:
    lot, ctrl = Counter(), Counter()
    if not audit_path.exists():
        return lot, ctrl
    for r in json.loads(audit_path.read_text()).get("results", []):
        d = r.get("day")
        if not d:
            continue
        if r.get("cohort") == "lottery_v2" and r.get("real_return_10U") is not None:
            lot[d] += 1
        elif r.get("cohort") == "matched_control" and r.get("real_return_10U") is not None:
            ctrl[d] += 1
    return lot, ctrl


def estimate_lottery_n(days: list[str] | None = None) -> dict:
    """Dry-run lottery/control counts from cached smart_buyers + labeled."""
    sys.path.insert(0, str(OUT_DIR))
    from _pump_exante_filtered_rank import build_enhanced_rows
    from _pump_lottery_v2_frozen_audit import frozen_lottery_row, match_control_day

    df = pd.DataFrame(build_enhanced_rows(sorted(OUT_DIR.glob("_pump_*replay.json"))))
    if days:
        df = df[df["day"].isin(days)]
    lot_df = df[df.apply(frozen_lottery_row, axis=1)]
    ctrl_parts = []
    for day, lot_d in lot_df.groupby("day"):
        pool_d = df[(df["day"] == day) & ~df.apply(frozen_lottery_row, axis=1)]
        ctrl_parts.append(match_control_day(lot_d, pool_d, seed=hash(day) % 10000 + 42))
    ctrl_df = pd.concat(ctrl_parts, ignore_index=True) if ctrl_parts else pd.DataFrame()
    by_day = {d: int((lot_df["day"] == d).sum()) for d in sorted(lot_df["day"].unique())}
    return {
        "lottery_n": len(lot_df),
        "control_n": len(ctrl_df),
        "lottery_by_day": by_day,
        "days": sorted(lot_df["day"].unique().tolist()),
    }


def status_report(phase_days: list[str] = PHASE1_DAYS) -> dict:
    sb = smart_buyers_by_day()
    lab = labeled_by_day()
    lot_r, ctrl_r = replay_by_day()
    est = estimate_lottery_n(phase_days)

    missing_sb = [d for d in phase_days if sb.get(d, 0) == 0]
    missing_lab = [d for d in phase_days if lab.get(d, 0) < 50]
    sparse_sb = [d for d in phase_days if 0 < sb.get(d, 0) < 100]

    rep = {
        "phase": "phase1_14d",
        "target_days": phase_days,
        "smart_buyers_by_day": {d: sb.get(d, 0) for d in phase_days},
        "labeled_by_day": {d: lab.get(d, 0) for d in phase_days},
        "replayed_lottery_by_day": {d: lot_r.get(d, 0) for d in phase_days},
        "replayed_control_by_day": {d: ctrl_r.get(d, 0) for d in phase_days},
        "estimated_lottery_n": est["lottery_n"],
        "estimated_control_n": est["control_n"],
        "estimated_lottery_by_day": est["lottery_by_day"],
        "missing_smart_buyers_days": missing_sb,
        "sparse_smart_buyers_days": sparse_sb,
        "missing_labeled_days": missing_lab,
        "target_n": TARGET_N,
        "lottery_n_ok": est["lottery_n"] >= TARGET_N,
        "control_n_ok": est["control_n"] >= TARGET_N,
    }
    return rep


def print_status(rep: dict) -> None:
    print("=== Lottery v2 Phase 1 覆盖状态 ===")
    print(f"目标: {len(rep['target_days'])} 天 | Lottery n>={TARGET_N} | Control n>={TARGET_N}")
    print(f"估计 Lottery n={rep['estimated_lottery_n']} | Control n={rep['estimated_control_n']}")
    print()
    print(f"{'day':<8} {'smart_b':>8} {'labeled':>8} {'lot_est':>8} {'lot_replay':>11} {'ctrl_replay':>12}")
    for d in rep["target_days"]:
        print(
            f"{d:<8} {rep['smart_buyers_by_day'].get(d,0):>8} "
            f"{rep['labeled_by_day'].get(d,0):>8} "
            f"{rep['estimated_lottery_by_day'].get(d,0):>8} "
            f"{rep['replayed_lottery_by_day'].get(d,0):>11} "
            f"{rep['replayed_control_by_day'].get(d,0):>12}"
        )
    if rep["missing_smart_buyers_days"]:
        print(f"\n缺 smart_buyers: {rep['missing_smart_buyers_days']}")
    if rep["sparse_smart_buyers_days"]:
        print(f"sparse smart_buyers (<100): {rep['sparse_smart_buyers_days']}")
    print(f"\nLottery n 达标: {rep['lottery_n_ok']} | Control n 达标: {rep['control_n_ok']}")


def _load_sol_rpc():
    spec = importlib.util.spec_from_file_location("samp", SOL_DIR / "sol_sample_creates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def enumerate_day_creates(day: str, max_sig_pages: int = 200) -> list[dict]:
    """Enumerate pump creates whose block_time falls on UTC day MM-DD."""
    samp = _load_sol_rpc()
    lo, hi = day_unix_window(day)
    rpc = samp.RPC(samp.rpc_url(), min_interval=0.22)
    out = []
    before = None
    pages = 0
    while pages < max_sig_pages:
        params = [samp.MINT_AUTHORITY, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        pages += 1
        if not sigs:
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None or s.get("err"):
                continue
            if lo <= bt < hi:
                out.append({"sig": s["signature"], "slot": s["slot"], "block_time": bt})
        if oldest and oldest < lo:
            break
        if pages % 20 == 0:
            print(f"  [{day}] pages={pages} hits={len(out)} rpc={rpc.calls}", flush=True)
    print(f"  [{day}] enumerated {len(out)} creates ({pages} pages, rpc={rpc.calls})")
    return out


def collect_day(day: str, n_sample: int, seen: set[str], sample_path: Path) -> int:
    """Sample creates for one day → append creates_sample.jsonl."""
    samp = _load_sol_rpc()
    creates = enumerate_day_creates(day)
    if not creates:
        print(f"  [{day}] WARNING: no creates found")
        return 0
    random.shuffle(creates)
    sample = creates[: min(n_sample, len(creates))]
    rpc = samp.RPC(samp.rpc_url(), min_interval=0.22)
    n_ok = 0
    with open(sample_path, "a") as f:
        for i, c in enumerate(sample):
            try:
                info = samp.mint_from_sig(rpc, c["sig"])
            except Exception:
                info = None
            if not info or info["mint"] in seen:
                continue
            seen.add(info["mint"])
            row = {
                "mint": info["mint"],
                "creator": info["creator"],
                "sig": c["sig"],
                "slot": c["slot"],
                "block_time": c["block_time"],
                "target_day": day,
            }
            f.write(json.dumps(row) + "\n")
            n_ok += 1
            if (i + 1) % 50 == 0:
                f.flush()
                print(f"  [{day}] resolve {i+1}/{len(sample)} ok={n_ok} rpc={rpc.calls}", flush=True)
    print(f"  [{day}] wrote {n_ok} new creates")
    return n_ok


def run_subprocess(cmd: list[str], desc: str) -> None:
    print(f"\n>>> {desc}")
    print("    ", " ".join(cmd))
    subprocess.run(cmd, check=True)


def collect_missing(
    days: list[str] | None = None,
    n_per_day: int = 600,
    skip_replay: bool = False,
) -> None:
    days = days or [d for d in PHASE1_NEW if smart_buyers_by_day().get(d, 0) == 0]
    if not days:
        print("[collect] 所有 Phase 1 新日已有 smart_buyers，跳过采集")
        return

    sample_path = DIAG / "creates_sample.jsonl"
    seen = {r["mint"] for r in load_jsonl(sample_path)}
    seen |= {r["mint"] for r in load_jsonl(DIAG / "creates_labeled.jsonl")}

    print(f"[collect] 目标日: {days} | ~{n_per_day}/day")
    for day in days:
        collect_day(day, n_per_day, seen, sample_path)

    run_subprocess([sys.executable, str(SOL_DIR / "sol_label_features.py")], "label new creates")
    run_subprocess([sys.executable, str(SOL_DIR / "sol_early_path.py")], "earlypath new creates")

    days_csv = ",".join(days)
    skip_mints: set[str] = set()
    for f in glob.glob(str(DIAG / "smart_buyers*.jsonl")):
        for r in load_jsonl(Path(f)):
            if r.get("mint"):
                skip_mints.add(r["mint"])
    skip_path = OUT_DIR / "_pump_lottery_v2_expand_skip_mints.jsonl"
    with open(skip_path, "w") as sf:
        for m in sorted(skip_mints):
            sf.write(json.dumps({"mint": m}) + "\n")

    sb_cmd = [
        sys.executable,
        str(SOL_DIR / "sol_smart_buyers.py"),
        "--days",
        days_csv,
        "--in",
        str(DIAG / "creates_labeled.jsonl"),
        "--out",
        str(SMART_BUYERS_EXPAND),
        "--append",
        "--skip-file",
        str(skip_path),
    ]
    run_subprocess(sb_cmd, f"smart_buyers day-mode {days_csv}")

    rep = status_report()
    print_status(rep)
    (OUT_DIR / "_pump_lottery_v2_expand_status.json").write_text(
        json.dumps({"collected_at": datetime.now(timezone.utc).isoformat(), **rep}, indent=2) + "\n"
    )


def run_replay(max_replay: int = 2000) -> None:
    cmd = [sys.executable, str(OUT_DIR / "_pump_lottery_v2_frozen_audit.py"), "--max-replay", str(max_replay)]
    if CGROUP.exists():
        run_subprocess(["bash", str(CGROUP), *cmd], "frozen audit replay (cgroup)")
    else:
        run_subprocess(cmd, "frozen audit replay")


def run_capital_compare(n_sim: int = 10_000) -> None:
    run_subprocess(
        [sys.executable, str(OUT_DIR / "_pump_lottery_v2_frozen_capital_compare.py"), "--n-sim", str(n_sim)],
        "capital compare (daily block bootstrap)",
    )


def p300_days_lottery(audit_path: Path = AUDIT_OUT) -> dict:
    by_day: dict[str, list[float]] = defaultdict(list)
    if not audit_path.exists():
        return {"days_with_p300": 0, "by_day": {}, "max_day_share": 1.0}
    for r in json.loads(audit_path.read_text()).get("results", []):
        if r.get("cohort") != "lottery_v2":
            continue
        rr = r.get("real_return_10U")
        if rr is None:
            continue
        d = r.get("day") or "unknown"
        if rr >= P300_TH:
            by_day[d].append(rr)
    counts = {d: len(v) for d, v in by_day.items()}
    total = sum(counts.values())
    max_share = max(counts.values()) / total if total else 1.0
    return {
        "days_with_p300": len(counts),
        "p300_by_day": counts,
        "total_p300_coins": total,
        "max_day_share": max_share,
        "max_day": max(counts, key=counts.get) if counts else None,
    }


def evaluate_gate() -> dict:
    rep = status_report()
    p300 = p300_days_lottery()

    cap = {}
    if CAPITAL_OUT.exists():
        cap = json.loads(CAPITAL_OUT.read_text())
    rows = cap.get("rows", [])
    summary = cap.get("summary_baseline_daily", {})

    def row(group, stress, plabel):
        return next(
            (r for r in rows if r["group"] == group and r["stress"] == stress and r["position_label"] == plabel),
            None,
        )

    w05 = summary.get("0.05pct", {})
    lot_rm1 = row("lottery_v2", "remove_top1", "0.05pct")
    ctrl_rm1 = row("matched_control", "remove_top1", "0.05pct")

    gates = {
        "G1_n1000": rep["lottery_n_ok"] and rep["control_n_ok"],
        "G2_median_005": bool(w05.get("lottery_wins_median")),
        "G3_p5_005": bool(w05.get("lottery_wins_p5")),
        "G4_drop_top1_005": (
            lot_rm1 and ctrl_rm1
            and lot_rm1.get("median") is not None
            and ctrl_rm1.get("median") is not None
            and lot_rm1["median"] > ctrl_rm1["median"]
        ),
        "G5_p300_days_ge3": p300["days_with_p300"] >= 3,
        "G6_p300_not_single_day": p300["max_day_share"] <= MEGA_DAY_SHARE_MAX,
    }

    phase1_pass = all(gates.values())
    out = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "phase1_14d",
        "status": rep,
        "p300_diversity": p300,
        "capital_summary_005": w05,
        "remove_top1_005": {
            "lottery_median": lot_rm1.get("median") if lot_rm1 else None,
            "control_median": ctrl_rm1.get("median") if ctrl_rm1 else None,
        },
        "gates": gates,
        "phase1_pass": phase1_pass,
        "phase2_recommendation": "EXPAND_30D" if phase1_pass else "STOP_NO_EXPAND",
        "notes": [],
    }

    if not gates["G1_n1000"]:
        out["notes"].append(f"n 未达标: lottery={rep['estimated_lottery_n']} control={rep['estimated_control_n']}")
    if not gates["G5_p300_days_ge3"]:
        out["notes"].append(f"+300% 仅 {p300['days_with_p300']} 天")
    if not gates["G6_p300_not_single_day"]:
        out["notes"].append(f"+300% 过于集中: max_day={p300.get('max_day')} share={p300['max_day_share']:.1%}")

    GATE_OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n=== Phase 1 Gate ===")
    for k, v in gates.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    print(f"\nPhase 1: {'PASS → 可扩 30 天' if phase1_pass else 'FAIL → 不扩 30 天'}")
    print(f"-> {GATE_OUT}")
    return out


def analyze() -> None:
    run_capital_compare()
    evaluate_gate()


def main():
    ap = argparse.ArgumentParser(description="Lottery v2 phased data expansion")
    ap.add_argument("--status", action="store_true", help="inventory only")
    ap.add_argument("--collect", action="store_true", help="collect missing days")
    ap.add_argument("--replay", action="store_true", help="run frozen audit replay")
    ap.add_argument("--analyze", action="store_true", help="capital compare + gate")
    ap.add_argument("--gate", action="store_true", help="gate only")
    ap.add_argument("--all", action="store_true", help="collect + replay + analyze")
    ap.add_argument("--days", type=str, default="", help="override target days CSV")
    ap.add_argument("--n-per-day", type=int, default=600, help="creates to sample per missing day")
    ap.add_argument("--max-replay", type=int, default=2000)
    ap.add_argument("--n-sim", type=int, default=10_000)
    args = ap.parse_args()

    if not any([args.status, args.collect, args.replay, args.analyze, args.gate, args.all]):
        args.status = True

    target_days = args.days.split(",") if args.days else PHASE1_DAYS

    if args.status or args.all:
        rep = status_report(target_days)
        print_status(rep)
        (OUT_DIR / "_pump_lottery_v2_expand_status.json").write_text(
            json.dumps(rep, indent=2) + "\n", encoding="utf-8"
        )

    if args.collect or args.all:
        collect_missing(
            days=[d for d in PHASE1_NEW if smart_buyers_by_day().get(d, 0) == 0] if not args.days else target_days,
            n_per_day=args.n_per_day,
        )

    if args.replay or args.all:
        run_replay(max_replay=args.max_replay)

    if args.analyze or args.all:
        run_capital_compare(n_sim=args.n_sim)
        evaluate_gate()
    elif args.gate:
        evaluate_gate()


if __name__ == "__main__":
    main()
