from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from safety import research_entry_guard


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LN_TICK = np.log(1.0001)


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / name, sep="\t", low_memory=False)


def _future_join(frame: pd.DataFrame, hours: int, value: str, out: str) -> pd.DataFrame:
    right = frame[["pool_address", "block_hour", value]].copy()
    right["block_hour"] = right["block_hour"] - pd.Timedelta(hours=hours)
    right = right.rename(columns={value: out})
    return frame.merge(right, on=["pool_address", "block_hour"], how="left", validate="one_to_one")


def build_features() -> pd.DataFrame:
    research_entry_guard()
    x = _read("hourly_v3.tsv")
    x["block_hour"] = pd.to_datetime(x["block_hour"], utc=True)
    x = x.sort_values(["pool_address", "block_hour"]).drop_duplicates(["pool_address", "block_hour"])
    numeric = [c for c in x.columns if c not in {"pool_address", "block_hour"}]
    x[numeric] = x[numeric].apply(pd.to_numeric, errors="coerce")

    # Directly observable features; all outcomes are attached later by exact
    # timestamp joins so a missing hour cannot silently become a one-hour bar.
    x["ret_0"] = (x.close_tick - x.open_tick) * LN_TICK
    x["tick_range"] = (x.high_tick - x.low_tick).clip(lower=0) * LN_TICK
    x["swaps_per_block"] = x.swap_count / x.unique_blocks.clip(lower=1)
    x["block_coverage"] = x.unique_blocks / x.swap_count.clip(lower=1)
    x["unique_sender_ratio"] = x.unique_senders / x.tx_count.clip(lower=1)
    x["repeat_sender_share"] = 1 - x.unique_sender_ratio.clip(upper=1)
    x["unique_recipient_ratio"] = x.unique_recipients / x.tx_count.clip(lower=1)
    x["breadth_gap"] = x.unique_sender_ratio - x.unique_recipient_ratio
    x["flow_imbalance"] = 2 * x.amount0_negative_share - 1
    x["direction_persistence"] = np.sign(x.ret_0) * x.flow_imbalance.abs()
    x["liq_change"] = x.close_log_liquidity - x.open_log_liquidity
    x["log_liquidity"] = x.mean_log_liquidity
    x["liq_cv"] = x.sd_log_liquidity / x.mean_log_liquidity.abs().clip(lower=1e-6)
    denom = (x.high_tick - x.low_tick).replace(0, np.nan)
    x["close_location"] = ((x.close_tick - x.low_tick) / denom).fillna(0.5)
    x["path_inefficiency"] = x.tick_range / x.ret_0.abs().clip(lower=LN_TICK)

    g = x.groupby("pool_address", sort=False)
    past_mean = g.swap_count.transform(lambda s: s.shift(1).rolling(24, min_periods=6).mean())
    past_std = g.swap_count.transform(lambda s: s.shift(1).rolling(24, min_periods=6).std())
    x["count_z"] = (x.swap_count - past_mean) / past_std.clip(lower=1)
    x["prior_hour"] = g.block_hour.shift(1)
    x["gap_hours"] = (x.block_hour - x.prior_hour).dt.total_seconds().div(3600)
    x["reactivation"] = np.log1p(x.gap_hours.clip(lower=1)) * np.log1p(x.swap_count)
    x["first_seen"] = g.block_hour.transform("min")
    x["pool_age_hours"] = (x.block_hour - x.first_seen).dt.total_seconds().div(3600)
    x["late_life_burst"] = x.count_z * np.log1p(x.pool_age_hours)
    x["hollow_jump"] = x.ret_0.abs() / x.unique_sender_ratio.clip(lower=0.05)
    x["breadth_activity_mismatch"] = x.unique_sender_ratio - np.log1p(x.swap_count) / np.log1p(x.swap_count.max())
    x["price_flow_mismatch"] = x.ret_0.abs() * (1 - x.flow_imbalance.abs())
    x["isolated_extreme"] = x.tick_range / np.sqrt(x.unique_senders.clip(lower=1))

    market = x.groupby("block_hour").agg(
        market_breadth=("ret_0", lambda s: float((s > 0).mean())),
        market_dispersion=("ret_0", "std"),
        market_activity=("swap_count", "sum"),
        market_liquidity=("mean_log_liquidity", "median"),
        v3_active_pools=("pool_address", "nunique"),
    ).reset_index()
    market["market_activity_z"] = (
        market.market_activity - market.market_activity.shift(1).rolling(24, min_periods=6).mean()
    ) / market.market_activity.shift(1).rolling(24, min_periods=6).std().clip(lower=1)
    x = x.merge(market, on="block_hour", how="left", validate="many_to_one")

    v2 = _read("hourly_v2_market.tsv")
    v2["block_hour"] = pd.to_datetime(v2.block_hour, utc=True)
    x = x.merge(v2, on="block_hour", how="left", validate="many_to_one")
    x["v2_market_share"] = x.v2_transactions.fillna(0) / (x.v2_transactions.fillna(0) + x.groupby("block_hour").tx_count.transform("sum")).clip(lower=1)
    x["venue_migration"] = x.v2_market_share - x.groupby("pool_address").v2_market_share.shift(1)

    meta = _read("pool_meta.tsv").drop_duplicates("pool_address", keep="last")
    keep = ["pool_address","token0_address","token1_address","token0_symbol","token1_symbol","fee_bps","has_weth_leg","created_block"]
    x = x.merge(meta[keep], on="pool_address", how="left", validate="many_to_one")
    x["fee_bps"] = pd.to_numeric(x.fee_bps, errors="coerce")
    x["low_fee_calm"] = -x.fee_bps.fillna(x.fee_bps.median()) * x.market_dispersion
    x["pair_key"] = x.apply(lambda r: "|".join(sorted([str(r.token0_address), str(r.token1_address)])), axis=1)
    peer = x.groupby(["pair_key","block_hour"]).agg(
        peer_ret=("ret_0","median"), peer_tick=("close_tick","median"), peer_n=("pool_address","nunique"),
        peer_liq=("mean_log_liquidity","max")
    ).reset_index()
    x = x.merge(peer, on=["pair_key","block_hour"], how="left", validate="many_to_one")
    x["cross_pool_divergence"] = np.where(x.peer_n >= 2, (x.close_tick - x.peer_tick) * LN_TICK, np.nan)
    x["peer_disagreement"] = np.where(x.peer_n >= 2, x.ret_0 - x.peer_ret, np.nan)
    x["deep_pool_lead"] = np.where(x.peer_n >= 2, x.peer_ret * (x.peer_liq - x.mean_log_liquidity), np.nan)

    lp = _read("hourly_lp.tsv")
    lp["block_hour"] = pd.to_datetime(lp.block_hour, utc=True)
    x = x.merge(lp, on=["pool_address","block_hour"], how="left", validate="one_to_one")
    for c in ["mint_count","burn_count","collect_count","mint_log_amount","burn_log_amount"]:
        x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0)
    x["burn_intensity"] = np.log1p(x.burn_count) / np.log1p(x.swap_count + 1)
    x["lp_net_flow"] = x.mint_log_amount - x.burn_log_amount
    x["collect_intensity"] = np.log1p(x.collect_count)
    x["range_recenter"] = np.nan  # requires position-level reconstruction

    x["duplicate_pressure"] = pd.to_numeric(x.duplicate_pressure, errors="coerce")
    # Exact forward closes and delayed-entry outcomes.
    for h in (1, 2, 3, 4, 5, 6, 8, 24, 25, 26, 28):
        x = _future_join(x, h, "close_tick", f"close_tick_p{h}")
    for h in (1, 4, 24):
        x[f"fwd_ret_{h}"] = (x[f"close_tick_p{h}"] - x.close_tick) * LN_TICK
        x[f"fwd_abs_{h}"] = x[f"fwd_ret_{h}"].abs()
    x["future_gap_hours"] = (g.block_hour.shift(-1) - x.block_hour).dt.total_seconds().div(3600)
    for horizon in (4, 24):
        x[f"future_activity_{horizon}"] = np.where(
            x.future_gap_hours.notna(), (x.future_gap_hours <= horizon).astype(float), np.nan
        )
    x["future_worst_4"] = pd.concat([x.fwd_ret_1, x.fwd_ret_4], axis=1).min(axis=1)
    x["future_worst_24"] = pd.concat([x.fwd_ret_1, x.fwd_ret_4, x.fwd_ret_24], axis=1).min(axis=1)
    x["relative_spread_1"] = _shifted_relative(x, 1)
    for delay in (1, 2, 4):
        x[f"delay{delay}_ret_4"] = (x[f"close_tick_p{delay+4}"] - x[f"close_tick_p{delay}"]) * LN_TICK
        x[f"delay{delay}_ret_1"] = (x[f"close_tick_p{delay+1}"] - x[f"close_tick_p{delay}"]) * LN_TICK

    # Exclude mechanically impossible tick jumps from economic screens but keep
    # them in a quarantine artifact for manual anomaly review.
    quarantine = x[x.ret_0.abs() > np.log(100)].copy()
    quarantine.to_csv(DATA / "quarantined_extreme_ticks.csv", index=False)
    x.loc[x.ret_0.abs() > np.log(100), ["ret_0","fwd_ret_1","fwd_ret_4","fwd_ret_24"]] = np.nan
    x.to_parquet(DATA / "feature_panel.parquet", index=False)
    quality = {
        "rows": int(len(x)),
        "pools": int(x.pool_address.nunique()),
        "start": str(x.block_hour.min()),
        "end": str(x.block_hour.max()),
        "exact_1h_outcomes": int(x.fwd_ret_1.notna().sum()),
        "exact_4h_outcomes": int(x.fwd_ret_4.notna().sum()),
        "exact_24h_outcomes": int(x.fwd_ret_24.notna().sum()),
        "quarantined_extreme_rows": int(len(quarantine)),
        "lp_event_rows_joined": int(((x.mint_count+x.burn_count+x.collect_count)>0).sum()),
        "matched_pair_rows": int((x.peer_n>=2).sum()),
    }
    (DATA / "feature_quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
    return x


def _shifted_relative(x: pd.DataFrame, hours: int) -> pd.Series:
    key = x[["pool_address","block_hour","cross_pool_divergence"]].copy()
    key["block_hour"] -= pd.Timedelta(hours=hours)
    key = key.rename(columns={"cross_pool_divergence":"future_divergence"})
    joined = x[["pool_address","block_hour","cross_pool_divergence"]].merge(key, on=["pool_address","block_hour"], how="left")
    return joined.future_divergence - joined.cross_pool_divergence


if __name__ == "__main__":
    build_features()
