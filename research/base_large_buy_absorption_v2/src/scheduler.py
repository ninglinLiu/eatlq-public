"""Primary scheduled-wallet estimand: fixed $1000, configurable capacity."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import load_freeze


def simulate_scheduled(
    opportunities: pd.DataFrame,
    *,
    score_col: str = "_sched_score",
    selected_col: str = "policy_selected",
    max_concurrent: int | None = None,
    one_position_per_token: bool | None = None,
    max_per_token: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Chronological scheduler with fixed priority among simultaneous candidates.

    Rules:
      - only policy_selected & scorable_primary & buy_status==SUCCESS
      - max_concurrent slots
      - token concurrency: if one_position_per_token, at most 1; else at most max_per_token
      - skip while at capacity / insufficient free capital
      - priority: score desc, then signal_t asc
      - month-end withdraw profits (no compound)

    Capacity overrides (v3 grid): pass max_concurrent / one_position_per_token / max_per_token
    to override freeze defaults without mutating freeze file.
    """
    freeze = load_freeze()
    initial_weth = float(freeze["capital"]["initial_weth"])
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])
    amount = float(freeze["execution"]["primary_size_usd"]) / eth_usd
    max_c = int(max_concurrent if max_concurrent is not None else freeze["capital"]["max_concurrent"])
    target = float(freeze["capital"]["monthly_withdraw_target_usd"])
    one_tok = bool(
        freeze["capital"]["one_position_per_token"]
        if one_position_per_token is None
        else one_position_per_token
    )
    # When one_tok=True, effectively max_per_token=1; when False, default 3 unless overridden
    if max_per_token is not None:
        mpt = int(max_per_token)
    elif one_tok:
        mpt = 1
    else:
        mpt = 3

    d = opportunities.copy()
    if selected_col in d.columns:
        d = d[d[selected_col].astype(bool)]
    if "scorable_primary" in d.columns:
        d = d[d["scorable_primary"].astype(bool)]
    if "buy_status" in d.columns:
        d = d[d["buy_status"].eq("SUCCESS")]
    if d.empty:
        empty = pd.DataFrame()
        return empty, empty, {
            "n_candidates": 0, "n_fills": 0, "n_skipped": 0, "skip_rate": 0.0,
            "cum_withdrawn_usd": 0.0, "pct_months_hit_1000_usd": 0.0,
            "median_monthly_withdraw_usd": 0.0, "ruin": False,
            "unconstrained_opportunity_pnl_weth": 0.0,
            "scheduled_trading_pnl_weth": 0.0,
            "max_concurrent": max_c,
            "one_position_per_token": one_tok,
            "max_per_token": mpt,
        }

    if score_col not in d.columns:
        d[score_col] = 0.0
    d = d.sort_values(["decision_time", score_col], ascending=[True, False]).reset_index(drop=True)

    unconstrained = float(pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0).sum())

    times = d["decision_time"].to_numpy(dtype=np.int64)
    ends = pd.to_numeric(d["label_end_time"], errors="coerce").fillna(d["decision_time"].astype(int) + 14400).to_numpy(dtype=np.int64)
    pnls = pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    tokens = d["token"].astype(str).to_numpy()
    months = pd.to_datetime(times, unit="s", utc=True).strftime("%Y-%m").to_numpy()

    cash = float(initial_weth)
    open_ends: list[int] = []
    open_stakes: list[float] = []
    open_tokens: list[str] = []
    month_withdraw: dict[str, float] = {}
    month_pnl: dict[str, float] = {}
    cum_withdrawn = 0.0
    n_fills = 0
    n_skipped = 0
    ruined = False
    filled_mask = np.zeros(len(d), dtype=bool)
    prev_month = months[0] if len(months) else None
    stake = float(amount)

    for i in range(len(times)):
        t = int(times[i])
        mk = months[i]
        if prev_month is not None and mk != prev_month:
            profit = cash - initial_weth
            w = max(0.0, profit)
            if w > 0:
                cash -= w
                cum_withdrawn += w
            month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w
            prev_month = mk

        if open_ends:
            keep_e, keep_s, keep_t = [], [], []
            for e, s, tok in zip(open_ends, open_stakes, open_tokens):
                if e > t:
                    keep_e.append(e)
                    keep_s.append(s)
                    keep_t.append(tok)
            open_ends, open_stakes, open_tokens = keep_e, keep_s, keep_t

        tok = tokens[i]
        locked = sum(open_stakes)
        free = cash - locked
        at_cap = len(open_ends) >= max_c
        tok_count = sum(1 for x in open_tokens if x == tok)
        tok_held = tok_count >= mpt
        if ruined or at_cap or tok_held or free < stake - 1e-18:
            n_skipped += 1
            continue

        open_ends.append(int(ends[i]))
        open_stakes.append(stake)
        open_tokens.append(tok)
        cash += float(pnls[i])
        n_fills += 1
        filled_mask[i] = True
        month_pnl[mk] = month_pnl.get(mk, 0.0) + float(pnls[i])
        if cash <= 1e-8:
            ruined = True
            cash = max(cash, 0.0)
        prev_month = mk

    if prev_month is not None:
        profit = cash - initial_weth
        w = max(0.0, profit)
        if w > 0:
            cash -= w
            cum_withdrawn += w
        month_withdraw[prev_month] = month_withdraw.get(prev_month, 0.0) + w

    mlist = sorted(set(month_withdraw) | set(month_pnl))
    monthly = pd.DataFrame({
        "month": mlist,
        "withdrawn_weth": [month_withdraw.get(m, 0.0) for m in mlist],
        "trading_pnl_weth": [month_pnl.get(m, 0.0) for m in mlist],
    })
    if len(monthly):
        monthly["withdrawn_usd"] = monthly["withdrawn_weth"] * eth_usd
        monthly["trading_pnl_usd"] = monthly["trading_pnl_weth"] * eth_usd
        monthly["hit_target"] = monthly["withdrawn_usd"] >= target
    fills = d.loc[filled_mask].copy()
    fills["scheduler_filled"] = True
    summary = {
        "n_candidates": int(len(d)),
        "n_fills": int(n_fills),
        "n_skipped": int(n_skipped),
        "skip_rate": float(n_skipped / max(1, n_fills + n_skipped)),
        "cum_withdrawn_weth": float(cum_withdrawn),
        "cum_withdrawn_usd": float(cum_withdrawn * eth_usd),
        "pct_months_hit_1000_usd": float(monthly["hit_target"].mean()) if len(monthly) else 0.0,
        "median_monthly_withdraw_usd": float(monthly["withdrawn_usd"].median()) if len(monthly) else 0.0,
        "p25_monthly_withdraw_usd": float(monthly["withdrawn_usd"].quantile(0.25)) if len(monthly) else 0.0,
        "p5_monthly_withdraw_usd": float(monthly["withdrawn_usd"].quantile(0.05)) if len(monthly) else 0.0,
        "n_months": int(len(monthly)),
        "avg_monthly_withdraw_usd": float(monthly["withdrawn_usd"].mean()) if len(monthly) else 0.0,
        "ruin": bool(ruined),
        "final_wallet_weth": float(cash),
        "unconstrained_opportunity_pnl_weth": unconstrained,
        "scheduled_trading_pnl_weth": float(pnls[filled_mask].sum()) if n_fills else 0.0,
        "max_concurrent": max_c,
        "one_position_per_token": one_tok,
        "max_per_token": mpt,
    }
    return fills, monthly, summary
