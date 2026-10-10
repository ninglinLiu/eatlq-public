"""OOF evaluation: wallet KPIs, right-tail audit, baseline comparisons, verdict helpers."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.v3.baselines import BASELINE_SELECT_COLS, apply_baselines
from src.v3.wallet_monthly import conservative_tail_haircut_pnl, simulate_wallet_monthly

ALLOWED_VERDICTS = {
    "ORTHOGONAL_EDGE_CONFIRMED",
    "ORTHOGONAL_EDGE_NOT_CONFIRMED",
    "LOTTERY_DEPENDENT_REJECT",
    "EXECUTION_RISK_DOMINATES",
    "INSUFFICIENT_INDEPENDENT_RIGHT_TAIL_EVENTS",
    "DATA_LINEAGE_INSUFFICIENT",
    "PROSPECTIVE_CONFIRMATION_REQUIRED",
}


def right_tail_audit(selected: pd.DataFrame, pnl_col: str = "wallet_pnl_weth") -> dict[str, Any]:
    if selected is None or len(selected) == 0:
        return {
            "n_selected": 0,
            "independent_right_tail_events": 0,
            "token_breadth": 0,
            "deployer_breadth": 0,
            "week_breadth": 0,
            "month_breadth": 0,
            "top1_pnl_share": None,
            "top3_pnl_share": None,
            "top5_pnl_share": None,
            "top10_pnl_share": None,
            "pnl_miss_top1": None,
            "pnl_miss_top3": None,
            "pnl_miss_top5": None,
            "total_pnl": 0.0,
            "haircut_pnl": 0.0,
        }
    s = selected.copy()
    s["_pnl"] = pd.to_numeric(s[pnl_col], errors="coerce").fillna(0.0)
    total = float(s["_pnl"].sum())
    pos = s[s["_pnl"] > 0].sort_values("_pnl", ascending=False)
    shares = {}
    for k in (1, 3, 5, 10):
        shares[f"top{k}_pnl_share"] = float(pos["_pnl"].head(k).sum() / total) if total != 0 else None
    miss = {}
    for k in (1, 3, 5):
        if len(pos) >= k:
            drop_idx = set(pos.head(k).index)
            miss[f"pnl_miss_top{k}"] = float(s.loc[~s.index.isin(drop_idx), "_pnl"].sum())
        else:
            miss[f"pnl_miss_top{k}"] = float(total)
    weeks = pd.to_datetime(s["decision_time"], unit="s", utc=True).dt.isocalendar().week.astype(str) + "-" + pd.to_datetime(s["decision_time"], unit="s", utc=True).dt.year.astype(str)
    months = pd.to_datetime(s["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m")
    rt = s[s.get("y_right_tail_25", pd.Series(0, index=s.index)).astype(bool) | (s.get("mfe", 0) >= 0.25)]
    return {
        "n_selected": int(len(s)),
        "independent_right_tail_events": int(rt["pool"].nunique()) if len(rt) and "pool" in rt.columns else int((s["_pnl"] > 0).sum()),
        "token_breadth": int(s["pool"].nunique()) if "pool" in s.columns else 0,
        "deployer_breadth": int(s["deployer"].nunique()) if "deployer" in s.columns else 0,
        "week_breadth": int(weeks.nunique()),
        "month_breadth": int(months.nunique()),
        **shares,
        **miss,
        "total_pnl": total,
        "haircut_pnl": conservative_tail_haircut_pnl(s["_pnl"]),
        "positive_pnl_tokens": int(s.loc[s["_pnl"] > 0, "pool"].nunique()) if "pool" in s.columns else 0,
        "positive_pnl_deployers": int(s.loc[s["_pnl"] > 0, "deployer"].nunique()) if "deployer" in s.columns else 0,
    }


def eval_policy(
    frame: pd.DataFrame,
    selected_col: str,
    freeze: dict,
    policy_name: str,
) -> dict[str, Any]:
    d = frame.copy()
    d["policy_selected"] = d[selected_col].astype(bool) if selected_col in d.columns else False
    sel = d[d["policy_selected"]]
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    curve, monthly, wsum = simulate_wallet_monthly(
        d,
        initial_weth=float(freeze["capital"]["initial_weth"]),
        eth_usd=float(freeze["capital"]["eth_usd_freeze"]),
        amount_in_weth=amount,
        max_concurrent=int(freeze["capital"]["max_concurrent"]),
        monthly_target_usd=float(freeze["capital"]["monthly_withdraw_target_usd"]),
        compound=False,
    )
    audit = right_tail_audit(sel)
    base_fatal = float(d["fatal_event"].mean()) if "fatal_event" in d.columns and len(d) else 0.0
    sel_fatal = float(sel["fatal_event"].mean()) if len(sel) and "fatal_event" in sel.columns else 0.0
    buy_fill = float(sel["buy_status"].eq("SUCCESS").mean()) if len(sel) and "buy_status" in sel.columns else None
    fold_pnl = {}
    if "fold" in sel.columns and len(sel):
        fold_pnl = sel.groupby("fold")["wallet_pnl_weth"].sum().to_dict()
    worst_fold = float(min(fold_pnl.values())) if fold_pnl else None

    return {
        "policy": policy_name,
        "selected_trades": int(len(sel)),
        "buy_fill_rate": buy_fill,
        "fatal_rate_selected": sel_fatal,
        "fatal_rate_universe": base_fatal,
        "total_wallet_pnl_weth": audit["total_pnl"],
        "total_wallet_pnl_usd": audit["total_pnl"] * float(freeze["capital"]["eth_usd_freeze"]),
        "conservative_tail_haircut_pnl_weth": audit["haircut_pnl"],
        "conservative_tail_haircut_pnl_usd": audit["haircut_pnl"] * float(freeze["capital"]["eth_usd_freeze"]),
        "worst_fold_pnl_weth": worst_fold,
        "wallet": wsum,
        "right_tail": audit,
        "monthly_rows": monthly.to_dict(orient="records"),
        "curve_rows": len(curve),
    }


def decide_route_verdict(metrics: dict[str, Any], baseline_metrics: dict[str, dict]) -> str:
    """Strategy-level verdict only — never venue rejection."""
    w = metrics.get("wallet", {})
    rt = metrics.get("right_tail", {})
    total = float(metrics.get("total_wallet_pnl_weth") or 0.0)
    hair = float(metrics.get("conservative_tail_haircut_pnl_weth") or 0.0)
    fatal_sel = float(metrics.get("fatal_rate_selected") or 0.0)
    fatal_uni = float(metrics.get("fatal_rate_universe") or 0.0)
    n_rt = int(rt.get("independent_right_tail_events") or 0)
    tok_p = int(rt.get("positive_pnl_tokens") or 0)
    dep_p = int(rt.get("positive_pnl_deployers") or 0)
    week_b = int(rt.get("week_breadth") or 0)
    miss5 = rt.get("pnl_miss_top5")
    ruin = bool(w.get("ruin"))

    if fatal_sel > fatal_uni + 0.05 and total <= 0:
        return "EXECUTION_RISK_DOMINATES"
    if n_rt < 3 and total > 0:
        return "INSUFFICIENT_INDEPENDENT_RIGHT_TAIL_EVENTS"
    if miss5 is not None and float(miss5) < 0 and (tok_p <= 2 or dep_p <= 1 or week_b <= 1):
        return "LOTTERY_DEPENDENT_REJECT"

    beats = []
    for bname in ("C1_CAPACITY_MATCHED_RANDOM", "C2_PRICE_PATH_BASELINE", "C3_SIMPLE_TRANSPARENT_RULE"):
        bp = baseline_metrics.get(bname, {}).get("total_wallet_pnl_weth")
        if bp is None:
            continue
        beats.append(total > float(bp))
    edge = (
        total > 0
        and hair > 0
        and fatal_sel <= fatal_uni + 1e-9
        and (not ruin)
        and tok_p >= 3
        and dep_p >= 2
        and week_b >= 2
        and (len(beats) == 0 or all(beats))
    )
    if edge:
        # still require prospective confirmation outside OOF
        return "PROSPECTIVE_CONFIRMATION_REQUIRED"
    return "ORTHOGONAL_EDGE_NOT_CONFIRMED"


def build_scorecard(
    oof_by_route: dict[str, pd.DataFrame],
    feat: pd.DataFrame,
    freeze: dict,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Evaluate each route + baselines on aligned OOF rows."""
    spine_key = "H_FULL_DUAL" if "H_FULL_DUAL" in oof_by_route else next(iter(oof_by_route))
    spine = oof_by_route[spine_key].copy()
    feat_u = feat.drop_duplicates("signal_id")
    join_cols = [c for c in feat_u.columns if c == "signal_id" or c not in spine.columns]
    base_frame = spine.merge(feat_u[join_cols], on="signal_id", how="left")
    n_sel = int(base_frame["policy_selected"].sum()) if "policy_selected" in base_frame.columns else 0
    base_frame = apply_baselines(base_frame, n_select_target=n_sel, top_k=int(freeze["oof"]["top_k_per_decision_time"]))

    baseline_metrics = {}
    for name, col in BASELINE_SELECT_COLS.items():
        baseline_metrics[name] = eval_policy(base_frame, col, freeze, name)

    rows = []
    route_details = {}
    best_curve = None
    best_monthly = None
    best_name = None
    best_pnl = -1e99

    for name, oof in oof_by_route.items():
        fr = oof.copy()
        if "buy_status" not in fr.columns and "buy_status" in feat.columns:
            fr = fr.merge(feat[["signal_id", "buy_status"]].drop_duplicates("signal_id"), on="signal_id", how="left")
        m = eval_policy(fr, "policy_selected", freeze, name)
        verdict = decide_route_verdict(m, baseline_metrics)
        m["verdict"] = verdict
        route_details[name] = m
        rows.append(
            {
                "policy": name,
                "verdict": verdict,
                "selected_trades": m["selected_trades"],
                "total_wallet_pnl_weth": m["total_wallet_pnl_weth"],
                "total_wallet_pnl_usd": m["total_wallet_pnl_usd"],
                "haircut_pnl_weth": m["conservative_tail_haircut_pnl_weth"],
                "fatal_rate_selected": m["fatal_rate_selected"],
                "fatal_rate_universe": m["fatal_rate_universe"],
                "pct_months_hit_1000_usd": m["wallet"]["pct_months_hit_1000_usd"],
                "median_monthly_withdraw_usd": m["wallet"]["median_monthly_withdraw_usd"],
                "worst_month_trading_pnl_usd": m["wallet"]["worst_month_trading_pnl_usd"],
                "cum_withdrawn_usd": m["wallet"]["cum_withdrawn_usd"],
                "withdrawn_12m_usd": m["wallet"]["withdrawn_12m_usd"],
                "fixed_capital_return": m["wallet"]["fixed_capital_return_on_initial"],
                "ruin": m["wallet"]["ruin"],
                "independent_right_tail_events": m["right_tail"]["independent_right_tail_events"],
                "token_breadth": m["right_tail"]["token_breadth"],
                "deployer_breadth": m["right_tail"]["deployer_breadth"],
                "top5_pnl_share": m["right_tail"]["top5_pnl_share"],
                "pnl_miss_top5": m["right_tail"]["pnl_miss_top5"],
                "worst_fold_pnl_weth": m["worst_fold_pnl_weth"],
            }
        )
        if m["total_wallet_pnl_weth"] > best_pnl:
            best_pnl = m["total_wallet_pnl_weth"]
            best_name = name
            # rebuild curve for best
            dtmp = fr.copy()
            dtmp["policy_selected"] = dtmp["policy_selected"].astype(bool)
            best_curve, best_monthly, _ = simulate_wallet_monthly(
                dtmp,
                initial_weth=float(freeze["capital"]["initial_weth"]),
                eth_usd=float(freeze["capital"]["eth_usd_freeze"]),
                amount_in_weth=int(freeze["execution"]["amount_in_wei"]) / 1e18,
                max_concurrent=int(freeze["capital"]["max_concurrent"]),
                monthly_target_usd=float(freeze["capital"]["monthly_withdraw_target_usd"]),
                compound=False,
            )

    for name, m in baseline_metrics.items():
        rows.append(
            {
                "policy": name,
                "verdict": "BASELINE",
                "selected_trades": m["selected_trades"],
                "total_wallet_pnl_weth": m["total_wallet_pnl_weth"],
                "total_wallet_pnl_usd": m["total_wallet_pnl_usd"],
                "haircut_pnl_weth": m["conservative_tail_haircut_pnl_weth"],
                "fatal_rate_selected": m["fatal_rate_selected"],
                "fatal_rate_universe": m["fatal_rate_universe"],
                "pct_months_hit_1000_usd": m["wallet"]["pct_months_hit_1000_usd"],
                "median_monthly_withdraw_usd": m["wallet"]["median_monthly_withdraw_usd"],
                "worst_month_trading_pnl_usd": m["wallet"]["worst_month_trading_pnl_usd"],
                "cum_withdrawn_usd": m["wallet"]["cum_withdrawn_usd"],
                "withdrawn_12m_usd": m["wallet"]["withdrawn_12m_usd"],
                "fixed_capital_return": m["wallet"]["fixed_capital_return_on_initial"],
                "ruin": m["wallet"]["ruin"],
                "independent_right_tail_events": m["right_tail"]["independent_right_tail_events"],
                "token_breadth": m["right_tail"]["token_breadth"],
                "deployer_breadth": m["right_tail"]["deployer_breadth"],
                "top5_pnl_share": m["right_tail"]["top5_pnl_share"],
                "pnl_miss_top5": m["right_tail"]["pnl_miss_top5"],
                "worst_fold_pnl_weth": m["worst_fold_pnl_weth"],
            }
        )

    scorecard = pd.DataFrame(rows)
    details = {
        "routes": route_details,
        "baselines": baseline_metrics,
        "best_oof_policy": best_name,
    }
    if best_curve is None:
        best_curve = pd.DataFrame()
    if best_monthly is None:
        best_monthly = pd.DataFrame()
    return scorecard, details, best_curve, best_monthly
