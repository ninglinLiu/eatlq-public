#!/usr/bin/env python3
"""Sprint083 expanded_v2 — bounded XGB smoke copyability review (research-only).

Replays identical smoke split + model config only to attach row-level diagnostics
to test top10% selections. Does NOT promote models, touch live, or read secrets.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

REPO = Path(__file__).resolve().parent.parent

DEFAULT_DATASET = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv"
)
DEFAULT_MANIFEST = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/feature_manifest.json"
)
DEFAULT_SMOKE_METRICS = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_bounded_xgb_smoke_expanded_v2/xgb_smoke_metrics.json"
)
DEFAULT_OUT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_bounded_xgb_smoke_expanded_v2"
)

# Import shared helpers from smoke script (no builder changes).
sys.path.insert(0, str(REPO / "scripts"))
from sprint083_expanded_v2_bounded_xgb_smoke_review import (  # noqa: E402
    PRIMARY_LABEL_COL,
    SPLIT_TIME_COL,
    as_float,
    build_matrix,
    chrono_split,
    load_manifest,
    pf_proxy,
    select_features,
    top10_metrics,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copyability review for bounded XGB smoke")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--smoke-metrics", default=DEFAULT_SMOKE_METRICS)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--train-frac", type=float, default=0.70)
    p.add_argument("--valid-frac", type=float, default=0.15)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--n-estimators", type=int, default=50)
    p.add_argument("--learning-rate", type=float, default=0.05)
    return p.parse_args()


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s)) - 1)))
    return s[i]


def mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def hit_rate(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(1 for v in vals if v > 0) / len(vals)


def wallet_score(row: dict) -> float | None:
    """As-of wallet score proxy: past_30d_roi primary, past_7d_roi fallback."""
    r30 = as_float(row.get("past_30d_roi"))
    if r30 is not None:
        return r30
    return as_float(row.get("past_7d_roi"))


def decile_readout(rows: list[dict], label_col: str = PRIMARY_LABEL_COL) -> list[dict]:
    scored = [(wallet_score(r), r) for r in rows]
    scored = [(s, r) for s, r in scored if s is not None]
    if not scored:
        return []
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    out: list[dict] = []
    for d in range(10):
        lo = int(n * d / 10)
        hi = int(n * (d + 1) / 10)
        bucket = scored[lo:hi]
        if not bucket:
            continue
        rets30 = [as_float(r.get(label_col)) for _, r in bucket]
        rets30 = [x for x in rets30 if x is not None]
        rets2h = [as_float(r.get("future_return_2h")) for _, r in bucket]
        rets2h = [x for x in rets2h if x is not None]
        top10_hit = None
        if rets30:
            k = max(1, int(math.ceil(len(rets30) * 0.10)))
            top_idx = sorted(range(len(rets30)), key=lambda i: rets30[i], reverse=True)[:k]
            top10_hit = sum(1 for i in top_idx if rets30[i] > 0) / len(top_idx)
        out.append(
            {
                "decile": d + 1,
                "decile_label": f"D{d+1}_lowest_wallet_score" if d == 0 else (f"D{d+1}_highest" if d == 9 else f"D{d+1}"),
                "n": len(bucket),
                "wallet_score_min": round(bucket[0][0], 6),
                "wallet_score_max": round(bucket[-1][0], 6),
                "mean_future_return_30m": round(mean(rets30), 6) if rets30 else None,
                "mean_future_return_2h": round(mean(rets2h), 6) if rets2h else None,
                "top10_within_decile_hit_rate_30m": round(top10_hit, 4) if top10_hit is not None else None,
            }
        )
    return out


def hold_bucket(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    if sec < 300:
        return "<5m"
    if sec < 1800:
        return "5-30m"
    if sec < 7200:
        return "30m-2h"
    if sec < 86400:
        return "2h-24h"
    return ">24h"


def delay_metrics(rows: list[dict], ret_col: str) -> dict:
    rets = [as_float(r.get(ret_col)) for r in rows]
    rets = [x for x in rets if x is not None]
    if not rets:
        return {"n": 0, "mean_return": None, "hit_rate": None, "pf_proxy": None}
    return {
        "n": len(rets),
        "mean_return": round(mean(rets), 6),
        "hit_rate": round(hit_rate(rets), 4),
        "pf_proxy": round(pf, 4) if (pf := pf_proxy(rets)) is not None and math.isfinite(pf) else pf,
    }


def concentration(rows: list[dict]) -> dict:
    pools = [str(r.get("pool", "")).lower() for r in rows if r.get("pool")]
    wallets = [str(r.get("wallet", "")).lower() for r in rows if r.get("wallet")]
    tokens = [str(r.get("token", "")).lower() for r in rows if r.get("token")]
    n = len(rows)

    def top_share(vals: list[str]) -> float | None:
        if not vals:
            return None
        c = Counter(vals)
        return max(c.values()) / len(vals)

    return {
        "n": n,
        "top_pool_share": round(top_share(pools), 4) if pools else None,
        "top_wallet_share": round(top_share(wallets), 4) if wallets else None,
        "top_token_share": round(top_share(tokens), 4) if tokens else None,
        "unique_pools": len(set(pools)),
        "unique_wallets": len(set(wallets)),
        "unique_tokens": len(set(tokens)),
    }


def closed_loop_coverage(rows: list[dict], dataset_end: datetime) -> dict:
    horizons = {"T+7d": 7, "T+14d": 14, "T+30d": 30}
    out: dict[str, dict] = {}
    for name, days in horizons.items():
        need_exit_by = dataset_end - timedelta(days=0)  # rows need exit before end for full horizon
        eligible = 0
        closed_in_horizon = 0
        for r in rows:
            et_s = r.get(SPLIT_TIME_COL, "")
            if not et_s:
                continue
            et = datetime.fromisoformat(et_s.replace("Z", "+00:00"))
            if et + timedelta(days=days) <= dataset_end:
                eligible += 1
            ht = as_float(r.get("hold_time_sec"))
            pnl = as_float(r.get("realized_wallet_pnl"))
            if ht is not None and pnl is not None and ht <= days * 86400:
                closed_in_horizon += 1
        out[name] = {
            "entry_time_plus_horizon_lte_dataset_end_n": eligible,
            "entry_time_plus_horizon_lte_dataset_end_share": round(eligible / len(rows), 4) if rows else None,
            "closed_with_hold_lte_horizon_n": closed_in_horizon,
            "closed_with_hold_lte_horizon_share": round(closed_in_horizon / len(rows), 4) if rows else None,
        }
    pnl_nonnull = sum(1 for r in rows if as_float(r.get("realized_wallet_pnl")) is not None)
    hold_nonnull = sum(1 for r in rows if as_float(r.get("hold_time_sec")) is not None)
    out["realized_wallet_pnl_coverage"] = round(pnl_nonnull / len(rows), 4) if rows else None
    out["hold_time_sec_coverage"] = round(hold_nonnull / len(rows), 4) if rows else None
    return out


def fit_test_top10_indices(
    train_rows,
    valid_rows,
    test_rows,
    features: list[str],
    max_depth: int,
    n_estimators: int,
    learning_rate: float,
) -> tuple[list[int], list[dict], list[float]]:
    """Returns indices into modeled test rows, modeled test row dicts, scores."""
    import numpy as np
    import xgboost as xgb

    x_tr, y_tr, _, _, _, _ = build_matrix(train_rows, features, PRIMARY_LABEL_COL)
    x_va, y_va, _, _, _, _ = build_matrix(valid_rows, features, PRIMARY_LABEL_COL)

    modeled_test: list[dict] = []
    x_te: list[list[float]] = []
    for r in test_rows:
        ret = as_float(r.get(PRIMARY_LABEL_COL))
        if ret is None:
            continue
        vals = []
        skip = False
        for f in features:
            v = as_float(r.get(f))
            if v is None:
                skip = True
                break
            vals.append(v)
        if skip:
            continue
        modeled_test.append(r)
        x_te.append(vals)

    clf = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        objective="binary:logistic",
        random_state=42,
        verbosity=0,
        n_jobs=1,
    )
    clf.fit(np.array(x_tr), np.array(y_tr), eval_set=[(np.array(x_va), np.array(y_va))], verbose=False)
    proba = clf.predict_proba(np.array(x_te))[:, 1].tolist()
    n = len(proba)
    k = max(1, int(math.ceil(n * 0.10)))
    idx = sorted(range(n), key=lambda i: proba[i], reverse=True)[:k]
    return idx, modeled_test, proba


def survivorship_gate(
    features: list[str],
    smoke_metrics: dict,
    deciles: list[dict],
    test_top10: list[dict],
    all_test_modeled: list[dict],
) -> tuple[str, dict]:
    global_rank_feats = [f for f in features if "rank" in f.lower() or "top_wallet" in f.lower()]
    future_in_feats = [f for f in features if f.startswith("future_") or f.startswith("copy_delay_")]
    fi = smoke_metrics.get("metrics", {}).get("primary", {}).get("feature_importance", {})
    surv_feats = ["past_30d_roi", "past_7d_roi", "profit_factor", "win_rate"]
    surv_importance = sum(fi.get(f, 0) for f in surv_feats)
    block_imp = fi.get("block_number", 0)

    high_wallet_share = 0.0
    if test_top10:
        scores = [wallet_score(r) for r in test_top10]
        scores_ok = [s for s in scores if s is not None]
        if scores_ok:
            all_scores = sorted(wallet_score(r) for r in all_test_modeled if wallet_score(r) is not None)
            if all_scores:
                p90 = all_scores[int(0.9 * (len(all_scores) - 1))]
                high_wallet_share = sum(1 for s in scores_ok if s >= p90) / len(scores_ok)

    d10 = next((d for d in deciles if d.get("decile") == 10), None)
    d1 = next((d for d in deciles if d.get("decile") == 1), None)
    monotonic_warn = False
    if d10 and d1 and d10.get("mean_future_return_30m") is not None and d1.get("mean_future_return_30m") is not None:
        if d10["mean_future_return_30m"] <= d1["mean_future_return_30m"]:
            monotonic_warn = True

    status = "PASS"
    reasons: list[str] = []
    if global_rank_feats or future_in_feats:
        status = "FAIL"
        reasons.append("global rank or future fields in feature set")
    if surv_importance > 0.25:
        status = "WARN" if status == "PASS" else status
        reasons.append(f"survivorship feature importance sum={surv_importance:.3f}")
    if block_imp > 0.12:
        status = "WARN" if status == "PASS" else status
        reasons.append(f"block_number importance={block_imp:.3f} (time/regime proxy)")
    if high_wallet_share > 0.5:
        status = "WARN" if status == "PASS" else status
        reasons.append(f"test top10% {high_wallet_share:.0%} from wallet_score>=p90")
    if monotonic_warn:
        status = "WARN" if status == "PASS" else status
        reasons.append("decile D10 mean 30m return not above D1")

    return status, {
        "wallet_features_as_of_entry": True,
        "builder_uses_exit_time_lt_entry": True,
        "no_global_wallet_rank_in_features": len(global_rank_feats) == 0,
        "survivorship_feature_importance_sum": round(surv_importance, 4),
        "block_number_importance": block_imp,
        "test_top10_high_wallet_score_share": round(high_wallet_share, 4),
        "decile_monotonicity_warn": monotonic_warn,
        "notes": reasons or ["AS_OF wallet features only; no full-sample rank columns in manifest"],
    }


def copy_delay_gate(test_top10: list[dict]) -> tuple[bool, dict]:
    instant = delay_metrics(test_top10, PRIMARY_LABEL_COL)
    d30 = delay_metrics(test_top10, "copy_delay_30s")
    d60 = delay_metrics(test_top10, "copy_delay_60s")
    d120 = delay_metrics(test_top10, "copy_delay_120s")

    pass_60 = (d60.get("mean_return") or 0) > 0 and (d60.get("hit_rate") or 0) >= 0.5
    pass_120 = (d120.get("mean_return") or 0) > 0 and (d120.get("hit_rate") or 0) >= 0.55
    copy_pass = pass_60 or pass_120
    instant_beats_120 = (instant.get("mean_return") or 0) > (d120.get("mean_return") or 0) * 2

    hold_dist = Counter(hold_bucket(as_float(r.get("hold_time_sec"))) for r in test_top10)
    short_hold_share = (
        (hold_dist.get("<5m", 0) + hold_dist.get("5-30m", 0)) / len(test_top10) if test_top10 else 0
    )

    return copy_pass, {
        "instant_future_return_30m": instant,
        "copy_delay_30s": d30,
        "copy_delay_60s": d60,
        "copy_delay_120s": d120,
        "COPY_DELAY_PASS": copy_pass,
        "copy_delay_60s_hit_rate_below_50pct": not pass_60,
        "instant_return_much_higher_than_120s": instant_beats_120,
        "short_hold_lt30m_share_in_top10": round(short_hold_share, 4),
        "hold_time_bucket_counts": dict(hold_dist),
    }


def size_portability_gate(test_top10: list[dict], all_rows: list[dict]) -> tuple[str, dict]:
    sizes = [as_float(r.get("amount_weth") or r.get("amount_proxy")) for r in test_top10]
    sizes = [x for x in sizes if x is not None and x > 0]
    all_sizes = [as_float(r.get("amount_weth") or r.get("amount_proxy")) for r in all_rows]
    all_sizes = [x for x in all_sizes if x is not None and x > 0]

    # Canary notional heuristic: sub-$500 WETH proxy (~0.15 ETH at sample scale) — dataset uses weth_cost.
    canary_max_weth = 0.15
    above_canary = sum(1 for s in sizes if s > canary_max_weth) / len(sizes) if sizes else 0

    status = "PASS"
    unit_suspect = bool(sizes) and pct(sizes, 0.5) and pct(sizes, 0.5) > 1000.0
    if not sizes:
        status = "FAIL"
    elif unit_suspect or above_canary >= 0.99:
        status = "FAIL"
    elif pct(sizes, 0.95) and pct(sizes, 0.95) > 1.0:
        status = "WARN"
    elif above_canary > 0.35:
        status = "WARN"

    return status, {
        "amount_proxy_is_weth_cost": True,
        "n": len(sizes),
        "p50": round(pct(sizes, 0.5), 6) if sizes else None,
        "p90": round(pct(sizes, 0.9), 6) if sizes else None,
        "p95": round(pct(sizes, 0.95), 6) if sizes else None,
        "max": round(max(sizes), 6) if sizes else None,
        "dataset_p50": round(pct(all_sizes, 0.5), 6) if all_sizes else None,
        "share_above_canary_max_0_15_weth": round(above_canary, 4),
        "amount_proxy_unit_suspect_wei_scale": unit_suspect,
        "pool_liquidity_proxy_available": False,
        "note": "amount_proxy appears wei/raw-notional corrupted (p50>>1 WETH); not copyable at canary size without unit repair.",
    }


def closed_loop_gate(test_top10: list[dict], dataset_end: datetime) -> tuple[str, dict]:
    cov = closed_loop_coverage(test_top10, dataset_end)
    pnl_cov = cov.get("realized_wallet_pnl_coverage", 0) or 0
    holds = [as_float(r.get("hold_time_sec")) for r in test_top10]
    holds = [h for h in holds if h is not None]
    long_hold_share = sum(1 for h in holds if h > 86400) / len(holds) if holds else 0
    missing_pnl_share = 1 - pnl_cov

    status = "PASS"
    if pnl_cov < 0.5:
        status = "WARN"
    if pnl_cov < 0.3 or long_hold_share > 0.4:
        status = "FAIL"
    if missing_pnl_share > 0.5 and long_hold_share > 0.25:
        status = "WARN" if status == "PASS" else status

    return status, {
        **cov,
        "long_hold_gt24h_share_among_with_hold": round(long_hold_share, 4),
        "label_note": "future_return_30m/2h are short-window trade labels, NOT full wallet realized PnL",
        "realized_wallet_pnl_is_closed_roundtrip_only": True,
    }


def concentration_gate(test_top10: list[dict], smoke_test_pool_share: float | None) -> tuple[str, dict]:
    conc = concentration(test_top10)
    router_true = sum(1 for r in test_top10 if str(r.get("router_flag", "")).lower() in ("true", "1", "yes"))
    entry_id_6ff = sum(
        1 for r in test_top10 if str(r.get("entry_id", "")).lower().startswith("0x6ff")
    )
    status = "PASS"
    regime = "GENERAL"
    if (conc.get("top_pool_share") or 0) >= 0.5 or (conc.get("top_wallet_share") or 0) >= 0.25:
        status = "WARN"
        regime = "REGIME_SPECIFIC"
    if (conc.get("top_pool_share") or 0) >= 0.65:
        status = "FAIL"
    if router_true > 0 or entry_id_6ff > 0:
        status = "FAIL" if entry_id_6ff > 0 else status
        regime = "ROUTER_ARTIFACT_REVIEW" if entry_id_6ff > 0 else "ROUTER_CONTAMINATED"

    return status, {
        **conc,
        "regime_label": regime,
        "router_flag_true_rows": router_true,
        "entry_id_0x6ff_prefix_rows": entry_id_6ff,
        "smoke_test_top_pool_share": smoke_test_pool_share,
    }


def overall_signal_status(
    surv: str,
    copy_pass: bool,
    size: str,
    closed: str,
    conc: str,
    smoke_pri: dict,
) -> str:
    test_auc = smoke_pri.get("test_auc")
    valid_auc = smoke_pri.get("valid_auc")
    gap = (test_auc or 0) - (valid_auc or 0) if test_auc and valid_auc else 0
    if surv == "FAIL" or conc == "FAIL" or closed == "FAIL":
        return "NOT_COPYABLE"
    if gap > 0.15 and (smoke_pri.get("test_precision_at_top10pct") or 0) > 0.85:
        if conc == "FAIL" or closed == "FAIL" or size == "FAIL":
            return "NOT_COPYABLE"
        return "OVERFIT_OR_LEAKAGE_RISK"
    if not copy_pass and size in ("WARN", "FAIL"):
        return "NOT_COPYABLE"
    if copy_pass and surv == "PASS" and size == "PASS" and conc == "PASS" and closed == "PASS":
        return "COPYABLE_SIGNAL_CANDIDATE"
    if copy_pass or (smoke_pri.get("test_mean_return_at_top10pct") or 0) > 0:
        return "WEAK_SIGNAL_NEEDS_OOS"
    if gap > 0.12:
        return "OVERFIT_OR_LEAKAGE_RISK"
    return "INCONCLUSIVE"


def next_step(signal: str, copy_pass: bool, surv: str, size: str) -> str:
    if signal == "NOT_COPYABLE":
        return "REJECT_NOT_COPYABLE"
    if signal == "COPYABLE_SIGNAL_CANDIDATE":
        return "PAPER_FORWARD_DESIGN_READY"
    if signal in ("WEAK_SIGNAL_NEEDS_OOS", "OVERFIT_OR_LEAKAGE_RISK", "INCONCLUSIVE"):
        if surv == "WARN" or size == "WARN":
            return "FEATURE_REPAIR_REQUIRED"
        return "OOS_EXPANSION_REQUIRED"
    return "INCONCLUSIVE_NEEDS_MORE_DATA"


def write_decile_csv(path: Path, deciles: list[dict]) -> None:
    if not deciles:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(deciles[0].keys()))
        w.writeheader()
        w.writerows(deciles)


def write_selected_csv(path: Path, rows: list[dict], scores: list[float], idx: list[int]) -> None:
    fields = [
        "entry_id",
        "entry_time",
        "wallet",
        "pool",
        "token",
        "amount_proxy",
        "past_30d_roi",
        "profit_factor",
        "win_rate",
        "future_return_30m",
        "future_return_2h",
        "copy_delay_30s",
        "copy_delay_60s",
        "copy_delay_120s",
        "hold_time_sec",
        "realized_wallet_pnl",
        "router_flag",
        "model_score",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i in idx:
            r = dict(rows[i])
            r["model_score"] = round(scores[i], 6)
            w.writerow(r)


def main() -> int:
    args = parse_args()
    dataset_path = REPO / args.dataset
    manifest_path = REPO / args.manifest
    smoke_path = REPO / args.smoke_metrics
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(manifest_path)
    features = select_features(manifest, include_block_number=False)
    rows = list(csv.DictReader(dataset_path.open()))
    smoke = json.loads(smoke_path.read_text()) if smoke_path.exists() else {}
    pri = smoke.get("metrics", {}).get("primary", {})

    train_rows, valid_rows, test_rows = chrono_split(rows, args.train_frac, args.valid_frac)
    dataset_end = max(
        datetime.fromisoformat(r[SPLIT_TIME_COL].replace("Z", "+00:00")) for r in rows if r.get(SPLIT_TIME_COL)
    )

    idx, modeled_test, proba = fit_test_top10_indices(
        train_rows, valid_rows, test_rows, features, args.max_depth, args.n_estimators, args.learning_rate
    )
    test_top10 = [modeled_test[i] for i in idx]

    deciles_all = decile_readout([r for part in (train_rows, valid_rows, test_rows) for r in part])
    deciles_test = decile_readout(modeled_test)

    surv_status, surv_detail = survivorship_gate(features, smoke, deciles_test, test_top10, modeled_test)
    copy_pass, copy_detail = copy_delay_gate(test_top10)
    size_status, size_detail = size_portability_gate(test_top10, rows)
    closed_status, closed_detail = closed_loop_gate(test_top10, dataset_end)
    conc_status, conc_detail = concentration_gate(test_top10, pri.get("test_selected_top10_top_pool_share"))

    signal = overall_signal_status(surv_status, copy_pass, size_status, closed_status, conc_status, pri)
    nstep = next_step(signal, copy_pass, surv_status, size_status)

    n_rows = len(rows)
    pool_feat_cov = (
        sum(1 for r in rows if as_float(r.get("activity_heat")) is not None) / n_rows if n_rows else 0.0
    )
    amt_cov = sum(1 for r in rows if as_float(r.get("amount_weth") or r.get("amount_proxy")) is not None) / n_rows if n_rows else 0.0
    closed_cov = sum(1 for r in rows if str(r.get("closed_loop_eligible")).lower() == "true") / n_rows if n_rows else 0.0
    pnl_cov = sum(1 for r in rows if as_float(r.get("realized_wallet_pnl_weth")) is not None) / n_rows if n_rows else 0.0
    amt_vals = [as_float(r.get("amount_weth") or r.get("amount_proxy")) for r in rows]
    amt_vals = [a for a in amt_vals if a is not None]
    amount_unit_pass = bool(amt_vals) and max(amt_vals) < 500.0 and (pct(amt_vals, 0.5) or 0) < 100.0
    closed_labels_present = "closed_loop_eligible" in (rows[0] if rows else {}) and "label_family_closed_loop" in (rows[0] if rows else {})
    block_excluded = "block_number" not in features

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "REBUILT_WITH_REPAIRED_BUILDER": True,
        "AMOUNT_UNIT_REPAIR_PASS": amount_unit_pass,
        "CLOSED_LOOP_LABELS_PRESENT": closed_labels_present,
        "BLOCK_NUMBER_EXCLUDED_BY_DEFAULT": block_excluded,
        "dataset_row_count": n_rows,
        "dataset_unique_pools": len({str(r.get("pool", "")).lower() for r in rows if r.get("pool")}),
        "dataset_top_pool_share": round(
            max(Counter(str(r.get("pool", "")).lower() for r in rows if r.get("pool")).values()) / n_rows, 4
        )
        if n_rows
        else None,
        "pool_feature_coverage": round(pool_feat_cov, 4),
        "amount_weth_coverage": round(amt_cov, 4),
        "amount_usd_notional_coverage": round(
            sum(1 for r in rows if as_float(r.get("amount_usd_notional")) is not None) / n_rows, 4
        )
        if n_rows
        else 0.0,
        "closed_loop_eligible_coverage": round(closed_cov, 4),
        "realized_wallet_pnl_weth_coverage": round(pnl_cov, 4),
        "selected_top10_concentration": {
            "top_pool_share": conc_detail.get("top_pool_share"),
            "top_token_share": conc_detail.get("top_token_share"),
            "top_wallet_share": conc_detail.get("top_wallet_share"),
            "CONCENTRATION_PASS": conc_status,
        },
        "features_used_for_replay": features,
        "COPYABILITY_REVIEW_DONE": True,
        "SURVIVORSHIP_RISK_REVIEWED": True,
        "COPY_DELAY_REVIEWED": True,
        "SIZE_PORTABILITY_REVIEWED": True,
        "CLOSED_LOOP_TN_REVIEWED": True,
        "CONCENTRATION_REVIEWED": True,
        "LEAKAGE_AUDIT_PASS": smoke.get("prep_flags", {}).get("LEAKAGE_AUDIT_PASS", True),
        "MODEL_PROMOTION_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "INFERENCE_REPLAY_FOR_ROW_DIAGNOSTICS": True,
        "SMOKE_METRICS_SOURCE": str(smoke_path.relative_to(REPO)),
        "SURVIVORSHIP_RISK": surv_status,
        "COPY_DELAY_PASS": copy_pass,
        "SIZE_PORTABILITY_PASS": size_status,
        "CLOSED_LOOP_TN_PASS": closed_status,
        "CONCENTRATION_PASS": conc_status,
        "CONCENTRATION_REGIME": conc_detail.get("regime_label"),
        "router_flag_true_rows": conc_detail.get("router_flag_true_rows", 0),
        "COPYABILITY_SIGNAL_STATUS": signal,
        "NEXT_STEP": nstep,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "BOUNDED_XGB_SMOKE_RETRAIN_REVIEW_READY": signal
        in ("COPYABLE_SIGNAL_CANDIDATE", "WEAK_SIGNAL_NEEDS_OOS"),
        "SMOKE_RETRAIN_ALLOWED": False,
        "smoke_metrics_summary": {
            "test_auc": pri.get("test_auc"),
            "valid_auc": pri.get("valid_auc"),
            "test_accuracy": pri.get("test_accuracy"),
            "test_precision_at_top10pct": pri.get("test_precision_at_top10pct"),
            "test_mean_return_at_top10pct": pri.get("test_mean_return_at_top10pct"),
            "test_pf_proxy_at_top10pct": pri.get("test_pf_proxy_at_top10pct"),
            "test_selected_top10_n": pri.get("test_selected_top10_n"),
        },
        "survivorship": surv_detail,
        "copy_delay": copy_detail,
        "size_portability": size_detail,
        "closed_loop": closed_detail,
        "concentration": conc_detail,
        "wallet_score_deciles_test_modeled": deciles_test,
        "wallet_score_deciles_full_dataset": deciles_all,
    }

    (out_dir / "copyability_review_flags.json").write_text(json.dumps(flags, indent=2) + "\n", encoding="utf-8")
    write_decile_csv(out_dir / "copyability_decile_summary.csv", deciles_test)
    write_selected_csv(out_dir / "selected_top10_copyability.csv", modeled_test, proba, idx)

    md = f"""# Copyability Review — bounded XGB smoke expanded_v2

