#!/usr/bin/env python3
"""PIT first-cross $0.3M/$0.5M FDV proxy, then simple buy-low/sell-high on post-cross window.

Universe = HIT_CONSERVATIVE_{300000|500000}.csv (non-stable).
FDV_t ≈ UniV3 WETH-leg hour close (USD) × current totalSupply.
First hour with FDV_t >= threshold is the PIT cross; trading only afterwards.

deploy_allowed=false. Not new-pool Rescue.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import ch_query, utcnow  # noqa: E402
from src.mature_mcap.simple_mean_reversion import (  # noqa: E402
    INITIAL_USD,
    STAKE_USD,
    add_features,
    mark_entries,
    run_bh,
    simulate_trades,
)

OUT = ROOT / "out_mature_mcap" / "pit_cross_mr"
WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 3000.0
THRESHOLDS = [300_000, 500_000]
STRATEGIES = ["Z72_BUY", "Z24_BUY", "RSI_OS", "DIP10_168H", "DIP20_168H", "MA72_BELOW", "BH_ALWAYS"]
MIN_POST_HOURS = 80
MIN_OOS_HOURS = 40


def load_universe(threshold: int) -> pd.DataFrame:
    path = ROOT / "out_mature_mcap" / f"HIT_CONSERVATIVE_{threshold}.csv"
    d = pd.read_csv(path)
    if "is_stable_or_core" in d.columns:
        d = d[~d["is_stable_or_core"].astype(bool)].copy()
    else:
        stables = {"usdc", "usdbc", "dai", "usdt", "weth", "eth", "cbeth"}
        d = d[~d["symbol"].astype(str).str.lower().isin(stables)].copy()
        d = d[d["token"].astype(str).str.lower() != "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"]
        d = d[d["token"].astype(str).str.lower() != "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"]
    d["token"] = d["token"].astype(str).str.lower()
    d["total_supply"] = pd.to_numeric(d["total_supply"], errors="coerce")
    d = d[d["total_supply"].notna() & (d["total_supply"] > 0)].copy()
    return d


def export_hourly_for_tokens(tokens: list[str], cache_path: Path) -> pd.DataFrame:
    if cache_path.exists():
        print(f"[pit] load hourly cache {cache_path}", flush=True)
        return pd.read_parquet(cache_path)
    rows = []
    chunk = 80
    for i in range(0, len(tokens), chunk):
        chunk_t = tokens[i : i + chunk]
        in_list = ",".join(f"'{t}'" for t in chunk_t)
        sql = f"""
        WITH lower('{WETH}') AS weth,
        pools AS (
          SELECT
            lower(pool_address) AS pool,
            if(lower(token0_address)=weth, lower(token1_address), lower(token0_address)) AS alt,
            if(lower(token0_address)=weth, 1, 0) AS weth_is_token0
          FROM eatlq.discovered_pools
          WHERE pool_type='v3' AND has_weth_leg=1
            AND if(lower(token0_address)=weth, lower(token1_address), lower(token0_address)) IN ({in_list})
        )
        SELECT
          p.alt AS token,
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
        GROUP BY token, hour
        SETTINGS max_bytes_before_external_group_by='20G'
        """
        part = ch_query(sql, timeout=1200)
        rows.append(part)
        print(f"[pit] hourly chunk {i//chunk+1}/{(len(tokens)+chunk-1)//chunk} rows={len(part)}", flush=True)
    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if len(df):
        df["hour"] = pd.to_datetime(df["hour"], utc=True)
        df["close_usd"] = pd.to_numeric(df["close_usd"], errors="coerce")
        df["token"] = df["token"].astype(str).str.lower()
        df = df.dropna(subset=["close_usd"]).copy()
        # per-token winsorize without dropping identity columns
        clipped = []
        for tok, g in df.groupby("token", sort=False):
            g = g.copy()
            lo, hi = g["close_usd"].quantile(0.005), g["close_usd"].quantile(0.995)
            g["close_usd"] = g["close_usd"].clip(lo, hi)
            clipped.append(g)
        df = pd.concat(clipped, ignore_index=True) if clipped else df
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def sanitize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Drop near-zero / absurd tick prints that explode returns."""
    b = bars.sort_values("hour").reset_index(drop=True).copy()
    if len(b) < 5:
        return b.iloc[0:0]
    med = float(b["close_usd"].median())
    if not np.isfinite(med) or med <= 0:
        return b.iloc[0:0]
    # keep prices within 1e-4x .. 1e4x of median and above tiny floor
    lo, hi = med * 1e-4, med * 1e4
    b = b[(b["close_usd"] >= max(lo, 1e-18)) & (b["close_usd"] <= hi)].copy()
    return b.reset_index(drop=True)


def first_cross(bars: pd.DataFrame, supply: float, threshold: float) -> pd.Timestamp | None:
    fdv = bars["close_usd"].to_numpy(dtype=float) * float(supply)
    hit = np.where(fdv >= threshold)[0]
    if len(hit) == 0:
        return None
    return pd.Timestamp(bars.iloc[int(hit[0])]["hour"])


