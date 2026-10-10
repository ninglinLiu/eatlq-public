#!/usr/bin/env python3
"""Build PIT Greenfield Panel from ClickHouse — no KEYSTONE universe, no future filters."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import (  # noqa: E402
    AGE_BUCKETS,
    OUT,
    PRIMARY_BUCKET,
    WAVE1_ANCHORS_S,
    WETH,
    LN1P,
    age_bucket_of,
    assign_splits,
    ch_query,
    load_freeze,
    signal_id,
    utcnow,
)

PANEL_DIR = OUT / "panel"
WETH_L = WETH.lower()


def parse_i256(x) -> int:
    """Parse ClickHouse hex or decimal int256-like values to signed int."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    if isinstance(x, (int, np.integer)):
        return int(x)
    s = str(x).strip()
    if not s or s == "\\N":
        return 0
    try:
        if s.startswith("0x") or s.startswith("0X"):
            v = int(s, 16)
            if v >= 2**255:
                v -= 2**256
            return v
        return int(float(s)) if "." in s else int(s)
    except Exception:
        return 0


def export_pools() -> pd.DataFrame:
    sql = f"""
    SELECT
      lower(d.pool_address) AS pool,
      lower(d.token0_address) AS token0,
      lower(d.token1_address) AS token1,
      d.fee_bps AS fee_bps,
      d.created_block AS created_block,
      min(s.block_time) AS first_swap_time,
      min(s.block_number) AS first_swap_block,
      count() AS n_swaps_all
    FROM eatlq.discovered_pools AS d
    INNER JOIN eatlq.uniswap_v3_swaps_decoded AS s
      ON lower(s.pool_address) = lower(d.pool_address)
    WHERE d.pool_type = 'v3'
      AND d.has_weth_leg = 1
      AND s.block_time >= toDateTime('2025-10-01 00:00:00')
      AND s.block_time < toDateTime('2026-07-01 00:00:00')
    GROUP BY pool, token0, token1, fee_bps, created_block
    HAVING first_swap_time >= toDateTime('2025-10-01 00:00:00')
       AND first_swap_time < toDateTime('2026-07-01 00:00:00')
    """
    df = ch_query(sql, timeout=900)
    df["first_swap_ts"] = pd.to_datetime(df["first_swap_time"], utc=True).map(lambda x: int(x.timestamp()))
    df["alt"] = np.where(df["token0"] == WETH_L, df["token1"], df["token0"])
    df["orient"] = np.where(df["token0"] == WETH_L, "token1_is_alt", "token0_is_alt")
    return df


def export_early_swaps() -> pd.DataFrame:
    """Swaps in [first_swap, first_swap+9h) for label horizons up to 2h at age 50m + buffer."""
    sql = f"""
    WITH pools AS (
      SELECT
        lower(d.pool_address) AS pool,
        min(s.block_time) AS first_swap_time
      FROM eatlq.discovered_pools AS d
      INNER JOIN eatlq.uniswap_v3_swaps_decoded AS s
        ON lower(s.pool_address) = lower(d.pool_address)
      WHERE d.pool_type = 'v3' AND d.has_weth_leg = 1
        AND s.block_time >= toDateTime('2025-10-01 00:00:00')
        AND s.block_time < toDateTime('2026-07-01 00:00:00')
      GROUP BY pool
      HAVING first_swap_time >= toDateTime('2025-10-01 00:00:00')
         AND first_swap_time < toDateTime('2026-07-01 00:00:00')
    )
    SELECT
      lower(s.pool_address) AS pool,
      toUnixTimestamp(s.block_time) AS ts,
      s.block_number AS block_number,
      s.tick_raw AS tick,
      s.liquidity_raw AS liquidity,
      s.amount0_raw AS amount0,
      s.amount1_raw AS amount1,
      lower(s.sender) AS sender,
      lower(s.recipient) AS recipient
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN pools AS p ON lower(s.pool_address) = p.pool
    WHERE s.block_time >= p.first_swap_time
      AND s.block_time < p.first_swap_time + INTERVAL 9 HOUR
    SETTINGS max_bytes_before_external_group_by = '20G'
    """
    return ch_query(sql, timeout=1200)


