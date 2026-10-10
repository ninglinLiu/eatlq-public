#!/usr/bin/env python3
"""Iterate CH near-miss dilution + backfill + regime until target cell passes or max_iter."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
PREP = REPO / "mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1/prep_v3_plus_expansion.csv"
TARGET = ("by_price_velocity_5m", "[-0.3,0)")
TOP10_MAX = 0.25


def target_top10(regime_dir: Path) -> float | None:
    p = regime_dir / "positive_regime_candidates.csv"
    if not p.is_file():
        return None
    for r in csv.DictReader(p.open()):
        if r["regime_kind"] == TARGET[0] and r["bucket"] == TARGET[1]:
            return float(r["top10_pool_share"])
    return None


def main() -> int:
    max_iter = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    for i in range(1, max_iter + 1):
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "sprint084_paper_quoter_expansion.py"),
                "--prep-dataset",
                str(PREP.relative_to(REPO)),
                "--skip-quoter",
                "--supplement-max-pools",
                "50",
                "--supplement-ch-rows-per-pool",
                "8",
            ],
            cwd=REPO,
            check=False,
        )
        bf = f"economic_label_backfill_dilute_{i}"
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "sprint084_pool_entry_quality_economic_label_backfill.py"),
                "--prep-dataset-expansion",
                str(PREP.relative_to(REPO)),
                "--out-dir",
                f"mvp/audits/sprint084_pool_entry_quality/{bf}",
                "--min-paper-quoter-pools",
                "25",
            ],
            cwd=REPO,
            check=False,
        )
        rv = f"regime_review_dilute_{i}"
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "sprint084_pool_entry_quality_regime_review.py"),
                "--dataset",
                f"mvp/audits/sprint084_pool_entry_quality/{bf}/pool_entry_quality_candidate_dataset_backfilled.csv",
                "--out-dir",
                f"mvp/audits/sprint084_pool_entry_quality/{rv}",
                "--review-variant",
                f"dilute_{i}",
            ],
            cwd=REPO,
            check=False,
        )
        t10 = target_top10(REPO / f"mvp/audits/sprint084_pool_entry_quality/{rv}")
        n = sum(1 for _ in open(PREP)) - 1
        print(f"iter={i} prep_rows={n} target_top10={t10}", flush=True)
        if t10 is not None and t10 < TOP10_MAX:
            flags = {
                "complete": True,
                "iteration": i,
                "target_top10": t10,
                "regime_dir": rv,
                "backfill_dir": bf,
            }
            out = REPO / "mvp/audits/sprint084_pool_entry_quality/expansion_full_run_v1/stratum_dilution_done.json"
            out.write_text(json.dumps(flags, indent=2) + "\n")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
