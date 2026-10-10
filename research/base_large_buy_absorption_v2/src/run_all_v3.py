"""Run LBA v3 research layer (Tracks A–D). Does not mutate v2 freeze/shadow."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/root/base_large_buy_absorption_v2")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> dict:
    import pandas as pd

    from src.capacity_grid import run_capacity_grid
    from src.common import OUT, save_json, utcnow
    from src.finalize_v3 import finalize_v3
    from src.matched_incremental import run_matched_incremental
    from src.ranker_gbm import run_ranker_gbm
    from src.v3_common import V3_OUT, ensure_v3_out, load_oof_arms

    ensure_v3_out()
    print(f"[v3] start {utcnow()} — research layer on frozen v2", flush=True)

    oof = load_oof_arms()
    print("[v3] Track A capacity grid …", flush=True)
    capacity = run_capacity_grid(oof)

    print("[v3] Track B matched incremental …", flush=True)
    matched = run_matched_incremental()

    print("[v3] Track C HistGBM ranker …", flush=True)
    ranking = run_ranker_gbm(oof["LARGE_BUY_ABSORPTION"])

    print("[v3] Track D finalize …", flush=True)
    payload = finalize_v3(capacity, matched, ranking)

    save_json(V3_OUT / "RUN_META.json", {
        "finished_at": utcnow(),
        "propose_v3_shadow_upgrade": payload.get("propose_v3_shadow_upgrade"),
        "per_hypothesis": payload.get("per_hypothesis"),
        "live": "NO_GO",
        "v2_shadow_mutated": False,
    })
    print("[v3] DONE", flush=True)
    for k, v in (payload.get("per_hypothesis") or {}).items():
        print(f"  {k}: {v}", flush=True)
    print(f"[v3] propose_upgrade={payload.get('propose_v3_shadow_upgrade')}", flush=True)
    return payload


if __name__ == "__main__":
    main()
