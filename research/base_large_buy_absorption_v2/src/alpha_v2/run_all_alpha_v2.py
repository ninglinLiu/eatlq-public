"""Alpha Hunt v2 driver: A2 xpool features → N clone family → gate → bakeoff → finalize."""
from __future__ import annotations

import argparse

import pandas as pd

from ..common import OUT, ROOT, save_json
from ..alpha.cost_attach import attach_pnl_net
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out, stamp_status, utcnow
from .pit_actor_xpool import attach_actor_xpool_features, pull_xpool_swaps
from .clone_family import attach_clone_family_features
from .leakage_gate_v2 import run_leakage_gate_v2
from .bakeoff_v2 import run_alpha_bakeoff_v2
from .finalize_alpha_v2 import finalize_alpha_v2


def main(*, skip_gate_raise: bool = False) -> dict:
    ensure_alpha_v2_out()
    stamp_status("IN_PROGRESS", started_at=utcnow())

    # Confirm v1 seal untouched
    v1 = (ROOT / "configs" / "freeze_alpha_v1.json").read_text()
    assert "SEALED_BOTH_REFUTED" in v1, "v1 seal must remain SEALED_BOTH_REFUTED"

    print("[alpha_v2] loading labeled panel…", flush=True)
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].copy()  # noqa: E712
    print(f"[alpha_v2] eligible={len(elig):,}", flush=True)

    print("[alpha_v2] Leg A2 cross-pool actor features…", flush=True)
    panel = attach_actor_xpool_features(elig)

    print("[alpha_v2] Leg N clone-family features…", flush=True)
    swaps = pd.read_parquet(
        ROOT / "data" / "resolved_swaps.parquet",
        columns=["token", "pool", "ts", "price_weth", "side"],
    )
    panel = attach_clone_family_features(panel, swaps=swaps)

    print("[alpha_v2] attach pnl_net…", flush=True)
    panel = attach_pnl_net(panel)
    panel["combined_score"] = (
        pd.to_numeric(panel["actor_xpool_score"], errors="coerce").fillna(0)
        + pd.to_numeric(panel["family_score"], errors="coerce").fillna(0)
    )
    out_path = ALPHA_V2_OUT / "panel_with_alpha_features.parquet"
    panel.to_parquet(out_path, index=False)
    print(f"[alpha_v2] wrote {out_path}", flush=True)

    print("[alpha_v2] leakage gate…", flush=True)
    try:
        gate = run_leakage_gate_v2(panel)
    except RuntimeError as e:
        if skip_gate_raise:
            print(f"[alpha_v2] GATE FAILED but continuing: {e}", flush=True)
            gate = {"passed": False, "verdict": "FAIL", "error": str(e)}
        else:
            stamp_status("LEAKAGE_GATE_FAILED")
            raise

    print("[alpha_v2] bakeoff…", flush=True)
    bake = run_alpha_bakeoff_v2(panel)

    print("[alpha_v2] finalize…", flush=True)
    final = finalize_alpha_v2(bake["matched"], bake["ranker"], gate)
    save_json(ALPHA_V2_OUT / "RUN_META.json", {
        "generated_at": utcnow(),
        "n_eligible": int(len(elig)),
        "n_with_cost": int(panel["cost_known"].sum()),
        "cold_start_a2": float(panel["actor_xpool_cold_start"].mean()),
        "cold_start_family": float(panel["family_cold_start"].mean()),
        "both_refuted": final.get("both_primary_legs_refuted"),
        "propose_shadow": final.get("propose_observer_shadow"),
    })
    return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-gate-raise", action="store_true")
    args = ap.parse_args()
    main(skip_gate_raise=args.skip_gate_raise)
