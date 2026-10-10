"""Fixed-capital wallet simulation with month-end profit withdrawal (no compound)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def month_key(ts: int) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").strftime("%Y-%m")


def simulate_wallet_monthly(
    rows: pd.DataFrame,
    *,
    initial_weth: float,
    eth_usd: float,
    amount_in_weth: float,
    max_concurrent: int,
    monthly_target_usd: float,
    selected_col: str = "policy_selected",
    pnl_col: str = "wallet_pnl_weth",
    time_col: str = "decision_time",
    end_col: str = "label_end_time",
    compound: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    rows: opportunities with selection flags and per-trade wallet pnl.
    Month-end: withdraw max(0, cash - initial_weth) unless compound=True.
    """
    if rows is None or len(rows) == 0:
        empty = pd.DataFrame()
        summary = {
            "initial_weth": initial_weth,
            "initial_usd": initial_weth * eth_usd,
            "final_wallet_weth": float(initial_weth),
            "final_wallet_usd": float(initial_weth * eth_usd),
            "cum_withdrawn_weth": 0.0,
            "cum_withdrawn_usd": 0.0,
            "withdrawn_12m_usd": 0.0,
            "fixed_capital_return_on_initial": 0.0,
            "n_months": 0,
            "pct_months_hit_1000_usd": 0.0,
            "median_monthly_withdraw_usd": 0.0,
            "worst_month_withdraw_usd": 0.0,
            "median_monthly_trading_pnl_usd": 0.0,
            "worst_month_trading_pnl_usd": 0.0,
            "max_consecutive_losing_months": 0,
            "ruin": False,
            "ruin_time": None,
            "n_fills": 0,
            "n_skipped_capital": 0,
            "max_drawdown_wallet_weth": 0.0,
            "compound": bool(compound),
        }
        return empty, empty, summary

    d = rows.sort_values(time_col)
    # Only simulate selected rows (callers should prefilter; keep filter for safety)
    if selected_col in d.columns:
        d = d[d[selected_col].astype(bool)]
    if len(d) == 0:
        return simulate_wallet_monthly(
            rows.iloc[0:0],
            initial_weth=initial_weth,
            eth_usd=eth_usd,
            amount_in_weth=amount_in_weth,
            max_concurrent=max_concurrent,
            monthly_target_usd=monthly_target_usd,
            compound=compound,
        )

    times = d[time_col].to_numpy(dtype=np.int64)
    ends = pd.to_numeric(d[end_col], errors="coerce").fillna(d[time_col].astype(int) + 3600).to_numpy(dtype=np.int64)
    pnls = pd.to_numeric(d[pnl_col], errors="coerce").fillna(0.0).to_numpy(dtype=np.float64)
    months = pd.to_datetime(times, unit="s", utc=True).strftime("%Y-%m").to_numpy()

    cash = float(initial_weth)
    open_ends: list[int] = []
    open_stakes: list[float] = []
    month_withdraw: dict[str, float] = {}
    month_pnl_trading: dict[str, float] = {}
    cum_withdrawn = 0.0
    n_fills = 0
    n_skipped_capital = 0
    ruined = False
    ruin_time = None
    wallet_path = np.empty(len(times), dtype=np.float64)
    prev_month = months[0] if len(months) else None
    stake = float(amount_in_weth)

    for i in range(len(times)):
        t = int(times[i])
        mk = months[i]
        if prev_month is not None and mk != prev_month:
            profit = cash - initial_weth
            w = max(0.0, profit) if not compound else 0.0
            if w > 0:
                cash -= w
                cum_withdrawn += w
            month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w
            prev_month = mk

        # close expired
        if open_ends:
            keep_e, keep_s = [], []
            for e, s in zip(open_ends, open_stakes):
                if e > t:
                    keep_e.append(e)
                    keep_s.append(s)
            open_ends, open_stakes = keep_e, keep_s

        locked = sum(open_stakes)
        free = cash - locked
        pnl = float(pnls[i])
        if not ruined and len(open_ends) < max_concurrent and free >= stake - 1e-18:
            open_ends.append(int(ends[i]))
            open_stakes.append(stake)
            cash += pnl
            n_fills += 1
            month_pnl_trading[mk] = month_pnl_trading.get(mk, 0.0) + pnl
        else:
            n_skipped_capital += 1
            pnl = 0.0

        if cash <= 1e-8 and not ruined:
            ruined = True
            ruin_time = t
            cash = max(cash, 0.0)

        wallet_path[i] = cash
        prev_month = mk

    if prev_month is not None:
        profit = cash - initial_weth
        w = max(0.0, profit) if not compound else 0.0
        if w > 0:
            cash -= w
            cum_withdrawn += w
        month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w

    curve = pd.DataFrame({
        "decision_time": times,
        "month": months,
        "wallet_weth": wallet_path,
        "pnl_event": pnls,
    })
    mlist = sorted(set(month_withdraw) | set(month_pnl_trading))
    monthly = pd.DataFrame({
        "month": mlist,
        "withdrawn_weth": [month_withdraw.get(m, 0.0) for m in mlist],
        "trading_pnl_weth": [month_pnl_trading.get(m, 0.0) for m in mlist],
    })
    if len(monthly):
        monthly["withdrawn_usd"] = monthly["withdrawn_weth"] * eth_usd
        monthly["hit_target"] = monthly["withdrawn_usd"] >= monthly_target_usd
        monthly["trading_pnl_usd"] = monthly["trading_pnl_weth"] * eth_usd
    else:
        monthly["withdrawn_usd"] = []
        monthly["hit_target"] = []
        monthly["trading_pnl_usd"] = []

    lose = (monthly["trading_pnl_weth"] < 0).astype(int).tolist() if len(monthly) else []
    max_lose_streak = 0
    cur = 0
    for x in lose:
        cur = cur + 1 if x else 0
        max_lose_streak = max(max_lose_streak, cur)

    peak = np.maximum.accumulate(wallet_path) if len(wallet_path) else np.array([])
    dd = float((wallet_path - peak).min()) if len(wallet_path) else 0.0
    w12 = float(monthly["withdrawn_usd"].head(12).sum()) if len(monthly) else 0.0

    summary = {
        "initial_weth": initial_weth,
        "initial_usd": initial_weth * eth_usd,
        "final_wallet_weth": float(cash),
        "final_wallet_usd": float(cash * eth_usd),
        "cum_withdrawn_weth": float(cum_withdrawn),
        "cum_withdrawn_usd": float(cum_withdrawn * eth_usd),
        "withdrawn_12m_usd": w12,
        "fixed_capital_return_on_initial": float(cum_withdrawn / initial_weth) if initial_weth else None,
        "n_months": int(len(monthly)),
        "pct_months_hit_1000_usd": float(monthly["hit_target"].mean()) if len(monthly) else 0.0,
        "median_monthly_withdraw_usd": float(monthly["withdrawn_usd"].median()) if len(monthly) else 0.0,
        "worst_month_withdraw_usd": float(monthly["withdrawn_usd"].min()) if len(monthly) else 0.0,
        "median_monthly_trading_pnl_usd": float(monthly["trading_pnl_usd"].median()) if len(monthly) else 0.0,
        "worst_month_trading_pnl_usd": float(monthly["trading_pnl_usd"].min()) if len(monthly) else 0.0,
        "max_consecutive_losing_months": int(max_lose_streak),
        "ruin": bool(ruined),
        "ruin_time": ruin_time,
        "n_fills": int(n_fills),
        "n_skipped_capital": int(n_skipped_capital),
        "max_drawdown_wallet_weth": dd,
        "compound": bool(compound),
    }
    return curve, monthly, summary


def conservative_tail_haircut_pnl(pnl: pd.Series, q: float = 0.95) -> float:
    x = pd.to_numeric(pnl, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    if len(x) == 0:
        return 0.0
    pos = x[x > 0]
    if len(pos) == 0:
        return float(x.sum())
    cap = float(np.quantile(pos, q))
    y = np.where(x > cap, cap, x)
    return float(y.sum())
