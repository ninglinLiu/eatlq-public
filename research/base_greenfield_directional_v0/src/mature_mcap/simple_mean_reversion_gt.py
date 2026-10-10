#!/usr/bin/env python3
"""Buy-low/sell-high probes using GeckoTerminal hourly OHLCV on DS mcap>=$1M pairs."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import utcnow  # noqa: E402
from src.mature_mcap.simple_mean_reversion import (  # noqa: E402
    INITIAL_USD,
    STAKE_USD,
    add_features,
    mark_entries,
    run_bh,
    simulate_trades,
)

OUT = ROOT / "out_mature_mcap" / "mean_reversion_gt"


def fetch_gt_hourly(pool: str, max_pages: int = 20) -> pd.DataFrame:
    """Paginate GeckoTerminal hour OHLCV for a Base pool address."""
    pool = pool.lower()
    rows = []
    before = None
    for page in range(max_pages):
        url = (
            f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/hour"
            f"?aggregate=1&limit=1000&currency=usd"
        )
        if before is not None:
            url += f"&before_timestamp={before}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 mature-mr", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                d = json.loads(resp.read().decode())
        except Exception as e:
            print(f"[gt] fail {pool} page={page}: {e}", flush=True)
            break
        lst = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        if not lst:
            break
        rows.extend(lst)
        before = int(min(x[0] for x in lst))
        if len(lst) < 50:
            break
        time.sleep(0.35)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["hour"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df["close_usd"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol_weth"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) / 3000.0  # rough
    df = df.dropna(subset=["close_usd"]).drop_duplicates("hour").sort_values("hour").reset_index(drop=True)
    return df[["hour", "close_usd", "vol_weth"]]


def load_universe() -> pd.DataFrame:
    d = pd.read_csv(ROOT / "out_mature_mcap" / "DEXSCREENER_CURRENT_MCAP_GE_1M.csv")
    d = d[~d["is_stable_or_core"].astype(bool)].copy()
    return d


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    uni = load_universe()
    strategies = ["Z72_BUY", "Z24_BUY", "RSI_OS", "DIP10_168H", "DIP20_168H", "MA72_BELOW", "BH_ALWAYS"]
    score_rows, fills_all, meta = [], [], []

    for r in uni.itertuples(index=False):
        sym, tok, pair = str(r.symbol), str(r.token).lower(), str(r.ds_pair)
        print(f"[gt] {sym} pair={pair}", flush=True)
        bars = fetch_gt_hourly(pair)
        cache = OUT / "cache"
        cache.mkdir(exist_ok=True)
        if len(bars):
            bars.to_parquet(cache / f"bars_{tok}.parquet", index=False)
        meta.append({"symbol": sym, "token": tok, "pair": pair, "n_hours": int(len(bars)), "status": "OK" if len(bars) >= 80 else "TOO_SHORT"})
        if len(bars) < 80:
            print(f"[gt] {sym} too short n={len(bars)}", flush=True)
            continue
        feat = add_features(bars)
        feat["token"] = tok
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
                    marked["token"] = tok
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
                        "pair": pair,
                        "strategy": strat,
                        "split": split,
                        "n_hours": int(len(part)),
                        "n_trades": res["n_trades"],
                        "total_pnl_usd": res["total_pnl_usd"],
                        "win_rate": res["win_rate"],
                        "avg_pnl_usd": res["avg_pnl_usd"],
                        "final_equity_usd": res["final_equity_usd"],
                        "ds_marketCap": float(r.ds_marketCap),
                    }
                )
                if len(res["fills"]):
                    ff = res["fills"].copy()
                    ff["symbol"] = sym
                    fills_all.append(ff)

    score = pd.DataFrame(score_rows)
    score.to_csv(OUT / "STRATEGY_SCORECARD.csv", index=False)
    pd.DataFrame(meta).to_csv(OUT / "BAR_META.csv", index=False)
    if fills_all:
        pd.concat(fills_all, ignore_index=True).to_parquet(OUT / "FILLS.parquet", index=False)

    oos = score[score["split"] == "OOS"] if len(score) else score
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
        if len(oos)
        else pd.DataFrame()
    )
    by.to_csv(OUT / "OOS_BY_STRATEGY.csv", index=False)
    if len(oos):
        best = oos.loc[oos.groupby("token")["total_pnl_usd"].idxmax()].sort_values("total_pnl_usd", ascending=False)
        best.to_csv(OUT / "OOS_BEST_PER_TOKEN.csv", index=False)
        # full OOS matrix
        piv = oos.pivot_table(index="symbol", columns="strategy", values="total_pnl_usd", aggfunc="sum")
        piv.to_csv(OUT / "OOS_PNL_MATRIX.csv")
    else:
        best = pd.DataFrame()
        piv = pd.DataFrame()

    summary = {
        "created_utc": utcnow(),
        "price_source": "geckoterminal_hourly_ohlcv_usd",
        "universe": "DexScreener current mcap>=$1M Base, exclude stables",
        "n_tokens": int(len(uni)),
        "n_tokens_with_bars": int(sum(1 for m in meta if m["status"] == "OK")),
        "capital_usd": INITIAL_USD,
        "stake_usd": STAKE_USD,
        "oos_by_strategy": by.to_dict(orient="records") if len(by) else [],
        "any_oos_strategy_total_positive": bool(len(by) and by["total_pnl_usd"].max() > 0),
        "deploy_allowed": False,
    }
    (OUT / "SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")
    (OUT / "README.md").write_text(
        "# GT mean-reversion probe\n\n"
        + f"Generated `{summary['created_utc']}`\n\n## OOS by strategy\n\n```\n"
        + (by.to_string(index=False) if len(by) else "none")
        + "\n```\n\n## Best OOS per token\n\n```\n"
        + (best[["symbol", "strategy", "n_trades", "total_pnl_usd", "win_rate"]].to_string(index=False) if len(best) else "none")
        + "\n```\n\n## OOS PnL matrix\n\n```\n"
        + (piv.to_string() if len(piv) else "none")
        + "\n```\n"
    )
    print(json.dumps(summary, indent=2), flush=True)
    print(by.to_string(index=False) if len(by) else "no oos", flush=True)
    if len(best):
        print(best[["symbol", "strategy", "n_trades", "total_pnl_usd"]].to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
