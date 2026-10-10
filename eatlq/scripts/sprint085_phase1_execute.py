#!/usr/bin/env python3
"""Sprint085 Phase 1 full execute — builder + copyability prep + gate review."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PHASE1 = REPO / "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset"


def run(cmd: list[str], timeout: int | None = None) -> int:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=REPO, timeout=timeout).returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint085 Phase 1 execute")
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset",
    )
    p.add_argument("--skip-builder", action="store_true", help="Reuse existing phase1 dataset_sample.csv")
    p.add_argument("--reuse-sprint083-dataset", action="store_true", help="Copy sprint083 expanded_v2 into phase1 (fast seed)")
    p.add_argument("--window-days", type=int, default=60)
    p.add_argument("--sample-limit", type=int, default=25000)
    p.add_argument("--max-rows", type=int, default=700000)
    p.add_argument("--query-timeout-sec", type=int, default=180)
    return p.parse_args()


def copy_sprint083_seed(out_dir: Path) -> None:
    src = REPO / "mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_dataset_builder_expanded_v2"
    for name in (
        "dataset_sample.csv",
        "closed_trades_sample.csv",
        "open_positions_sample.csv",
        "feature_manifest.json",
        "decision_flags.json",
        "sample_size_summary.csv",
    ):
        s, d = src / name, out_dir / name
        if s.exists():
            d.write_bytes(s.read_bytes())
    seed_flags = {
        "seeded_from": str(src),
        "seeded_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Fast seed from sprint083 expanded_v2; run without --reuse-sprint083-dataset for fresh CH rebuild",
    }
    (out_dir / "seed_provenance.json").write_text(json.dumps(seed_flags, indent=2) + "\n")


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    steps: list[dict] = []

    if args.reuse_sprint083_dataset:
        copy_sprint083_seed(out_dir)
        steps.append({"step": "seed_from_sprint083", "exit_code": 0})
    elif not args.skip_builder:
        rc = run(
            [
                sys.executable,
                "scripts/v3_smart_wallet_roundtrip_dataset_builder.py",
                "--window-days",
                str(args.window_days),
                "--sample-limit",
                str(args.sample_limit),
                "--max-rows",
                str(args.max_rows),
                "--date-shard-days",
                "1",
                "--max-pools",
                "150",
                "--per-pool-max-rows",
                "30000",
                "--balanced-pool-sample",
                "--pool-metadata-source",
                "union",
                "--pool-feature-source",
                "sidecar",
                "--out-dir",
                str(args.out_dir),
                "--query-timeout-sec",
                str(args.query_timeout_sec),
            ],
            timeout=3600,
        )
        steps.append({"step": "v3_smart_wallet_roundtrip_dataset_builder", "exit_code": rc})
        if rc != 0:
            return rc

    dataset = out_dir / "dataset_sample.csv"
    if not dataset.exists():
        print(f"ERROR: missing {dataset}", file=sys.stderr)
        return 1

    rc = run(
        [
            sys.executable,
            "scripts/sprint083_copyability_safe_training_prep.py",
            "--dataset",
            str(out_dir / "dataset_sample.csv"),
            "--out-dir",
            str(out_dir),
        ]
    )
    steps.append({"step": "copyability_safe_training_prep", "exit_code": rc})

    rc_gate = run(
        [
            sys.executable,
            "scripts/sprint085_phase1_gate_review.py",
            "--dataset",
            str(out_dir / "copyability_safe_dataset_sample.csv"),
            "--prep-flags",
            str(out_dir / "copyability_safe_prep_flags.json"),
            "--builder-flags",
            str(out_dir / "decision_flags.json"),
            "--out-dir",
            str(out_dir),
        ]
    )
    steps.append({"step": "phase1_gate_review", "exit_code": rc_gate})

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - started, 1),
        "steps": steps,
        "phase1_pass": rc_gate == 0,
        "out_dir": str(args.out_dir),
    }
    (out_dir / "phase1_execute_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return rc_gate


if __name__ == "__main__":
    raise SystemExit(main())
