"""Loss-truth labels: conservative floors, infra exclusion, no censored future-path PnL."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, load_freeze, save_json, utcnow


def _px_at(ts_arr: np.ndarray, px_arr: np.ndarray, t: int) -> float | None:
    if len(ts_arr) == 0:
        return None
    i = int(np.searchsorted(ts_arr, t, side="right") - 1)
    if i < 0:
        return None
    p = float(px_arr[i])
    return p if p > 0 else None


def _first_px_after(ts_arr: np.ndarray, px_arr: np.ndarray, t: int) -> tuple[float | None, int | None]:
    if len(ts_arr) == 0:
        return None, None
    i = int(np.searchsorted(ts_arr, t, side="right"))
    if i >= len(ts_arr):
        return None, None
    p = float(px_arr[i])
    return (p if p > 0 else None), int(ts_arr[i])


def label_events_truth(panel: pd.DataFrame, resolved: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    freeze = load_freeze()
    primary_h = int(freeze["targets"]["primary_horizon_seconds"])
    ladder = list(freeze["targets"]["tp_ladder"])
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    size_usd = float(freeze["execution"]["primary_size_usd"])
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])
    stake = size_usd / eth_usd
    slip_ceil = float(freeze["execution"]["depth_slippage_ceiling"])
    floor = float(freeze["loss_truth"]["economic_unsellable_recovery_floor"])
    permanent = float(freeze["loss_truth"]["permanent_unsellable_default"])
    fee = 0.003
    gas_frac = (2 * gas) / max(stake, 1e-12)
    floor_pnl = stake * (floor - 1.0)  # e.g. -0.9 * stake
    permanent_pnl = stake * permanent  # -100%

    infra_tiers = set(freeze["loss_truth"]["infra_tiers_excluded_from_primary"])
    infra_routes = set(freeze["loss_truth"]["infra_routes_excluded_from_primary"])

    paths: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for pool, g in resolved.sort_values("ts").groupby("pool", sort=False):
        paths[str(pool)] = (
            g["ts"].to_numpy(dtype=np.int64),
            g["price_weth"].to_numpy(dtype=np.float64),
        )

    n = len(panel)
    entry_px = np.full(n, np.nan)
    fill_ts = np.full(n, np.nan)
    ret_tp = np.full(n, np.nan)
    wallet_pnl = np.full(n, np.nan)
    wallet_pnl_optimistic = np.full(n, np.nan)
    wallet_pnl_worst = np.full(n, np.nan)
    buy_status = np.array(["NO_FILL"] * n, dtype=object)
    loss_class = np.array(["ok"] * n, dtype=object)
    scorable = np.ones(n, dtype=bool)
    label_end = np.zeros(n, dtype=np.int64)

    pools = panel["pool"].astype(str).to_numpy()
    entry_ts = panel["entry_t"].astype(int).to_numpy()
    tiers = panel["tier"].astype(str).to_numpy() if "tier" in panel.columns else np.array(["C"] * n)
    routes = panel["route_family"].astype(str).to_numpy() if "route_family" in panel.columns else np.array(["unknown"] * n)
    censored = panel["censored"].astype(bool).to_numpy() if "censored" in panel.columns else np.zeros(n, dtype=bool)

    order = np.argsort(pools, kind="mergesort")
    i = 0
    while i < n:
        j = i + 1
        p = pools[order[i]]
        while j < n and pools[order[j]] == p:
            j += 1
        ts_arr, px_arr = paths.get(p, (np.array([], dtype=np.int64), np.array([], dtype=np.float64)))
        for k in order[i:j]:
            entry_t = int(entry_ts[k])
            label_end[k] = entry_t + primary_h

            is_infra = (tiers[k] in infra_tiers) or (routes[k] in infra_routes)
            is_cens = bool(censored[k])

            epx, fts = _first_px_after(ts_arr, px_arr, entry_t)
            if epx is None:
                buy_status[k] = "NO_FILL"
                loss_class[k] = "no_fill"
                wallet_pnl[k] = floor_pnl
                wallet_pnl_optimistic[k] = 0.0
                wallet_pnl_worst[k] = permanent_pnl
                if is_infra:
                    scorable[k] = False
                    loss_class[k] = "infra_no_fill"
                continue

            entry_px_eff = epx * (1.0 + slip_ceil * 0.25)
            entry_px[k] = entry_px_eff
            fill_ts[k] = fts if fts is not None else np.nan
            buy_status[k] = "SUCCESS"
            base_t = int(fts) if fts is not None else entry_t

            def horizon_ret(h: int) -> float:
                px = _px_at(ts_arr, px_arr, base_t + h)
                if px is None or entry_px_eff <= 0:
                    return floor - 1.0  # conservative
                px_eff = px * (1.0 - slip_ceil * 0.25)
                return px_eff / entry_px_eff - 1.0 - fee - gas_frac

            rp = horizon_ret(primary_h)
            # TP ladder
            tp_ret = rp
            end = base_t + primary_h
            i0 = int(np.searchsorted(ts_arr, base_t, side="right"))
            i1 = int(np.searchsorted(ts_arr, end, side="right"))
            hit = None
            for ii in range(i0, i1):
                if px_arr[ii] <= 0:
                    continue
                move = px_arr[ii] / entry_px_eff - 1.0
                for tp in ladder:
                    if move >= tp:
                        hit = tp
                        break
                if hit is not None:
                    break
            if hit is not None:
                tp_ret = hit * (1.0 - slip_ceil * 0.1) - fee - gas_frac
            ret_tp[k] = tp_ret
            opt = stake * float(tp_ret)
            wallet_pnl_optimistic[k] = opt

            if is_infra:
                scorable[k] = False
                loss_class[k] = "infra_or_unknown_route"
                wallet_pnl[k] = floor_pnl  # primary uses conservative
                wallet_pnl_worst[k] = permanent_pnl
            elif is_cens:
                # censored: no future price-path PnL
                scorable[k] = True  # still scorable but charged floor
                loss_class[k] = "censored_transfer_out"
                wallet_pnl[k] = floor_pnl
                wallet_pnl_worst[k] = permanent_pnl
            else:
                loss_class[k] = "ok"
                wallet_pnl[k] = opt
                wallet_pnl_worst[k] = min(opt, floor_pnl)
            label_end[k] = base_t + primary_h
        i = j
        if i % 100000 < (j - i) or i == n:
            print(f"[labels] {min(i, n):,}/{n:,}", flush=True)

    out = panel.copy()
    out["entry_px"] = entry_px
    out["fill_ts"] = fill_ts
    out["ret_tp_ladder"] = ret_tp
    out["wallet_pnl_weth"] = wallet_pnl
    out["wallet_pnl_optimistic_weth"] = wallet_pnl_optimistic
    out["wallet_pnl_worst_weth"] = wallet_pnl_worst
    out["buy_status"] = buy_status
    out["loss_class"] = loss_class
    out["scorable_primary"] = scorable
    out["label_end_time"] = label_end
    out["stake_weth"] = stake

    # Problem-class report
    report = {"generated_at": utcnow(), "stake_weth": stake, "floor_pnl_weth": floor_pnl, "classes": {}}
    for cls in sorted(set(loss_class.tolist())):
        m = out["loss_class"] == cls
        report["classes"][cls] = {
            "n": int(m.sum()),
            "pnl_primary_sum": float(out.loc[m, "wallet_pnl_weth"].fillna(0).sum()),
            "pnl_optimistic_sum": float(out.loc[m, "wallet_pnl_optimistic_weth"].fillna(0).sum()),
            "pnl_worst_sum": float(out.loc[m, "wallet_pnl_worst_weth"].fillna(0).sum()),
        }
    report["n_scorable"] = int(out["scorable_primary"].sum())
    report["n_excluded_infra"] = int((~out["scorable_primary"]).sum())
    report["verdict"] = "LOSS_TRUTH_OK"
    # Fail if floor never charged on NO_FILL
    no_fill = out["buy_status"].eq("NO_FILL")
    if no_fill.any():
        charged = (out.loc[no_fill, "wallet_pnl_weth"] - floor_pnl).abs() < 1e-12
        if not bool(charged.all()):
            report["verdict"] = "LOSS_TRUTH_FAIL"
            report["fail_reason"] = "NO_FILL not charged floor"
    save_json(OUT / "loss_truth_report.json", report)
    return out, report
