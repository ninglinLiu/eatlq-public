"""Build multi-lifecycle PIT panel from raw Phase0 opportunities + cached swaps.

Never uses old-line selected subsets. Phase0 gates are not applied as filters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.build_panel import parse_i256  # noqa: E402
from src.common import LN1P, WETH, assert_no_banned_features, sha256_file, utcnow  # noqa: E402

OUT = ROOT / "out_v3"
CACHE = OUT / "cache"
WETH_L = WETH.lower()


def load_freeze() -> dict:
    return json.loads((ROOT / "configs" / "v3_rescue_freeze.json").read_text())


def assign_rescue_splits(decision_times: pd.Series, freeze: dict) -> pd.Series:
    q = decision_times.rank(method="first") / max(len(decision_times), 1)
    ld = freeze["splits"]["label_design_frac"]
    dev = freeze["splits"]["dev_frac"]
    ih = freeze["splits"]["internal_holdout_frac"]
    cut1, cut2, cut3 = ld, ld + dev, ld + dev + ih
    out = np.where(
        q <= cut1,
        "LABEL_DESIGN",
        np.where(q <= cut2, "DEV", np.where(q <= cut3, "INTERNAL_HOLDOUT", "UNTOUCHED")),
    )
    return pd.Series(out, index=decision_times.index)


def _decode(swaps: pd.DataFrame) -> pd.DataFrame:
    s = swaps.copy()
    s["tick_i"] = [parse_i256(x) for x in s["tick"]]
    s["liq_i"] = [parse_i256(x) for x in s["liquidity"]]
    s["a0_i"] = [parse_i256(x) for x in s["amount0"]]
    s["a1_i"] = [parse_i256(x) for x in s["amount1"]]
    return s


def _orient(token0: str, token1: str) -> str:
    if str(token0).lower() == WETH_L:
        return "token1_is_alt"
    return "token0_is_alt"


def compute_pre_features(row, g: pd.DataFrame) -> dict:
    """PIT features from swaps with ts <= decision_time."""
    dt = int(row.decision_time)
    ts = g["ts"].to_numpy(dtype=np.int64)
    idx = int(np.searchsorted(ts, dt, side="right"))
    empty = {
        "ns_pre": 0,
        "active_minutes_pre_feat": 0,
        "buyers_pre": 0,
        "sellers_pre": 0,
        "senders_pre": 0,
        "buyer_sender_ratio": 0.0,
        "early_ret": 0.0,
        "pre_vol": 0.0,
        "pre_pullback": 0.0,
        "pre_runup_from_lo": 0.0,
        "range_location": 0.5,
        "liq_log": 0.0,
        "liq_trend": 0.0,
        "liq_drawdown": 0.0,
        "liq_accel": 0.0,
        "mom_h1": 0.0,
        "mom_h2": 0.0,
        "accel": 0.0,
        "net_flow": 0.0,
        "trade_count_imbalance": 0.0,
        "buys_10m": 0,
        "sells_10m": 0,
        "flow_persist_score": 0.0,
        "buyer_growth": 0.0,
        "seller_growth": 0.0,
        "part_concentration": 1.0,
        "vol_depth": 0.0,
        "depth_proxy": 0.0,
        "entry_slip_proxy": 1.0,
        "buy_ok_proxy": 0.0,
        "feat_ok": 0,
    }
    if idx < 4:
        return empty
    pre = slice(0, idx)
    ticks = g["tick_i"].to_numpy(dtype=np.float64)[pre]
    liqs = g["liq_i"].to_numpy(dtype=np.float64)[pre]
    pre_ts = ts[pre]
    if np.isnan(ticks[-1]) or liqs[-1] <= 0:
        return empty
    orient = _orient(row.canonical_token0 if hasattr(row, "canonical_token0") else row.registry_token0,
                     row.canonical_token1 if hasattr(row, "canonical_token1") else row.registry_token1)
    a0 = g["a0_i"].to_numpy()[pre]
    a1 = g["a1_i"].to_numpy()[pre]
    if orient == "token1_is_alt":
        sign = np.array([1 if x > 0 else (-1 if x < 0 else 0) for x in a0], dtype=np.int8)
    else:
        sign = np.array([1 if x > 0 else (-1 if x < 0 else 0) for x in a1], dtype=np.int8)
    win10 = pre_ts >= (dt - 600)
    buys = int(((sign > 0) & win10).sum())
    sells = int(((sign < 0) & win10).sum())
    # persistence: fraction of last 6 five-minute bins with >=1 swap
    bins = (pre_ts[-min(len(pre_ts), 180):] // 300)
    if len(bins):
        uniq = len(np.unique(bins))
        span = max(int(bins.max() - bins.min()) + 1, 1)
        flow_persist = uniq / min(span, 12)
    else:
        flow_persist = 0.0
    mid = max(idx // 2, 1)
    tick_first, tick_mid, tick_last = float(ticks[0]), float(ticks[mid - 1]), float(ticks[-1])
    sign_price = -1.0 if orient == "token1_is_alt" else 1.0  # alt-priced return
    early_ret = sign_price * (tick_last - tick_first) * LN1P
    mom_h1 = sign_price * (tick_mid - tick_first) * LN1P
    mom_h2 = sign_price * (tick_last - tick_mid) * LN1P
    rets = np.diff(ticks[~np.isnan(ticks)]) * LN1P * sign_price
    vol = float(np.std(rets)) if len(rets) > 2 else 0.0
    peak, trough = float(np.nanmax(ticks)), float(np.nanmin(ticks))
    pullback = sign_price * (tick_last - peak) * LN1P
    runup = sign_price * (tick_last - trough) * LN1P
    range_loc = 0.5 if peak == trough else (tick_last - trough) / (peak - trough)
    liq_last, liq_first = float(liqs[-1]), float(liqs[0]) if liqs[0] > 0 else float(liqs[-1])
    third = max(idx // 3, 1)
    liq_mid = float(liqs[third])
    liq_trend = float(np.log1p(max(liq_last, 0)) - np.log1p(max(liq_first, 0)))
    liq_accel = float(np.log1p(max(liq_last, 0)) - 2 * np.log1p(max(liq_mid, 0)) + np.log1p(max(liq_first, 0)))
    recipients = g["recipient"].to_numpy()[pre]
    senders = g["sender"].to_numpy()[pre]
    buy_mask = sign > 0
    sell_mask = sign < 0
    buyers = len(set(recipients[buy_mask][-200:])) if buy_mask.any() else 0
    sellers = len(set(recipients[sell_mask][-200:])) if sell_mask.any() else 0
    senders_n = len(set(senders[-200:]))
    half = idx // 2
    buyers_early = len(set(recipients[:half][sign[:half] > 0])) if half else 0
    buyers_late = len(set(recipients[half:][sign[half:] > 0])) if half else 0
    sellers_early = len(set(recipients[:half][sign[:half] < 0])) if half else 0
    sellers_late = len(set(recipients[half:][sign[half:] < 0])) if half else 0
    n_minutes = len(np.unique(pre_ts // 60))
    liq_log = float(np.log1p(max(liq_last, 0)))
    return {
        "ns_pre": int(idx),
        "active_minutes_pre_feat": int(n_minutes),
        "buyers_pre": int(buyers),
        "sellers_pre": int(sellers),
        "senders_pre": int(senders_n),
        "buyer_sender_ratio": buyers / max(senders_n, 1),
        "early_ret": float(early_ret),
        "pre_vol": float(vol),
        "pre_pullback": float(pullback),
        "pre_runup_from_lo": float(runup),
        "range_location": float(range_loc),
        "liq_log": liq_log,
        "liq_trend": liq_trend,
        "liq_drawdown": float((liq_last / max(liq_first, 1e-12)) - 1.0),
        "liq_accel": liq_accel,
        "mom_h1": float(mom_h1),
        "mom_h2": float(mom_h2),
        "accel": float(mom_h2 - mom_h1),
        "net_flow": float((sign > 0).sum() - (sign < 0).sum()),
        "trade_count_imbalance": float(buys - sells) / max(buys + sells, 1),
        "buys_10m": buys,
        "sells_10m": sells,
        "flow_persist_score": float(flow_persist),
        "buyer_growth": float(buyers_late - buyers_early),
        "seller_growth": float(sellers_late - sellers_early),
        "part_concentration": float(1.0 / max(buyers, 1)),
        "vol_depth": float(vol / (abs(liq_log) + 1e-6)),
        "depth_proxy": float(min(liq_log / 40.0, 1.0)),
        "entry_slip_proxy": float(max(0.0, min(1.0, 1.0 - min(liq_log / 50.0, 1.0)))),
        "buy_ok_proxy": float(1.0 if buys >= 1 and sells >= 1 and idx >= 6 else 0.0),
        "feat_ok": 1,
    }


def attach_deployer_pit(panel: pd.DataFrame) -> pd.DataFrame:
    """Deployer history using only prior decisions (PIT)."""
    d = panel.sort_values("decision_time").copy()
    prior_pools: dict[str, set[str]] = {}
    prior_fatal: dict[str, list[int]] = {}
    prior_rt: dict[str, list[int]] = {}
    first_seen: dict[str, int] = {}
    out_pools, out_fatal, out_rt, out_age = [], [], [], []
    for r in d.itertuples(index=False):
        dep = str(getattr(r, "deployer", "") or "")
        if dep not in first_seen and dep:
            first_seen[dep] = int(r.decision_time)
        pools = prior_pools.get(dep, set())
        fats = prior_fatal.get(dep, [])
        rts = prior_rt.get(dep, [])
        out_pools.append(len(pools))
        out_fatal.append(float(np.mean(fats)) if fats else 0.0)
        out_rt.append(float(np.mean(rts)) if rts else 0.0)
        out_age.append((int(r.decision_time) - first_seen.get(dep, int(r.decision_time))) / 3600.0)
        # update AFTER emitting features
        if dep:
            prior_pools.setdefault(dep, set()).add(str(r.pool))
            # labels may not exist yet — updated later in build_labels via merge; placeholder 0
    d["deployer_prior_pools"] = out_pools
    d["deployer_prior_fatal_rate"] = out_fatal
    d["deployer_prior_right_tail_rate"] = out_rt
    d["deployer_age_hours"] = out_age
    return d


def update_deployer_pit_with_labels(panel: pd.DataFrame) -> pd.DataFrame:
    """Second pass once labels exist."""
    d = panel.sort_values("decision_time").copy()
    prior_pools: dict[str, set[str]] = {}
    prior_fatal: dict[str, list[int]] = {}
    prior_rt: dict[str, list[int]] = {}
    first_seen: dict[str, int] = {}
    out_pools, out_fatal, out_rt, out_age = [], [], [], []
    for r in d.itertuples(index=False):
        dep = str(getattr(r, "deployer", "") or "")
        if dep and dep not in first_seen:
            first_seen[dep] = int(r.decision_time)
        pools = prior_pools.get(dep, set())
        fats = prior_fatal.get(dep, [])
        rts = prior_rt.get(dep, [])
        out_pools.append(len(pools))
        out_fatal.append(float(np.mean(fats)) if fats else 0.0)
        out_rt.append(float(np.mean(rts)) if rts else 0.0)
        out_age.append((int(r.decision_time) - first_seen.get(dep, int(r.decision_time))) / 3600.0)
        if dep:
            prior_pools.setdefault(dep, set()).add(str(r.pool))
            prior_fatal.setdefault(dep, []).append(int(getattr(r, "y_future_unsellable", 0) or 0))
            prior_rt.setdefault(dep, []).append(int(getattr(r, "y_right_tail_25", 0) or 0))
    d["deployer_prior_pools"] = out_pools
    d["deployer_prior_fatal_rate"] = out_fatal
    d["deployer_prior_right_tail_rate"] = out_rt
    d["deployer_age_hours"] = out_age
    return d


def build_panel(freeze: dict | None = None) -> pd.DataFrame:
    freeze = freeze or load_freeze()
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    opp_path = ROOT / freeze["source_panel"]
    swaps_path = ROOT / freeze["source_swaps"]
    opp = pd.read_parquet(opp_path)
    swaps = _decode(pd.read_parquet(swaps_path))
    grouped = {p: g.sort_values("ts") for p, g in swaps.groupby("pool", sort=False)}

    rows = []
    for r in opp.itertuples(index=False):
        g = grouped.get(str(r.pool))
        feats = compute_pre_features(r, g) if g is not None else compute_pre_features(r, pd.DataFrame({"ts": [], "tick_i": [], "liq_i": [], "a0_i": [], "a1_i": [], "sender": [], "recipient": []}))
        tok0 = getattr(r, "canonical_token0", None) or r.registry_token0
        tok1 = getattr(r, "canonical_token1", None) or r.registry_token1
        rows.append(
            {
                "signal_id": r.opportunity_id,
                "opportunity_id": r.opportunity_id,
                "pool": r.pool,
                "age_bucket": r.age_bucket,
                "age_s": int(r.age_s),
                "first_swap_ts": int(r.first_swap_ts),
                "decision_time": int(r.decision_time),
                "future_time": int(r.future_time),
                "decision_block": int(r.decision_block),
                "future_block": int(r.future_block),
                "decision_block_lag_s": int(r.decision_block_lag_s),
                "n_swaps_pre": int(r.n_swaps_pre),
                "active_minutes_pre": int(r.active_minutes_pre),
                "enough_pit_history": bool(r.enough_pit_history),
                "entry_tick": float(r.entry_tick) if pd.notna(r.entry_tick) else np.nan,
                "entry_liquidity": float(r.entry_liquidity) if pd.notna(r.entry_liquidity) else np.nan,
                "future_ticks_json": r.future_ticks_json,
                "canonical_valid": bool(r.canonical_valid),
                "fee_canonical": int(r.fee_canonical) if pd.notna(r.fee_canonical) else int(r.fee_raw),
                "canonical_alt": r.canonical_alt,
                "canonical_token0": tok0,
                "canonical_token1": tok1,
                "deployer": getattr(r, "deployer", "") or "",
                "deployer_status": getattr(r, "deployer_status", "") or "",
                "buy_status": r.buy_status,
                "current_sell_status": r.current_sell_status,
                "future_sell_status": r.future_sell_status,
                "future_permanent_unsellable": bool(r.future_permanent_unsellable),
                "infrastructure_unknown": bool(r.infrastructure_unknown),
                "entry_slippage": float(r.entry_slippage) if pd.notna(r.entry_slippage) else np.nan,
                "current_exit_slippage": float(r.current_exit_slippage) if pd.notna(r.current_exit_slippage) else np.nan,
                "future_exit_slippage": float(r.future_exit_slippage) if pd.notna(r.future_exit_slippage) else np.nan,
                "executable_depth_weth": float(r.executable_depth_weth) if pd.notna(r.executable_depth_weth) else 0.0,
                "production_depth_pass": bool(r.production_depth_pass),
                "actual_token_received": r.actual_token_received,
                "current_weth_out": r.current_weth_out,
                "future_weth_out": r.future_weth_out,
                "mfe_6h_census": float(r.mfe_6h) if pd.notna(r.mfe_6h) else np.nan,
                "mae_6h_census": float(r.mae_6h) if pd.notna(r.mae_6h) else np.nan,
                "is_negative_control": r.age_bucket == freeze["negative_control_bucket"],
                "formal_bucket": r.age_bucket in freeze["formal_buckets"],
                "infra_lag_s": float(r.decision_block_lag_s),
                **feats,
            }
        )
    panel = pd.DataFrame(rows)
    panel = attach_deployer_pit(panel)
    panel = panel.sort_values("decision_time").reset_index(drop=True)
    # market context (PIT within panel)
    times = panel["decision_time"].to_numpy()
    pools_arr = panel["pool"].to_numpy()
    breadth = np.zeros(len(panel))
    j0 = 0
    for i, t in enumerate(times):
        while times[j0] < t - 3600:
            j0 += 1
        breadth[i] = len(set(pools_arr[j0 : i + 1]))
    panel["base_breadth_1h"] = breadth
    panel["opportunity_supply"] = panel.groupby("decision_time")["signal_id"].transform("count")
    panel["xs_early_ret_rank"] = panel.groupby("decision_time")["early_ret"].rank(pct=True)
    panel["split_role"] = assign_rescue_splits(panel["decision_time"], freeze)
    panel["old_line_inputs_used"] = False
    assert_no_banned_features([c for c in panel.columns if c not in ("future_ticks_json",)])
    path = OUT / "panel" / "rescue_panel.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(path, index=False)
    meta = {
        "created_utc": utcnow(),
        "n_rows": int(len(panel)),
        "n_formal": int(panel["formal_bucket"].sum()),
        "n_neg_control": int(panel["is_negative_control"].sum()),
        "source_panel_sha256": sha256_file(opp_path),
        "source_swaps_sha256": sha256_file(swaps_path),
        "split_counts": panel["split_role"].value_counts().to_dict(),
        "bucket_counts": panel["age_bucket"].value_counts().to_dict(),
    }
    (OUT / "panel" / "PANEL_META.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[v3.panel] rows={len(panel)} formal={meta['n_formal']} splits={meta['split_counts']}", flush=True)
    return panel


def main() -> None:
    build_panel()


if __name__ == "__main__":
    main()
