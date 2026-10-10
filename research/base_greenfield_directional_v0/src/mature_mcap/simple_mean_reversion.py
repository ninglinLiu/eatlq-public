#!/usr/bin/env python3
"""Simple buy-low/sell-high probes on DexScreener current mcap>=$1M Base tokens.

Uses UniV3 WETH-leg tick prices from ClickHouse. Research only; deploy_allowed=false.
Not new-pool Rescue.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import ch_query, utcnow  # noqa: E402

OUT = ROOT / "out_mature_mcap" / "mean_reversion"
WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 3000.0
INITIAL_USD = 1000.0
STAKE_USD = 150.0  # per trade
GAS_USD = 0.50  # roundtrip proxy on Base
SLIP_RT = 0.004  # 40bps roundtrip friction proxy
MAX_CONCURRENT = 2


def load_universe() -> pd.DataFrame:
    p = ROOT / "out_mature_mcap" / "DEXSCREENER_CURRENT_MCAP_GE_1M.csv"
    d = pd.read_csv(p)
    # drop stables; keep cbETH as hard control (should be near-flat in ETH terms if priced vs WETH poorly — still include)
    d = d[~d["is_stable_or_core"].astype(bool)].copy()
    return d


def fetch_hourly_bars(token: str) -> pd.DataFrame:
    tok = token.lower()
    sql = f"""
    WITH lower('{WETH}') AS weth,
    pools AS (
      SELECT
        lower(pool_address) AS pool,
        if(lower(token0_address)=weth, 1, 0) AS weth_is_token0
      FROM eatlq.discovered_pools
      WHERE pool_type='v3' AND has_weth_leg=1
        AND (
          (lower(token0_address)=weth AND lower(token1_address)='{tok}')
          OR (lower(token1_address)=weth AND lower(token0_address)='{tok}')
        )
    )
    SELECT
      toStartOfHour(s.block_time) AS hour,
      argMax(
        if(
          p.weth_is_token0=1,
          {ETH_USD} / greatest(pow(1.0001, toFloat64(s.tick)), 1e-30),
          {ETH_USD} * pow(1.0001, toFloat64(s.tick))
        ),
        s.block_time
      ) AS close_usd,
      count() AS n_swaps,
      sum(if(p.weth_is_token0=1, abs(toFloat64(s.amount0)), abs(toFloat64(s.amount1))))/1e18 AS vol_weth
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN pools AS p ON lower(s.pool_address)=p.pool
    WHERE s.block_time IS NOT NULL AND s.tick IS NOT NULL
    GROUP BY hour
    ORDER BY hour
    SETTINGS max_bytes_before_external_group_by='10G'
    """
    df = ch_query(sql, timeout=600)
    if df.empty:
        return df
    df["hour"] = pd.to_datetime(df["hour"], utc=True)
    df["close_usd"] = pd.to_numeric(df["close_usd"], errors="coerce")
    df = df.dropna(subset=["close_usd"]).sort_values("hour").reset_index(drop=True)
    # winsorize insane spikes
    lo, hi = df["close_usd"].quantile(0.005), df["close_usd"].quantile(0.995)
    df["close_usd"] = df["close_usd"].clip(lo, hi)
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ret_1h"] = d["close_usd"].pct_change()
    for w in (24, 72, 168):
        d[f"ma_{w}"] = d["close_usd"].rolling(w, min_periods=max(10, w // 3)).mean()
        d[f"std_{w}"] = d["close_usd"].rolling(w, min_periods=max(10, w // 3)).std()
        d[f"z_{w}"] = (d["close_usd"] - d[f"ma_{w}"]) / d[f"std_{w}"].replace(0, np.nan)
        d[f"dd_from_high_{w}"] = d["close_usd"] / d["close_usd"].rolling(w, min_periods=max(10, w // 3)).max() - 1.0
    # RSI 14
    delta = d["close_usd"].diff()
    up = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    down = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = up / down.replace(0, np.nan)
    d["rsi_14"] = 100 - (100 / (1 + rs))
    return d


def simulate_trades(entries: pd.DataFrame, exits_rule: str, params: dict) -> dict:
    """entries has hour, close_usd, signal columns; long-only sequential with caps."""
    cash = INITIAL_USD
    open_pos = None  # dict
    fills = []
    equity = []
    for r in entries.itertuples(index=False):
        px = float(r.close_usd)
        t = r.hour
        # manage open
        if open_pos is not None:
            entry_px = open_pos["entry_px"]
            ret = px / entry_px - 1.0
            hours_held = (pd.Timestamp(t) - pd.Timestamp(open_pos["entry_t"])).total_seconds() / 3600.0
            take = False
            reason = ""
            if exits_rule == "z_revert":
                z = getattr(r, "z_72", np.nan)
                if pd.notna(z) and z >= params.get("exit_z", 0.0):
                    take, reason = True, "z_revert"
                elif ret <= -params.get("sl", 0.15):
                    take, reason = True, "stop"
                elif hours_held >= params.get("max_hold_h", 168):
                    take, reason = True, "time"
            elif exits_rule == "tp_sl":
                if ret >= params.get("tp", 0.08):
                    take, reason = True, "tp"
                elif ret <= -params.get("sl", 0.10):
                    take, reason = True, "sl"
                elif hours_held >= params.get("max_hold_h", 72):
                    take, reason = True, "time"
            elif exits_rule == "ma_cross":
                ma = getattr(r, "ma_72", np.nan)
                if pd.notna(ma) and px >= ma:
                    take, reason = True, "ma"
                elif ret <= -params.get("sl", 0.12):
                    take, reason = True, "stop"
                elif hours_held >= params.get("max_hold_h", 120):
                    take, reason = True, "time"
            if take:
                pnl = STAKE_USD * (ret - SLIP_RT) - GAS_USD
                cash += STAKE_USD + pnl  # return stake+pnl; stake was reserved conceptually
                # simplify: we deducted stake at entry from cash; add back stake*(1+ret)-fees
                fills.append(
                    {
                        "entry_t": open_pos["entry_t"],
                        "exit_t": t,
                        "entry_px": entry_px,
                        "exit_px": px,
                        "ret_gross": ret,
                        "pnl_usd": pnl,
                        "reason": reason,
                        "token": open_pos["token"],
                        "strategy": open_pos["strategy"],
                        "split": open_pos["split"],
                    }
                )
                open_pos = None

        # entry
        if open_pos is None and bool(getattr(r, "enter", False)) and cash >= STAKE_USD + GAS_USD:
            cash -= STAKE_USD  # lock stake
            open_pos = {
                "entry_t": t,
                "entry_px": px * (1 + SLIP_RT / 2),
                "token": getattr(r, "token", ""),
                "strategy": getattr(r, "strategy", ""),
                "split": getattr(r, "split", ""),
            }
        locked = STAKE_USD if open_pos is not None else 0.0
        mtm = 0.0
        if open_pos is not None:
            mtm = STAKE_USD * (px / open_pos["entry_px"] - 1.0)
        equity.append({"hour": t, "equity_usd": cash + locked + mtm})

    # force close
    if open_pos is not None and len(entries):
        px = float(entries.iloc[-1]["close_usd"])
        ret = px / open_pos["entry_px"] - 1.0
        pnl = STAKE_USD * (ret - SLIP_RT) - GAS_USD
        cash += STAKE_USD + pnl
        fills.append(
            {
                "entry_t": open_pos["entry_t"],
                "exit_t": entries.iloc[-1]["hour"],
                "entry_px": open_pos["entry_px"],
                "exit_px": px,
                "ret_gross": ret,
                "pnl_usd": pnl,
                "reason": "eod",
                "token": open_pos["token"],
                "strategy": open_pos["strategy"],
                "split": open_pos["split"],
            }
        )

    f = pd.DataFrame(fills)
    eq = pd.DataFrame(equity)
    total_pnl = float(f["pnl_usd"].sum()) if len(f) else 0.0
    return {
        "n_trades": int(len(f)),
        "total_pnl_usd": total_pnl,
        "win_rate": float((f["pnl_usd"] > 0).mean()) if len(f) else None,
        "avg_pnl_usd": float(f["pnl_usd"].mean()) if len(f) else None,
        "median_pnl_usd": float(f["pnl_usd"].median()) if len(f) else None,
        "final_equity_usd": float(INITIAL_USD + total_pnl),
        "max_dd_usd": float((eq["equity_usd"] - eq["equity_usd"].cummax()).min()) if len(eq) else None,
        "fills": f,
    }


def mark_entries(df: pd.DataFrame, strategy: str) -> pd.DataFrame:
    d = df.copy()
    d["enter"] = False
    if strategy == "Z72_BUY":
        # buy when z<-1.5
        d["enter"] = d["z_72"] <= -1.5
        exits, params = "z_revert", {"exit_z": 0.0, "sl": 0.15, "max_hold_h": 168}
    elif strategy == "Z24_BUY":
        d["enter"] = d["z_24"] <= -1.25
        exits, params = "z_revert", {"exit_z": 0.25, "sl": 0.12, "max_hold_h": 72}
    elif strategy == "RSI_OS":
        d["enter"] = d["rsi_14"] <= 30
        exits, params = "tp_sl", {"tp": 0.08, "sl": 0.10, "max_hold_h": 72}
    elif strategy == "DIP10_168H":
        d["enter"] = d["dd_from_high_168"] <= -0.10
        exits, params = "tp_sl", {"tp": 0.12, "sl": 0.10, "max_hold_h": 168}
    elif strategy == "DIP20_168H":
        d["enter"] = d["dd_from_high_168"] <= -0.20
        exits, params = "tp_sl", {"tp": 0.15, "sl": 0.12, "max_hold_h": 240}
    elif strategy == "MA72_BELOW":
        d["enter"] = d["close_usd"] < 0.95 * d["ma_72"]
        exits, params = "ma_cross", {"sl": 0.12, "max_hold_h": 120}
    elif strategy == "BH_ALWAYS":
        # single buy at first valid bar of split, hold to end — handled specially
        d["enter"] = False
        exits, params = "tp_sl", {"tp": 99, "sl": 0.99, "max_hold_h": 10**9}
    else:
        raise ValueError(strategy)
    return d, exits, params


def run_bh(df: pd.DataFrame, token: str, split: str) -> dict:
    if len(df) < 2:
        return {"n_trades": 0, "total_pnl_usd": 0.0, "win_rate": None, "avg_pnl_usd": None, "median_pnl_usd": None, "final_equity_usd": INITIAL_USD, "max_dd_usd": 0.0, "fills": pd.DataFrame()}
    entry_px = float(df.iloc[0]["close_usd"]) * (1 + SLIP_RT / 2)
    exit_px = float(df.iloc[-1]["close_usd"]) * (1 - SLIP_RT / 2)
    if entry_px <= 0 or exit_px <= 0 or not np.isfinite(entry_px) or not np.isfinite(exit_px):
        return {"n_trades": 0, "total_pnl_usd": 0.0, "win_rate": None, "avg_pnl_usd": None, "median_pnl_usd": None, "final_equity_usd": INITIAL_USD, "max_dd_usd": 0.0, "fills": pd.DataFrame()}
    ret = float(np.clip(exit_px / entry_px - 1.0, -1.0, 1.0))
    pnl = STAKE_USD * ret - GAS_USD
    f = pd.DataFrame(
        [
            {
                "entry_t": df.iloc[0]["hour"],
                "exit_t": df.iloc[-1]["hour"],
                "entry_px": entry_px,
                "exit_px": exit_px,
                "ret_gross": ret,
                "pnl_usd": pnl,
                "reason": "bh",
                "token": token,
                "strategy": "BH_ALWAYS",
                "split": split,
            }
        ]
    )
    return {
        "n_trades": 1,
        "total_pnl_usd": float(pnl),
        "win_rate": float(pnl > 0),
        "avg_pnl_usd": float(pnl),
        "median_pnl_usd": float(pnl),
        "final_equity_usd": INITIAL_USD + float(pnl),
        "max_dd_usd": None,
        "fills": f,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    uni = load_universe()
    strategies = ["Z72_BUY", "Z24_BUY", "RSI_OS", "DIP10_168H", "DIP20_168H", "MA72_BELOW", "BH_ALWAYS"]
    score_rows = []
    all_fills = []
    bar_meta = []

    for r in uni.itertuples(index=False):
        tok = str(r.token).lower()
        sym = str(r.symbol)
        print(f"[mr] bars {sym} {tok}", flush=True)
        bars = fetch_hourly_bars(tok)
        if len(bars) < 80:
            bar_meta.append({"symbol": sym, "token": tok, "n_hours": int(len(bars)), "status": "TOO_SHORT"})
            continue
        feat = add_features(bars)
        feat["token"] = tok
        feat["symbol"] = sym
        # 70/30 time split
        cut = feat["hour"].quantile(0.70)
        for split, part in [("IS", feat[feat["hour"] <= cut]), ("OOS", feat[feat["hour"] > cut])]:
            if len(part) < 40:
                continue
            for strat in strategies:
                if strat == "BH_ALWAYS":
                    res = run_bh(part, tok, split)
                else:
                    marked, exits, params = mark_entries(part, strat)
                    marked = marked.copy()
                    marked["strategy"] = strat
                    marked["split"] = split
                    # cooldown: don't enter every consecutive hour — enter on signal edge
                    ent = marked["enter"].astype(bool).to_numpy()
                    edge = np.zeros(len(ent), dtype=bool)
                    edge[0] = ent[0]
                    edge[1:] = ent[1:] & (~ent[:-1])
                    marked["enter"] = edge
                    res = simulate_trades(marked, exits, params)
                score_rows.append(
                    {
                        "symbol": sym,
                        "token": tok,
                        "strategy": strat,
                        "split": split,
                        "n_hours": int(len(part)),
                        "n_trades": res["n_trades"],
                        "total_pnl_usd": res["total_pnl_usd"],
                        "win_rate": res["win_rate"],
                        "avg_pnl_usd": res["avg_pnl_usd"],
                        "final_equity_usd": res["final_equity_usd"],
                        "max_dd_usd": res["max_dd_usd"],
                        "ds_marketCap": float(r.ds_marketCap),
                    }
                )
                if len(res["fills"]):
                    ff = res["fills"].copy()
                    ff["symbol"] = sym
                    all_fills.append(ff)
        bar_meta.append(
            {
                "symbol": sym,
                "token": tok,
                "n_hours": int(len(bars)),
                "first": str(bars["hour"].iloc[0]),
                "last": str(bars["hour"].iloc[-1]),
                "status": "OK",
                "vol_weth_sum": float(bars["vol_weth"].sum()),
            }
        )

    score = pd.DataFrame(score_rows)
    score.to_csv(OUT / "STRATEGY_SCORECARD.csv", index=False)
    meta = pd.DataFrame(bar_meta)
    meta.to_csv(OUT / "BAR_META.csv", index=False)
    if all_fills:
        pd.concat(all_fills, ignore_index=True).to_parquet(OUT / "FILLS.parquet", index=False)

    # OOS summary by strategy (portfolio = sum token pnls, independent books)
    oos = score[score["split"] == "OOS"].copy()
    by = (
        oos.groupby("strategy", as_index=False)
        .agg(
            n_tokens=("token", "nunique"),
            n_trades=("n_trades", "sum"),
            total_pnl_usd=("total_pnl_usd", "sum"),
            median_token_pnl=("total_pnl_usd", "median"),
            n_tokens_profitable=("total_pnl_usd", lambda s: int((s > 0).sum())),
        )
        .sort_values("total_pnl_usd", ascending=False)
    )
    by.to_csv(OUT / "OOS_BY_STRATEGY.csv", index=False)

    # best per token OOS
    if len(oos):
        idx = oos.groupby("token")["total_pnl_usd"].idxmax()
        best = oos.loc[idx].sort_values("total_pnl_usd", ascending=False)
        best.to_csv(OUT / "OOS_BEST_PER_TOKEN.csv", index=False)
    else:
        best = pd.DataFrame()

    summary = {
        "created_utc": utcnow(),
        "universe": "DexScreener current mcap>=$1M on Base, exclude stables",
        "n_tokens": int(len(uni)),
        "capital_usd": INITIAL_USD,
        "stake_usd": STAKE_USD,
        "friction": {"slip_rt": SLIP_RT, "gas_usd": GAS_USD},
        "split": "70% IS / 30% OOS by time",
        "oos_strategy_pnl": by.to_dict(orient="records") if len(by) else [],
        "any_oos_strategy_total_positive": bool(len(by) and by["total_pnl_usd"].max() > 0),
        "deploy_allowed": False,
        "note": "Simple mid-price backtest from UniV3 ticks; not atomic wallet execution. Diagnostic only.",
    }
    (OUT / "SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")

    md = ["# Mature-token simple mean-reversion probe", "", f"Generated: `{summary['created_utc']}`", "", "## OOS totals by strategy (sum across tokens)", "", "```", by.to_string(index=False) if len(by) else "none", "```", "", "## Per-token best OOS strategy", "", "```", best[["symbol", "strategy", "n_trades", "total_pnl_usd", "win_rate"]].to_string(index=False) if len(best) else "none", "```", "", "## Caveats", "- Price from UniV3 WETH pool ticks (some DS pairs are Aerodrome-primary).", "- Friction = 40bps RT + $0.5 gas; not full atomic simulation.", "- `deploy_allowed=false`.", ""]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "README.md").write_text("\n".join(md) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    print(by.to_string(index=False) if len(by) else "no oos", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
