"""Alpha Hunt v1 driver: features → leakage gate → bakeoff → finalize."""
from __future__ import annotations

import argparse

import pandas as pd

from ..common import OUT, ROOT, save_json
from .common_alpha import ALPHA_OUT, ensure_alpha_out, stamp_status, utcnow
from .cost_attach import attach_pnl_net
from .pit_actor_features import attach_actor_features, build_closed_round_trips
from .pit_genesis_features import attach_genesis_features
from .leakage_gate import run_leakage_gate
from .alpha_bakeoff import run_alpha_bakeoff
from .finalize_alpha import finalize_alpha


def main(*, skip_gate_raise: bool = False, max_rows: int | None = None) -> dict:
    ensure_alpha_out()
    stamp_status("IN_PROGRESS", started_at=utcnow())
    print("[alpha] loading labeled panel…", flush=True)
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].copy()  # noqa: E712
    if max_rows is not None and len(elig) > max_rows:
        elig = elig.sample(n=max_rows, random_state=11).sort_values("decision_time")
        print(f"[alpha] subsampled to {len(elig):,} rows for this run", flush=True)
    print(f"[alpha] eligible={len(elig):,}", flush=True)

    # Shared swaps load once
    swaps = pd.read_parquet(ROOT / "data" / "resolved_swaps.parquet")
    print(f"[alpha] swaps={len(swaps):,}", flush=True)

    print("[alpha] Family A actor features…", flush=True)
    # Build trips over the full resolved universe (all actors) so pool-buyer / cluster
    # reputation has coverage; panel actors still cold-start until they close a trip.
    trips = build_closed_round_trips(swaps, actors_filter=None)
    panel = attach_actor_features(elig, trips=trips, swaps=swaps)

    print("[alpha] Family B genesis features…", flush=True)
    panel = attach_genesis_features(panel, swaps=swaps)

    print("[alpha] attach pnl_net…", flush=True)
    panel = attach_pnl_net(panel)
    panel["combined_score"] = (
        pd.to_numeric(panel["actor_score"], errors="coerce").fillna(0)
        + pd.to_numeric(panel["genesis_score"], errors="coerce").fillna(0)
    )
    out_path = ALPHA_OUT / "panel_with_alpha_features.parquet"
    panel.to_parquet(out_path, index=False)
    print(f"[alpha] wrote {out_path}", flush=True)

    print("[alpha] leakage gate…", flush=True)
    try:
        gate = run_leakage_gate(panel)
    except RuntimeError as e:
        if skip_gate_raise:
            print(f"[alpha] GATE FAILED but continuing (debug): {e}", flush=True)
            gate = {"passed": False, "verdict": "FAIL", "error": str(e)}
        else:
            stamp_status("LEAKAGE_GATE_FAILED")
            raise

    print("[alpha] bakeoff…", flush=True)
    bake = run_alpha_bakeoff(panel)

    print("[alpha] finalize…", flush=True)
    final = finalize_alpha(bake["matched"], bake["ranker"], gate)
    save_json(ALPHA_OUT / "RUN_META.json", {
        "generated_at": utcnow(),
        "n_eligible": int(len(elig)),
        "n_with_cost": int(panel["cost_known"].sum()),
        "final_status": final.get("both_refuted"),
        "propose_shadow": final.get("propose_observer_shadow"),
    })
    return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--skip-gate-raise", action="store_true")
    args = ap.parse_args()
    main(skip_gate_raise=args.skip_gate_raise, max_rows=args.max_rows)