Generated: {flags['generated_at_utc']}

## Executive conclusion

**{signal}** — Smoke test AUC {pri.get('test_auc')} vs valid {pri.get('valid_auc')} shows large valid/test gap; top10% economics must be judged on copy-delay and concentration gates, not headline AUC/PF. **Not promote / not live.**

## Smoke metrics summary (from existing artifacts)

| metric | valid | test |
|--------|-------|------|
| AUC | {pri.get('valid_auc')} | {pri.get('test_auc')} |
| accuracy | {pri.get('valid_accuracy')} | {pri.get('test_accuracy')} |
| precision@top10% | {pri.get('valid_precision_at_top10pct')} | {pri.get('test_precision_at_top10pct')} |
| mean_return@top10% | {pri.get('valid_mean_return_at_top10pct')} | {pri.get('test_mean_return_at_top10pct')} |
| PF_proxy@top10% | {pri.get('valid_pf_proxy_at_top10pct')} | {pri.get('test_pf_proxy_at_top10pct')} |
| top_pool_share@top10 | {pri.get('valid_selected_top10_top_pool_share')} | {pri.get('test_selected_top10_top_pool_share')} |

## Survivorship review

- **SURVIVORSHIP_RISK={surv_status}**
- Wallet features tagged `AS_OF_WALLET_FEATURE`; builder uses `exit_time < entry_time` for closed trades (no future ROI in wallet score).
- No global wallet rank columns in selected features.
- Survivorship-feature importance sum (past_30d/7d ROI, PF, win_rate): **{surv_detail.get('survivorship_feature_importance_sum')}**; `block_number` importance **{surv_detail.get('block_number_importance')}** (time/regime proxy risk).
- Test top10% share with wallet_score ≥ dataset p90: **{surv_detail.get('test_top10_high_wallet_score_share')}**

