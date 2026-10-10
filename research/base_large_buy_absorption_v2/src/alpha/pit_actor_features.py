"""Family A: PIT actor-quality features.

STRICT RULE: only round-trips whose *close_ts* is strictly before decision_time T
may contribute to features at T. Lifetime aggregate tables are forbidden.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..common import OUT, ROOT
from .common_alpha import ALPHA_OUT, ensure_alpha_out


def build_closed_round_trips(
    swaps: pd.DataFrame | None = None,
    *,
    cache_path: Path | None = None,
    actors_filter: set[str] | None = None,
) -> pd.DataFrame:
    """Fast FIFO buy→sell episodes per (economic_actor, token).

    Uses numpy arrays inside each group (no itertuples). Optional actors_filter
    restricts to panel-relevant wallets.
    """
    ensure_alpha_out()
    cache_path = cache_path or (ALPHA_OUT / "actor_closed_trips.parquet")
    if cache_path.exists():
        print(f"[alpha/A] loading cached trips {cache_path}", flush=True)
        return pd.read_parquet(cache_path)

    cols = [
        "economic_actor", "token", "pool", "ts", "side",
        "quote_weth", "price_weth", "pool_age_s", "entity_cluster",
    ]
    if swaps is None:
        swaps = pd.read_parquet(ROOT / "data" / "resolved_swaps.parquet", columns=cols)
    else:
        swaps = swaps[[c for c in cols if c in swaps.columns]].copy()
        if "entity_cluster" not in swaps.columns:
            swaps["entity_cluster"] = swaps["economic_actor"]

    d = swaps
    d["economic_actor"] = d["economic_actor"].astype(str).str.lower()
    if actors_filter is not None:
        d = d[d["economic_actor"].isin(actors_filter)]
    d["token"] = d["token"].astype(str).str.lower()
    d["pool"] = d["pool"].astype(str).str.lower()
    d["entity_cluster"] = d["entity_cluster"].astype(str).str.lower()
    d["ts"] = d["ts"].astype(np.int64)
    d["price_weth"] = pd.to_numeric(d["price_weth"], errors="coerce")
    d["quote_weth"] = pd.to_numeric(d["quote_weth"], errors="coerce").abs()
    d["pool_age_s"] = pd.to_numeric(d.get("pool_age_s", 0), errors="coerce").fillna(0.0)
    d = d[d["economic_actor"].ne("") & d["economic_actor"].ne("nan") & (d["price_weth"] > 0)]
    d = d.sort_values(["economic_actor", "token", "ts"], kind="mergesort").reset_index(drop=True)
    print(f"[alpha/A] FIFO over {len(d):,} swaps…", flush=True)

    # factorize groups
    keys = d["economic_actor"].to_numpy() + "|" + d["token"].to_numpy()
    # use pandas group indices
    actors = d["economic_actor"].to_numpy()
    tokens = d["token"].to_numpy()
    pools = d["pool"].to_numpy()
    clusters = d["entity_cluster"].to_numpy()
    ts = d["ts"].to_numpy(dtype=np.int64)
    px = d["price_weth"].to_numpy(dtype=np.float64)
    qw = d["quote_weth"].to_numpy(dtype=np.float64)
    age = d["pool_age_s"].to_numpy(dtype=np.float64)
    is_buy = (d["side"].to_numpy() == "buy")

    # group boundaries
    change = np.ones(len(d), dtype=bool)
    change[1:] = (actors[1:] != actors[:-1]) | (tokens[1:] != tokens[:-1])
    starts = np.flatnonzero(change)
    ends = np.r_[starts[1:], len(d)]

    out_actor, out_token, out_pool, out_cluster = [], [], [], []
    out_entry, out_close, out_hold, out_ret, out_age, out_win = [], [], [], [], [], []

    for s, e in zip(starts, ends):
        # simple LIFO stack of buys (faster; still only uses closed sells)
        stack_ts: list[int] = []
        stack_px: list[float] = []
        stack_qw: list[float] = []
        stack_pool: list[str] = []
        stack_age: list[float] = []
        a0 = actors[s]
        t0 = tokens[s]
        cl0 = clusters[s]
        for i in range(s, e):
            if is_buy[i] and qw[i] > 0 and px[i] > 0:
                stack_ts.append(int(ts[i]))
                stack_px.append(float(px[i]))
                stack_qw.append(float(qw[i]))
                stack_pool.append(pools[i])
                stack_age.append(float(age[i]))
            elif (not is_buy[i]) and qw[i] > 0 and px[i] > 0 and stack_ts:
                remain = float(qw[i])
                sell_px = float(px[i])
                sell_ts = int(ts[i])
                while remain > 1e-12 and stack_ts:
                    entry_ts = stack_ts[-1]
                    entry_px = stack_px[-1]
                    lot = stack_qw[-1]
                    take = min(remain, lot)
                    ret = (sell_px / entry_px) - 1.0 if entry_px > 0 else 0.0
                    if abs(ret) < 50:
                        out_actor.append(a0)
                        out_token.append(t0)
                        out_pool.append(stack_pool[-1])
                        out_cluster.append(cl0)
                        out_entry.append(entry_ts)
                        out_close.append(sell_ts)
                        out_hold.append(sell_ts - entry_ts)
                        out_ret.append(ret)
                        out_age.append(stack_age[-1])
                        out_win.append(1 if ret > 0 else 0)
                    lot -= take
                    remain -= take
                    if lot <= 1e-12:
                        stack_ts.pop(); stack_px.pop(); stack_qw.pop()
                        stack_pool.pop(); stack_age.pop()
                    else:
                        stack_qw[-1] = lot

    trips = pd.DataFrame({
        "actor": out_actor,
        "token": out_token,
        "pool": out_pool,
        "cluster": out_cluster,
        "entry_ts": out_entry,
        "close_ts": out_close,
        "hold_s": out_hold,
        "ret": out_ret,
        "pool_age_at_entry": out_age,
        "win": out_win,
    })
    trips.to_parquet(cache_path, index=False)
    print(f"[alpha/A] closed trips={len(trips):,} actors={trips['actor'].nunique() if len(trips) else 0:,}", flush=True)
    return trips


def _expanding_actor_stats(trips: pd.DataFrame) -> pd.DataFrame:
    """Per-actor cumulative stats indexed by close_ts (usable for asof join)."""
    if trips.empty:
        return pd.DataFrame(columns=[
            "actor", "close_ts", "n_trips", "win_rate", "avg_ret",
            "median_hold_s", "early_entry_skill",
        ])
    t = trips.sort_values(["actor", "close_ts"]).copy()
    # early-entry skill: inverse of pool_age_at_entry among winners (higher = entered earlier on winners)
    t["early_score"] = np.where(
        t["win"] == 1,
        1.0 / (1.0 + t["pool_age_at_entry"].clip(lower=0) / 3600.0),
        0.0,
    )
    g = t.groupby("actor", sort=False)
    t["n_trips"] = g.cumcount() + 1
    t["win_cum"] = g["win"].cumsum()
    t["ret_cum"] = g["ret"].cumsum()
    t["early_cum"] = g["early_score"].cumsum()
    t["hold_cum"] = g["hold_s"].cumsum()
    t["win_rate"] = t["win_cum"] / t["n_trips"]
    t["avg_ret"] = t["ret_cum"] / t["n_trips"]
    t["early_entry_skill"] = t["early_cum"] / t["n_trips"]
    t["median_hold_s"] = t["hold_cum"] / t["n_trips"]  # mean as cheap proxy for median
    return t[[
        "actor", "close_ts", "n_trips", "win_rate", "avg_ret",
        "median_hold_s", "early_entry_skill",
    ]].reset_index(drop=True)


def _expanding_cluster_winrate(trips: pd.DataFrame) -> pd.DataFrame:
    if trips.empty or "cluster" not in trips.columns:
        return pd.DataFrame(columns=["cluster", "close_ts", "cluster_win_rate", "cluster_n"])
    t = trips.sort_values(["cluster", "close_ts"]).copy()
    g = t.groupby("cluster", sort=False)
    t["cluster_n"] = g.cumcount() + 1
    t["cluster_win_rate"] = g["win"].cumsum() / t["cluster_n"]
    return t[["cluster", "close_ts", "cluster_win_rate", "cluster_n"]].reset_index(drop=True)


def _asof_actor(signals: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    """For each signal (actor, T) take last expanding stats with close_ts < T.

    Implemented with per-actor searchsorted (avoids pandas merge_asof sort quirks
    on large string `by` keys).
    """
    left = signals[["_row", "economic_actor", "decision_time"]].copy()
    left["economic_actor"] = left["economic_actor"].astype(str).str.lower()
    left["decision_time"] = pd.to_numeric(left["decision_time"], errors="coerce").fillna(0).astype(np.int64)

    cols = ["n_trips", "win_rate", "avg_ret", "median_hold_s", "early_entry_skill"]
    out = left.copy()
    for c in cols:
        out[c] = np.nan

    if stats is None or stats.empty:
        return out

    right = stats.rename(columns={"actor": "economic_actor"}).copy()
    right["economic_actor"] = right["economic_actor"].astype(str).str.lower()
    right["close_ts"] = pd.to_numeric(right["close_ts"], errors="coerce").fillna(0).astype(np.int64)
    right = right.sort_values(["economic_actor", "close_ts"], kind="mergesort")
    right = right.drop_duplicates(["economic_actor", "close_ts"], keep="last")

    # index signals by actor for fast lookup
    sig_groups = {a: idx for a, idx in left.groupby("economic_actor", sort=False).groups.items()}
    for actor, g in right.groupby("economic_actor", sort=False):
        idxs = sig_groups.get(actor)
        if idxs is None:
            continue
        close = g["close_ts"].to_numpy(dtype=np.int64)
        # values matrix
        vals = g[cols].to_numpy(dtype=float)
        times = left.loc[idxs, "decision_time"].to_numpy(dtype=np.int64)
        # strict < T → searchsorted side='left' then -1
        pos = np.searchsorted(close, times, side="left") - 1
        valid = pos >= 0
        if not valid.any():
            continue
        # map back
        row_ilocs = left.index.get_indexer(idxs)
        # idxs may be Index of original positions
        dest = np.asarray(idxs)
        out.loc[dest[valid], cols] = vals[pos[valid]]
    return out.reset_index(drop=True)


def _pool_buyers_agg(
    signals: pd.DataFrame,
    swaps: pd.DataFrame,
    actor_stats: pd.DataFrame,
) -> pd.DataFrame:
    """Pool-level expanding mean of prior-buyer PIT win-rate, asof-joined to signals.

    At each first-buy event in a pool, look up that buyer's stats as-of buy_ts,
    then maintain an expanding mean over buyers-so-far. Signals asof-join that panel.
    """
    buys = swaps.loc[swaps["side"].eq("buy"), ["pool", "economic_actor", "ts"]].copy()
    buys["pool"] = buys["pool"].astype(str).str.lower()
    buys["economic_actor"] = buys["economic_actor"].astype(str).str.lower()
    buys["ts"] = buys["ts"].astype(np.int64)
    first = buys.sort_values("ts").groupby(["pool", "economic_actor"], as_index=False).first()
    if first.empty or actor_stats.empty:
        out = signals[["_row"]].copy()
        out["pool_buyers_mean_win_rate"] = 0.0
        out["pool_buyers_mean_n_trips"] = 0.0
        out["pool_buyers_n_with_history"] = 0.0
        return out

    # asof each first-buy to actor stats at buy ts
    left = first.copy().reset_index(drop=True)
    left["economic_actor"] = left["economic_actor"].astype(str)
    left["ts"] = pd.to_numeric(left["ts"], errors="coerce").fillna(0).astype(np.int64)
    left["n_trips"] = np.nan
    left["win_rate"] = np.nan
    right = actor_stats.rename(columns={"actor": "economic_actor"}).copy()
    right["economic_actor"] = right["economic_actor"].astype(str)
    right["close_ts"] = pd.to_numeric(right["close_ts"], errors="coerce").fillna(0).astype(np.int64)
    right = right.sort_values(["economic_actor", "close_ts"], kind="mergesort")
    right = right.drop_duplicates(["economic_actor", "close_ts"], keep="last")
    left_g = {a: idx for a, idx in left.groupby("economic_actor", sort=False).groups.items()}
    for actor, g in right.groupby("economic_actor", sort=False):
        idxs = left_g.get(actor)
        if idxs is None:
            continue
        close = g["close_ts"].to_numpy(dtype=np.int64)
        nt = g["n_trips"].to_numpy(dtype=float)
        wr = g["win_rate"].to_numpy(dtype=float)
        times = left.loc[idxs, "ts"].to_numpy(dtype=np.int64)
        pos = np.searchsorted(close, times, side="left") - 1
        valid = pos >= 0
        dest = np.asarray(idxs)
        left.loc[dest[valid], "n_trips"] = nt[pos[valid]]
        left.loc[dest[valid], "win_rate"] = wr[pos[valid]]
    left["win_rate"] = pd.to_numeric(left["win_rate"], errors="coerce")
    left["n_trips"] = pd.to_numeric(left["n_trips"], errors="coerce")
    left["has_hist"] = left["n_trips"].notna().astype(float)
    left["win_rate"] = left["win_rate"].fillna(0.0)
    left["n_trips"] = left["n_trips"].fillna(0.0)
    left = left.sort_values(["pool", "ts"])
    g = left.groupby("pool", sort=False)
    left["cum_wr"] = g["win_rate"].cumsum()
    left["cum_nt"] = g["n_trips"].cumsum()
    left["cum_n"] = g.cumcount() + 1
    left["cum_hist"] = g["has_hist"].cumsum()
    left["pool_buyers_mean_win_rate"] = left["cum_wr"] / left["cum_n"]
    left["pool_buyers_mean_n_trips"] = left["cum_nt"] / left["cum_n"]
    left["pool_buyers_n_with_history"] = left["cum_hist"]
    panel = left[[
        "pool", "ts", "pool_buyers_mean_win_rate",
        "pool_buyers_mean_n_trips", "pool_buyers_n_with_history",
    ]]

    sig = signals[["_row", "pool", "decision_time"]].copy()
    sig["pool"] = sig["pool"].astype(str).str.lower()
    sig["decision_time"] = pd.to_numeric(sig["decision_time"], errors="coerce").fillna(0).astype(np.int64)
    panel = panel.copy()
    panel["pool"] = panel["pool"].astype(str).str.lower()
    panel["ts"] = pd.to_numeric(panel["ts"], errors="coerce").fillna(0).astype(np.int64)
    panel = panel.sort_values(["pool", "ts"], kind="mergesort")
    panel = panel.drop_duplicates(["pool", "ts"], keep="last")
    cols = ["pool_buyers_mean_win_rate", "pool_buyers_mean_n_trips", "pool_buyers_n_with_history"]
    out = sig.copy()
    for c in cols:
        out[c] = 0.0
    sig_g = {p: idx for p, idx in sig.groupby("pool", sort=False).groups.items()}
    for pool, g in panel.groupby("pool", sort=False):
        idxs = sig_g.get(pool)
        if idxs is None:
            continue
        ts = g["ts"].to_numpy(dtype=np.int64)
        vals = g[cols].to_numpy(dtype=float)
        times = sig.loc[idxs, "decision_time"].to_numpy(dtype=np.int64)
        pos = np.searchsorted(ts, times, side="left") - 1
        valid = pos >= 0
        dest = np.asarray(idxs)
        out.loc[dest[valid], cols] = vals[pos[valid]]
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out[["_row", *cols]]


def attach_actor_features(
    panel: pd.DataFrame,
    *,
    trips: pd.DataFrame | None = None,
    swaps: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach Family-A PIT features to labeled panel rows."""
    ensure_alpha_out()
    d = panel.copy()
    d["_row"] = np.arange(len(d))
    d["economic_actor"] = d["economic_actor"].astype(str).str.lower()
    d["decision_time"] = d["decision_time"].astype(np.int64)
    d["pool"] = d["pool"].astype(str).str.lower()
    if "entity_cluster" in d.columns:
        d["entity_cluster"] = d["entity_cluster"].astype(str).str.lower()
    else:
        d["entity_cluster"] = d["economic_actor"]

    trips = trips if trips is not None else build_closed_round_trips()
    actor_stats = _expanding_actor_stats(trips)
    cluster_stats = _expanding_cluster_winrate(trips)

    print(f"[alpha/A] asof-joining actor stats onto {len(d):,} signals…", flush=True)
    a = _asof_actor(d, actor_stats)
    feat = d[["_row"]].merge(
        a[["_row", "n_trips", "win_rate", "avg_ret", "median_hold_s", "early_entry_skill"]],
        on="_row",
        how="left",
    )
    feat = feat.rename(columns={
        "n_trips": "actor_n_prior_trips",
        "win_rate": "actor_prior_win_rate",
        "avg_ret": "actor_prior_avg_ret",
        "median_hold_s": "actor_prior_median_hold_s",
        "early_entry_skill": "actor_early_entry_skill",
    })

    # cluster asof via searchsorted
    feat["actor_cluster_prior_win_rate"] = np.nan
    if not cluster_stats.empty:
        left_c = d[["_row", "entity_cluster", "decision_time"]].copy()
        left_c["entity_cluster"] = left_c["entity_cluster"].astype(str)
        left_c["decision_time"] = pd.to_numeric(left_c["decision_time"], errors="coerce").fillna(0).astype(np.int64)
        right_c = cluster_stats.copy()
        right_c["cluster"] = right_c["cluster"].astype(str)
        right_c["close_ts"] = pd.to_numeric(right_c["close_ts"], errors="coerce").fillna(0).astype(np.int64)
        right_c = right_c.sort_values(["cluster", "close_ts"], kind="mergesort")
        right_c = right_c.drop_duplicates(["cluster", "close_ts"], keep="last")
        sig_g = {a: idx for a, idx in left_c.groupby("entity_cluster", sort=False).groups.items()}
        wr_out = pd.Series(np.nan, index=left_c.index)
        for cl, g in right_c.groupby("cluster", sort=False):
            idxs = sig_g.get(cl)
            if idxs is None:
                continue
            close = g["close_ts"].to_numpy(dtype=np.int64)
            wr = g["cluster_win_rate"].to_numpy(dtype=float)
            times = left_c.loc[idxs, "decision_time"].to_numpy(dtype=np.int64)
            pos = np.searchsorted(close, times, side="left") - 1
            valid = pos >= 0
            dest = np.asarray(idxs)
            wr_out.loc[dest[valid]] = wr[pos[valid]]
        feat = feat.merge(
            left_c.assign(actor_cluster_prior_win_rate=wr_out.values)[["_row", "actor_cluster_prior_win_rate"]],
            on="_row",
            how="left",
            suffixes=("_old", ""),
        )
        if "actor_cluster_prior_win_rate_old" in feat.columns:
            feat = feat.drop(columns=["actor_cluster_prior_win_rate_old"])

    feat["actor_cold_start"] = feat["actor_n_prior_trips"].isna().astype(float)
    for c in [
        "actor_n_prior_trips", "actor_prior_win_rate", "actor_prior_avg_ret",
        "actor_prior_median_hold_s", "actor_early_entry_skill", "actor_cluster_prior_win_rate",
    ]:
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    # pool buyers aggregate (needs swaps)
    if swaps is None:
        swaps = pd.read_parquet(
            ROOT / "data" / "resolved_swaps.parquet",
            columns=["pool", "economic_actor", "ts", "side"],
        )
    print("[alpha/A] aggregating pool buyers…", flush=True)
    pba = _pool_buyers_agg(d, swaps, actor_stats)
    feat = feat.merge(pba, on="_row", how="left")
    for c in ["pool_buyers_mean_win_rate", "pool_buyers_mean_n_trips", "pool_buyers_n_with_history"]:
        feat[c] = pd.to_numeric(feat.get(c, 0), errors="coerce").fillna(0.0)

    # composite score for matched-ATT treatment (top decile).
    # Own-history terms zero out on cold-start; pool-buyer aggregate remains (still PIT).
    feat["actor_score"] = (
        (1.0 - feat["actor_cold_start"])
        * (
            feat["actor_prior_win_rate"]
            + 0.3 * feat["actor_prior_avg_ret"].clip(-1, 5)
            + 0.2 * feat["actor_early_entry_skill"]
            + 0.1 * np.log1p(feat["actor_n_prior_trips"])
        )
        + 0.5 * feat["pool_buyers_mean_win_rate"]
        + 0.1 * np.log1p(feat["pool_buyers_n_with_history"])
    )

    out_cols = [
        "actor_n_prior_trips", "actor_prior_win_rate", "actor_prior_avg_ret",
        "actor_prior_median_hold_s", "actor_early_entry_skill",
        "actor_cluster_prior_win_rate", "actor_cold_start",
        "pool_buyers_mean_win_rate", "pool_buyers_mean_n_trips",
        "pool_buyers_n_with_history", "actor_score",
    ]
    result = panel.copy()
    for c in out_cols:
        result[c] = feat[c].to_numpy()
    cache = ALPHA_OUT / "panel_actor_features.parquet"
    result[["signal_id"] + out_cols if "signal_id" in result.columns else out_cols].to_parquet(
        cache if "signal_id" in result.columns else ALPHA_OUT / "actor_features_only.parquet",
        index=False,
    )
    print(
        f"[alpha/A] cold_start_rate={result['actor_cold_start'].mean():.1%} "
        f"mean_win_rate={result.loc[result['actor_cold_start']==0, 'actor_prior_win_rate'].mean():.3f}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))]  # noqa: E712
    attach_actor_features(elig)
