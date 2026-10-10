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
    rows: time-sorted opportunities with selection flags and per-trade wallet pnl.
    Month-end: withdraw max(0, cash - initial_weth) unless compound=True.
    Withdrawn profits never re-enter trading capital when compound=False.
    """
    d = rows.sort_values(time_col).reset_index(drop=True).copy()
    cash = float(initial_weth)
    open_pos: list[tuple[int, float]] = []
    equity_rows = []
    month_withdraw = {}
    month_pnl_trading = {}
    cum_withdrawn = 0.0
    n_fills = 0
    n_skipped_capital = 0
    ruined = False
    ruin_time = None

    def close_expired(t: int) -> None:
        nonlocal open_pos
        open_pos = [(e, s) for e, s in open_pos if e > t]

    times = d[time_col].astype(int).tolist()
    # process month ends between events
    prev_month = month_key(times[0]) if times else None

    for r in d.itertuples(index=False):
        t = int(getattr(r, time_col))
        mk = month_key(t)
        if prev_month is not None and mk != prev_month:
            # month boundary: withdraw profits above initial capital
            profit = cash - initial_weth
            w = max(0.0, profit) if not compound else 0.0
            if w > 0:
                cash -= w
                cum_withdrawn += w
            month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w
            prev_month = mk
        close_expired(t)
        locked = sum(s for _, s in open_pos)
        free = cash - locked
        selected = bool(getattr(r, selected_col))
        pnl = float(getattr(r, pnl_col)) if pd.notna(getattr(r, pnl_col)) else 0.0
        filled = False
        if selected and not ruined:
            stake = float(amount_in_weth)
            if len(open_pos) < max_concurrent and free >= stake - 1e-18:
                end_t = int(getattr(r, end_col)) if pd.notna(getattr(r, end_col)) else t + 3600
                # For fatal / roundtrip, pnl already includes capital return effects.
                # Lock stake until end; apply pnl to cash immediately as wallet delta convention.
                open_pos.append((end_t, stake))
                cash += pnl
                filled = True
                n_fills += 1
                month_pnl_trading[mk] = month_pnl_trading.get(mk, 0.0) + pnl
            else:
                n_skipped_capital += 1
                pnl = 0.0
        elif selected and ruined:
            pnl = 0.0

        if cash <= 1e-8 and not ruined:
            ruined = True
            ruin_time = t
            cash = max(cash, 0.0)

        equity_rows.append(
            {
                "decision_time": t,
                "signal_id": getattr(r, "signal_id", None),
                "month": mk,
                "wallet_weth": cash,
                "locked_weth": locked,
                "cum_withdrawn_weth": cum_withdrawn,
                "pnl_event": pnl if filled else 0.0,
                "filled": filled,
                "selected": selected,
            }
        )
        prev_month = mk

    # final month withdraw
    if prev_month is not None:
        profit = cash - initial_weth
        w = max(0.0, profit) if not compound else 0.0
        if w > 0:
            cash -= w
            cum_withdrawn += w
        month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w

    curve = pd.DataFrame(equity_rows)
    months = sorted(set(month_withdraw) | set(month_pnl_trading))
    monthly = pd.DataFrame(
        {
            "month": months,
            "withdrawn_weth": [month_withdraw.get(m, 0.0) for m in months],
            "trading_pnl_weth": [month_pnl_trading.get(m, 0.0) for m in months],
        }
    )
    if len(monthly):
        monthly["withdrawn_usd"] = monthly["withdrawn_weth"] * eth_usd
        monthly["hit_target"] = monthly["withdrawn_usd"] >= monthly_target_usd
        monthly["trading_pnl_usd"] = monthly["trading_pnl_weth"] * eth_usd
    else:
        monthly["withdrawn_usd"] = []
        monthly["hit_target"] = []
        monthly["trading_pnl_usd"] = []

    # consecutive losing months (trading pnl < 0)
    lose = (monthly["trading_pnl_weth"] < 0).astype(int).tolist() if len(monthly) else []
    max_lose_streak = 0
    cur = 0
    for x in lose:
        cur = cur + 1 if x else 0
        max_lose_streak = max(max_lose_streak, cur)

    peak = curve["wallet_weth"].cummax() if len(curve) else pd.Series(dtype=float)
    dd = float((curve["wallet_weth"] - peak).min()) if len(curve) else 0.0

    # 12-month cumulative withdraw (or all months if fewer)
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
