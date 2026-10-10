#!/usr/bin/env python3
"""V4 one-shot orthogonal hypothesis round after V3 failure."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.v3.hypothesis_registry import validate_registry  # noqa: E402
from src.v3.oof_eval import build_scorecard  # noqa: E402
from src.v4.confirmation import run_v4_confirmation  # noqa: E402
from src.v4.finalize import finalize_v4  # noqa: E402
from src.v4.orthogonal_routes import fit_v4_routes  # noqa: E402
from src.v4.reseal import build_v4_frame, load_v4_freeze, load_v4_registry  # noqa: E402

OUT = ROOT / "out_v4"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "confirmation").mkdir(parents=True, exist_ok=True)
    (OUT / "predictions").mkdir(parents=True, exist_ok=True)
    (OUT / "panel").mkdir(parents=True, exist_ok=True)

    freeze = load_v4_freeze()
    registry = load_v4_registry()
    validate_registry(registry)
    assert freeze["expand_search_after_failure_forbidden"] is True
    assert freeze["one_shot_round"] is True
    assert freeze["deploy_allowed"] is False
    banned = set(freeze["banned_reuse_from_v3"]["hypothesis_ids"])
    for h in registry["hypotheses"]:
        assert h["id"] not in banned, h["id"]
        assert int(h["max_searches"]) == 1

    print("[v4] === RESEAL FRAME ===", flush=True)
    feat = build_v4_frame(freeze)
    feat.to_parquet(OUT / "panel" / "v4_resealed_frame.parquet", index=False)
    split_counts = feat["split_role"].value_counts().to_dict()
    burned_n = int(feat.get("v3_confirmation_burned", False).sum()) if "v3_confirmation_burned" in feat.columns else 0
    panel_meta = {
        "n_rows": int(len(feat)),
        "n_formal": int(feat["formal_bucket"].sum()) if "formal_bucket" in feat.columns else None,
        "split_counts": split_counts,
        "split_salt": freeze["splits"]["salt"],
        "v3_confirmation_ids_flagged": burned_n,
        "n_burned_moved_to_dev": int(getattr(feat, "attrs", {}).get("n_burned_moved_to_dev", 0)),
        "parent_phase": freeze["parent_phase"],
        "source_feature_matrix": freeze["source_feature_matrix"],
    }
    # attrs may not persist; recompute burn moves
    v3_path = ROOT / freeze["splits"]["v3_panel_path"]
    if v3_path.exists():
        import pandas as pd

        v3 = pd.read_parquet(v3_path, columns=["signal_id", "split_role"])
        burned_ids = set(v3[v3["split_role"].isin(["INTERNAL_HOLDOUT", "UNTOUCHED"])]["signal_id"].astype(str))
        moved = int(
            (
                feat["signal_id"].astype(str).isin(burned_ids)
                & (feat["split_role"] == "DEV")
                & (feat["split_role_v3"].isin(["INTERNAL_HOLDOUT", "UNTOUCHED"]))
            ).sum()
        ) if "split_role_v3" in feat.columns else 0
        panel_meta["n_burned_moved_to_dev"] = moved
    (OUT / "panel" / "PANEL_META.json").write_text(json.dumps(panel_meta, indent=2) + "\n")
    print(f"[v4.reseal] rows={len(feat)} splits={split_counts} burned_flagged={burned_n}", flush=True)

    print("[v4] === ORTHOGONAL OOF ROUTES (max_searches=1 each) ===", flush=True)
    routes = fit_v4_routes(feat, freeze)
    oof_map = {}
    for name, (oof, meta) in routes.items():
        oof_map[name] = oof
        if len(oof):
            oof.to_parquet(OUT / "predictions" / f"oof_{name}.parquet", index=False)
        (OUT / "predictions" / f"meta_{name}.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
        print(f"[v4.oof] {name}: n_oof={meta.get('n_oof')} selected={meta.get('n_selected')}", flush=True)

    print("[v4] === SCORECARD ===", flush=True)
    # build_scorecard expects freeze with capital/execution like v3
    scorecard, details, curve, monthly = build_scorecard(oof_map, feat, freeze)

    best = details.get("best_oof_policy") or "H_HARD_FATAL_VETO"
    cand = scorecard[scorecard["verdict"] != "BASELINE"].copy()
    if len(cand):
        prefer = cand[~cand["verdict"].isin(["LOTTERY_DEPENDENT_REJECT", "EXECUTION_RISK_DOMINATES"])]
        use = prefer if len(prefer) else cand
        best = str(use.sort_values("haircut_pnl_weth", ascending=False).iloc[0]["policy"])

    print(f"[v4] === CONFIRMATION best={best} (one-shot) ===", flush=True)
    confirmation = run_v4_confirmation(feat, freeze, best, baselines=details.get("baselines"))
    (OUT / "confirmation" / "CONFIRMATION.json").write_text(json.dumps(confirmation, indent=2, default=str) + "\n")

    print("[v4] === FINALIZE / HONEST CLOSEOUT ===", flush=True)
    final = finalize_v4(
        freeze=freeze,
        scorecard=scorecard,
        route_details=details,
        confirmation=confirmation,
        registry=registry,
        panel_meta=panel_meta,
        curve=curve,
        monthly=monthly,
    )
    print(
        json.dumps(
            {
                "verdict": final["verdict"],
                "best_oof_policy": final["best_oof_policy"],
                "one_shot_round_complete": True,
                "deploy_allowed": False,
                "expand_search_after_failure": False,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
