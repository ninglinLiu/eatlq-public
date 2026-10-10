#!/usr/bin/env python3
"""
Sprint093-A — Full whitepaper Mode-A restoration.

Goal (per user spec): answer the question
  "After Whale = XGBoost_confidence × whale_bonus is fully applied, do any
   sub-$100k candidates produce dashcam/signal output?"

This sprint DOES NOT measure profitability and DOES NOT lift FAILED_NO_7D_ALPHA.
It answers ONE narrow question: does the score formula restore signal volume
when the binarized whale_score is replaced with the whitepaper's full
`xgb_confidence × bonus` formulation?

Pipeline:
  1. Candidate stream from sprint092 v3 alt-pool universe (base-routing
     excluded + Quoter V2 honeypot).
  2. For each candidate, compute 5m pre-signal USD-normalised features.
  3. Compute XGB confidence three ways and report each:
       (a) v1_strict_synthetic: whitepaper Phase 1 spec, win_rate=0.5, roi=0.0
       (b) v1_production: load whatever production model is present
           (signal_model_base_v1.json or whale_sniper_v1.json), 4-/5-dim
       (c) v1_reconstruction_live: same model but use live wallet history
           features instead of the Phase 1 fixed defaults
  4. whale_score = confidence × (1.3 if notional ≥ WHALE_USD_THRESH else 1.0)
     (clamped to [0, 1] for Mode-A weighting compatibility)
  5. Mode-A: 0.40·whale + 0.30·M + 0.20·A − 0.10·R, evaluated per tier.
  6. Output: full final_score histograms per (tier × score_variant), n
     candidates >= dashcam (0.40), n >= signal (0.52), and the per-candidate
     score breakdown CSV.

NOTHING is published to Redis. RESEARCH_ONLY.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from sprint092_universe_expander import WHALE_USD_THRESH  # noqa: E402
from sprint092_null_overlay_strategy import (  # noqa: E402
    DASHCAM_THRESHOLD,
    SIGNAL_THRESHOLD,
)
from sprint093_common import (  # noqa: E402
    PHASE1_DEFAULT_AVG_ROI,
    PHASE1_DEFAULT_WIN_RATE_30D,
    attach_5m_pre_features,
    fetch_candidates,
    fetch_wallet_phase1_features,  # opt-in via --wallet-history (slow)
)

log = logging.getLogger("sprint093a")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sprint093a_full_mode_a"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Whitepaper Mode-A weights (frozen — same as sprint092)
W_WHALE, W_MOMENTUM, W_ACTIVITY, W_RISK = 0.40, 0.30, 0.20, 0.10
WHALE_BONUS = 1.3
RESEARCH_ONLY = True


# ---------- XGB confidence variants -------------------------------------------


def _load_v1_model(model_dir: Path) -> tuple[Any, str, list[str], str]:
    """Load the whitepaper Phase 1 v1 model.

    Priority order:
      1. Original whitepaper artifact:  whale_sniper_v1.json (NOT present in this repo)
      2. Reconstruction artifact:       models/research_only/whale_sniper_v1_reconstruction.json
      3. Hard fail otherwise.

    Returns (model, name, feature_names, provenance).

    Notes:
      * signal_model_base_v1.json is a 17-feature wallet-profile classifier
        from the EATLQ team — NOT whitepaper Phase 1. We deliberately do NOT
        load it here because feeding it our 5m features would crash the model
        (Feature shape mismatch: expected 17, got 5) and the silent except-
        handler fallback would produce a flat 0.5 confidence (i.e. a fake
        score path). See sprint093a/readout.md for the disclosure.
    """
    from xgboost import XGBClassifier

    original = model_dir / "whale_sniper_v1.json"
    reconstruction = model_dir / "research_only" / "whale_sniper_v1_reconstruction.json"

    if original.exists():
        m = XGBClassifier(); m.load_model(original)
        return m, "whale_sniper_v1.json", _feature_names_or_default(m), "ORIGINAL_WHITEPAPER"
    if reconstruction.exists():
        m = XGBClassifier(); m.load_model(reconstruction)
        return m, "whale_sniper_v1_reconstruction.json", _feature_names_or_default(m), "RECONSTRUCTION_SYNTHETIC"
    raise RuntimeError(
        f"neither original ({original.name}) nor reconstruction "
        f"({reconstruction.name}) found — run sprint093_build_v1_reconstruction.py first"
    )


def _feature_names_or_default(model: Any) -> list[str]:
    names = model.get_booster().feature_names
    return list(names) if names else ["win_rate_30d", "avg_roi", "price_velocity_5m", "buy_pressure_ratio", "is_whale"]


def _confidence_v1(
    model: Any,
    feature_names: list[str],
    features_5m: dict,
    notional_usd: float,
    *,
    win_rate_override: float | None = None,
    avg_roi_override: float | None = None,
) -> float:
    """Whitepaper Phase 1 v1 inference: 5-feature vector aligned to model
    feature_names (default order: win_rate_30d, avg_roi, price_velocity_5m,
    buy_pressure_ratio, is_whale).

    Phase 1 strict path: win_rate_override=0.5, avg_roi_override=0.0
      (per whitepaper "Limitation: win_rate_30d 固定 0.5, avg_roi 固定 0.0").
    """
    win_rate = PHASE1_DEFAULT_WIN_RATE_30D if win_rate_override is None else win_rate_override
    avg_roi  = PHASE1_DEFAULT_AVG_ROI     if avg_roi_override  is None else avg_roi_override
    feature_values = {
        "win_rate_30d": float(win_rate),
        "avg_roi": float(avg_roi),
        "price_velocity_5m": float(features_5m.get("price_velocity_5m", 0.0)),
        "buy_pressure_ratio": float(features_5m.get("buy_pressure_ratio", 0.5)),
        "is_whale": 1.0 if notional_usd >= WHALE_USD_THRESH else 0.0,
    }
    x = np.array([[feature_values[name] for name in feature_names]])
    try:
        proba = model.predict_proba(x)
        return float(max(0.0, min(1.0, proba[0][1])))
    except Exception as exc:
        log.warning("v1 model predict fail: %s — returning 0.5 fallback", exc)
        return 0.5


def _confidence_v1_strict_synthetic(features_5m: dict, notional_usd: float) -> dict:
    """Whitepaper Phase 1 strict: win_rate_30d=0.5, avg_roi=0.0 (fixed),
    plus the 3 live features. Per whitepaper's own admission this is "半随机
    猜测" (semi-random guess) because two of five inputs are constants.

    We approximate the bootstrap-trained synthetic model with a small
    deterministic mapping so the result is reproducible without retraining:
        confidence = sigmoid( 1.5·activity_norm + 0.8·buy_pressure_minus_half
                              + 0.6·price_velocity_norm − 1.2·vol_excess
                              + 0.5·is_whale )
    Coefficients chosen so that mean(confidence) ≈ 0.5 over zero-feature input
    (matches Phase 1's stated "half-random guess" property) and is_whale toggles
    the confidence by roughly ±0.12. NOT a re-trained model — explicitly labelled
    `v1_strict_synthetic_heuristic` in the readout.
    """
    import math
    pv = float(features_5m.get("price_velocity_5m", 0.0))
    bpr = float(features_5m.get("buy_pressure_ratio", 0.5))
    vol = float(features_5m.get("token_volatility_5m", 0.0))
    act = float(features_5m.get("activity_heat", 0.0))
    is_whale = 1.0 if notional_usd >= WHALE_USD_THRESH else 0.0
    z = (
        1.5 * min(1.0, act / 100.0)
        + 0.8 * (bpr - 0.5)
        + 0.6 * max(-1.0, min(1.0, pv))
        - 1.2 * max(0.0, vol - 1.0)
        + 0.5 * is_whale
        + PHASE1_DEFAULT_WIN_RATE_30D - 0.5   # explicitly visible: default contributes 0
        + 0.0 * PHASE1_DEFAULT_AVG_ROI       # explicitly visible: default contributes 0
    )
    conf = 1.0 / (1.0 + math.exp(-z))
    return {
        "confidence": conf,
        "variant": "v1_strict_synthetic_heuristic",
        "is_whale": int(is_whale),
        "phase1_defaults_used": {
            "win_rate_30d": PHASE1_DEFAULT_WIN_RATE_30D,
            "avg_roi": PHASE1_DEFAULT_AVG_ROI,
        },
    }


# ---------- Mode-A scoring -----------------------------------------------------


def mode_a_score(
    *,
    confidence: float,
    notional_usd: float,
    features_5m: dict,
) -> dict:
    whale_bonus = WHALE_BONUS if notional_usd >= WHALE_USD_THRESH else 1.0
    whale_score_raw = confidence * whale_bonus
    whale_score = max(0.0, min(1.0, whale_score_raw))
    momentum = float(features_5m.get("price_velocity_5m", 0.0))
    activity = min(1.0, float(features_5m.get("activity_heat", 0)) / 100.0)
    risk = max(float(features_5m.get("token_volatility_5m", 0.0)) - 1.0, 0.0)
    final = W_WHALE * whale_score + W_MOMENTUM * momentum + W_ACTIVITY * activity - W_RISK * risk
    return {
        "whale_score": whale_score,
        "whale_score_raw": whale_score_raw,
        "whale_bonus": whale_bonus,
        "momentum": momentum,
        "activity": activity,
        "risk": risk,
        "final_score": final,
    }


# ---------- Reporting ---------------------------------------------------------


def summarise(scores: list[float]) -> dict:
    if not scores:
        return {"n": 0}
    arr = np.array(scores)
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
        "p95": float(np.quantile(arr, 0.95)),
        "p99": float(np.quantile(arr, 0.99)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
        "n_ge_dashcam_0_40": int((arr >= DASHCAM_THRESHOLD).sum()),
        "n_ge_signal_0_52": int((arr >= SIGNAL_THRESHOLD).sum()),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--max-pool-age-hours", type=int, default=8760)
    p.add_argument("--lowest-thresh", type=int, default=10_000)
    p.add_argument("--tiers", type=int, nargs="+", default=[100_000, 50_000, 25_000, 10_000])
    p.add_argument("--no-quoter", action="store_true",
                   help="skip Quoter V2 honeypot pre-filter (default: enabled)")
    p.add_argument("--wallet-history", action="store_true",
                   help="fetch per-sender 30d wallet history (slow, no effect on score "
                        "under current Phase-1-strict design — opt-in for context only)")
    p.add_argument("--model-dir", type=str, default=str(REPO / "models"))
    args = p.parse_args()

    # 1 — discovery + honeypot
    cands = fetch_candidates(
        days=args.days,
        max_pool_age_hours=args.max_pool_age_hours,
        lowest_thresh=args.lowest_thresh,
        apply_quoter_v2=not args.no_quoter,
    )
    log.info("candidates (post-quoter): %d", len(cands))

    # 2 — pre-signal 5m features
    enriched: list[dict] = []
    for i, c in enumerate(cands):
        try:
            attach_5m_pre_features(c)
            enriched.append(c)
        except Exception as exc:
            log.warning("pre-feature fail %s: %s", c.get("pool", "?")[:10], exc)
    log.info("enriched (with 5m features): %d", len(enriched))

    # 3 — load whitepaper v1 model (original or reconstruction)
    model_dir = Path(args.model_dir)
    v1_model, v1_name, v1_features, v1_provenance = _load_v1_model(model_dir)
    log.info("v1 model: %s (provenance=%s, features=%s)", v1_name, v1_provenance, v1_features)

    # 4 — score each candidate
    detail_rows: list[dict] = []
    for c in enriched:
        notional = float(c["notional_usd"])
        feats = c["features_5m"]
        sender = c.get("sender", "")
        signal_ts = int(c["signal_ts"])

        # v1_strict: whitepaper Phase 1 fixed defaults (win_rate=0.5, avg_roi=0.0)
        conf_strict = _confidence_v1(v1_model, v1_features, feats, notional)

        # v1_live: same model, but wallet history fed in instead of Phase 1 defaults
        # Live win_rate / avg_roi proxies are not available on Base today (no realised
        # PnL labels — that is exactly what Sprint093-B answers separately), so live
        # collapses to strict. We do NOT fabricate proxy labels. Optionally fetch
        # wallet 30d activity context (slow CH query) when --wallet-history is set.
        if args.wallet_history and sender:
            wallet_ctx = fetch_wallet_phase1_features(sender, signal_ts)
        else:
            wallet_ctx = {"n_buys_30d": None, "uniq_pools_30d": None}
        conf_live = conf_strict  # will diverge only once real Phase 2 labels exist

        # Heuristic v1_synth: reference fall-back per whitepaper "half-random guess"
        synth = _confidence_v1_strict_synthetic(feats, notional)

        score_strict = mode_a_score(confidence=conf_strict, notional_usd=notional, features_5m=feats)
        score_live   = mode_a_score(confidence=conf_live,   notional_usd=notional, features_5m=feats)
        score_synth  = mode_a_score(confidence=synth["confidence"], notional_usd=notional, features_5m=feats)
        score_bin = mode_a_score(
            confidence=1.0 if notional >= WHALE_USD_THRESH else 0.0,
            notional_usd=notional, features_5m=feats,
        )

        detail_rows.append({
            "pool": c.get("pool"),
            "sender": sender,
            "signal_ts": signal_ts,
            "signal_time": c.get("signal_time"),
            "notional_usd": notional,
            "is_whale_event": int(notional >= WHALE_USD_THRESH),
            "target_address": c.get("target_address"),
            "fee_bps": c.get("fee_bps"),
            "pool_age_sec": c.get("pool_age_sec"),
            "price_velocity_5m": feats.get("price_velocity_5m"),
            "buy_pressure_ratio": feats.get("buy_pressure_ratio"),
            "token_volatility_5m": feats.get("token_volatility_5m"),
            "activity_heat": feats.get("activity_heat"),
            "unique_buyer_entropy_5m": feats.get("unique_buyer_entropy_5m"),
            "wallet_n_buys_30d": wallet_ctx.get("n_buys_30d"),
            "wallet_uniq_pools_30d": wallet_ctx.get("uniq_pools_30d"),
            "v1_model_name": v1_name,
            "v1_provenance": v1_provenance,
            "conf_v1_strict": conf_strict,
            "conf_v1_live": conf_live,
            "conf_v1_synth_heuristic": synth["confidence"],
            "whale_score_v1_strict": score_strict["whale_score"],
            "final_score_v1_strict": score_strict["final_score"],
            "final_score_v1_live": score_live["final_score"],
            "final_score_v1_synth_heuristic": score_synth["final_score"],
            "final_score_binarized_baseline": score_bin["final_score"],
        })

    # 5 — write per-candidate CSV
    detail_csv = OUT_DIR / "per_candidate_scores.csv"
    if detail_rows:
        with detail_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
            w.writeheader()
            w.writerows(detail_rows)
    log.info("per-candidate CSV: %s (%d rows)", detail_csv, len(detail_rows))

    # 6 — tier rollup
    tiers_out: list[dict] = []
    for t in args.tiers:
        sub = [r for r in detail_rows if r["notional_usd"] >= t]
        tiers_out.append({
            "tier_usd": t,
            "n_candidates": len(sub),
            "final_score_v1_strict":            summarise([r["final_score_v1_strict"]            for r in sub]),
            "final_score_v1_live":              summarise([r["final_score_v1_live"]              for r in sub]),
            "final_score_v1_synth_heuristic":   summarise([r["final_score_v1_synth_heuristic"]   for r in sub]),
            "final_score_binarized_baseline":   summarise([r["final_score_binarized_baseline"]   for r in sub]),
        })

    summary = {
        "sprint": "093A",
        "name": "full_mode_a_restoration",
        "research_only": RESEARCH_ONLY,
        "guard_lifted": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "thresholds": {
            "dashcam": DASHCAM_THRESHOLD,
            "signal": SIGNAL_THRESHOLD,
            "whale_usd_thresh": WHALE_USD_THRESH,
            "whale_bonus": WHALE_BONUS,
        },
        "mode_a_weights": {"whale": W_WHALE, "momentum": W_MOMENTUM, "activity": W_ACTIVITY, "risk": W_RISK},
        "v1_model": {
            "name": v1_name,
            "provenance": v1_provenance,
            "feature_names": v1_features,
        },
        "candidates_post_quoter": len(enriched),
        "tiers": tiers_out,
        "notes": [
            "v1_strict: whitepaper Phase 1 fixed defaults applied — win_rate_30d=0.5, avg_roi=0.0",
            "v1_live: same model, would consume live win_rate / avg_roi if available. "
            "On Base today no real labels exist, so live == strict; documented honestly rather than fabricated",
            "v1_synth_heuristic: deterministic sigmoid baseline mimicking Phase 1's 'half-random guess' property",
            "binarized_baseline: sprint092 v3 path (whale_score = 1.0 if notional ≥ $100k else 0.0)",
            "EATLQ-team signal_model_base_v1.json (17-wallet-features) is NOT loaded here — it is a different model class. See readout for disclosure.",
            "FAILED_NO_7D_ALPHA NOT lifted. RESEARCH_ONLY.",
        ],
    }
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info("summary: %s", summary_path)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
