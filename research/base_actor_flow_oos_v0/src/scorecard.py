"""Dual-verdict scorecard: INFORMATION_EDGE_GO vs COPY_EDGE_GO."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .arms import ARM_SCORE_COLS, capacity_matched_random, wallet_id_permutation
from .common import OUT, load_freeze, save_json, utcnow
from .wallet_monthly import conservative_tail_haircut_pnl, simulate_wallet_monthly


def right_tail_audit(selected: pd.DataFrame, pnl_col: str = "wallet_pnl_weth") -> dict[str, Any]:
    if selected is None or len(selected) == 0:
        return {
            "n_selected": 0,
            "independent_right_tail_events": 0,
            "token_breadth": 0,
            "cluster_breadth": 0,
            "week_breadth": 0,
            "month_breadth": 0,
            "top5_pnl_share": None,
            "pnl_miss_top5": None,
            "total_pnl": 0.0,
            "haircut_pnl": 0.0,
            "positive_pnl_tokens": 0,
        }
    s = selected.copy()
    s["_pnl"] = pd.to_numeric(s[pnl_col], errors="coerce").fillna(0.0)
    total = float(s["_pnl"].sum())
    pos = s[s["_pnl"] > 0].sort_values("_pnl", ascending=False)
    top5_share = float(pos["_pnl"].head(5).sum() / total) if total != 0 and len(pos) else None
    if len(pos) >= 5:
        drop_idx = set(pos.head(5).index)
        miss5 = float(s.loc[~s.index.isin(drop_idx), "_pnl"].sum())
    else:
        miss5 = total
    weeks = pd.to_datetime(s["decision_time"], unit="s", utc=True).dt.isocalendar().week.astype(str)
    months = pd.to_datetime(s["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m")
    return {
        "n_selected": int(len(s)),
        "independent_right_tail_events": int(s.loc[s["_pnl"] > 0, "token"].nunique()) if "token" in s.columns else int((s["_pnl"] > 0).sum()),
        "token_breadth": int(s["token"].nunique()) if "token" in s.columns else 0,
        "cluster_breadth": int(s["entity_cluster"].nunique()) if "entity_cluster" in s.columns else 0,
        "week_breadth": int(weeks.nunique()),
        "month_breadth": int(months.nunique()),
        "top5_pnl_share": top5_share,
        "pnl_miss_top5": miss5,
        "total_pnl": total,
        "haircut_pnl": conservative_tail_haircut_pnl(s["_pnl"]),
        "positive_pnl_tokens": int(s.loc[s["_pnl"] > 0, "token"].nunique()) if "token" in s.columns else 0,
    }


def cluster_bootstrap_ev(selected: pd.DataFrame, n_boot: int = 80, seed: int = 0) -> dict[str, float]:
    if selected is None or len(selected) == 0:
        return {"mean": 0.0, "p05": 0.0, "frac_positive": 0.0}
    rng = np.random.default_rng(seed)
    s = selected
    pnl = pd.to_numeric(s["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    key = "token" if "token" in s.columns else "signal_id"
    group_sums = pnl.groupby(s[key]).sum().to_numpy(dtype=float)
    if len(group_sums) == 0:
        return {"mean": 0.0, "p05": 0.0, "frac_positive": 0.0}
    # Vectorized bootstrap of cluster sums
    idx = rng.integers(0, len(group_sums), size=(n_boot, len(group_sums)))
    boots = group_sums[idx].sum(axis=1)
    return {
        "mean": float(boots.mean()),
        "p05": float(np.quantile(boots, 0.05)),
        "frac_positive": float((boots > 0).mean()),
    }


def leave_one_out_flip(selected: pd.DataFrame, unit: str, max_units: int = 80) -> dict[str, Any]:
    """Check whether dropping any single unit flips total PnL sign.

    For large unit counts, only probe the top-|pnl| contributors (bound runtime).
    """
    if selected is None or len(selected) == 0 or unit not in selected.columns:
        return {"n_units": 0, "n_sign_flips": 0, "flip_rate": 0.0}
    s = selected
    pnl = pd.to_numeric(s["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    total = float(pnl.sum())
    by_unit = pnl.groupby(s[unit]).sum().sort_values(key=lambda x: x.abs(), ascending=False)
    units = by_unit.head(max_units)
    flips = 0
    for u, u_pnl in units.items():
        sub = total - float(u_pnl)
        if total != 0 and (total > 0) != (sub > 0):
            flips += 1
    n_all = int(by_unit.shape[0])
    n_probe = int(len(units))
    return {
        "n_units": n_all,
        "n_probed": n_probe,
        "n_sign_flips": int(flips),
        "flip_rate": float(flips / n_probe) if n_probe else 0.0,
    }


def eval_policy(frame: pd.DataFrame, selected_col: str, freeze: dict, policy_name: str) -> dict[str, Any]:
    d = frame
    selected = d[selected_col].astype(bool) if selected_col in d.columns else pd.Series(False, index=d.index)
    # Wallet sim only needs selected opportunities (chronological capacity among fills)
    sel = d.loc[selected].copy()
    if "label_end_time" not in sel.columns:
        sel["label_end_time"] = sel["decision_time"] + int(freeze["targets"]["primary_horizon_seconds"])
    sel["wallet_pnl_weth"] = pd.to_numeric(sel.get("wallet_pnl_weth"), errors="coerce").fillna(0.0)
    sel["policy_selected"] = True
    amount = float(freeze["execution"]["primary_size_usd"]) / float(freeze["capital"]["eth_usd_freeze"])
    print(f"[scorecard] eval {policy_name}: selected={len(sel):,}", flush=True)
    if len(sel) > 50_000:
        # Cap extreme policies (e.g. BOT_REGIME near-all) for tractable monthly sim
        sel = sel.sort_values("decision_time").iloc[:: max(1, len(sel) // 25_000)].copy()
        print(f"[scorecard]   downsampled sim to {len(sel):,}", flush=True)
    curve, monthly, wsum = simulate_wallet_monthly(
        sel,
        initial_weth=float(freeze["capital"]["initial_weth"]),
        eth_usd=float(freeze["capital"]["eth_usd_freeze"]),
        amount_in_weth=amount,
        max_concurrent=int(freeze["capital"]["max_concurrent"]),
        monthly_target_usd=float(freeze["capital"]["monthly_withdraw_target_usd"]),
        compound=False,
    )
    audit = right_tail_audit(sel)
    boot = cluster_bootstrap_ev(sel)
    loo_tok = leave_one_out_flip(sel, "token")
    loo_ent = leave_one_out_flip(sel, "entity_cluster") if "entity_cluster" in sel.columns else {"n_units": 0, "n_sign_flips": 0, "flip_rate": 0.0}
    fold_pnl = {}
    if "fold" in sel.columns and len(sel):
        fold_pnl = sel.groupby("fold")["wallet_pnl_weth"].sum().to_dict()
    worst_fold = float(min(fold_pnl.values())) if fold_pnl else None
    return {
        "policy": policy_name,
        "selected_trades": int(len(sel)),
        "total_wallet_pnl_weth": audit["total_pnl"],
        "haircut_pnl_weth": audit["haircut_pnl"],
        "worst_fold_pnl_weth": worst_fold,
        "fold_pnl": {str(k): float(v) for k, v in fold_pnl.items()},
        "wallet": wsum,
        "right_tail": audit,
        "bootstrap": boot,
        "loo_token": loo_tok,
        "loo_entity": loo_ent,
        "monthly_rows": monthly.to_dict(orient="records"),
    }


def decide_information_edge(arm_metrics: dict[str, Any], pool_only: dict[str, Any], perm_metrics: dict[str, Any] | None) -> str:
    """INFORMATION_EDGE_GO if identity arm beats POOL_ONLY and permutation control."""
    total = float(arm_metrics.get("total_wallet_pnl_weth") or 0.0)
    hair = float(arm_metrics.get("haircut_pnl_weth") or 0.0)
    boot = arm_metrics.get("bootstrap", {})
    rt = arm_metrics.get("right_tail", {})
    pool_total = float(pool_only.get("total_wallet_pnl_weth") or 0.0)
    incremental = total - pool_total
    loo_tok = arm_metrics.get("loo_token", {})
    loo_ent = arm_metrics.get("loo_entity", {})
    n_rt = int(rt.get("independent_right_tail_events") or 0)
    if n_rt < 3 and total > 0:
        return "INSUFFICIENT_INDEPENDENT_EVENTS"
    # Pool structure suffices if identity doesn't beat pool
    if incremental <= 0 and total <= pool_total:
        return "POOL_STRUCTURE_SUFFICES" if pool_total > total else "INFORMATION_EDGE_NO_GO"
    folds = arm_metrics.get("fold_pnl") or {}
    fold_consistent = (len(folds) >= 2 and sum(1 for v in folds.values() if v > 0) >= (len(folds) + 1) // 2) or len(folds) < 2
    perm_total = float(perm_metrics.get("total_wallet_pnl_weth") or 0.0) if perm_metrics else None
    beats_perm = (perm_total is None) or (total > perm_total)
    edge = (
        total > 0
        and hair > 0
        and incremental > 0
        and float(boot.get("p05") or 0) > 0
        and float(loo_tok.get("flip_rate") or 0) < 0.5
        and float(loo_ent.get("flip_rate") or 1) < 0.75
        and fold_consistent
        and beats_perm
        and int(rt.get("positive_pnl_tokens") or 0) >= 3
    )
    if edge:
        return "INFORMATION_EDGE_GO"
    if incremental > 0 and total > 0:
        return "PROSPECTIVE_CONFIRMATION_REQUIRED"
    return "INFORMATION_EDGE_NO_GO"


def decide_copy_edge(info_verdict: str, metrics: dict[str, Any], freeze: dict) -> str:
    if info_verdict not in ("INFORMATION_EDGE_GO", "PROSPECTIVE_CONFIRMATION_REQUIRED"):
        return "COPY_EDGE_NO_GO"
    w = metrics.get("wallet", {})
    if bool(w.get("ruin")):
        return "COPY_EDGE_NO_GO"
    pct_hit = float(w.get("pct_months_hit_1000_usd") or 0.0)
    worst = float(w.get("worst_month_trading_pnl_usd") or 0.0)
    hair = float(metrics.get("haircut_pnl_weth") or 0.0)
    # Cashflow target: at least 25% of months hit $1000 OR median monthly withdraw > 0 with no deep ruin months
    cashflow_ok = pct_hit >= 0.25 or (
        float(w.get("median_monthly_withdraw_usd") or 0) > 0
        and worst > -0.5 * float(freeze["capital"]["initial_capital_usd"])
    )
    if hair > 0 and cashflow_ok and float(metrics.get("total_wallet_pnl_weth") or 0) > 0:
        # Still require prospective confirmation for live
        return "COPY_EDGE_GO"
    return "COPY_EDGE_NO_GO"


def build_scorecard(oof_by_arm: dict[str, pd.DataFrame], freeze: dict | None = None) -> tuple[pd.DataFrame, dict]:
    freeze = freeze or load_freeze()
    # Align on POOL_ONLY spine for controls
    spine = oof_by_arm.get("POOL_ONLY")
    if spine is None:
        spine = next(iter(oof_by_arm.values()))

    pool_m = eval_policy(spine, "policy_selected", freeze, "POOL_ONLY")

    # Capacity-matched random control on spine
    n_sel = int(spine["policy_selected"].sum()) if "policy_selected" in spine.columns else 0
    rand_frame = spine.copy()
    rand_frame["policy_selected"] = capacity_matched_random(rand_frame, n_sel)
    rand_m = eval_policy(rand_frame, "policy_selected", freeze, "CAPACITY_MATCHED_RANDOM")

    rows = []
    details = {"arms": {}, "controls": {"POOL_ONLY": pool_m, "CAPACITY_MATCHED_RANDOM": rand_m}}
    best_info = None
    best_info_score = -1e99

    for arm, frame in oof_by_arm.items():
        m = eval_policy(frame, "policy_selected", freeze, arm)
        # Permutation control for identity arms
        perm_m = None
        if arm != "POOL_ONLY" and ARM_SCORE_COLS.get(arm) in frame.columns:
            pf = frame.copy()
            pf["_perm_score"] = wallet_id_permutation(pf, ARM_SCORE_COLS[arm])
            # Select using permuted scores at same quantile mass
            q = float(freeze["selection"]["score_quantile"])
            thr = float(pf["_perm_score"].quantile(q)) if len(pf) else 1e9
            pf["policy_selected"] = pf["_perm_score"] >= thr
            if "buy_status" in pf.columns:
                pf["policy_selected"] &= pf["buy_status"].eq("SUCCESS")
            perm_m = eval_policy(pf, "policy_selected", freeze, f"{arm}_PERM")
            details["controls"][f"{arm}_PERM"] = perm_m

        if arm == "POOL_ONLY":
            info_v = "BASELINE"
            copy_v = "BASELINE"
        else:
            info_v = decide_information_edge(m, pool_m, perm_m)
            copy_v = decide_copy_edge(info_v, m, freeze)

        m["information_verdict"] = info_v
        m["copy_verdict"] = copy_v
        m["incremental_vs_pool_only_weth"] = float(m["total_wallet_pnl_weth"] - pool_m["total_wallet_pnl_weth"])
        details["arms"][arm] = m
        rows.append({
            "arm": arm,
            "information_verdict": info_v,
            "copy_verdict": copy_v,
            "selected_trades": m["selected_trades"],
            "total_wallet_pnl_weth": m["total_wallet_pnl_weth"],
            "haircut_pnl_weth": m["haircut_pnl_weth"],
            "incremental_vs_pool_only_weth": m["incremental_vs_pool_only_weth"],
            "bootstrap_p05_weth": m["bootstrap"]["p05"],
            "loo_token_flip_rate": m["loo_token"]["flip_rate"],
            "loo_entity_flip_rate": m["loo_entity"]["flip_rate"],
            "pct_months_hit_1000_usd": m["wallet"]["pct_months_hit_1000_usd"],
            "median_monthly_withdraw_usd": m["wallet"]["median_monthly_withdraw_usd"],
            "worst_month_trading_pnl_usd": m["wallet"]["worst_month_trading_pnl_usd"],
            "cum_withdrawn_usd": m["wallet"]["cum_withdrawn_usd"],
            "ruin": m["wallet"]["ruin"],
            "token_breadth": m["right_tail"]["token_breadth"],
            "positive_pnl_tokens": m["right_tail"]["positive_pnl_tokens"],
            "top5_pnl_share": m["right_tail"]["top5_pnl_share"],
            "worst_fold_pnl_weth": m["worst_fold_pnl_weth"],
        })
        if arm != "POOL_ONLY" and m["incremental_vs_pool_only_weth"] > best_info_score:
            best_info_score = m["incremental_vs_pool_only_weth"]
            best_info = arm

    # Random control row
    rows.append({
        "arm": "CAPACITY_MATCHED_RANDOM",
        "information_verdict": "CONTROL",
        "copy_verdict": "CONTROL",
        "selected_trades": rand_m["selected_trades"],
        "total_wallet_pnl_weth": rand_m["total_wallet_pnl_weth"],
        "haircut_pnl_weth": rand_m["haircut_pnl_weth"],
        "incremental_vs_pool_only_weth": rand_m["total_wallet_pnl_weth"] - pool_m["total_wallet_pnl_weth"],
        "bootstrap_p05_weth": rand_m["bootstrap"]["p05"],
        "loo_token_flip_rate": rand_m["loo_token"]["flip_rate"],
        "loo_entity_flip_rate": rand_m["loo_entity"]["flip_rate"],
        "pct_months_hit_1000_usd": rand_m["wallet"]["pct_months_hit_1000_usd"],
        "median_monthly_withdraw_usd": rand_m["wallet"]["median_monthly_withdraw_usd"],
        "worst_month_trading_pnl_usd": rand_m["wallet"]["worst_month_trading_pnl_usd"],
        "cum_withdrawn_usd": rand_m["wallet"]["cum_withdrawn_usd"],
        "ruin": rand_m["wallet"]["ruin"],
        "token_breadth": rand_m["right_tail"]["token_breadth"],
        "positive_pnl_tokens": rand_m["right_tail"]["positive_pnl_tokens"],
        "top5_pnl_share": rand_m["right_tail"]["top5_pnl_share"],
        "worst_fold_pnl_weth": rand_m["worst_fold_pnl_weth"],
    })

    scorecard = pd.DataFrame(rows)
    # Global dual verdict
    info_gos = [r for r in rows if r["information_verdict"] == "INFORMATION_EDGE_GO"]
    copy_gos = [r for r in rows if r["copy_verdict"] == "COPY_EDGE_GO"]
    pool_suffices = all(
        r["information_verdict"] in ("POOL_STRUCTURE_SUFFICES", "INFORMATION_EDGE_NO_GO", "BASELINE", "CONTROL")
        for r in rows if r["arm"] != "CAPACITY_MATCHED_RANDOM"
    ) and best_info is not None and best_info_score <= 0

    prosp = [r for r in rows if r["information_verdict"] == "PROSPECTIVE_CONFIRMATION_REQUIRED"]
    if info_gos and copy_gos:
        # Prefer an arm that clears both
        both = [r for r in info_gos if r["copy_verdict"] == "COPY_EDGE_GO"]
        pick = both[0] if both else info_gos[0]
        global_info = "INFORMATION_EDGE_GO"
        global_copy = pick["copy_verdict"]
        winner = pick["arm"]
    elif info_gos:
        global_info = "INFORMATION_EDGE_GO"
        global_copy = "COPY_EDGE_NO_GO"
        winner = info_gos[0]["arm"]
    elif prosp and copy_gos:
        # Do NOT silently upgrade PROSPECTIVE → INFORMATION_EDGE_GO
        pick = next(
            (r for r in prosp if r["copy_verdict"] == "COPY_EDGE_GO"),
            prosp[0],
        )
        global_info = "PROSPECTIVE_CONFIRMATION_REQUIRED"
        global_copy = "COPY_EDGE_GO"
        winner = pick["arm"]
    elif prosp:
        global_info = "PROSPECTIVE_CONFIRMATION_REQUIRED"
        global_copy = "COPY_EDGE_NO_GO"
        winner = prosp[0]["arm"]
    elif pool_suffices:
        global_info = "POOL_STRUCTURE_SUFFICES"
        global_copy = "COPY_EDGE_NO_GO"
        winner = "POOL_ONLY"
    else:
        global_info = "INFORMATION_EDGE_NO_GO"
        global_copy = "COPY_EDGE_NO_GO"
        winner = best_info or "NONE"

    summary = {
        "information_verdict": global_info,
        "copy_verdict": global_copy,
        "best_arm": winner,
        "best_incremental_vs_pool_only_weth": best_info_score if best_info else None,
        "pool_only_pnl_weth": pool_m["total_wallet_pnl_weth"],
        "deploy_allowed": False,
        "generated_at": utcnow(),
        "details": details,
    }
    out_dir = OUT / "oos"
    out_dir.mkdir(parents=True, exist_ok=True)
    scorecard.to_csv(out_dir / "scorecard.csv", index=False)
    save_json(out_dir / "scorecard_details.json", {
        "information_verdict": global_info,
        "copy_verdict": global_copy,
        "best_arm": winner,
        "pool_only_pnl_weth": pool_m["total_wallet_pnl_weth"],
        "arms": {k: {kk: vv for kk, vv in v.items() if kk != "monthly_rows"} for k, v in details["arms"].items()},
    })
    return scorecard, summary