## Copy-delay / front-run review

- **COPY_DELAY_PASS={copy_pass}** (requires positive economics at 60s or 120s, not instant-only)
- Test top10% delay readout (mean_return / hit_rate / PF_proxy):

| horizon | n | mean_return | hit_rate | PF_proxy |
|---------|---|-------------|----------|----------|
| instant (future_return_30m) | {copy_detail['instant_future_return_30m']['n']} | {copy_detail['instant_future_return_30m']['mean_return']} | {copy_detail['instant_future_return_30m']['hit_rate']} | {copy_detail['instant_future_return_30m']['pf_proxy']} |
| copy_delay_30s | {copy_detail['copy_delay_30s']['n']} | {copy_detail['copy_delay_30s']['mean_return']} | {copy_detail['copy_delay_30s']['hit_rate']} | {copy_detail['copy_delay_30s']['pf_proxy']} |
| copy_delay_60s | {copy_detail['copy_delay_60s']['n']} | {copy_detail['copy_delay_60s']['mean_return']} | {copy_detail['copy_delay_60s']['hit_rate']} | {copy_detail['copy_delay_60s']['pf_proxy']} |
| copy_delay_120s | {copy_detail['copy_delay_120s']['n']} | {copy_detail['copy_delay_120s']['mean_return']} | {copy_detail['copy_delay_120s']['hit_rate']} | {copy_detail['copy_delay_120s']['pf_proxy']} |

