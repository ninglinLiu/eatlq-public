#!/usr/bin/env python3
"""End-to-end Base V3 Actor-Flow OOS Harness."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.actor_resolution import run_resolution
from src.arms import run_arms_oos
from src.common import OUT, load_freeze, save_json, utcnow
from src.finalize import write_final
from src.identity_scores import run_identity_pipeline
from src.labels_exec import label_events
from src.resolution_gate import run_gate
from src.scorecard import build_scorecard


def main() -> int:
    freeze = load_freeze()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[run_all] phase={freeze['phase']} start={utcnow()}", flush=True)

    # ---- Phase 1: resolution + gate ----
    try:
        # Prefer cache resume (skip multi-million-row CH reload when available)
        resolved = run_resolution(
            rpc_sample=min(40, int(freeze["actor_resolution_gate"]["golden_sample_size"])),
            resume=True,
        )
    except Exception as e:
        traceback.print_exc()
        gate = {
            "verdict": "ACTOR_RESOLUTION_INSUFFICIENT",
            "passed": False,
            "error": str(e)[:500],
            "generated_at": utcnow(),
        }
        save_json(OUT / "phase1" / "RESOLUTION_GATE.json", gate)
        write_final(gate, None, {})
        return 2

    gate = run_gate(resolved)
    if not gate.get("passed"):
        print("[run_all] FOUNDATION GATE FAILED — stopping before arm scoring", flush=True)
        write_final(gate, None, {
            "information_verdict": "ACTOR_RESOLUTION_INSUFFICIENT",
            "copy_verdict": "ACTOR_RESOLUTION_INSUFFICIENT",
            "best_arm": None,
        })
        return 1

    # ---- Phase 2: PIT panel + identity ----
    panel = run_identity_pipeline(resolved)

    # ---- Phase 3: labels + arms ----
    labeled = label_events(panel, resolved)
    oof = run_arms_oos(labeled)

    # ---- Phase 4: dual verdict ----
    scorecard, summary = build_scorecard(oof, freeze)
    final = write_final(gate, scorecard, summary)
    print("[run_all] FINAL", flush=True)
    print(f"  information_verdict={final.get('information_verdict')}", flush=True)
    print(f"  copy_verdict={final.get('copy_verdict')}", flush=True)
    print(f"  best_arm={final.get('best_arm')}", flush=True)
    print(f"  deploy_allowed={final.get('deploy_allowed')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
