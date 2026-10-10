"""End-to-end DIAGNOSTIC_REPLAY for LARGE_BUY_ABSORPTION_v1."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/root/base_large_buy_absorption_v1")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> dict:
    import pandas as pd

    from src.arms_v1 import run_arms_oos
    from src.calendar_audit import run_calendar_audit
    from src.common import OUT, ROOT as R, load_freeze, save_json, utcnow
    from src.features_pit import build_buy_universe
    from src.finalize_v1 import finalize
    from src.labels_truth import label_events_truth
    from src.reconcile import run_reconcile
    from src.scorecard_v1 import build_scorecard
    from src.signals import assert_lookahead_free, build_arm_panel

    OUT.mkdir(parents=True, exist_ok=True)
    freeze = load_freeze()
    print(f"[v1] start DIAGNOSTIC_REPLAY {utcnow()}", flush=True)
    print(f"[v1] inherits_v0_oos={freeze.get('inherits_v0_oos')} deploy_allowed={freeze.get('deploy_allowed')}", flush=True)

    resolved_path = R / "data" / "resolved_swaps.parquet"
    print(f"[v1] loading {resolved_path} …", flush=True)
    resolved = pd.read_parquet(resolved_path)
    print(f"[v1] resolved rows={len(resolved):,}", flush=True)

    cal = run_calendar_audit(resolved)

    buys = build_buy_universe(resolved)
    buys_path = OUT / "panel" / "buys_features.parquet"
    buys_path.parent.mkdir(parents=True, exist_ok=True)
    buys.to_parquet(buys_path, index=False)

    # Label once per timing mode (immediate vs wait_absorption), then map to arms
    from src.signals import ARM_TIMING

    labeled_by_mode = {}
    loss_report = None
    lookahead = {"pass": False, "verdict": "LOOKAHEAD_FAIL"}
    for mode in ("immediate", "wait_absorption"):
        sample_arm = next(a for a, m in ARM_TIMING.items() if m == mode and a in freeze["arms"])
        print(f"[v1] timing+label mode={mode} via {sample_arm}", flush=True)
        panel = build_arm_panel(buys, sample_arm)
        if mode == "wait_absorption":
            lookahead = assert_lookahead_free(panel)
            save_json(OUT / "lookahead_assert.json", lookahead)
            print(f"[v1] lookahead: {lookahead['verdict']}", flush=True)
            if not lookahead["pass"]:
                raise RuntimeError(f"LOOKAHEAD_FAIL: {lookahead}")
        labeled, loss_report = label_events_truth(panel, resolved)
        labeled_by_mode[mode] = labeled
        labeled.to_parquet(OUT / "panel" / f"labeled_{mode}.parquet", index=False)

    labeled_by_arm = {}
    for arm in freeze["arms"]:
        mode = ARM_TIMING[arm]
        base = labeled_by_mode[mode]
        # Same timing mode ⇒ reuse labeled frame; tag arm name
        fr = base.copy()
        fr["arm_for_timing"] = arm
        labeled_by_arm[arm] = fr
        fr.to_parquet(OUT / "panel" / f"labeled_{arm}.parquet", index=False)

    la_imm = assert_lookahead_free(labeled_by_arm["LARGE_BUY_ONLY"])
    save_json(OUT / "lookahead_assert_immediate.json", la_imm)

    oof = run_arms_oos(buys, labeled_by_arm)
    for arm, fr in oof.items():
        fr.to_parquet(OUT / "oos" / f"oof_{arm}.parquet", index=False)

    scorecard, summary = build_scorecard(oof)
    recon = run_reconcile(oof, scorecard, cal)
    payload = finalize(scorecard, summary, lookahead, loss_report or {}, cal, recon)

    save_json(OUT / "RUN_META.json", {
        "finished_at": utcnow(),
        "freeze_gate": payload.get("freeze_gate"),
        "primary_scheduled_verdict": payload.get("primary_scheduled_verdict"),
        "historical_label": "DIAGNOSTIC_REPLAY",
    })
    print("[v1] DONE", flush=True)
    print(f"[v1] freeze_gate={payload.get('freeze_gate')}", flush=True)
    print(f"[v1] primary={payload.get('primary_scheduled_verdict')}", flush=True)
    return payload


if __name__ == "__main__":
    main()
