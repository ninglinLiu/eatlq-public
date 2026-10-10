#!/usr/bin/env python3
"""Sprint084 full expansion run: A (paper-shadow cron) + B (Quoter supplement) + D (accumulate).

Research-only. No training, live trading, or paper-forward.
Stops when REVIEW_CANDIDATE > 0 or max_runtime_sec.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sprint084_pool_entry_quality_regime_review import TOP10_POOL_SHARE_RISK, as_float  # noqa: E402
from sprint084_pool_entry_quality_economic_label_backfill import (  # noqa: E402
    count_paper_quoter_pools,
    load_prep_rows,
)

EXPANSION_DIR = REPO / "mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1"
PREP_EXPANDED = EXPANSION_DIR / "prep_v3_plus_expansion.csv"
PAPER_LOG = REPO / "logs/sprint084_expansion_paper_shadow.csv"
TARGET_REGIME = ("by_price_velocity_5m", "[-0.3,0)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084 A+B+D full run until complete")
    p.add_argument("--poll-interval-sec", type=int, default=300)
    p.add_argument("--max-runtime-sec", type=int, default=7200)
    p.add_argument("--pipeline-every-polls", type=int, default=2)
    p.add_argument("--max-polls", type=int, default=0, help="0 = unlimited within runtime")
    p.add_argument("--min-paper-quoter-pools", type=int, default=25)
    p.add_argument("--out-dir", default="mvp/audits/sprint084_pool_entry_quality/expansion_full_run_v1")
    return p.parse_args()


def run_cmd(cmd: list[str], env: dict | None = None, timeout: int = 600) -> dict[str, Any]:
    e = os.environ.copy()
    if env:
        e.update(env)
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(REPO),
            env=e,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": cp.returncode == 0,
            "code": cp.returncode,
            "stdout": (cp.stdout or "")[-800:],
            "stderr": (cp.stderr or "")[-400:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "error": "timeout"}
    except Exception as ex:
        return {"ok": False, "code": -1, "error": str(ex)}


def paper_shadow_once() -> dict[str, Any]:
    return run_cmd(
        [sys.executable, str(SCRIPTS / "sprint055_fee100_paper_shadow.py"), "--once"],
        env={
            "FEE_FILTER_ONLY": "500",
            "TRACK_SUFFIX": "_084exp",
            "LOG_CSV_OVERRIDE": "sprint084_expansion_paper_shadow.csv",
        },
        timeout=300,
    )


def merge_paper_log_into_prep(prep_path: Path, log_path: Path) -> int:
    """Append paper-shadow rows with exit_30m_ok as pool-entry rows."""
    if not log_path.is_file():
        return 0
    existing = load_prep_rows(prep_path) if prep_path.is_file() else []
    keys = {(r["pool"].lower(), r.get("entry_time", "")) for r in existing}
    added = 0
    with log_path.open(newline="") as f:
        for raw in csv.DictReader(f):
            if str(raw.get("exit_30m_ok", "")).lower() != "true":
                continue
            pool = (raw.get("pool") or "").lower()
            st = raw.get("signal_time") or ""
            if not pool or not st:
                continue
            if (pool, st) in keys:
                continue
            t30 = raw.get("net_return_30m")
            if t30 in (None, ""):
                continue
            row = {
                "entry_id": f"paper_exp_{pool[:10]}_{st}",
                "source": "paper_shadow_onset",
                "entry_time": st,
                "pool": pool,
                "token_other": (raw.get("token_other") or "").lower(),
                "fee_bps": raw.get("fee_bps"),
                "fee_tier": raw.get("fee_bps"),
                "price_velocity_5m": raw.get("price_velocity_5m"),
                "buy_pressure_ratio": raw.get("buy_pressure_ratio"),
                "activity_heat": raw.get("activity_heat"),
                "pool_age_sec": raw.get("pool_age_sec"),
                "T_POOL_FWD_30M": t30,
                "T_POOL_FWD_30M_source": "paper_shadow_historical_backtest",
                "T_ONSET_PASS": "true",
                "wallet_diagnostic_joined": "false",
                "expansion_from_paper_log": "true",
            }
            existing.append(row)
            keys.add((pool, st))
            added += 1
    if not existing:
        return 0
    fieldnames: list[str] = []
    seen: set[str] = set()
    for r in existing:
        for k in r:
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    prep_path.parent.mkdir(parents=True, exist_ok=True)
    with prep_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing)
    return added


def run_quoter_supplement() -> dict[str, Any]:
    return run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "sprint084_paper_quoter_expansion.py"),
            "--top-pools",
            "25",
            "--prep-dataset",
            str(PREP_EXPANDED.relative_to(REPO)),
            "--supplement-max-pools",
            "40",
            "--supplement-ch-rows-per-pool",
            "6",
        ],
        timeout=900,
    )


def run_backfill(out_tag: str) -> Path:
    out = REPO / f"mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_{out_tag}"
    run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "sprint084_pool_entry_quality_economic_label_backfill.py"),
            "--prep-dataset-expansion",
            str(PREP_EXPANDED.relative_to(REPO)),
            "--out-dir",
            str(out.relative_to(REPO)),
            "--min-paper-quoter-pools",
            "25",
            "--max-rpc-rows",
            "400",
        ],
        timeout=900,
    )
    return out


def run_regime_review(backfill_dir: Path, tag: str) -> Path:
    out = REPO / f"mvp/audits/sprint084_pool_entry_quality/regime_review_{tag}"
    run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "sprint084_pool_entry_quality_regime_review.py"),
            "--dataset",
            str((backfill_dir / "pool_entry_quality_candidate_dataset_backfilled.csv").relative_to(REPO)),
            "--prep-flags",
            "mvp/audits/sprint084_pool_entry_quality/prep_v3/pool_entry_quality_prep_flags.json",
            "--out-dir",
            str(out.relative_to(REPO)),
            "--review-variant",
            tag,
        ],
        timeout=120,
    )
    return out


def check_target_regime(regime_dir: Path) -> dict[str, Any]:
    path = regime_dir / "positive_regime_candidates.csv"
    if not path.is_file():
        return {"found": False}
    for r in csv.DictReader(path.open()):
        if r.get("regime_kind") == TARGET_REGIME[0] and r.get("bucket") == TARGET_REGIME[1]:
            t10 = as_float(r.get("top10_pool_share"))
            fails = []
            if int(float(r.get("unique_pools") or 0)) < 20:
                fails.append("LOW_POOL_DIVERSITY")
            if t10 is not None and t10 >= TOP10_POOL_SHARE_RISK:
                fails.append("TOP10_CONCENTRATION")
            if as_float(r.get("T30_proxy_share") or 0) > 0.5:
                fails.append("PROXY_T30")
            if as_float(r.get("source_top_share") or 0) > 0.7:
                fails.append("SINGLE_SOURCE")
            return {
                "found": True,
                "top10_pool_share": t10,
                "gate_failures": fails,
                "is_review_candidate": len(fails) == 0,
                "n": r.get("n"),
            }
    return {"found": False}


def count_paper_log() -> dict[str, int]:
    if not PAPER_LOG.is_file():
        return {"rows": 0, "mature_30m": 0, "pools": 0}
    rows = list(csv.DictReader(PAPER_LOG.open()))
    mature = [r for r in rows if str(r.get("exit_30m_ok", "")).lower() == "true"]
    pools = {r.get("pool", "").lower() for r in rows if r.get("pool")}
    return {"rows": len(rows), "mature_30m": len(mature), "pools": len(pools)}


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    deadline = t0 + args.max_runtime_sec
    poll_n = 0
    iter_n = 0
    history: list[dict] = []
    complete = False
    reason = ""

    if not PREP_EXPANDED.is_file():
        run_cmd(
            [
                sys.executable,
                str(SCRIPTS / "sprint084_paper_quoter_expansion.py"),
                "--prep-dataset",
                "mvp/audits/sprint084_pool_entry_quality/prep_v3/pool_entry_quality_candidate_dataset.csv",
            ],
            timeout=600,
        )

    while time.time() < deadline:
        poll_n += 1
        if args.max_polls and poll_n > args.max_polls:
            reason = "max_polls"
            break

        ps = paper_shadow_once()
        log_stats = count_paper_log()
        merged = 0
        if poll_n % args.pipeline_every_polls == 0:
            iter_n += 1
            merged = merge_paper_log_into_prep(PREP_EXPANDED, PAPER_LOG)
            run_quoter_supplement()
            bf = run_backfill(f"fullrun_{iter_n}")
            rv = run_regime_review(bf, f"fullrun_{iter_n}")
            flags_path = bf / "economic_label_backfill_flags.json"
            regime_flags_path = rv / "pool_entry_quality_regime_review_flags.json"
            bf_flags = json.loads(flags_path.read_text()) if flags_path.is_file() else {}
            rv_flags = json.loads(regime_flags_path.read_text()) if regime_flags_path.is_file() else {}
            target = check_target_regime(rv)
            rows = load_prep_rows(bf / "pool_entry_quality_candidate_dataset_backfilled.csv")
            pq = count_paper_quoter_pools(rows)
            snap = {
                "poll": poll_n,
                "iteration": iter_n,
                "utc": datetime.now(timezone.utc).isoformat(),
                "paper_shadow_ok": ps.get("ok"),
                "paper_log": log_stats,
                "prep_rows_merged": merged,
                "paper_quoter_pools": pq,
                "review_candidates": rv_flags.get("review_candidates_count", 0),
                "target_regime": target,
                "global_top10": (rv_flags.get("global_stats") or {}).get("top10_pool_share"),
            }
            history.append(snap)
            (out_dir / "run_history.jsonl").open("a").write(json.dumps(snap) + "\n")

            if rv_flags.get("review_candidates_count", 0) > 0:
                complete = True
                reason = "REVIEW_CANDIDATE"
                break
            if target.get("is_review_candidate"):
                complete = True
                reason = "TARGET_REGIME_REVIEW_CANDIDATE"
                break

        if poll_n < args.max_polls or not args.max_polls:
            sleep_sec = min(args.poll_interval_sec, max(1, int(deadline - time.time())))
            if sleep_sec <= 0:
                reason = "max_runtime_sec"
                break
            time.sleep(sleep_sec)

    if not reason:
        reason = "max_runtime_sec"

    final_bf = REPO / "mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v3"
    final_rv = REPO / "mvp/audits/sprint084_pool_entry_quality/regime_review_v4"
    if history:
        last = history[-1]
        merge_paper_log_into_prep(PREP_EXPANDED, PAPER_LOG)
        run_quoter_supplement()
        final_bf = run_backfill("v5")
        final_rv = run_regime_review(final_bf, "v5")

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "EXPANSION_FULL_RUN_DONE": True,
        "complete": complete,
        "stop_reason": reason,
        "polls": poll_n,
        "pipeline_iterations": iter_n,
        "runtime_sec": round(time.time() - t0, 1),
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": "false",
        "MODEL_PROMOTION_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "paper_log": count_paper_log(),
        "final_backfill": str(final_bf.relative_to(REPO)),
        "final_regime_review": str(final_rv.relative_to(REPO)),
        "target_regime": check_target_regime(final_rv),
        "history_tail": history[-5:],
        "NEXT_STEP": (
            "POOL_ENTRY_QUALITY_PAPER_FORWARD_REVIEW_CANDIDATE"
            if complete
            else "continue_A_cron_until_target_regime_top10_lt_25pct"
        ),
    }
    rvf = final_rv / "pool_entry_quality_regime_review_flags.json"
    if rvf.is_file():
        flags["review_candidates_count"] = json.loads(rvf.read_text()).get(
            "review_candidates_count", 0
        )
        flags["PAPER_FORWARD_ALLOWED"] = json.loads(rvf.read_text()).get(
            "PAPER_FORWARD_ALLOWED", "false"
        )

    (out_dir / "expansion_full_run_flags.json").write_text(json.dumps(flags, indent=2) + "\n")
    print(json.dumps(flags, indent=2))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
