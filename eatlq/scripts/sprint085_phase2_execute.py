#!/usr/bin/env python3
"""Sprint085 Phase 2 — rolling OOS bounded XGB smoke + concentration readout."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PHASE2 = REPO / "mvp/audits/sprint085_wallet_conditional_entry/phase2_rolling_oos"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint085 Phase 2 rolling OOS smoke")
    p.add_argument(
        "--dataset",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "copyability_safe_dataset_sample.csv"
        ),
    )
    p.add_argument(
        "--manifest",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "feature_manifest.json"
        ),
    )
    p.add_argument(
        "--prep-flags",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "copyability_safe_prep_flags.json"
        ),
    )
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint085_wallet_conditional_entry/phase2_rolling_oos",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "scripts/sprint083_copyability_safe_xgb_smoke_review.py",
        "--dataset",
        str(REPO / args.dataset),
        "--manifest",
        str(REPO / args.manifest),
        "--prep-flags",
        str(REPO / args.prep_flags),
        "--out-dir",
        str(out_dir),
        "--label",
        "copy_delay_120s",
        "--optional-label",
        "copy_delay_60s",
        "--run-block-number-ablation",
    ]
    print("+ " + " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=REPO).returncode

    phase1_flags_path = REPO / "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/phase1_gate_flags.json"
    phase1_pass = False
    if phase1_flags_path.exists():
        phase1_pass = json.loads(phase1_flags_path.read_text()).get("PHASE1_PASS", False)

    smoke_flags_path = out_dir / "copyability_safe_smoke_flags.json"
    if not smoke_flags_path.exists():
        smoke_flags_path = out_dir / "smart_wallet_copyability_closure_flags.json"
    if not smoke_flags_path.exists():
        smoke_flags_path = out_dir / "copyability_safe_xgb_smoke_flags.json"
    smoke_flags = json.loads(smoke_flags_path.read_text()) if smoke_flags_path.exists() else {}

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase1_pass_required": True,
        "phase1_pass": phase1_pass,
        "smoke_exit_code": rc,
        "PHASE2_SMOKE_DONE": smoke_flags_path.exists(),
        "NOT_COPYABLE_LINE_CLOSED": True,
        "MODEL_PROMOTION_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "smoke_flags_path": str(smoke_flags_path) if smoke_flags_path.exists() else None,
        "smoke_verdict": smoke_flags.get("COPYABILITY_SIGNAL_STATUS")
        or smoke_flags.get("SMOKE_VERDICT")
        or ("done" if rc == 0 else "failed"),
        "NEXT_STEP": "phase3_paper_shadow_after_human_review" if phase1_pass else "fix_phase1_gates",
        "note": (
            "Research-only bounded XGB smoke; Sprint083 established direct copy NOT_COPYABLE. "
            "Phase 2 validates wallet-conditioned incremental signal vs pool-only baseline."
        ),
    }
    (out_dir / "phase2_execute_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return rc if phase1_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
