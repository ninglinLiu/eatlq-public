"""Primary scorecard = scheduled wallet paired vs POOL_ONLY under identical scheduler."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .arms_v1 import ARM_SCORE_COLS, capacity_matched_random, size_permutation
from .common import OUT, load_freeze, save_json, utcnow
from .scheduler import simulate_scheduled


def _cluster_boot_sum(values: np.ndarray, clusters: np.ndarray, n_boot: int = 200, seed: int = 7) -> dict:
    if len(values) == 0:
        return {"mean": 0.0, "p05": 0.0, "frac_positive": 0.0, "n_clusters": 0}
    rng = np.random.default_rng(seed)
    s = pd.Series(values).groupby(clusters, sort=False).sum()
    g = s.to_numpy(dtype=float)
    idx = rng.integers(0, len(g), size=(n_boot, len(g)))
    boots = g[idx].sum(axis=1)
    return {
        "mean": float(boots.mean()),
        "p05": float(np.quantile(boots, 0.05)),
        "frac_positive": float((boots > 0).mean()),
        "n_clusters": int(len(g)),
    }


def paired_scheduled_delta(
    treat_fills: pd.DataFrame,
    ctrl_fills: pd.DataFrame,
    *,
    n_boot: int = 200,
    seed: int = 7,
) -> dict[str, Any]:
    """Pair filled trades by calendar day capacity: compare daily scheduled PnL."""
    def daily(df: pd.DataFrame) -> pd.Series:
        if df is None or len(df) == 0:
            return pd.Series(dtype=float)
        day = pd.to_datetime(df["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
        return pd.to_numeric(df["wallet_pnl_weth"], errors="coerce").fillna(0).groupby(day).sum()

    td = daily(treat_fills)
    cd = daily(ctrl_fills)
    days = sorted(set(td.index) | set(cd.index))
    if not days:
        return {"n_days": 0, "sum_delta": 0.0, "p05_day": 0.0, "passes": False}
    delta = np.array([float(td.get(d, 0.0) - cd.get(d, 0.0)) for d in days])
    boot = _cluster_boot_sum(delta, np.array(days), n_boot=n_boot, seed=seed)
    # also token-level if available
    tok_boot = {}
    if treat_fills is not None and len(treat_fills) and "token" in treat_fills.columns:
        # approximate: sum treat by token minus ctrl by token
        tt = treat_fills.groupby("token")["wallet_pnl_weth"].sum()
        cc = ctrl_fills.groupby("token")["wallet_pnl_weth"].sum() if len(ctrl_fills) else pd.Series(dtype=float)
        toks = sorted(set(tt.index) | set(cc.index))
        tdlt = np.array([float(tt.get(t, 0.0) - cc.get(t, 0.0)) for t in toks])
        tok_boot = _cluster_boot_sum(tdlt, np.array(toks), n_boot=n_boot, seed=seed + 1)
    return {
        "n_days": int(len(days)),
        "sum_delta": float(delta.sum()),
        "mean_daily_delta": float(delta.mean()),
        "bootstrap_day": boot,
        "bootstrap_token": tok_boot,
        "p05_day": boot["p05"],
        "p05_token": tok_boot.get("p05"),
        "passes": bool(boot["p05"] > 0),
    }


def eval_arm(frame: pd.DataFrame, arm: str) -> dict[str, Any]:
    score_col = ARM_SCORE_COLS.get(arm, "pool_only_score")
    d = frame.copy()
    d["_sched_score"] = pd.to_numeric(d.get(score_col, 0), errors="coerce").fillna(0.0)
    fills, monthly, summary = simulate_scheduled(d, score_col="_sched_score")
    sel = d[d["policy_selected"].astype(bool)] if "policy_selected" in d.columns else d.iloc[0:0]
    return {
        "arm": arm,
        "selected_trades": int(len(sel)),
        "UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth": summary["unconstrained_opportunity_pnl_weth"],
        "scheduled": summary,
        "monthly": monthly.to_dict(orient="records") if len(monthly) else [],
        "n_fills": summary["n_fills"],
        "fills": fills,
    }


def build_scorecard(oof: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    freeze = load_freeze()
    details = {"arms": {}, "controls": {}, "historical_label": "DIAGNOSTIC_REPLAY", "inherits_v0_oos": False}
    rows = []

    pool_m = eval_arm(oof["POOL_ONLY"], "POOL_ONLY")
    details["arms"]["POOL_ONLY"] = {k: v for k, v in pool_m.items() if k != "fills"}

    # capacity matched random on POOL spine
    spine = oof["POOL_ONLY"].copy()
    n_sel = int(spine["policy_selected"].sum()) if "policy_selected" in spine.columns else 0
    rand = spine.copy()
    rand["policy_selected"] = capacity_matched_random(rand, n_sel)
    rand_m = eval_arm(rand, "CAPACITY_MATCHED_RANDOM")
    details["controls"]["CAPACITY_MATCHED_RANDOM"] = {k: v for k, v in rand_m.items() if k != "fills"}

    best_arm = None
    best_delta = -1e99
    primary_paired = None

    for arm, frame in oof.items():
        m = eval_arm(frame, arm)
        # size permutation control for flow arms
        perm_m = None
        if arm != "POOL_ONLY" and ARM_SCORE_COLS.get(arm) in frame.columns:
            pf = frame.copy()
            pf["_perm"] = size_permutation(pf, ARM_SCORE_COLS[arm])
            q = float(freeze["selection"]["score_quantile"])
            thr = float(pf["_perm"].quantile(q)) if len(pf) else 1e9
            pf["policy_selected"] = pf["_perm"] >= thr
            if "buy_status" in pf.columns:
                pf["policy_selected"] &= pf["buy_status"].eq("SUCCESS")
            if "scorable_primary" in pf.columns:
                pf["policy_selected"] &= pf["scorable_primary"].astype(bool)
            perm_m = eval_arm(pf, f"{arm}_SIZE_PERM")
            details["controls"][f"{arm}_SIZE_PERM"] = {k: v for k, v in perm_m.items() if k != "fills"}

        paired = paired_scheduled_delta(m["fills"], pool_m["fills"])
        sched_pnl = float(m["scheduled"]["scheduled_trading_pnl_weth"])
        pool_sched = float(pool_m["scheduled"]["scheduled_trading_pnl_weth"])
        incr = sched_pnl - pool_sched

        if arm == "POOL_ONLY":
            info_v = "BASELINE"
        elif paired["passes"] and incr > 0:
            info_v = "SCHEDULED_EDGE_POSITIVE"
        elif incr > 0:
            info_v = "SCHEDULED_EDGE_UNCONFIRMED"
        else:
            info_v = "SCHEDULED_EDGE_NEGATIVE"

        row = {
            "arm": arm,
            "scheduled_verdict": info_v,
            "selected_trades": m["selected_trades"],
            "n_fills_scheduled": m["n_fills"],
            "skip_rate": m["scheduled"]["skip_rate"],
            "scheduled_trading_pnl_weth": sched_pnl,
            "scheduled_cum_withdrawn_usd": m["scheduled"]["cum_withdrawn_usd"],
            "median_monthly_withdraw_usd": m["scheduled"]["median_monthly_withdraw_usd"],
            "avg_monthly_withdraw_usd": m["scheduled"]["avg_monthly_withdraw_usd"],
            "pct_months_hit_1000_usd": m["scheduled"]["pct_months_hit_1000_usd"],
            "UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth": m["UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth"],
            "incremental_scheduled_vs_pool_weth": incr,
            "paired_delta_p05_day": paired["p05_day"],
            "paired_delta_passes": paired["passes"],
            "ruin": m["scheduled"]["ruin"],
        }
        rows.append(row)
        details["arms"][arm] = {**( {k: v for k, v in m.items() if k != "fills"}), "paired_vs_pool": paired, "scheduled_verdict": info_v}
        if arm == "LARGE_BUY_ABSORPTION":
            primary_paired = paired
        if arm != "POOL_ONLY" and incr > best_delta:
            best_delta = incr
            best_arm = arm

    # control row
    rows.append({
        "arm": "CAPACITY_MATCHED_RANDOM",
        "scheduled_verdict": "CONTROL",
        "selected_trades": rand_m["selected_trades"],
        "n_fills_scheduled": rand_m["n_fills"],
        "skip_rate": rand_m["scheduled"]["skip_rate"],
        "scheduled_trading_pnl_weth": rand_m["scheduled"]["scheduled_trading_pnl_weth"],
        "scheduled_cum_withdrawn_usd": rand_m["scheduled"]["cum_withdrawn_usd"],
        "median_monthly_withdraw_usd": rand_m["scheduled"]["median_monthly_withdraw_usd"],
        "avg_monthly_withdraw_usd": rand_m["scheduled"]["avg_monthly_withdraw_usd"],
        "pct_months_hit_1000_usd": rand_m["scheduled"]["pct_months_hit_1000_usd"],
        "UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth": rand_m["UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth"],
        "incremental_scheduled_vs_pool_weth": rand_m["scheduled"]["scheduled_trading_pnl_weth"] - pool_m["scheduled"]["scheduled_trading_pnl_weth"],
        "paired_delta_p05_day": None,
        "paired_delta_passes": False,
        "ruin": rand_m["scheduled"]["ruin"],
    })

    scorecard = pd.DataFrame(rows)
    primary = details["arms"].get("LARGE_BUY_ABSORPTION", {})
    pct_hit = float(primary.get("scheduled", {}).get("pct_months_hit_1000_usd") or 0)
    summary = {
        "historical_label": "DIAGNOSTIC_REPLAY",
        "inherits_v0_oos": False,
        "primary_arm": "LARGE_BUY_ABSORPTION",
        "best_arm_by_scheduled_incremental": best_arm,
        "primary_scheduled_verdict": primary.get("scheduled_verdict"),
        "primary_paired": primary_paired,
        "target_verdict": "TARGET_NO_GO" if pct_hit < 0.25 else "TARGET_PLAUSIBLE",
        "deploy_allowed": False,
        "generated_at": utcnow(),
        "details": {k: v for k, v in details.items() if k != "arms"} | {
            "arms": {a: {kk: vv for kk, vv in am.items() if kk not in ("fills", "monthly")} for a, am in details["arms"].items()}
        },
    }
    out = OUT / "oos"
    out.mkdir(parents=True, exist_ok=True)
    scorecard.to_csv(out / "scorecard_v1.csv", index=False)
    save_json(out / "scorecard_v1_summary.json", {
        k: v for k, v in summary.items() if k != "details"
    } | {"arms_brief": {
        a: {
            "scheduled_verdict": details["arms"][a].get("scheduled_verdict"),
            "n_fills": details["arms"][a].get("n_fills"),
            "cum_withdrawn_usd": details["arms"][a].get("scheduled", {}).get("cum_withdrawn_usd"),
            "unconstrained_diag": details["arms"][a].get("UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth"),
        } for a in details["arms"]
    }})
    return scorecard, summary
