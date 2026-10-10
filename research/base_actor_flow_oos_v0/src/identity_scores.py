"""PIT identity trait scoring from matured episodes only.

Traits (non-exclusive):
  DIAMOND, WHALE_EVENT, SMART_ALPHA, BOT_REGIME, INDEPENDENT_CONSENSUS helpers
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import OUT, load_freeze, save_json, signal_id, utcnow
from .fifo_episodes import build_episodes


def _bayes_shrink(mean: float, n: int, prior: float = 0.0, strength: float = 10.0) -> float:
    return (n * mean + strength * prior) / (n + strength) if n + strength > 0 else prior


def compute_pool_features(events: pd.DataFrame) -> pd.DataFrame:
    """POOL_ONLY features at each buy event (no identity)."""
    buys = events[events["side"] == "buy"].sort_values(["pool", "ts"]).copy()
    buys["age_bucket"] = pd.cut(
        buys["pool_age_s"],
        bins=[-1, 900, 3600, 21600, 86400, 604800],
        labels=["0-15m", "15-60m", "1-6h", "6-24h", "1-7d"],
    )
    liq = pd.to_numeric(buys["liquidity"], errors="coerce").fillna(0.0)
    buys["depth_proxy"] = np.log1p(liq.clip(lower=0))
    buys["log_size_weth"] = np.log1p(buys["quote_weth"].clip(lower=0))
    first_px = buys.groupby("pool")["price_weth"].transform("first")
    buys["mom_from_open"] = np.where(first_px > 0, buys["price_weth"] / first_px - 1.0, 0.0)
    intens = []
    for _, g in buys.groupby("pool", sort=False):
        ts = g["ts"].to_numpy()
        n = len(ts)
        left = 0
        vals = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > 1800:
                left += 1
            vals[i] = i - left
        intens.append(pd.Series(vals, index=g.index))
    buys["buy_intensity_30m"] = pd.concat(intens).sort_index() if intens else 0.0
    buys["pool_only_score"] = (
        0.25 * buys["depth_proxy"].rank(pct=True)
        + 0.25 * buys["buy_intensity_30m"].rank(pct=True)
        + 0.25 * (1.0 - buys["pool_age_s"].rank(pct=True))
        + 0.25 * buys["mom_from_open"].clip(-1, 5).rank(pct=True)
    )
    return buys


def compute_actor_daily_traits_vectorized(
    episodes: pd.DataFrame,
    past_buys: pd.DataFrame,
    past_sells: pd.DataFrame,
    *,
    asof_ts: int,
    freeze: dict,
) -> pd.DataFrame:
    """Vectorized matured traits using only data with closed_at/ts <= asof_ts - maturation."""
    mat = int(freeze["identity"]["maturation_horizon_seconds"])
    cutoff = asof_ts - mat
    prior_s = float(freeze["identity"]["smart_alpha_prior_strength"])
    min_ep = int(freeze["identity"]["min_matured_episodes_for_trait"])
    min_pos = int(freeze["identity"]["min_positive_tokens_for_alpha"])

    ep = episodes[(episodes["closed_at"] <= cutoff) & (~episodes["censored"])].copy()
    buys = past_buys[past_buys["ts"] <= cutoff]
    sells = past_sells[past_sells["ts"] <= cutoff]

    if ep.empty and buys.empty:
        return pd.DataFrame(columns=[
            "actor", "asof_ts", "n_matured_episodes", "n_positive_tokens",
            "diamond_score", "retention_24h_mean", "retention_72h_mean",
            "mean_executable_return", "smart_alpha", "bot_toxicity", "is_bot",
            "med_buy_gap_s", "med_hold_s", "sell_buy_ratio", "n_buys",
        ])

    global_mean = float(ep["return_frac"].mean()) if len(ep) else 0.0

    if len(ep):
        g = ep.groupby("actor", sort=False)
        ep_agg = g.agg(
            n_matured_episodes=("return_frac", "count"),
            mean_executable_return=("return_frac", "mean"),
            retention_24h_mean=("retention_24h", "mean"),
            retention_72h_mean=("retention_72h", "mean"),
            med_hold_s=("hold_s", "median"),
        ).reset_index()
        pos = ep[ep["return_frac"] > 0].groupby("actor")["token"].nunique()
        ep_agg["n_positive_tokens"] = ep_agg["actor"].map(pos).fillna(0).astype(int)
    else:
        ep_agg = pd.DataFrame(columns=[
            "actor", "n_matured_episodes", "mean_executable_return",
            "retention_24h_mean", "retention_72h_mean", "med_hold_s", "n_positive_tokens",
        ])

    buy_n = buys.groupby("economic_actor").size().rename("n_buys") if len(buys) else pd.Series(dtype=float)
    sell_n = sells.groupby("economic_actor").size().rename("n_sells") if len(sells) else pd.Series(dtype=float)

    # Fast median buy-gap: only actors with >=5 buys; vectorized via sorted diffs
    med_gap_s = pd.Series(dtype=float, name="med_buy_gap_s")
    if len(buys):
        freq = buy_n[buy_n >= 5]
        if len(freq):
            sub = buys[buys["economic_actor"].isin(freq.index)][
                ["economic_actor", "ts"]
            ].sort_values(["economic_actor", "ts"])
            sub["prev"] = sub.groupby("economic_actor", sort=False)["ts"].shift(1)
            sub["gap"] = sub["ts"] - sub["prev"]
            med_gap_s = sub.dropna(subset=["gap"]).groupby("economic_actor", sort=False)["gap"].median()

    # Restrict actor universe to those with matured episodes OR enough buys (avoid 100k+ empty rows)
    actors = sorted(
        set(ep_agg["actor"].tolist() if len(ep_agg) else [])
        | set(buy_n[buy_n >= 3].index.tolist())
    )
    out = pd.DataFrame({"actor": actors})
    if len(ep_agg):
        out = out.merge(ep_agg, on="actor", how="left")
    else:
        for c in ("n_matured_episodes", "mean_executable_return", "retention_24h_mean",
                  "retention_72h_mean", "med_hold_s", "n_positive_tokens"):
            out[c] = 0.0 if c != "n_matured_episodes" and c != "n_positive_tokens" else 0
    out["n_matured_episodes"] = out["n_matured_episodes"].fillna(0).astype(int)
    out["n_positive_tokens"] = out["n_positive_tokens"].fillna(0).astype(int)
    out["mean_executable_return"] = out["mean_executable_return"].fillna(0.0)
    out["retention_24h_mean"] = out["retention_24h_mean"].fillna(0.0)
    out["retention_72h_mean"] = out["retention_72h_mean"].fillna(0.0)
    out["med_hold_s"] = out["med_hold_s"].fillna(1e9)
    out["n_buys"] = out["actor"].map(buy_n).fillna(0).astype(int)
    out["n_sells"] = out["actor"].map(sell_n).fillna(0).astype(int)
    out["med_buy_gap_s"] = out["actor"].map(med_gap_s).fillna(1e9)
    out["sell_buy_ratio"] = np.where(out["n_buys"] > 0, out["n_sells"] / out["n_buys"], 0.0)

    # DIAMOND
    raw_d = (
        0.5 * out["retention_24h_mean"]
        + 0.3 * out["retention_72h_mean"]
        + 0.2 * np.tanh(out["mean_executable_return"])
    )
    out["diamond_score"] = np.where(
        (out["n_matured_episodes"] >= min_ep) & (out["mean_executable_return"] > 0),
        raw_d, 0.0,
    )

    # SMART_ALPHA
    raw_alpha = out["mean_executable_return"] - global_mean
    n = out["n_matured_episodes"].to_numpy(dtype=float)
    shrunk = (n * raw_alpha.to_numpy() + prior_s * 0.0) / np.maximum(n + prior_s, 1e-9)
    shrunk = np.where(out["n_positive_tokens"] < min_pos, shrunk * 0.25, shrunk)
    shrunk = np.where(out["n_matured_episodes"] >= 1, shrunk, 0.0)
    out["smart_alpha"] = shrunk

    # BOT
    bot = (
        (out["med_buy_gap_s"] < 30).astype(float)
        + (out["med_hold_s"] < 120).astype(float)
        + ((out["sell_buy_ratio"] > 0.8) & (out["med_hold_s"] < 600)).astype(float)
    ) / 3.0
    bot = np.where(out["n_buys"] >= 5, bot, 0.0)
    out["bot_toxicity"] = bot
    out["is_bot"] = out["bot_toxicity"] >= 0.5
    out["asof_ts"] = asof_ts
    return out


def attach_event_identity_features(
    buy_events: pd.DataFrame,
    resolved: pd.DataFrame,
    episodes: pd.DataFrame,
    freeze: dict,
) -> pd.DataFrame:
    """Attach PIT identity features to each buy event (weekly trait anchors)."""
    buys = buy_events.copy()
    buys["day"] = (buys["ts"].astype(int) // 86400) * 86400
    days = sorted(buys["day"].unique())
    anchors = days[::7] if len(days) > 7 else days
    if days and (not anchors or anchors[-1] != days[-1]):
        anchors = list(anchors) + [int(days[-1])]

    # Only score actors that appear in the event panel (huge speedup)
    panel_actors = set(
        buys.loc[buys["economic_actor"].astype(str).str.len() == 42, "economic_actor"].unique()
    )
    past_buys = resolved[
        (resolved["side"] == "buy")
        & (resolved["economic_actor"].isin(panel_actors))
    ][["economic_actor", "ts", "token", "quote_weth"]].copy()
    past_sells = resolved[
        (resolved["side"] == "sell")
        & (resolved["economic_actor"].isin(panel_actors))
    ][["economic_actor", "ts"]].copy()
    ep_panel = episodes[episodes["actor"].isin(panel_actors)].copy() if len(episodes) else episodes

    print(
        f"[phase2] computing actor traits at {len(anchors)} week-anchors "
        f"(panel_actors={len(panel_actors):,})…",
        flush=True,
    )
    trait_cache: dict[int, pd.DataFrame] = {}
    for i, a in enumerate(anchors, 1):
        trait_cache[int(a)] = compute_actor_daily_traits_vectorized(
            ep_panel, past_buys, past_sells, asof_ts=int(a), freeze=freeze
        )
        if i == 1 or i % 4 == 0 or i == len(anchors):
            print(f"[phase2] trait anchor {i}/{len(anchors)} asof={a}", flush=True)

    anchor_arr = np.array(list(trait_cache.keys()), dtype=np.int64)
    day_to_anchor = {}
    for d in days:
        idx = int(np.searchsorted(anchor_arr, d, side="right") - 1)
        day_to_anchor[int(d)] = int(anchor_arr[max(0, idx)])

    frames = []
    for d, g in buys.groupby("day", sort=False):
        traits = trait_cache[day_to_anchor[int(d)]]
        if traits is None or traits.empty:
            gg = g.copy()
            for c in ("diamond_score", "smart_alpha", "bot_toxicity", "n_matured_episodes", "n_positive_tokens"):
                gg[c] = 0.0
            gg["is_bot"] = False
        else:
            gg = g.merge(
                traits[[
                    "actor", "diamond_score", "smart_alpha", "bot_toxicity",
                    "is_bot", "n_matured_episodes", "n_positive_tokens",
                ]],
                left_on="economic_actor", right_on="actor", how="left",
            )
            for c in ("diamond_score", "smart_alpha", "bot_toxicity", "n_matured_episodes", "n_positive_tokens"):
                gg[c] = gg[c].fillna(0.0)
            gg["is_bot"] = gg["is_bot"].fillna(False)
        frames.append(gg)
    out = pd.concat(frames, ignore_index=True)
    out = _attach_whale_features(out)
    out = _attach_consensus(out)
    out = _attach_bot_regime(out)
    return out


def _attach_whale_features(buys: pd.DataFrame) -> pd.DataFrame:
    d = buys.sort_values(["pool", "ts"]).copy()
    rel, nonrev = [], []
    for _, g in d.groupby("pool", sort=False):
        ts = g["ts"].to_numpy()
        qw = g["quote_weth"].to_numpy()
        px = g["price_weth"].to_numpy()
        n = len(g)
        left = 0
        ri = np.zeros(n)
        nr = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > 1800:
                left += 1
            window_vol = qw[left:i].sum() + 1e-9
            ri[i] = qw[i] / window_vol
            j = i
            while j < n and ts[j] - ts[i] < 300:
                j += 1
            if j > i + 1 and px[i] > 0:
                nr[i] = 1.0 if px[min(j - 1, n - 1)] >= px[i] * 0.99 else 0.0
        rel.append(pd.Series(ri, index=g.index))
        nonrev.append(pd.Series(nr, index=g.index))
    d["whale_impact"] = pd.concat(rel).sort_index()
    d["whale_absorbed"] = pd.concat(nonrev).sort_index()
    d["whale_score"] = d["whale_impact"].clip(0, 5) * (0.5 + 0.5 * d["whale_absorbed"])
    return d


def _attach_consensus(buys: pd.DataFrame) -> pd.DataFrame:
    d = buys.sort_values(["token", "ts"]).copy()
    if "entity_cluster" not in d.columns:
        d["entity_cluster"] = d.get("economic_actor", "")
    vals = []
    for _, g in d.groupby("token", sort=False):
        ts = g["ts"].to_numpy()
        clusters = g["entity_cluster"].astype(str).to_numpy()
        n = len(g)
        left = 0
        ccount = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > 1800:
                left += 1
            ccount[i] = len(set(clusters[left:i + 1]) - {"", "nan", "None"})
        vals.append(pd.Series(ccount, index=g.index))
    d["independent_consensus"] = pd.concat(vals).sort_index()
    return d


def _attach_bot_regime(buys: pd.DataFrame) -> pd.DataFrame:
    d = buys.sort_values(["pool", "ts"]).copy()
    tox, org = [], []
    for _, g in d.groupby("pool", sort=False):
        ts = g["ts"].to_numpy()
        is_bot = g["is_bot"].astype(bool).to_numpy() if "is_bot" in g.columns else np.zeros(len(g), dtype=bool)
        n = len(g)
        left = 0
        tvals = np.zeros(n)
        ovals = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > 1800:
                left += 1
            window = is_bot[left:i + 1]
            if len(window):
                tvals[i] = window.mean()
                ovals[i] = 1.0 - window.mean()
        tox.append(pd.Series(tvals, index=g.index))
        org.append(pd.Series(ovals, index=g.index))
    d["sniper_density"] = pd.concat(tox).sort_index()
    d["organic_buyer_share"] = pd.concat(org).sort_index()
    d["bot_regime_score"] = (1.0 - d["sniper_density"]) * d["organic_buyer_share"]
    return d


def run_identity_pipeline(resolved: pd.DataFrame) -> pd.DataFrame:
    freeze = load_freeze()
    print("[phase2] building FIFO episodes…", flush=True)
    episodes = build_episodes(resolved)
    ep_path = OUT / "phase2" / "episodes.parquet"
    ep_path.parent.mkdir(parents=True, exist_ok=True)
    episodes.to_parquet(ep_path, index=False)
    print(
        f"[phase2] episodes: {len(episodes):,} "
        f"(censored={int(episodes['censored'].sum()) if len(episodes) else 0})",
        flush=True,
    )

    print("[phase2] pool-only buy events…", flush=True)
    scoped = resolved[
        (resolved["side"] == "buy")
        & (resolved["pool_age_s"] >= 900)
        & (resolved["pool_age_s"] < 86400)
        & (resolved["quote_weth"] >= 0.001)
    ].copy()
    if len(scoped) > 200_000:
        scoped = scoped.sample(200_000, random_state=42).sort_values(["pool", "ts"])
    buys = compute_pool_features(scoped)

    print("[phase2] attaching PIT identity features…", flush=True)
    panel = attach_event_identity_features(buys, resolved, episodes, freeze)
    panel["signal_id"] = [
        signal_id(str(p), int(t), str(a) if isinstance(a, str) else "")
        for p, t, a in zip(panel["pool"], panel["ts"], panel["economic_actor"])
    ]
    panel["decision_time"] = panel["ts"].astype(int)
    out_path = OUT / "panel" / "events.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out_path, index=False)
    save_json(OUT / "phase2" / "panel_summary.json", {
        "n_events": int(len(panel)),
        "n_pools": int(panel["pool"].nunique()),
        "n_tokens": int(panel["token"].nunique()),
        "n_actors": int(panel.loc[panel["economic_actor"].astype(str).str.len() == 42, "economic_actor"].nunique()),
        "generated_at": utcnow(),
    })
    print(f"[phase2] panel rows: {len(panel):,}", flush=True)
    return panel
