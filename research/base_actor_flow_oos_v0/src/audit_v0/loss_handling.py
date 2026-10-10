"""Task 6: verify sell-fail / censored / infrastructure-unknown conservative loss handling."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..common import OUT, load_freeze
from .data import save_audit_json


def run_loss_handling(panel: pd.DataFrame) -> dict[str, Any]:
    freeze = load_freeze()
    floor = float(freeze["execution"]["economic_unsellable_recovery_floor"])
    stake = float(freeze["execution"]["primary_size_usd"]) / float(freeze["capital"]["eth_usd_freeze"])
    # Conservative loss if recovery floor applied: pnl = stake * (floor - 1) = -0.9 * stake
    expected_floor_pnl = stake * (floor - 1.0)

    d = panel.copy()
    d["wallet_pnl_weth"] = pd.to_numeric(d.get("wallet_pnl_weth"), errors="coerce")
    buy = d["buy_status"].astype(str) if "buy_status" in d.columns else pd.Series(["UNKNOWN"] * len(d))
    cens = d["censored"].astype(bool) if "censored" in d.columns else pd.Series(False, index=d.index)
    tier = d["tier"].astype(str) if "tier" in d.columns else pd.Series(["?"] * len(d))
    route = d["route_family"].astype(str) if "route_family" in d.columns else pd.Series(["?"] * len(d))

    no_fill = buy.ne("SUCCESS")
    infra = route.isin(["shared_infra", "solver_settlement"]) | tier.eq("D")
    unknown_actor = d["economic_actor"].astype(str).str.len().ne(42) if "economic_actor" in d.columns else pd.Series(False, index=d.index)

    findings = []

    # 1) recovery floor never referenced in labels_exec
    findings.append({
        "check": "recovery_floor_applied_in_labels",
        "pass": False,
        "detail": (
            f"economic_unsellable_recovery_floor={floor} is present in freeze but never applied in "
            "src/labels_exec.py (confirmed by repo grep). NO_FILL rows keep wallet_pnl=NaN."
        ),
        "expected_floor_pnl_weth": expected_floor_pnl,
    })

    # 2) NO_FILL pnl distribution
    nf = d.loc[no_fill, "wallet_pnl_weth"]
    findings.append({
        "check": "no_fill_charged_conservative_loss",
        "pass": False,
        "n_no_fill": int(no_fill.sum()),
        "n_no_fill_nan_pnl": int(nf.isna().sum()),
        "n_no_fill_zero_pnl": int((nf.fillna(0) == 0).sum()),
        "n_no_fill_at_floor": int(((nf - expected_floor_pnl).abs() < 1e-12).sum()) if len(nf) else 0,
        "detail": "NO_FILL rows are NaN/0, not charged stake*(floor-1). Mitigated only because selection requires buy_status==SUCCESS.",
    })

    # 3) Censored rows that are SUCCESS still get full path PnL
    cens_success = cens & buy.eq("SUCCESS")
    cs_pnl = d.loc[cens_success, "wallet_pnl_weth"]
    findings.append({
        "check": "censored_transfer_out_conservative",
        "pass": bool(cens_success.sum() == 0 or (cs_pnl.isna() | (cs_pnl <= expected_floor_pnl + 1e-9)).all()),
        "n_censored": int(cens.sum()),
        "n_censored_success_labeled": int(cens_success.sum()),
        "mean_pnl_censored_success": float(cs_pnl.mean()) if len(cs_pnl) else None,
        "detail": (
            "Panel censored flag comes from actor-resolution FIFO transfer-out logic; "
            "labels_exec still marks price-path SUCCESS and assigns full TP-ladder PnL. "
            "FIFO episodes.parquet uses NaN pnl when censored, but copy-sim labels do not."
        ),
    })

    # 4) Infrastructure / unknown actor in selected arms — check panel presence
    findings.append({
        "check": "infrastructure_unknown_excluded_or_conservatively_charged",
        "n_infra_rows": int(infra.sum()),
        "n_unknown_actor_rows": int(unknown_actor.sum()),
        "n_infra_with_success_fill": int((infra & buy.eq("SUCCESS")).sum()),
        "mean_pnl_infra_success": float(
            d.loc[infra & buy.eq("SUCCESS"), "wallet_pnl_weth"].mean()
        ) if (infra & buy.eq("SUCCESS")).any() else None,
        "pass": int((infra & buy.eq("SUCCESS")).sum()) == 0,
        "detail": "Shared-infra/solver tier-D rows should not be scorable copy subjects.",
    })

    # Cross-check episodes parquet if present
    ep_path = OUT / "phase2" / "episodes.parquet"
    episode_check: dict[str, Any] = {"present": ep_path.exists()}
    if ep_path.exists():
        ep = pd.read_parquet(ep_path)
        if "censored" in ep.columns:
            c = ep[ep["censored"].astype(bool)]
            pnl = pd.to_numeric(c.get("pnl_weth"), errors="coerce") if len(c) else pd.Series(dtype=float)
            episode_check.update({
                "n_episodes": int(len(ep)),
                "n_censored": int(len(c)),
                "censored_pnl_all_nan": bool(pnl.isna().all()) if len(c) else True,
                "detail": "FIFO episodes correctly NaN pnl when censored; identity scores exclude them.",
            })

    # Overall
    n_fail = sum(1 for f in findings if f.get("pass") is False)
    report = {
        "task": "conservative_loss_handling",
        "recovery_floor": floor,
        "stake_weth": stake,
        "expected_floor_pnl_weth": expected_floor_pnl,
        "findings": findings,
        "episode_cross_check": episode_check,
        "summary": {
            "n_checks": len(findings),
            "n_failures": n_fail,
            "verdict": "LOSS_HANDLING_GAPS_FOUND" if n_fail else "LOSS_HANDLING_OK",
            "materiality_for_whale_scorecard": (
                "Selection requires buy_status==SUCCESS, so NO_FILL NaNs do not enter WHALE totals. "
                "Main residual risk: censored transfer-out buys that still receive optimistic "
                "price-path PnL in wallet_pnl_weth, and recovery_floor never applied."
            ),
        },
    }
    save_audit_json("06_loss_handling.json", report)
    print(
        f"[audit:loss] failures={n_fail} censored_success={int(cens_success.sum())} "
        f"floor_applied=False",
        flush=True,
    )
    return report
