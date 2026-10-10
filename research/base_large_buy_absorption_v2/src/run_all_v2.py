"""End-to-end LARGE_BUY_ABSORPTION_v2 pipeline (Phases 0–5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/root/base_large_buy_absorption_v2")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> dict:
    import pandas as pd

    from src.arms_v1 import run_arms_oos
    from src.calendar_audit import run_calendar_audit
    from src.common import OUT, ROOT as R, load_freeze, save_json, utcnow
    from src.coverage_diag import run_coverage_diag
    from src.features_pit import build_buy_universe
    from src.finalize_v2 import finalize_v2
    from src.labels_truth import label_events_truth
    from src.pnl_decomp import run_pnl_decomp
    from src.ranking import run_intraday_ranking
    from src.scorecard_v1 import build_scorecard
    from src.sensitivity import run_sensitivity
    from src.signals import ARM_TIMING, assert_lookahead_free, build_arm_panel
    from src.shadow_recorder import arm_shadow

    OUT.mkdir(parents=True, exist_ok=True)
    freeze = load_freeze()
    print(f"[v2] start {utcnow()}", flush=True)
    print(
        f"[v2] primary timing absorb={freeze['signal_timing']['absorption_window_seconds']}s "
        f"latency={freeze['signal_timing']['measured_latency_seconds']}s",
        flush=True,
    )

    resolved_path = R / "data" / "resolved_swaps_full.parquet"
    print(f"[v2] loading {resolved_path} …", flush=True)
    resolved = pd.read_parquet(resolved_path)
    print(f"[v2] resolved rows={len(resolved):,}", flush=True)

    # Phase 0 HARD GATE
    print("[v2] Phase 0 coverage diagnostic…", flush=True)
    coverage = run_coverage_diag(resolved)
    trigger = coverage["rebuild_recommendation"]["phase1_trigger"]
    save_json(OUT / "PHASE1_TRIGGER.json", {
        "trigger": trigger,
        "actions": coverage["rebuild_recommendation"]["actions"],
        "note": "Full rebuild skipped when intended universe already matches CH ground truth.",
    })
    if trigger == "RUN_FULL_REBUILD":
        print("[v2] Phase 1 FULL REBUILD triggered — running resolution…", flush=True)
        from src.actor_resolution import run_resolution
        resolved = run_resolution(rpc_sample=80, resume=False)
        resolved.to_parquet(R / "data" / "resolved_swaps_full.parquet", index=False)
        coverage = run_coverage_diag(resolved)
    else:
        print("[v2] Phase 1 SKIP_FULL_REBUILD — hardening loader present; data already complete.", flush=True)

    cal = run_calendar_audit(resolved)

    print("[v2] building buy universe + multi-window absorption…", flush=True)
    buys = build_buy_universe(resolved)
    buys_path = OUT / "panel" / "buys_features.parquet"
    buys_path.parent.mkdir(parents=True, exist_ok=True)
    buys.to_parquet(buys_path, index=False)

    labeled_by_mode = {}
    loss_report = None
    lookahead = {"pass": False, "verdict": "LOOKAHEAD_FAIL"}
    for mode in ("immediate", "wait_absorption"):
        sample_arm = next(a for a, m in ARM_TIMING.items() if m == mode and a in freeze["arms"])
        print(f"[v2] timing+label mode={mode} via {sample_arm}", flush=True)
        panel = build_arm_panel(buys, sample_arm)
        if mode == "wait_absorption":
            lookahead = assert_lookahead_free(panel)
            save_json(OUT / "lookahead_assert.json", lookahead)
            print(f"[v2] lookahead: {lookahead['verdict']}", flush=True)
            if not lookahead["pass"]:
                raise RuntimeError(f"LOOKAHEAD_FAIL: {lookahead}")
        labeled, loss_report = label_events_truth(panel, resolved)
        labeled_by_mode[mode] = labeled
        labeled.to_parquet(OUT / "panel" / f"labeled_{mode}.parquet", index=False)

    labeled_by_arm = {}
    for arm in freeze["arms"]:
        mode = ARM_TIMING[arm]
        fr = labeled_by_mode[mode].copy()
        fr["arm_for_timing"] = arm
        labeled_by_arm[arm] = fr

    oof = run_arms_oos(buys, labeled_by_arm)
    (OUT / "oos").mkdir(parents=True, exist_ok=True)
    for arm, fr in oof.items():
        fr.to_parquet(OUT / "oos" / f"oof_{arm}.parquet", index=False)

    scorecard, summary = build_scorecard(oof)

    # Phase 2
    print("[v2] Phase 2 PnL decomposition…", flush=True)
    details = summary.get("details") or {}
    if "CAPACITY_MATCHED_RANDOM" not in details.get("controls", {}):
        from src.scorecard_v1 import eval_arm
        from src.arms_v1 import capacity_matched_random
        details.setdefault("arms", {})
        details.setdefault("controls", {})
        for arm, fr in oof.items():
            if arm not in details["arms"]:
                m = eval_arm(fr, arm)
                details["arms"][arm] = {k: v for k, v in m.items() if k != "fills"}
        spine = oof["POOL_ONLY"].copy()
        n_sel = int(spine["policy_selected"].sum())
        rand = spine.copy()
        rand["policy_selected"] = capacity_matched_random(rand, n_sel)
        rm = eval_arm(rand, "CAPACITY_MATCHED_RANDOM")
        details["controls"]["CAPACITY_MATCHED_RANDOM"] = {k: v for k, v in rm.items() if k != "fills"}
    pnl = run_pnl_decomp(oof, details)

    # Phase 4 ranking (before sensitivity — uses primary LBA oof)
    print("[v2] Phase 4 intraday ranking…", flush=True)
    ranking = run_intraday_ranking(oof["LARGE_BUY_ABSORPTION"])

    # Phase 3 sensitivity diagnostic (after primary locked)
    print("[v2] Phase 3 sensitivity diagnostic (not for selection)…", flush=True)
    sens = run_sensitivity(buys, resolved)

    payload = finalize_v2(
        scorecard, summary, lookahead, loss_report or {}, cal, coverage, pnl, ranking, sens
    )

    # Phase 5 shadow — observer only; upgrade flag in payload
    try:
        arm_shadow(upgrade=bool(payload.get("shadow_upgrade")))
    except TypeError:
        arm_shadow()

    save_json(OUT / "RUN_META.json", {
        "finished_at": utcnow(),
        "freeze_gate": payload.get("freeze_gate"),
        "per_hypothesis": payload.get("per_hypothesis"),
        "shadow_upgrade": payload.get("shadow_upgrade"),
        "historical_label": "DIAGNOSTIC_REPLAY",
    })
    print("[v2] DONE", flush=True)
    print(f"[v2] freeze_gate={payload.get('freeze_gate')} shadow_upgrade={payload.get('shadow_upgrade')}", flush=True)
    for k, v in (payload.get("per_hypothesis") or {}).items():
        print(f"  {k}: {v}", flush=True)
    return payload


def _load_scorecard_details() -> dict:
    from src.common import OUT
    import json
    p = OUT / "oos" / "scorecard_v1_summary.json"
    if p.exists():
        return json.loads(p.read_text()).get("details") or {}
    return {}


if __name__ == "__main__":
    main()
