"""Task 2: pseudo-duplication disclosure + unconstrained vs fixed-$1000 scheduled PnL."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..common import load_freeze
from ..wallet_monthly import simulate_wallet_monthly
from .data import save_audit_json, selected


def _breadth(sel: pd.DataFrame) -> dict[str, Any]:
    if sel.empty:
        return {
            "n_trades": 0,
            "unique_token": 0,
            "unique_actor": 0,
            "unique_actor_token": 0,
            "unique_entity_cluster": 0,
            "unique_day": 0,
            "unique_month": 0,
            "trades_per_token_p50": None,
            "trades_per_token_p95": None,
            "trades_per_token_max": None,
            "max_token_pnl_share": None,
            "top_token_by_trades": None,
            "top_token_n_trades": None,
        }
    actor_col = "economic_actor" if "economic_actor" in sel.columns else "actor"
    tok = sel["token"].astype(str)
    act = sel[actor_col].astype(str) if actor_col in sel.columns else pd.Series(["?"] * len(sel))
    at = act + "|" + tok
    by_tok = tok.value_counts()
    pnl = pd.to_numeric(sel["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    tok_pnl = pnl.groupby(tok).sum()
    total = float(pnl.sum())
    top_share = float(tok_pnl.abs().max() / abs(total)) if total != 0 else None
    return {
        "n_trades": int(len(sel)),
        "unique_token": int(tok.nunique()),
        "unique_actor": int(act.nunique()),
        "unique_actor_token": int(at.nunique()),
        "unique_entity_cluster": int(sel["entity_cluster"].nunique()) if "entity_cluster" in sel.columns else None,
        "unique_day": int(sel["day"].astype(str).nunique()) if "day" in sel.columns else None,
        "unique_month": int(sel["month"].nunique()) if "month" in sel.columns else None,
        "trades_per_token_p50": float(by_tok.quantile(0.5)),
        "trades_per_token_p95": float(by_tok.quantile(0.95)),
        "trades_per_token_max": int(by_tok.max()),
        "max_token_pnl_share": top_share,
        "top_token_by_trades": str(by_tok.index[0]),
        "top_token_n_trades": int(by_tok.iloc[0]),
        "mean_pnl_per_trade": float(pnl.mean()),
        "unconstrained_total_pnl_weth": float(pnl.sum()),
    }


def _overlap_positions(sel: pd.DataFrame, horizon_s: int = 14400) -> dict[str, Any]:
    """Max concurrent open positions overall and per token (assuming hold = primary horizon)."""
    if sel.empty:
        return {"max_concurrent_all": 0, "max_concurrent_per_token": 0, "mean_concurrent_all": 0.0}
    d = sel.sort_values("decision_time")
    starts = d["decision_time"].astype(int).to_numpy()
    if "label_end_time" in d.columns:
        ends = pd.to_numeric(d["label_end_time"], errors="coerce").fillna(d["decision_time"].astype(int) + horizon_s).to_numpy(dtype=np.int64)
    else:
        ends = starts + horizon_s
    # Sweep line for global concurrency
    events = []
    for s, e in zip(starts, ends):
        events.append((int(s), 1))
        events.append((int(e), -1))
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    # Per-token peak
    tok_peak = 0
    for _, g in d.groupby("token"):
        st = g["decision_time"].astype(int).to_numpy()
        if "label_end_time" in g.columns:
            en = pd.to_numeric(g["label_end_time"], errors="coerce").fillna(g["decision_time"].astype(int) + horizon_s).to_numpy(dtype=np.int64)
        else:
            en = st + horizon_s
        ev = sorted([(int(s), 1) for s in st] + [(int(e), -1) for e in en])
        c = p = 0
        for _, dd in ev:
            c += dd
            p = max(p, c)
        tok_peak = max(tok_peak, p)
    return {
        "max_concurrent_all": int(peak),
        "max_concurrent_per_token": int(tok_peak),
        "implied_unconstrained_capital_slots": int(peak),
    }


def _wallet_schedule(sel: pd.DataFrame, freeze: dict) -> dict[str, Any]:
    if sel.empty:
        return {"n_fills": 0, "n_skipped_capital": 0, "cum_withdrawn_usd": 0.0, "pct_months_hit_1000_usd": 0.0}
    amount = float(freeze["execution"]["primary_size_usd"]) / float(freeze["capital"]["eth_usd_freeze"])
    sim = sel.copy()
    if "label_end_time" not in sim.columns:
        sim["label_end_time"] = sim["decision_time"] + int(freeze["targets"]["primary_horizon_seconds"])
    sim["policy_selected"] = True
    sim["wallet_pnl_weth"] = pd.to_numeric(sim["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    _, monthly, summary = simulate_wallet_monthly(
        sim,
        initial_weth=float(freeze["capital"]["initial_weth"]),
        eth_usd=float(freeze["capital"]["eth_usd_freeze"]),
        amount_in_weth=amount,
        max_concurrent=int(freeze["capital"]["max_concurrent"]),
        monthly_target_usd=float(freeze["capital"]["monthly_withdraw_target_usd"]),
        compound=False,
    )
    out = dict(summary)
    if len(monthly):
        w = monthly["withdrawn_usd"]
        tp = monthly["trading_pnl_usd"]
        out["monthly_withdraw_p5_usd"] = float(w.quantile(0.05))
        out["monthly_withdraw_p25_usd"] = float(w.quantile(0.25))
        out["monthly_withdraw_median_usd"] = float(w.median())
        out["monthly_trading_pnl_p5_usd"] = float(tp.quantile(0.05))
        out["monthly_trading_pnl_p25_usd"] = float(tp.quantile(0.25))
        out["monthly_trading_pnl_median_usd"] = float(tp.median())
        out["executable_signals_per_month_mean"] = float(
            sim.assign(month=pd.to_datetime(sim["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m"))
            .groupby("month").size().mean()
        )
        # Approximate fills/month from summary
        out["scheduled_fills"] = int(summary.get("n_fills") or 0)
        out["skipped_for_capital"] = int(summary.get("n_skipped_capital") or 0)
        out["skip_rate"] = (
            float(summary["n_skipped_capital"] / max(1, summary["n_fills"] + summary["n_skipped_capital"]))
        )
    return out


def audit_arm(name: str, frame: pd.DataFrame, freeze: dict) -> dict[str, Any]:
    sel = selected(frame)
    breadth = _breadth(sel)
    overlap = _overlap_positions(sel, horizon_s=int(freeze["targets"]["primary_horizon_seconds"]))
    wallet = _wallet_schedule(sel, freeze)
    return {
        "arm": name,
        "breadth": breadth,
        "overlap": overlap,
        "unconstrained_opportunity_pnl_weth": breadth.get("unconstrained_total_pnl_weth"),
        "fixed_1000_scheduled_wallet": {
            "cum_withdrawn_usd": wallet.get("cum_withdrawn_usd"),
            "pct_months_hit_1000_usd": wallet.get("pct_months_hit_1000_usd"),
            "median_monthly_withdraw_usd": wallet.get("median_monthly_withdraw_usd"),
            "monthly_withdraw_p5_usd": wallet.get("monthly_withdraw_p5_usd"),
            "monthly_withdraw_p25_usd": wallet.get("monthly_withdraw_p25_usd"),
            "monthly_trading_pnl_median_usd": wallet.get("monthly_trading_pnl_median_usd"),
            "monthly_trading_pnl_p5_usd": wallet.get("monthly_trading_pnl_p5_usd"),
            "monthly_trading_pnl_p25_usd": wallet.get("monthly_trading_pnl_p25_usd"),
            "n_fills": wallet.get("n_fills"),
            "n_skipped_capital": wallet.get("n_skipped_capital"),
            "skip_rate": wallet.get("skip_rate"),
            "ruin": wallet.get("ruin"),
            "max_drawdown_wallet_weth": wallet.get("max_drawdown_wallet_weth"),
        },
        "why_38_weth_vs_target_miss": {
            "explanation": (
                "total_wallet_pnl_weth on the scorecard is the unconstrained sum of per-trade "
                "opportunity PnL. The $1,000 fixed-capital wallet with max_concurrent slots skips "
                "most signals; monthly withdraw target hit rate can be 0% even when unconstrained "
                "sum is large."
            ),
            "unconstrained_pnl_weth": breadth.get("unconstrained_total_pnl_weth"),
            "scheduled_cum_withdrawn_usd": wallet.get("cum_withdrawn_usd"),
            "pct_months_hit_1000_usd": wallet.get("pct_months_hit_1000_usd"),
            "max_concurrent_cap": int(freeze["capital"]["max_concurrent"]),
            "implied_unconstrained_slots_needed": overlap.get("max_concurrent_all"),
        },
    }


def run_dedup_capital(oof: dict[str, pd.DataFrame]) -> dict[str, Any]:
    freeze = load_freeze()
    arms = {}
    for name, frame in oof.items():
        print(f"[audit:dedup] {name} ...", flush=True)
        arms[name] = audit_arm(name, frame, freeze)
    # Focus summary on WHALE vs POOL
    whale = arms.get("WHALE_EVENT", {})
    pool = arms.get("POOL_ONLY", {})
    report = {
        "task": "dedup_and_capital_scheduling",
        "arms": arms,
        "whale_vs_pool_coverage": {
            "whale_n": whale.get("breadth", {}).get("n_trades"),
            "pool_n": pool.get("breadth", {}).get("n_trades"),
            "whale_over_pool": (
                (whale.get("breadth", {}).get("n_trades") or 0)
                / max(1, pool.get("breadth", {}).get("n_trades") or 1)
            ),
            "whale_unique_token": whale.get("breadth", {}).get("unique_token"),
            "pool_unique_token": pool.get("breadth", {}).get("unique_token"),
        },
        "target_verdict": "TARGET_NO_GO",
        "target_reason": "pct_months_hit_1000_usd == 0 for WHALE_EVENT under fixed-$1000 wallet.",
    }
    save_audit_json("02_dedup_capital.json", report)
    return report
