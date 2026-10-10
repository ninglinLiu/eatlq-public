#!/usr/bin/env python3
"""GREENFIELD V3 Narrow-Market Rescue — full pipeline entrypoint."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.v3.build_labels import build_labels  # noqa: E402
from src.v3.build_panel import build_panel, load_freeze  # noqa: E402
from src.v3.confirmation import run_confirmation  # noqa: E402
from src.v3.dual_head_model import fit_hypothesis_routes  # noqa: E402
from src.v3.features import build_features  # noqa: E402
from src.v3.finalize import finalize  # noqa: E402
from src.v3.hypothesis_registry import load_registry  # noqa: E402
from src.v3.oof_eval import build_scorecard  # noqa: E402

OUT = ROOT / "out_v3"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "confirmation").mkdir(parents=True, exist_ok=True)
    (OUT / "predictions").mkdir(parents=True, exist_ok=True)

    freeze = load_freeze()
    registry = load_registry()
    assert freeze.get("venue_rejection_forbidden") is True
    assert freeze.get("deploy_allowed") is False
    assert freeze.get("stop_research_on_phase0_gate_fail") is False

    print("[v3] === PANEL ===", flush=True)
    panel = build_panel(freeze)
    panel_meta = json.loads((OUT / "panel" / "PANEL_META.json").read_text())

    print("[v3] === LABELS ===", flush=True)
    labels = build_labels(panel, freeze)

    print("[v3] === FEATURES ===", flush=True)
    feat = build_features(labels)

    print("[v3] === DUAL-HEAD OOF ROUTES ===", flush=True)
    routes = fit_hypothesis_routes(feat, freeze)
    oof_map = {}
    for name, (oof, meta) in routes.items():
        oof_map[name] = oof
        if len(oof):
            oof.to_parquet(OUT / "predictions" / f"oof_{name}.parquet", index=False)
        (OUT / "predictions" / f"meta_{name}.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
        print(f"[v3.oof] {name}: n_oof={meta['n_oof']} selected={meta['n_selected']}", flush=True)

    print("[v3] === OOF SCORECARD + WALLET ===", flush=True)
    scorecard, details, curve, monthly = build_scorecard(oof_map, feat, freeze)

    # pick best hypothesis for confirmation: among registered hyps + full, max haircut pnl with non-lottery verdict
    best = details.get("best_oof_policy") or "H_FULL_DUAL"
    cand = scorecard[scorecard["verdict"] != "BASELINE"].copy()
    if len(cand):
        cand = cand[~cand["verdict"].isin(["LOTTERY_DEPENDENT_REJECT", "EXECUTION_RISK_DOMINATES"])]
        if len(cand):
            best = str(cand.sort_values("haircut_pnl_weth", ascending=False).iloc[0]["policy"])

    print(f"[v3] === CONFIRMATION best={best} ===", flush=True)
    confirmation = run_confirmation(
        feat,
        freeze,
        best_hypothesis=best,
        baseline_metrics_for_compare=details.get("baselines"),
    )
    (OUT / "confirmation" / "CONFIRMATION.json").write_text(json.dumps(confirmation, indent=2, default=str) + "\n")

    print("[v3] === FINALIZE ===", flush=True)
    final = finalize(
        freeze=freeze,
        scorecard=scorecard,
        route_details=details,
        confirmation=confirmation,
        registry=registry,
        panel_meta=panel_meta,
        curve=curve,
        monthly=monthly,
    )
    print(json.dumps({"verdict": final["verdict"], "best_oof_policy": final["best_oof_policy"], "deploy_allowed": False}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
