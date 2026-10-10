"""Path-causal exit labeling for Exit Alpha v0.

Reuses fill / fee / gas / slip / loss-truth floors from labels_truth.
Only prices observed up to exit time may determine the exit.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..common import ROOT, load_freeze
from ..labels_truth import _first_px_after, _px_at
from .common_exit import EXIT_OUT, ensure_exit_out, load_exit_cfg


def _exec_constants() -> dict[str, float]:
    freeze = load_freeze()
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    size_usd = float(freeze["execution"]["primary_size_usd"])
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])
    stake = size_usd / eth_usd
    slip_ceil = float(freeze["execution"]["depth_slippage_ceiling"])
    floor = float(freeze["loss_truth"]["economic_unsellable_recovery_floor"])
    permanent = float(freeze["loss_truth"]["permanent_unsellable_default"])
    fee = 0.003
    gas_frac = (2 * gas) / max(stake, 1e-12)
    return {
        "stake": stake,
        "slip_ceil": slip_ceil,
        "floor": floor,
        "permanent": permanent,
        "fee": fee,
        "gas_frac": gas_frac,
        "floor_pnl": stake * (floor - 1.0),
        "permanent_pnl": stake * permanent,
        "infra_tiers": set(freeze["loss_truth"]["infra_tiers_excluded_from_primary"]),
        "infra_routes": set(freeze["loss_truth"]["infra_routes_excluded_from_primary"]),
    }


def _ret_at_exit(
    entry_px_eff: float,
    exit_px_raw: float | None,
    *,
    slip_ceil: float,
    fee: float,
    gas_frac: float,
    floor: float,
    hit_tp_level: float | None = None,
) -> float:
    if hit_tp_level is not None:
        return hit_tp_level * (1.0 - slip_ceil * 0.1) - fee - gas_frac
    if exit_px_raw is None or entry_px_eff <= 0:
        return floor - 1.0
    px_eff = exit_px_raw * (1.0 - slip_ceil * 0.25)
    return px_eff / entry_px_eff - 1.0 - fee - gas_frac


def _simulate_exit(
    policy: str,
    params: dict,
    ts_arr: np.ndarray,
    px_arr: np.ndarray,
    base_t: int,
    entry_px_eff: float,
    consts: dict,
) -> tuple[float, int, str]:
    """Return (net_return, exit_ts, exit_reason). Path-causal only."""
    slip = consts["slip_ceil"]
    fee = consts["fee"]
    gas_frac = consts["gas_frac"]
    floor = consts["floor"]

    if policy == "FIXED_TP_4H":
        horizon = int(params["horizon_s"])
        ladder = list(params["tp_ladder"])
        end = base_t + horizon
        i0 = int(np.searchsorted(ts_arr, base_t, side="right"))
        i1 = int(np.searchsorted(ts_arr, end, side="right"))
        for ii in range(i0, i1):
            if px_arr[ii] <= 0:
                continue
            move = px_arr[ii] / entry_px_eff - 1.0
            for tp in ladder:
                if move >= tp:
                    ret = _ret_at_exit(
                        entry_px_eff, None, slip_ceil=slip, fee=fee,
                        gas_frac=gas_frac, floor=floor, hit_tp_level=tp,
                    )
                    return ret, int(ts_arr[ii]), f"tp_{tp}"
        px = _px_at(ts_arr, px_arr, end)
        ret = _ret_at_exit(entry_px_eff, px, slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor)
        return ret, end, "time_4h"

    if policy in ("TIME_30M", "TIME_4H"):
        horizon = int(params["horizon_s"])
        end = base_t + horizon
        px = _px_at(ts_arr, px_arr, end)
        ret = _ret_at_exit(entry_px_eff, px, slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor)
        return ret, end, f"time_{horizon}s"

    if policy == "TRAIL_20_FROM_PEAK":
        arm_peak = float(params["arm_peak"])
        trail_frac = float(params["trail_frac_of_peak"])
        hard_stop = float(params["hard_stop"])
        max_hold = int(params["max_hold_s"])
        end = base_t + max_hold
        i0 = int(np.searchsorted(ts_arr, base_t, side="right"))
        i1 = int(np.searchsorted(ts_arr, end, side="right"))
        peak = 0.0
        armed = False
        for ii in range(i0, i1):
            if px_arr[ii] <= 0:
                continue
            move = px_arr[ii] / entry_px_eff - 1.0
            if move > peak:
                peak = move
            if peak >= arm_peak:
                armed = True
            if move <= hard_stop:
                ret = _ret_at_exit(
                    entry_px_eff, float(px_arr[ii]),
                    slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor,
                )
                return ret, int(ts_arr[ii]), "hard_stop"
            if armed:
                stop_level = peak * (1.0 - trail_frac)
                if move <= stop_level:
                    ret = _ret_at_exit(
                        entry_px_eff, float(px_arr[ii]),
                        slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor,
                    )
                    return ret, int(ts_arr[ii]), "trail_stop"
        px = _px_at(ts_arr, px_arr, end)
        ret = _ret_at_exit(entry_px_eff, px, slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor)
        return ret, end, "max_hold"

    if policy == "MOMENTUM_HOLD":
        early_cut_s = int(params["early_cut_s"])
        early_cut_ret = float(params["early_cut_ret"])
        arm_by_s = int(params["arm_by_s"])
        arm_peak = float(params["arm_peak"])
        trail_frac = float(params["trail_frac_of_peak"])
        max_hold = int(params["max_hold_s"])
        end = base_t + max_hold
        i0 = int(np.searchsorted(ts_arr, base_t, side="right"))
        i1 = int(np.searchsorted(ts_arr, end, side="right"))
        peak = 0.0
        armed = False
        early_deadline = base_t + early_cut_s
        arm_deadline = base_t + arm_by_s
        for ii in range(i0, i1):
            t = int(ts_arr[ii])
            if px_arr[ii] <= 0:
                continue
            move = px_arr[ii] / entry_px_eff - 1.0
            if move > peak:
                peak = move
            if peak >= arm_peak:
                armed = True
            # early cut: within first 15m, if ret < -10%
            if t <= early_deadline and move <= early_cut_ret:
                ret = _ret_at_exit(
                    entry_px_eff, float(px_arr[ii]),
                    slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor,
                )
                return ret, t, "early_cut"
            # if never armed by 2h, exit at 2h mark (on this tick or at deadline)
            if (not armed) and t >= arm_deadline:
                ret = _ret_at_exit(
                    entry_px_eff, float(px_arr[ii]),
                    slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor,
                )
                return ret, t, "no_momentum_2h"
            if armed:
                stop_level = peak * (1.0 - trail_frac)
                if move <= stop_level:
                    ret = _ret_at_exit(
                        entry_px_eff, float(px_arr[ii]),
                        slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor,
                    )
                    return ret, t, "trail_stop"
        # if path ends before arm_deadline and never armed, mark at end of available / max
        if not armed:
            cut_t = min(arm_deadline, end)
            px = _px_at(ts_arr, px_arr, cut_t)
            ret = _ret_at_exit(entry_px_eff, px, slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor)
            return ret, cut_t, "no_momentum_2h"
        px = _px_at(ts_arr, px_arr, end)
        ret = _ret_at_exit(entry_px_eff, px, slip_ceil=slip, fee=fee, gas_frac=gas_frac, floor=floor)
        return ret, end, "max_hold"

    raise ValueError(f"Unknown exit policy: {policy}")


def label_under_exit(
    panel: pd.DataFrame,
    resolved: pd.DataFrame,
    policy: str,
) -> pd.DataFrame:
    """Label panel under a single exit policy. Returns copy with wallet_pnl_weth etc."""
    ensure_exit_out()
    cfg = load_exit_cfg()
    if policy not in cfg["exit_params"]:
        raise ValueError(f"Policy {policy} not in freeze")
    params = cfg["exit_params"][policy]
    consts = _exec_constants()
    stake = consts["stake"]
    floor_pnl = consts["floor_pnl"]
    permanent_pnl = consts["permanent_pnl"]
    infra_tiers = consts["infra_tiers"]
    infra_routes = consts["infra_routes"]

    paths: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for pool, g in resolved.sort_values("ts").groupby("pool", sort=False):
        paths[str(pool).lower()] = (
            g["ts"].to_numpy(dtype=np.int64),
            g["price_weth"].to_numpy(dtype=np.float64),
        )

    n = len(panel)
    entry_px = np.full(n, np.nan)
    fill_ts = np.full(n, np.nan)
    ret_exit = np.full(n, np.nan)
    wallet_pnl = np.full(n, np.nan)
    buy_status = np.array(["NO_FILL"] * n, dtype=object)
    loss_class = np.array(["ok"] * n, dtype=object)
    scorable = np.ones(n, dtype=bool)
    label_end = np.zeros(n, dtype=np.int64)
    exit_reason = np.array([""] * n, dtype=object)

    pools = panel["pool"].astype(str).str.lower().to_numpy()
    entry_ts = panel["entry_t"].astype(int).to_numpy()
    tiers = panel["tier"].astype(str).to_numpy() if "tier" in panel.columns else np.array(["C"] * n)
    routes = (
        panel["route_family"].astype(str).to_numpy()
        if "route_family" in panel.columns
        else np.array(["unknown"] * n)
    )
    censored = (
        panel["censored"].astype(bool).to_numpy()
        if "censored" in panel.columns
        else np.zeros(n, dtype=bool)
    )

    order = np.argsort(pools, kind="mergesort")
    i = 0
    done = 0
    while i < n:
        j = i + 1
        p = pools[order[i]]
        while j < n and pools[order[j]] == p:
            j += 1
        ts_arr, px_arr = paths.get(p, (np.array([], dtype=np.int64), np.array([], dtype=np.float64)))
        for k in order[i:j]:
            entry_t = int(entry_ts[k])
            is_infra = (tiers[k] in infra_tiers) or (routes[k] in infra_routes)
            is_cens = bool(censored[k])

            epx, fts = _first_px_after(ts_arr, px_arr, entry_t)
            if epx is None:
                buy_status[k] = "NO_FILL"
                loss_class[k] = "infra_no_fill" if is_infra else "no_fill"
                wallet_pnl[k] = floor_pnl
                label_end[k] = entry_t
                exit_reason[k] = "no_fill"
                if is_infra:
                    scorable[k] = False
                continue

            entry_px_eff = epx * (1.0 + consts["slip_ceil"] * 0.25)
            entry_px[k] = entry_px_eff
            fill_ts[k] = fts if fts is not None else np.nan
            buy_status[k] = "SUCCESS"
            base_t = int(fts) if fts is not None else entry_t

            if is_infra:
                scorable[k] = False
                loss_class[k] = "infra_or_unknown_route"
                wallet_pnl[k] = floor_pnl
                label_end[k] = base_t
                exit_reason[k] = "infra"
                continue
            if is_cens:
                loss_class[k] = "censored_transfer_out"
                wallet_pnl[k] = floor_pnl
                label_end[k] = base_t
                exit_reason[k] = "censored"
                continue

            ret, exit_t, reason = _simulate_exit(
                policy, params, ts_arr, px_arr, base_t, entry_px_eff, consts
            )
            ret_exit[k] = ret
            wallet_pnl[k] = stake * float(ret)
            label_end[k] = int(exit_t)
            exit_reason[k] = reason
            loss_class[k] = "ok"
        prev = i
        i = j
        if (i // 100000) != (prev // 100000) or i == n:
            print(f"[exit/labels] {policy} {i:,}/{n:,}", flush=True)

    out = panel.copy()
    out["entry_px"] = entry_px
    out["fill_ts"] = fill_ts
    out["ret_exit"] = ret_exit
    out["wallet_pnl_weth"] = wallet_pnl
    out["buy_status"] = buy_status
    out["loss_class"] = loss_class
    out["scorable_primary"] = scorable
    out["label_end_time"] = label_end
    out["exit_reason"] = exit_reason
    out["exit_policy"] = policy
    out["stake_weth"] = stake
    print(
        f"[exit/labels] {policy}: success={(buy_status=='SUCCESS').mean():.1%} "
        f"mean_pnl={np.nanmean(wallet_pnl):.6f} "
        f"reasons={pd.Series(exit_reason).value_counts().head(5).to_dict()}",
        flush=True,
    )
    return out


def label_all_exits(
    panel: pd.DataFrame,
    resolved: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    ensure_exit_out()
    if resolved is None:
        resolved = pd.read_parquet(
            ROOT / "data" / "resolved_swaps.parquet",
            columns=["pool", "ts", "price_weth"],
        )
    # normalize pool keys once
    resolved = resolved.copy()
    resolved["pool"] = resolved["pool"].astype(str).str.lower()
    cfg = load_exit_cfg()
    out = {}
    for policy in cfg["exits"]:
        cache = EXIT_OUT / f"labeled_{policy}.parquet"
        if cache.exists():
            cached = pd.read_parquet(cache)
            if len(cached) == len(panel):
                print(f"[exit/labels] loading cache {cache.name}", flush=True)
                out[policy] = cached
                continue
            print(
                f"[exit/labels] cache size mismatch {len(cached)}!={len(panel)}; relabel {policy}",
                flush=True,
            )
        labeled = label_under_exit(panel, resolved, policy)
        labeled.to_parquet(cache, index=False)
        out[policy] = labeled
    return out


if __name__ == "__main__":
    from ..common import OUT

    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab.head(5000)
    rs = pd.read_parquet(ROOT / "data" / "resolved_swaps.parquet", columns=["pool", "ts", "price_weth"])
    d = label_under_exit(elig, rs, "TRAIL_20_FROM_PEAK")
    print(d["exit_reason"].value_counts())