def _clip_fills(fills: pd.DataFrame, max_abs_ret: float = 1.0) -> pd.DataFrame:
    if fills is None or len(fills) == 0:
        return fills
    f = fills.copy()
    f["ret_gross"] = pd.to_numeric(f["ret_gross"], errors="coerce").clip(-max_abs_ret, max_abs_ret)
    # recompute pnl with clipped ret and same friction assumptions as simulator
    from src.mature_mcap.simple_mean_reversion import STAKE_USD, SLIP_RT, GAS_USD

    f["pnl_usd"] = STAKE_USD * (f["ret_gross"] - SLIP_RT) - GAS_USD
    return f


def eval_token(bars: pd.DataFrame, token: str, symbol: str, supply: float, threshold: float) -> tuple[list[dict], list[pd.DataFrame], dict]:
    bars = sanitize_bars(bars)
    cross = first_cross(bars, supply, threshold)
    meta = {
        "token": token,
        "symbol": symbol,
        "threshold": threshold,
        "n_hours_raw": int(len(bars)),
        "cross_hour": str(cross) if cross is not None else None,
        "status": "NO_CROSS",
    }
    if cross is None:
        return [], [], meta
    post = bars[bars["hour"] >= cross].copy()
    meta["n_hours_post"] = int(len(post))
    if len(post) < MIN_POST_HOURS:
        meta["status"] = "POST_TOO_SHORT"
        return [], [], meta
    feat = add_features(post)
    feat["token"] = token
    cut = feat["hour"].quantile(0.70)
    scores = []
    fills = []
    for split, part in [("IS", feat[feat["hour"] <= cut]), ("OOS", feat[feat["hour"] > cut])]:
        if split == "OOS" and len(part) < MIN_OOS_HOURS:
            continue
        if len(part) < 30:
            continue
        for strat in STRATEGIES:
            if strat == "BH_ALWAYS":
                res = run_bh(part, token, split)
            else:
                marked, exits, params = mark_entries(part, strat)
                marked = marked.copy()
                marked["strategy"] = strat
                marked["split"] = split
                marked["token"] = token
                ent = marked["enter"].astype(bool).to_numpy()
                edge = np.zeros(len(ent), dtype=bool)
                if len(ent):
                    edge[0] = ent[0]
                    if len(ent) > 1:
                        edge[1:] = ent[1:] & (~ent[:-1])
                marked["enter"] = edge
                res = simulate_trades(marked, exits, params)
            # clip pathological trade returns (+/-100%) then recompute totals
            ff = _clip_fills(res["fills"]) if len(res["fills"]) else res["fills"]
            total_pnl = float(ff["pnl_usd"].sum()) if ff is not None and len(ff) else 0.0
            win_rate = float((ff["pnl_usd"] > 0).mean()) if ff is not None and len(ff) else None
            avg_pnl = float(ff["pnl_usd"].mean()) if ff is not None and len(ff) else None
            scores.append(
                {
                    "symbol": symbol,
                    "token": token,
                    "threshold": threshold,
                    "strategy": strat,
                    "split": split,
                    "n_hours": int(len(part)),
                    "n_trades": int(len(ff)) if ff is not None else 0,
                    "total_pnl_usd": total_pnl,
                    "win_rate": win_rate,
                    "avg_pnl_usd": avg_pnl,
                    "cross_hour": str(cross),
                }
            )
            if ff is not None and len(ff):
                ff = ff.copy()
                ff["symbol"] = symbol
                ff["threshold"] = threshold
                fills.append(ff)
    meta["status"] = "OK"
    return scores, fills, meta


def summarize(score: pd.DataFrame, threshold: int, out_dir: Path) -> dict:
    oos = score[score["split"] == "OOS"] if len(score) else score
    by = (
        oos.groupby("strategy", as_index=False)
        .agg(
            n_tokens=("token", "nunique"),
            n_trades=("n_trades", "sum"),
            total_pnl_usd=("total_pnl_usd", "sum"),
            median_token_pnl=("total_pnl_usd", "median"),
            mean_token_pnl=("total_pnl_usd", "mean"),
            n_tokens_profitable=("total_pnl_usd", lambda s: int((s > 0).sum())),
        )
        .sort_values("total_pnl_usd", ascending=False)
        if len(oos)
        else pd.DataFrame()
    )
    by.to_csv(out_dir / "OOS_BY_STRATEGY.csv", index=False)
    if len(oos):
        best = oos.loc[oos.groupby("token")["total_pnl_usd"].idxmax()].sort_values("total_pnl_usd", ascending=False)
        best.to_csv(out_dir / "OOS_BEST_PER_TOKEN.csv", index=False)
        # equal-weight portfolio interpretation: sum of per-token books
        top5 = best.head(5)[["symbol", "strategy", "n_trades", "total_pnl_usd", "win_rate"]]
    else:
        best = pd.DataFrame()
        top5 = pd.DataFrame()
    summary = {
        "threshold_usd": threshold,
        "n_score_rows": int(len(score)),
        "n_oos_tokens": int(oos["token"].nunique()) if len(oos) else 0,
        "any_oos_strategy_total_positive": bool(len(by) and by["total_pnl_usd"].max() > 0),
        "best_oos_strategy": (by.iloc[0].to_dict() if len(by) else None),
        "oos_by_strategy": by.to_dict(orient="records") if len(by) else [],
        "top5_best_per_token": top5.to_dict(orient="records") if len(top5) else [],
    }
    return summary


