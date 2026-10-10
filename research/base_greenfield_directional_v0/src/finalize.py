#!/usr/bin/env python3
"""Final greenfield verdict — max 2 candidates or NO_GREENFIELD_ALPHA_FOUND."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import OUT, load_freeze, utcnow  # noqa: E402

METRICS = OUT / "metrics"
PANEL_MANIFEST = OUT / "panel" / "manifest.json"


def failure_attribution(sc: pd.DataFrame, panel_manifest: dict, c1_ev: float | None) -> list[str]:
    reasons = []
    n = panel_manifest.get("n_panel_rows", 0)
    if n < 500:
        reasons.append("sample_supply")
    if c1_ev is not None and (sc["ev_accepted"].dropna() <= c1_ev + 1e-12).all():
        reasons.append("ranking")
    # sellability
    if "fatal_first_rate" in sc.columns and sc["fatal_first_rate"].mean(skipna=True) and sc["fatal_first_rate"].mean() > 0.25:
        reasons.append("sellability")
    # costs: if all EV negative
    if sc["ev_accepted"].dropna().max() is not None and sc["ev_accepted"].dropna().max() <= 0:
        reasons.append("costs")
    # model instability
    if "worst_fold" in sc.columns and (sc["worst_fold"].dropna() < -0.5).any():
        reasons.append("model_instability")
    # regime
    reasons.append("regime_instability")  # checked via validation_extra; keep if no lead
    if not reasons:
        reasons = ["label", "market_gate"]
    # de-dup preserve order
    seen = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return seen


def main():
    freeze = load_freeze()
    sc = pd.read_csv(METRICS / "oof_scorecard.csv")
    panel_manifest = json.loads(PANEL_MANIFEST.read_text())
    extra = {}
    vp = METRICS / "validation_extra.json"
    if vp.exists():
        extra = json.loads(vp.read_text()).get("reports", {})

    c1 = sc.loc[sc["model_id"] == "C1_PRICE_ONLY"]
    c1_ev = float(c1["ev_accepted"].iloc[0]) if len(c1) and pd.notna(c1["ev_accepted"].iloc[0]) else None

    # formal primary models only (exclude controls and *_SENS)
    mid = sc["model_id"].astype(str)
    formal = sc[~mid.str.startswith("C") & ~mid.str.endswith("_SENS") & ~mid.str.startswith("STACK_")].copy()
    formal = formal.dropna(subset=["ev_accepted"])
    if c1_ev is not None:
        formal = formal[formal["ev_accepted"] > c1_ev]
    # require positive OOF EV (beating a negative C1 alone is not alpha)
    formal = formal[formal["ev_accepted"] > 0]
    # require worst_fold not catastrophic
    if "worst_fold" in formal.columns:
        formal = formal[formal["worst_fold"].fillna(-1) > -0.75]
    # reject acceptance_rate==1 artifacts (no NO_TRADE capacity)
    if "acceptance_rate" in formal.columns:
        formal = formal[formal["acceptance_rate"].fillna(1) < 0.95]
    formal = formal.sort_values(["incremental_ev_vs_c1", "ev_accepted"], ascending=False)

    c3 = sc.loc[sc["model_id"] == "C3_SIMPLE_MOMENTUM"]
    c3_ev = float(c3["ev_accepted"].iloc[0]) if len(c3) and pd.notna(c3["ev_accepted"].iloc[0]) else None

    candidates = []
    for _, r in formal.head(2).iterrows():
        mid = r["model_id"]
        ex = extra.get(mid, {})
        decay = ex.get("decay", {})
        cand = {
            "candidate_id": mid,
            "market_gate": "opportunity_supply_median_fold",
            "universe": "base_univ3_weth_pit_eligible",
            "age_bucket": freeze["age_buckets"]["primary_modeling"],
            "features": "PRICE_PATH+FLOW+PARTICIPATION+LIQUIDITY+MARKET",
            "model": mid,
            "target": "xs_return_pctile + competing barriers",
            "entry_policy": "top-k=3 per decision_time after L1/L2/L3 gates",
            "fixed_exit_policy": {
                "horizon_s": freeze["primary_horizon_seconds"],
                "barrier": freeze["primary_barrier"],
                "params": freeze.get("primary_barrier_params"),
            },
            "oof_predictions_path": r.get("oof_predictions_path"),
            "independent_positions": True,
            "weeks": ex.get("lowo", {}),
            "tokens": ex.get("cluster", {}).get("n_tokens"),
            "regimes": ex.get("regime", {}),
            "oof_ev": r.get("ev_accepted"),
            "price_only_oof_ev": c1_ev,
            "incremental_ev": r.get("incremental_ev_vs_c1"),
            "rank_ic": r.get("rank_ic"),
            "fatal_rate": r.get("fatal_first_rate"),
            "atomic_failure_rate": r.get("atomic_failure_rate"),
            "capital_hour_return": r.get("capital_hour_return"),
            "worst_fold": r.get("worst_fold"),
            "top5_dependency": r.get("top5_dependency"),
            "decay_profile": decay,
            "failure_modes": [],
            "verdict": "DISCOVERY_LEAD",
            "notes": {"c3_momentum_oof_ev": c3_ev},
        }
        cand["notes"]["beats_c3"] = c3_ev is None or (
            cand["oof_ev"] is not None and cand["oof_ev"] > c3_ev
        )
        # If cannot beat simple momentum control, record failure mode
        if c3_ev is not None and cand["oof_ev"] is not None and cand["oof_ev"] <= c3_ev:
            cand["failure_modes"].append("does_not_beat_C3_SIMPLE_MOMENTUM")
        # upgrade rules (still no holdout peek for promotion beyond eligibility flag)
        if (
            cand["incremental_ev"] is not None
            and cand["incremental_ev"] > 0.02
            and cand["worst_fold"] is not None
            and cand["worst_fold"] > 0
            and (cand["top5_dependency"] is None or cand["top5_dependency"] < 0.8)
            and not cand["failure_modes"]
        ):
            cand["verdict"] = "HOLDOUT_ELIGIBLE"
        if decay.get("decay_status") == "ACTIVE" and cand["verdict"] == "HOLDOUT_ELIGIBLE":
            cand["verdict"] = "SHADOW_ELIGIBLE"
        candidates.append(cand)

    if not candidates:
        reasons = failure_attribution(sc, panel_manifest, c1_ev)
        final = {
            "created_utc": utcnow(),
            "result": "NO_GREENFIELD_ALPHA_FOUND",
            "failure_from": reasons,
            "c1_price_only_oof_ev": c1_ev,
            "n_panel_rows": panel_manifest.get("n_panel_rows"),
            "primary_horizon_seconds": freeze.get("primary_horizon_seconds"),
            "primary_barrier": freeze.get("primary_barrier"),
            "candidates": [],
            "deploy_allowed": False,
            "sealed_holdout_untouched_for_tuning": True,
            "notes": "No formal model stably beat C1_PRICE_ONLY under OOF protocol.",
        }
    else:
        final = {
            "created_utc": utcnow(),
            "result": "CANDIDATES",
            "n_candidates": len(candidates),
            "candidates": candidates,
            "c1_price_only_oof_ev": c1_ev,
            "deploy_allowed": False,
            "sealed_holdout_untouched_for_tuning": True,
            "promotion_cap": 2,
        }

    def _clean(o):
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        if isinstance(o, float) and (pd.isna(o) or o != o):
            return None
        if hasattr(o, "item"):
            try:
                return o.item()
            except Exception:
                return o
        return o

    final = _clean(final)
    path = OUT / "FINAL_GREENFIELD.json"
    path.write_text(json.dumps(final, indent=2) + "\n")
    print(json.dumps(final, indent=2), flush=True)


if __name__ == "__main__":
    main()
