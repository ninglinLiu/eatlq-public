"""End-to-end Exit Alpha v0: label → bakeoff → finalize."""
from __future__ import annotations

import argparse

import pandas as pd

from ..common import OUT, ROOT
from .common_exit import EXIT_OUT, ensure_exit_out, stamp_status, utcnow
from .exit_bakeoff import run_exit_bakeoff
from .finalize_exit import finalize_exit


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description="Exit Alpha v0 pipeline")
    ap.add_argument("--force-relabel", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="optional row cap for smoke tests")
    args = ap.parse_args(argv)

    ensure_exit_out()
    stamp_status("IN_PROGRESS", phase="EXIT_ALPHA_V0", live="NO_GO")
    print(f"[exit/run] start {utcnow()} out={EXIT_OUT}", flush=True)

    panel = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    if args.limit and args.limit > 0:
        panel = panel.head(int(args.limit)).copy()
        print(f"[exit/run] SMOKE limit={args.limit}", flush=True)

    resolved = pd.read_parquet(
        ROOT / "data" / "resolved_swaps.parquet",
        columns=["pool", "ts", "price_weth"],
    )
    bakeoff = run_exit_bakeoff(
        panel, resolved=resolved, force_relabel=bool(args.force_relabel)
    )
    final = finalize_exit(bakeoff)
    print(f"[exit/run] done status → see configs/freeze_exit_alpha_v0.json", flush=True)
    return {"bakeoff": bakeoff, "final": final}


if __name__ == "__main__":
    main()