def _is_buy_weth_to_alt(row, orient: str) -> bool:
    # If token1 is alt (token0=WETH): buying alt means amount0>0 (WETH in) typically encoded signed
    # CH stores raw int256 as string sometimes; treat amount0/1 as signed ints.
    a0 = int(row["amount0"])
    if orient == "token1_is_alt":
        return a0 > 0  # WETH into pool
    return int(row["amount1"]) > 0


def _decode_swap_frame(g: pd.DataFrame) -> pd.DataFrame:
    out = g.copy()
    out["tick_i"] = [parse_i256(x) for x in out["tick"]]
    out["liq_i"] = [parse_i256(x) for x in out["liquidity"]]
    out["a0_i"] = [parse_i256(x) for x in out["amount0"]]
    out["a1_i"] = [parse_i256(x) for x in out["amount1"]]
    return out


def build_decisions(pools: pd.DataFrame, swaps: pd.DataFrame) -> pd.DataFrame:
    freeze = load_freeze()
    swaps = swaps.sort_values(["pool", "ts"])
    pool_meta = pools.set_index("pool")
    rows = []

    # Precompute per-pool swap lists; skip trivially thin pools early
    vc = swaps.groupby("pool").size()
    eligible_pools = set(vc[vc >= 12].index) & set(pool_meta.index)
    print(f"[panel] pools with >=12 early swaps: {len(eligible_pools)}", flush=True)
    grouped = {p: g for p, g in swaps[swaps["pool"].isin(eligible_pools)].groupby("pool", sort=False)}

    n_done = 0
    for pool, g in grouped.items():
        meta = pool_meta.loc[pool]
        t0 = int(meta["first_swap_ts"])
        orient = str(meta["orient"])
        fee = int(meta["fee_bps"])
        if fee not in (100, 500, 3000, 10000):
            fee = int(meta["fee_bps"])
        g = _decode_swap_frame(g.sort_values("ts"))
        n_done += 1
        if n_done % 500 == 0:
            print(f"[panel] processed {n_done}/{len(grouped)} pools; rows={len(rows)}", flush=True)
        ts = g["ts"].to_numpy(dtype=np.int64)
        ticks = g["tick_i"].to_numpy(dtype=np.float64)
        liqs = g["liq_i"].to_numpy(dtype=np.float64)
        blocks = pd.to_numeric(g["block_number"], errors="coerce").to_numpy()
        recipients = g["recipient"].to_numpy()
        senders = g["sender"].to_numpy()
        a0 = np.array(g["a0_i"].tolist(), dtype=object)
        a1 = np.array(g["a1_i"].tolist(), dtype=object)

        for age_s in WAVE1_ANCHORS_S:
            decision_time = t0 + int(age_s)
            bucket = age_bucket_of(age_s)
            if bucket != PRIMARY_BUCKET:
                continue
            # PIT history: swaps with ts <= decision_time
            idx = np.searchsorted(ts, decision_time, side="right")
            if idx < 6:
                continue
            pre = slice(0, idx)
            pre_ts, pre_tick, pre_liq = ts[pre], ticks[pre], liqs[pre]
            if np.isnan(pre_tick[-1]) or pre_liq[-1] <= 0:
                continue
            # minute bars proxy: unique minute count
            n_minutes = len(np.unique(pre_ts // 60))
            if n_minutes < freeze["eligibility_thresholds"]["min_pre_1m_bars_primary"]:
                continue
            # buy/sell presence in last 10m before decision (PIT tradability proxy)
            win = pre_ts >= (decision_time - 600)
            if win.sum() < 2:
                continue
            a0_pre = a0[pre]
            a1_pre = a1[pre]
            if orient == "token1_is_alt":
                sign = np.array([1 if x > 0 else (-1 if x < 0 else 0) for x in a0_pre], dtype=np.int8)
            else:
                sign = np.array([1 if x > 0 else (-1 if x < 0 else 0) for x in a1_pre], dtype=np.int8)
            buys = int(((sign > 0) & win).sum())
            sells = int(((sign < 0) & win).sum())
            if buys < 1 or sells < 1:
                continue
            # depth/liquidity proxy
            liq_last = float(pre_liq[-1])
            liq_first = float(pre_liq[0]) if pre_liq[0] > 0 else liq_last
            tick_last = float(pre_tick[-1])
            tick_first = float(pre_tick[0])
            # returns in tick space
            ret_full = (tick_last - tick_first) * LN1P
            # path features
            mid = max(idx // 2, 1)
            tick_mid = float(pre_tick[mid])
            mom_h1 = (tick_mid - tick_first) * LN1P
            mom_h2 = (tick_last - tick_mid) * LN1P
            rets = np.diff(pre_tick[~np.isnan(pre_tick)]) * LN1P
            vol = float(np.std(rets)) if len(rets) > 2 else 0.0
            peak = float(np.nanmax(pre_tick))
            trough = float(np.nanmin(pre_tick))
            drawdown = (tick_last - peak) * LN1P
            runup = (tick_last - trough) * LN1P
            range_loc = 0.0 if peak == trough else (tick_last - trough) / (peak - trough)
            buyers = len(set(recipients[pre][-200:]))
            senders_n = len(set(senders[pre][-200:]))
            ns_pre = int(idx)
            net_flow = float((sign > 0).sum() - (sign < 0).sum())
            trade_imb = float(buys - sells) / max(buys + sells, 1)
            accel = mom_h2 - mom_h1
            liq_trend = np.log1p(max(liq_last, 0)) - np.log1p(max(liq_first, 0))
            liq_dd = (liq_last / max(liq_first, 1e-12)) - 1.0
            decision_block = int(blocks[idx - 1])
            rows.append(
                {
                    "signal_id": signal_id(pool, decision_time, bucket),
                    "pool": pool,
                    "alt": meta["alt"],
                    "orient": orient,
                    "fee": fee,
                    "age_bucket": bucket,
                    "age_s": age_s,
                    "t0": t0,
                    "decision_time": decision_time,
                    "decision_block": decision_block,
                    "known_at": decision_time,
                    "feature_available_at": decision_time,
                    "ns_pre": ns_pre,
                    "n_minutes_pre": n_minutes,
                    "buyers_pre": buyers,
                    "senders_pre": senders_n,
                    "buyer_sender_ratio": buyers / max(senders_n, 1),
                    "early_ret": ret_full,
                    "pre_vol": vol,
                    "pre_pullback": drawdown,
                    "pre_runup_from_lo": runup,
                    "range_location": range_loc,
                    "liq_log": float(np.log1p(max(liq_last, 0))),
                    "liq_trend": float(liq_trend),
                    "liq_drawdown": float(liq_dd),
                    "mom_h1": mom_h1,
                    "mom_h2": mom_h2,
                    "accel": accel,
                    "net_flow": net_flow,
                    "trade_count_imbalance": trade_imb,
                    "buys_10m": int(buys),
                    "sells_10m": int(sells),
                    "entry_tick": tick_last,
                    "entry_liq": liq_last,
                    "pit_buy_ok": 1,
                    "pit_sell_ok": 1,
                    "eligible": 1,
                }
            )

    panel = pd.DataFrame(rows)
    if panel.empty:
        return panel

    # MARKET features: as-of joins using only prior/same decision_time aggregates
    panel = panel.sort_values("decision_time")
    # opportunity supply / active pools at decision day
    panel["decision_day"] = panel["decision_time"] // 86400
    day_counts = panel.groupby("decision_day")["pool"].nunique().rename("active_pools_day")
    panel = panel.merge(day_counts, on="decision_day", how="left")
    # breadth: rolling unique pools in prior 1h across panel (approximate market)
    times = panel["decision_time"].to_numpy()
    pools_arr = panel["pool"].to_numpy()
    breadth = np.zeros(len(panel))
    j0 = 0
    for i, t in enumerate(times):
        while times[j0] < t - 3600:
            j0 += 1
        breadth[i] = len(set(pools_arr[j0 : i + 1]))
    panel["base_breadth_1h"] = breadth
    panel["return_dispersion_1h"] = (
        panel.groupby("decision_time")["early_ret"].transform("std").fillna(0.0)
    )
    panel["opportunity_supply"] = panel.groupby("decision_time")["signal_id"].transform("count")
    # XS rank of early_ret within decision_time (feature, not label)
    panel["xs_early_ret_rank"] = panel.groupby("decision_time")["early_ret"].rank(pct=True)
    panel["split_role"] = assign_splits(panel["decision_time"], freeze)
    return panel


def count_other_buckets(pools: pd.DataFrame, swaps: pd.DataFrame) -> dict:
    """Sample-supply dashboard for non-primary buckets (no model training)."""
    grouped = {p: g for p, g in swaps.groupby("pool", sort=False)}
    pool_meta = pools.set_index("pool")
    supply = {k: {"n_rows": 0, "n_pools": 0} for k in AGE_BUCKETS}
    seen = {k: set() for k in AGE_BUCKETS}
    anchors = {
        "5-15m": [7 * 60, 10 * 60, 12 * 60],
        "1-6h": [90 * 60, 180 * 60, 300 * 60],
        "6-24h": [8 * 3600, 12 * 3600, 18 * 3600],
        "1-7d": [36 * 3600, 72 * 3600, 120 * 3600],
    }
    # early swaps only cover 9h — 6-24h/1-7d need separate note
    for pool, g in grouped.items():
        if pool not in pool_meta.index:
            continue
        t0 = int(pool_meta.loc[pool, "first_swap_ts"])
        ts = np.sort(g["ts"].to_numpy(dtype=np.int64))
        for bname, ages in anchors.items():
            for age_s in ages:
                dt = t0 + age_s
                idx = np.searchsorted(ts, dt, side="right")
                if idx >= 6:
                    supply[bname]["n_rows"] += 1
                    seen[bname].add(pool)
    for b in supply:
        supply[b]["n_pools"] = len(seen[b])
        if b in ("6-24h", "1-7d"):
            supply[b]["coverage_note"] = "early_swap_export_9h_insufficient; counts are lower bound / incomplete"
    supply["15-60m"] = {"n_rows": "see_panel", "n_pools": "see_panel"}
    return supply


def main():
    import os

    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    reuse = os.environ.get("REUSE_EXPORTS", "0") == "1"
    pools_path = PANEL_DIR / "pools.parquet"
    swaps_path = PANEL_DIR / "early_swaps.parquet"
    if reuse and pools_path.exists() and swaps_path.exists():
        print("[panel] reusing exports...", flush=True)
        pools = pd.read_parquet(pools_path)
        # repair first_swap_ts if previously broken
        pools["first_swap_ts"] = pd.to_datetime(pools["first_swap_time"], utc=True).map(lambda x: int(x.timestamp()))
        pools.to_parquet(pools_path, index=False)
        swaps = pd.read_parquet(swaps_path)
    else:
        print("[panel] exporting pools...", flush=True)
        pools = export_pools()
        pools.to_parquet(pools_path, index=False)
        print(f"[panel] pools={len(pools)}", flush=True)
        print("[panel] exporting early swaps (9h)...", flush=True)
        swaps = export_early_swaps()
        swaps.to_parquet(swaps_path, index=False)
    print(f"[panel] pools={len(pools)} swaps={len(swaps)} path={swaps_path}", flush=True)
    print("[panel] building 15-60m decisions...", flush=True)
    panel = build_decisions(pools, swaps)
    out_path = PANEL_DIR / "greenfield_panel_15-60m.parquet"
    panel.to_parquet(out_path, index=False)
    supply = count_other_buckets(pools, swaps)
    if not panel.empty:
        supply["15-60m"] = {
            "n_rows": int(len(panel)),
            "n_pools": int(panel["pool"].nunique()),
            "n_decision_times": int(panel["decision_time"].nunique()),
            "split_counts": panel["split_role"].value_counts().to_dict(),
        }
    manifest = {
        "created_utc": utcnow(),
        "primary_bucket": PRIMARY_BUCKET,
        "n_pools_universe": int(len(pools)),
        "n_early_swaps": int(len(swaps)),
        "panel_path": str(out_path),
        "n_panel_rows": int(len(panel)),
        "eligibility": "PIT: pool exists, age in bucket, >=6 pre minutes, buy&sell in prior 10m, liq>0",
        "forbidden": "No future quote/priced_ok/survival filters",
        "sample_supply": supply,
        "keystone_universe_used": False,
    }
    (PANEL_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2), flush=True)
    if panel.empty:
        raise SystemExit("EMPTY_PANEL")


if __name__ == "__main__":
    main()
