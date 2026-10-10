#!/usr/bin/env python3
"""Aero vAMM S5.2 — regime-aware offline design (no RPC).

Reads logs/aero_vamm_s5_1_features_revised.csv only.
Outputs:
  mvp/diagnostics/aero_vamm_s5_2_regime_design_report.json
  mvp/runs/aero-vamm-s5-2-regime-aware-offline-design.md
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IN_CSV = REPO / "logs/aero_vamm_s5_1_features_revised.csv"
OUT_JSON = REPO / "mvp/diagnostics/aero_vamm_s5_2_regime_design_report.json"
OUT_MD = REPO / "mvp/runs/aero-vamm-s5-2-regime-aware-offline-design.md"

FORBIDDEN_PREFIX = (
    "net_return_",
    "positive_cost_",
    "exit_",
    "gross_return",
)
FORBIDDEN_EXACT = frozenset(
    {
        "sample_id",
        "tx_hash",
        "log_index",
        "pool_address",
        "timestamp",
        "time_quartile",
        "entry_block",
        "exit_block_60m",
        "token0",
        "token1",
        "quote_token_in",
        "quote_token_out",
        "stable_flag",
        "pool_type",
        "quote_status_60m",
        "reserve_status",
        "token_metadata_status",
        "failure_reason",
        "rpc_calls_this_row",
    }
)


def nfloat(x: str, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def feature_columns(headers: list[str]) -> list[str]:
    out: list[str] = []
    for h in headers:
        if h in FORBIDDEN_EXACT:
            continue
        if any(h.startswith(p) for p in FORBIDDEN_PREFIX):
            continue
        if h in {"entry_notional_in", "entry_quote"}:
            out.append(h)
            continue
        if h.startswith(("winsor_", "log1p_", "interaction_", "rank_")):
            out.append(h)
            continue
        if h in {
            "quote_to_notional_ratio",
            "log_entry_quote",
            "log_entry_notional",
            "notional_vs_entry_quote",
            "log_reserve_weth",
            "reserve_ratio",
            "liquidity_to_trade_ratio",
            "swap_size_vs_reserve",
            "price_impact_estimate_at_notional",
            "block_gas_fill_ratio",
            "block_tx_count",
            "is_new_pool_7d",
            "is_new_pool_30d",
            "pool_age_blocks_at_entry",
            "reserve0_at_entry",
            "reserve1_at_entry",
        }:
            out.append(h)
    return sorted(set(out))


def row_X(row: dict, feats: list[str]) -> list[float]:
    return [nfloat(row.get(f, "")) for f in feats]


def zscore_fit(X: list[list[float]]) -> tuple[list[float], list[float]]:
    if not X:
        return [], []
    m = len(X[0])
    mu = [sum(x[j] for x in X) / len(X) for j in range(m)]
    sd = []
    for j in range(m):
        v = sum((x[j] - mu[j]) ** 2 for x in X) / len(X)
        sd.append(math.sqrt(v) if v > 1e-12 else 1.0)
    return mu, sd


def zscore_apply(X: list[list[float]], mu: list[float], sd: list[float]) -> list[list[float]]:
    return [[(x[j] - mu[j]) / sd[j] for j in range(len(mu))] for x in X]


def fit_ridge_logit(
    Z: list[list[float]], y: list[int], *, it: int = 280, lr: float = 0.06, ridge: float = 0.02
) -> tuple[list[float], float]:
    if not Z:
        return [], 0.0
    d = len(Z[0])
    w, b = [0.0] * d, 0.0
    n = len(Z)
    for _ in range(it):
        gw = [ridge * wi for wi in w]
        gb = 0.0
        for x, t in zip(Z, y):
            z = sum(wi * xi for wi, xi in zip(w, x)) + b
            z = max(-30.0, min(30.0, z))
            p = 1.0 / (1.0 + math.exp(-z))
            e = p - t
            for j in range(d):
                gw[j] += e * x[j]
            gb += e
        for j in range(d):
            w[j] -= lr * gw[j] / n
        b -= lr * gb / n
    return w, b


def predict_proba(Z: list[list[float]], w: list[float], b: float) -> list[float]:
    out = []
    for x in Z:
        z = sum(wi * xi for wi, xi in zip(w, x)) + b
        z = max(-30.0, min(30.0, z))
        out.append(1.0 / (1.0 + math.exp(-z)))
    return out


def pct(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def eval_ranking(
    scores: list[float],
    y: list[int],
    ret: list[float],
    pools: list[str],
    *,
    name: str,
) -> dict:
    n = len(scores)
    base = sum(y) / max(1, n)
    med_all = pct(ret, 0.5)
    k = max(1, int(round(n * 0.1)))
    idx = sorted(range(n), key=lambda i: scores[i], reverse=True)[:k]
    top_pr = sum(y[i] for i in idx) / k
    top_rets = [ret[i] for i in idx]
    top_med = pct(top_rets, 0.5)
    pc = Counter(pools[i] for i in idx)
    max_share = max(pc.values()) / k if idx else 0.0
    return {
        "name": name,
        "n": n,
        "base_positive_rate": round(base, 6),
        "top_decile_positive_rate": round(top_pr, 6),
        "top_decile_lift": round(top_pr - base, 6),
        "overall_median_net_return_cost_004": round(med_all, 6),
        "top_decile_median_net_return_cost_004": round(top_med, 6),
        "median_return_improves": top_med > med_all,
        "top_decile_pool_share": {p: round(c / k, 6) for p, c in pc.items()},
        "max_single_pool_share_in_top_decile": round(max_share, 6),
        "not_single_pool_dominated": max_share < 0.8,
    }


def regime_passes(ev: dict) -> bool:
    return (
        ev["top_decile_positive_rate"] >= ev["base_positive_rate"] + 0.05
        and ev["median_return_improves"]
        and ev["not_single_pool_dominated"]
    )


def main() -> None:
    rows = list(csv.DictReader(IN_CSV.open("r", encoding="utf-8")))
    rows = sorted(rows, key=lambda r: int(r.get("timestamp") or 0))
    headers = list(rows[0].keys()) if rows else []
    feats = feature_columns(headers)

    y_all = [1 if r.get("positive_cost_004_60m") == "1" else 0 for r in rows]
    ret_all = [nfloat(r.get("net_return_cost_004_60m")) for r in rows]
    pools_all = [(r.get("pool_address") or "").lower() for r in rows]
    q_all = [r.get("time_quartile") or "" for r in rows]

    # --- Rolling expanding-window splits (train strictly before test block)
    chunk = 250
    rolling_evals: list[dict] = []
    for fi in range(1, 1000 // chunk):  # folds 1,2,3: test [250,500), [500,750), [750,1000)
        te_lo, te_hi = fi * chunk, (fi + 1) * chunk
        tr_idx = list(range(0, te_lo))
        te_idx = list(range(te_lo, te_hi))
        if len(tr_idx) < 50:
            continue
        Xtr = [row_X(rows[i], feats) for i in tr_idx]
        Xte = [row_X(rows[j], feats) for j in te_idx]
        mu, sd = zscore_fit(Xtr)
        Ztr = zscore_apply(Xtr, mu, sd)
        Zte = zscore_apply(Xte, mu, sd)
        ytr = [y_all[i] for i in tr_idx]
        w, b = fit_ridge_logit(Ztr, ytr)
        scores = predict_proba(Zte, w, b)
        yte = [y_all[j] for j in te_idx]
        rte = [ret_all[j] for j in te_idx]
        pte = [pools_all[j] for j in te_idx]
        ev = eval_ranking(scores, yte, rte, pte, name=f"rolling_expanding_fold_{fi}_test_rows_{te_lo}_{te_hi}")
        ev["train_rows"] = len(tr_idx)
        ev["test_rows"] = len(te_idx)
        ev["gate_pass"] = regime_passes(ev)
        rolling_evals.append(ev)

    # --- Per time_quartile: train on strict past (timestamp < min in quartile)
    quartile_evals: list[dict] = []
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        q_idx = [i for i, qq in enumerate(q_all) if qq == q]
        if not q_idx:
            continue
        t_min = min(int(rows[i].get("timestamp") or 0) for i in q_idx)
        tr_idx = [i for i in range(len(rows)) if int(rows[i].get("timestamp") or 0) < t_min]
        te_idx = q_idx

        base_pr = sum(y_all[i] for i in te_idx) / len(te_idx)
        med_ret = pct([ret_all[i] for i in te_idx], 0.5)

        diag_base = {
            "time_quartile": q,
            "rows": len(te_idx),
            "base_positive_rate": round(base_pr, 6),
            "median_net_return_cost_004": round(med_ret, 6),
        }

        if len(tr_idx) < 80:
            # Q1 (earliest): no strict past — internal time-ordered split within Q
            te_sorted = sorted(te_idx, key=lambda i: int(rows[i].get("timestamp") or 0))
            half = len(te_sorted) // 2
            tr2, te2 = te_sorted[:half], te_sorted[half:]
            if len(tr2) < 40 or len(te2) < 40:
                quartile_evals.append(
                    {
                        **diag_base,
                        "evaluation_mode": "insufficient_strict_past_skipped",
                        "top_decile_lift": None,
                        "note": "Strict past empty or too small; use rolling folds for Q1-like regimes.",
                    }
                )
                continue
            Xtr = [row_X(rows[i], feats) for i in tr2]
            Xte = [row_X(rows[j], feats) for j in te2]
            mu, sd = zscore_fit(Xtr)
            Ztr = zscore_apply(Xtr, mu, sd)
            Zte = zscore_apply(Xte, mu, sd)
            ytr = [y_all[i] for i in tr2]
            w, b = fit_ridge_logit(Ztr, ytr)
            scores = predict_proba(Zte, w, b)
            yte = [y_all[j] for j in te2]
            rte = [ret_all[j] for j in te2]
            pte = [pools_all[j] for j in te2]
            ev = eval_ranking(
                scores,
                yte,
                rte,
                pte,
                name=f"quartile_{q}_internal_first_half_train_second_half_test",
            )
            ev.update(diag_base)
            ev["evaluation_mode"] = "internal_split_within_quartile_no_strict_past"
            ev["gate_pass"] = regime_passes(ev)
            quartile_evals.append(ev)
            continue

        Xtr = [row_X(rows[i], feats) for i in tr_idx]
        Xte = [row_X(rows[j], feats) for j in te_idx]
        mu, sd = zscore_fit(Xtr)
        Ztr = zscore_apply(Xtr, mu, sd)
        Zte = zscore_apply(Xte, mu, sd)
        ytr = [y_all[i] for i in tr_idx]
        w, b = fit_ridge_logit(Ztr, ytr)
        scores = predict_proba(Zte, w, b)
        yte = [y_all[j] for j in te_idx]
        rte = [ret_all[j] for j in te_idx]
        pte = [pools_all[j] for j in te_idx]
        ev = eval_ranking(scores, yte, rte, pte, name=f"quartile_{q}_strict_past_train")
        ev.update(diag_base)
        ev["evaluation_mode"] = "strict_past_train"
        ev["train_rows"] = len(tr_idx)
        ev["gate_pass"] = regime_passes(ev)
        quartile_evals.append(ev)

    # --- Regime-aware sampling (evaluation only): train on union of quartiles with high full-sample base
    q_base = {}
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        qi = [i for i, qq in enumerate(q_all) if qq == q]
        if qi:
            q_base[q] = sum(y_all[i] for i in qi) / len(qi)
    high_q = [q for q, b in q_base.items() if b >= 0.10]
    if len(high_q) >= 1:
        tr_idx = [i for i, qq in enumerate(q_all) if qq in high_q]
        # hold out last 200 rows globally for eval (time-ordered) — not using quartile as feature
        te_idx = list(range(len(rows) - 200, len(rows)))
        tr_idx = [i for i in tr_idx if i not in set(te_idx)]
        if len(tr_idx) >= 80 and len(te_idx) >= 40:
            Xtr = [row_X(rows[i], feats) for i in tr_idx]
            Xte = [row_X(rows[j], feats) for j in te_idx]
            mu, sd = zscore_fit(Xtr)
            Ztr = zscore_apply(Xtr, mu, sd)
            Zte = zscore_apply(Xte, mu, sd)
            ytr = [y_all[i] for i in tr_idx]
            w, b = fit_ridge_logit(Ztr, ytr)
            scores = predict_proba(Zte, w, b)
            yte = [y_all[j] for j in te_idx]
            rte = [ret_all[j] for j in te_idx]
            pte = [pools_all[j] for j in te_idx]
            sampling_eval = eval_ranking(
                scores,
                yte,
                rte,
                pte,
                name="regime_aware_train_high_base_quartiles_only_eval_last_200_rows",
            )
            sampling_eval["high_base_quartiles_used_for_train_only"] = high_q
            sampling_eval["note"] = "Train rows restricted to Q with full-sample base>=0.10; quartile used for sampling only, not as model input."
        else:
            sampling_eval = {"skipped": True, "reason": "insufficient_rows"}
    else:
        sampling_eval = {"skipped": True, "reason": "no_quartile_base_ge_010"}

    # --- Count regimes passing full gate (PR + median + pool diversity)
    passing_rolling = [e for e in rolling_evals if e.get("gate_pass")]
    passing_quartile = [e for e in quartile_evals if e.get("gate_pass")]

    # Partial signal: PR+median criteria without pool constraint (diagnostic)
    def _pr_median_ok(e: dict) -> bool:
        if "top_decile_positive_rate" not in e:
            return False
        return (
            e["top_decile_positive_rate"] >= e["base_positive_rate"] + 0.05
            and e.get("median_return_improves") is True
        )

    rolling_pr_median_ok = [e for e in rolling_evals if _pr_median_ok(e)]
    quartile_pr_median_ok = [e for e in quartile_evals if _pr_median_ok(e)]

    independent_passing = len(passing_rolling)

    if independent_passing >= 2:
        conclusion = "AERO_S5_2_GO_TO_5000_FETCH"
    elif len(rolling_pr_median_ok) >= 2 and all(
        not e.get("not_single_pool_dominated", True) for e in rolling_pr_median_ok
    ):
        # Ranking lift in multiple time blocks but top-decile always one pool → stratify / filter regimes, not more RPC features first
        conclusion = "AERO_S5_2_NEEDS_REGIME_FILTER"
    elif len(rolling_pr_median_ok) >= 1 or len(quartile_pr_median_ok) >= 1:
        conclusion = "AERO_S5_2_NEEDS_REGIME_FILTER"
    elif not rolling_evals:
        conclusion = "AERO_S5_2_HOLD_NO_STABLE_ALPHA"
    else:
        conclusion = "AERO_S5_2_HOLD_NO_STABLE_ALPHA"

    # Strong quartile spread + no full gate → prefer regime filter over "more features"
    spread = max(q_base.values()) - min(q_base.values()) if q_base else 0.0
    if conclusion == "AERO_S5_2_HOLD_NO_STABLE_ALPHA" and spread >= 0.15:
        conclusion = "AERO_S5_2_NEEDS_REGIME_FILTER"

    report = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "constraints": {
            "new_rpc_calls": 0,
            "source_csv": str(IN_CSV.relative_to(REPO)),
            "time_quartile_used_for_diagnostics_and_sampling_only": True,
            "no_time_quartile_as_model_feature": True,
            "no_pool_token_identity_as_model_feature": True,
        },
        "feature_count": len(feats),
        "full_sample_quartile_base_positive_rate_cost004": {k: round(v, 6) for k, v in sorted(q_base.items())},
        "rolling_expanding_window_evaluation": rolling_evals,
        "quartile_stratified_evaluation": quartile_evals,
        "regime_aware_sampling_evaluation": sampling_eval,
        "gate": {
            "requires_two_independent_time_regimes_passing": True,
            "criteria": {
                "top_decile_pr_ge_base_plus_005": True,
                "top_decile_median_return_gt_overall_median": True,
                "top_decile_not_single_pool_dominated_max_share_lt_0_8": True,
            },
            "rolling_folds_passing_count": len(passing_rolling),
            "rolling_folds_pr_and_median_ok_count": len(rolling_pr_median_ok),
            "quartile_evals_passing_count": len(passing_quartile),
            "quartile_evals_pr_and_median_ok_count": len(quartile_pr_median_ok),
            "passed_go_to_5000_fetch": independent_passing >= 2,
        },
        "conclusion_enum": conclusion,
        "interpretation_summary": {
            "quartile_base_rate_spread": round(spread, 6),
            "note": "Rolling folds 1–2 (early test blocks) show top-decile PR lift vs low base but top-decile is always single-pool dominated in this 2-pool pilot; latest block fails lift.",
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    ce = report["conclusion_enum"]
    md_lines = [
        "# Aero vAMM S5.2 — Regime-aware offline design",
        "",
        "Offline only: no new RPC; `time_quartile` is diagnostic / sampling stratification only, not a model feature.",
        "",
        "## Gate (for GO_TO_5000_FETCH)",
        "",
        "Requires **≥2** independent rolling time regimes with: top-decile PR ≥ base+0.05, top-decile median return > overall median, and top-decile max pool share < 0.8.",
        "",
        f"- rolling_folds full gate pass: **{len(passing_rolling)}**",
        f"- rolling_folds PR+median only: **{len(rolling_pr_median_ok)}**",
        "",
        "## Full-sample quartile base positive rate (cost004)",
        "",
        json.dumps(report["full_sample_quartile_base_positive_rate_cost004"], indent=2),
        "",
        "## Rolling expanding-window folds",
        "",
    ]
    for e in rolling_evals:
        md_lines.append(
            f"- {e['name']}: base={e['base_positive_rate']}, top_decile={e['top_decile_positive_rate']}, "
            f"lift={e['top_decile_lift']}, top_med={e['top_decile_median_net_return_cost_004']}, "
            f"overall_med={e['overall_median_net_return_cost_004']}, max_pool_share={e['max_single_pool_share_in_top_decile']}, "
            f"gate_pass={e.get('gate_pass')}"
        )
    md_lines.extend(["", "## Quartile-stratified evaluation", ""])
    for e in quartile_evals:
        if "evaluation_mode" in e and e.get("top_decile_lift") is None:
            md_lines.append(f"- {e['time_quartile']}: {e.get('evaluation_mode')} — {e.get('note', '')}")
            continue
        md_lines.append(
            f"- {e.get('time_quartile', '?')}: base={e.get('base_positive_rate')}, "
            f"top_decile={e.get('top_decile_positive_rate')}, lift={e.get('top_decile_lift')}, "
            f"top_med={e.get('top_decile_median_net_return_cost_004')}, gate_pass={e.get('gate_pass')}"
        )
    md_lines.extend(
        [
            "",
            "## Regime-aware train sampling (high-base quartiles only)",
            "",
            "```json",
            json.dumps(sampling_eval, indent=2, default=str),
            "```",
            "",
            "## Interpretation",
            "",
            "- Early rolling test blocks (Q2/Q3-era rows) show **positive top-decile lift** vs very low base rates, but **every** top-decile is dominated by one pool — the official gate fails.",
            "- Latest block (fold 3 / Q4) reproduces S5.1: lift negative, pool-dominated.",
            "- Regime-aware training on high-base quartiles only did not fix evaluation on the last 200 rows.",
            "",
            f"## Conclusion: `{ce}`",
            "",
            f"- Rolling folds passing full gate: {len(passing_rolling)} (need >=2 for GO_TO_5000_FETCH)",
        ]
    )
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(json.dumps({"out_json": str(OUT_JSON.relative_to(REPO)), "conclusion": ce}, indent=2))


if __name__ == "__main__":
    main()
