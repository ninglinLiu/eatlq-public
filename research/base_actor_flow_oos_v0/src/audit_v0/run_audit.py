"""Driver: run the full diagnostic estimand-repair audit end-to-end.

Usage:
  python -m src.audit_v0.run_audit
  AUDIT_RUN_RPC=1 python -m src.audit_v0.run_audit   # optional golden expansion
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python -m src.audit_v0.run_audit` from project root
ROOT = Path("/root/base_actor_flow_oos_v0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> dict:
    from src.audit_v0.coverage_ci import run_coverage_ci
    from src.audit_v0.data import AUDIT_OUT, load_labeled_panel, load_or_build_oof, save_audit_json
    from src.audit_v0.dedup_capital import run_dedup_capital
    from src.audit_v0.incremental import run_incremental
    from src.audit_v0.loss_handling import run_loss_handling
    from src.audit_v0.proxy_ablation import run_proxy_ablation
    from src.audit_v0.report import write_reports
    from src.audit_v0.resampling import run_resampling
    from src.common import utcnow

    AUDIT_OUT.mkdir(parents=True, exist_ok=True)
    print(f"[audit] start {utcnow()} → {AUDIT_OUT}", flush=True)

    panel = load_labeled_panel()
    print(f"[audit] labeled panel rows={len(panel):,}", flush=True)
    oof = load_or_build_oof(panel)
    for arm, fr in oof.items():
        nsel = int(fr["policy_selected"].sum()) if "policy_selected" in fr.columns else 0
        print(f"[audit] oof {arm}: n={len(fr):,} selected={nsel:,}", flush=True)

    incremental = run_incremental(oof)
    dedup = run_dedup_capital(oof)
    coverage = run_coverage_ci()
    resampling = run_resampling(oof, incremental)
    proxy = run_proxy_ablation(oof, panel)
    loss = run_loss_handling(panel)
    diagnostic = write_reports(incremental, dedup, coverage, resampling, proxy, loss)

    save_audit_json("AUDIT_RUN_META.json", {
        "finished_at": utcnow(),
        "overall": diagnostic.get("overall"),
        "spec_decision": diagnostic.get("spec_decision"),
        "mutates_scored_outputs": False,
    })
    print("[audit] DONE", flush=True)
    print(f"[audit] overall={diagnostic.get('overall')}", flush=True)
    print(f"[audit] spec_decision={diagnostic.get('spec_decision')}", flush=True)
    return diagnostic


if __name__ == "__main__":
    main()