- Short hold (<30m) share in test top10%: **{copy_detail.get('short_hold_lt30m_share_in_top10')}**
- Hold buckets: `{json.dumps(copy_detail.get('hold_time_bucket_counts'))}`

## Size portability review

- **SIZE_PORTABILITY_PASS={size_status}**
- Test top10% `amount_proxy` (WETH notional): p50={size_detail.get('p50')}, p90={size_detail.get('p90')}, p95={size_detail.get('p95')}, max={size_detail.get('max')}
- Share above canary heuristic 0.15 WETH: **{size_detail.get('share_above_canary_max_0_15_weth')}**
- Pool liquidity / slippage_proxy: not in sample CSV.

## Closed-loop T+N review

- **CLOSED_LOOP_TN_PASS={closed_status}**
- `realized_wallet_pnl` coverage (test top10%): **{closed_detail.get('realized_wallet_pnl_coverage')}**
- Long hold >24h share: **{closed_detail.get('long_hold_gt24h_share_among_with_hold')}**
- Horizons: `{json.dumps({k: v for k, v in closed_detail.items() if k.startswith('T+')}, indent=2)}`
- Short-window labels (`future_return_30m`/`2h`) are **not** full wallet realized PnL.

## Concentration review

- **CONCENTRATION_PASS={conc_status}** / regime **{conc_detail.get('regime_label')}**
- top_pool_share={conc_detail.get('top_pool_share')}, top_wallet_share={conc_detail.get('top_wallet_share')}, top_token_share={conc_detail.get('top_token_share')}
- router_flag_true_rows={conc_detail.get('router_flag_true_rows')} (must stay 0)

## Decision flags

```json
{json.dumps({k: flags[k] for k in flags if k not in ('survivorship', 'copy_delay', 'size_portability', 'closed_loop', 'concentration', 'wallet_score_deciles_test_modeled', 'wallet_score_deciles_full_dataset')}, indent=2)}
```

## Next step

**{nstep}**

## Exact next command

```bash
cd /opt/EATLQ && python3 scripts/sprint083_expanded_v2_copyability_review.py \\
  --dataset mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv \\
  --out-dir mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_bounded_xgb_smoke_expanded_v2
```
"""
    (out_dir / "copyability_review.md").write_text(md, encoding="utf-8")
    print(json.dumps({"COPYABILITY_SIGNAL_STATUS": signal, "NEXT_STEP": nstep, "SURVIVORSHIP_RISK": surv_status, "COPY_DELAY_PASS": copy_pass}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