def run_threshold(threshold: int, hourly: pd.DataFrame, uni: pd.DataFrame) -> dict:
    out_dir = OUT / f"th_{threshold}"
    out_dir.mkdir(parents=True, exist_ok=True)
    supply_map = uni.set_index("token")["total_supply"].to_dict()
    sym_map = uni.set_index("token")["symbol"].to_dict()
    tokens = [t for t in uni["token"].tolist() if t in set(hourly["token"])]
    print(f"[pit] threshold={threshold} universe={len(uni)} with_bars={len(tokens)}", flush=True)

    all_scores, all_fills, metas = [], [], []
    for i, tok in enumerate(tokens):
        bars = hourly[hourly["token"] == tok]
        sc, fl, meta = eval_token(bars, tok, str(sym_map.get(tok, "")), float(supply_map[tok]), threshold)
        metas.append(meta)
        all_scores.extend(sc)
        all_fills.extend(fl)
        if (i + 1) % 25 == 0:
            print(f"[pit] {threshold} processed {i+1}/{len(tokens)} ok={sum(1 for m in metas if m['status']=='OK')}", flush=True)

    meta_df = pd.DataFrame(metas)
    meta_df.to_csv(out_dir / "TOKEN_META.csv", index=False)
    score = pd.DataFrame(all_scores)
    score.to_csv(out_dir / "STRATEGY_SCORECARD.csv", index=False)
    if all_fills:
        pd.concat(all_fills, ignore_index=True).to_parquet(out_dir / "FILLS.parquet", index=False)
    summary = summarize(score, threshold, out_dir)
    summary["token_status_counts"] = meta_df["status"].value_counts().to_dict() if len(meta_df) else {}
    (out_dir / "SUMMARY.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps({k: summary[k] for k in ["threshold_usd", "n_oos_tokens", "any_oos_strategy_total_positive", "best_oos_strategy", "token_status_counts"]}, indent=2, default=str), flush=True)
    return summary


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    # union tokens for one hourly extract
    uni_map = {th: load_universe(th) for th in THRESHOLDS}
    all_tokens = sorted(set().union(*[set(u["token"]) for u in uni_map.values()]))
    print(f"[pit] unique tokens across thresholds: {len(all_tokens)}", flush=True)
    hourly = export_hourly_for_tokens(all_tokens, OUT / "cache" / "hourly_all.parquet")
    print(f"[pit] hourly rows={len(hourly)} tokens={hourly['token'].nunique() if len(hourly) else 0}", flush=True)

    finals = {
        "created_utc": utcnow(),
        "fdv_definition": "hour_close_usd_from_univ3_weth_tick * current_totalSupply",
        "pit_rule": "trade only after first hour with FDV>=threshold",
        "capital_usd": INITIAL_USD,
        "stake_usd": STAKE_USD,
        "deploy_allowed": False,
        "by_threshold": {},
    }
    for th in THRESHOLDS:
        finals["by_threshold"][str(th)] = run_threshold(th, hourly, uni_map[th])

    (OUT / "FINAL_SUMMARY.json").write_text(json.dumps(finals, indent=2, default=str) + "\n")
    # short markdown
    lines = ["# PIT first-cross mean-reversion", "", f"Generated `{finals['created_utc']}`", ""]
    for th in THRESHOLDS:
        s = finals["by_threshold"][str(th)]
        lines += [f"## Threshold ${th/1e6:.1f}M", ""]
        lines.append(f"- OOS tokens tradable: {s.get('n_oos_tokens')}")
        lines.append(f"- Any OOS strategy total PnL > 0: {s.get('any_oos_strategy_total_positive')}")
        lines.append(f"- Status: `{s.get('token_status_counts')}`")
        best = s.get("best_oos_strategy")
        if best:
            lines.append(f"- Best OOS strategy sum: `{best.get('strategy')}` pnl={best.get('total_pnl_usd'):.2f} trades={best.get('n_trades')} profitable_tokens={best.get('n_tokens_profitable')}/{best.get('n_tokens')}")
        lines.append("")
    lines += ["`deploy_allowed=false`. FDV uses current supply (approx).", ""]
    (OUT / "README.md").write_text("\n".join(lines))
    print(json.dumps({"any_positive": {str(th): finals["by_threshold"][str(th)]["any_oos_strategy_total_positive"] for th in THRESHOLDS}}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
