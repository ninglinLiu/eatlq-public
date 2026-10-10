"""Resume v2 pipeline after OOF/scorecard cached (skip expensive label rebuild)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/root/base_large_buy_absorption_v2")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> dict:
    import pandas as pd

    from src.common import OUT, ROOT as R, load_freeze, save_json, utcnow
    from src.finalize_v2 import finalize_v2
    from src.pnl_decomp import run_pnl_decomp
    from src.ranking import run_intraday_ranking
    from src.sensitivity import run_sensitivity
    from src.shadow_recorder import arm_shadow
    from src.scorecard_v1 import eval_arm
    from src.arms_v1 import capacity_matched_random

    freeze = load_freeze()
    print(f"[resume] {utcnow()}", flush=True)

    oof = {}
    for arm in freeze["arms"]:
        oof[arm] = pd.read_parquet(OUT / "oos" / f"oof_{arm}.parquet")

    scorecard = pd.read_csv(OUT / "oos" / "scorecard_v1.csv")
    summary = json.loads((OUT / "oos" / "scorecard_v1_summary.json").read_text())
    # rebuild details for pnl
    details = {"arms": {}, "controls": {}}
    for arm, fr in oof.items():
        m = eval_arm(fr, arm)
        details["arms"][arm] = {k: v for k, v in m.items() if k != "fills"}
        details["arms"][arm]["scheduled_verdict"] = scorecard.loc[
            scorecard["arm"] == arm, "scheduled_verdict"
        ].iloc[0]
    spine = oof["POOL_ONLY"].copy()
    n_sel = int(spine["policy_selected"].sum())
    rand = spine.copy()
    rand["policy_selected"] = capacity_matched_random(rand, n_sel)
    rm = eval_arm(rand, "CAPACITY_MATCHED_RANDOM")
    details["controls"]["CAPACITY_MATCHED_RANDOM"] = {k: v for k, v in rm.items() if k != "fills"}
    summary["details"] = details
    summary["primary_scheduled_verdict"] = details["arms"]["LARGE_BUY_ABSORPTION"].get("scheduled_verdict")

    print("[resume] Phase 2 PnL…", flush=True)
    pnl = run_pnl_decomp(oof, details)

    print("[resume] Phase 4 ranking…", flush=True)
    ranking = run_intraday_ranking(oof["LARGE_BUY_ABSORPTION"])

    print("[resume] Phase 3 sensitivity…", flush=True)
    buys = pd.read_parquet(OUT / "panel" / "buys_features.parquet")
    resolved = pd.read_parquet(R / "data" / "resolved_swaps_full.parquet")
    sens = run_sensitivity(buys, resolved)

    lookahead = json.loads((OUT / "lookahead_assert.json").read_text())
    loss_report = json.loads((OUT / "loss_truth_report.json").read_text())
    cal = json.loads((OUT / "calendar_audit.json").read_text())
    coverage = json.loads((OUT / "diag" / "DATA_COVERAGE_DIAGNOSTIC.json").read_text())

    payload = finalize_v2(
        scorecard, summary, lookahead, loss_report, cal, coverage, pnl, ranking, sens
    )
    arm_shadow(upgrade=bool(payload.get("shadow_upgrade")))
    save_json(OUT / "RUN_META.json", {
        "finished_at": utcnow(),
        "freeze_gate": payload.get("freeze_gate"),
        "per_hypothesis": payload.get("per_hypothesis"),
        "shadow_upgrade": payload.get("shadow_upgrade"),
        "resumed_from": "ranking",
    })
    print("[resume] DONE", flush=True)
    print(json.dumps(payload.get("per_hypothesis"), indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
