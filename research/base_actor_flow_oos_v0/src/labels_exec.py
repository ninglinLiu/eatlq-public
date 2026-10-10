"""Net executable returns at fixed horizons + strong-TP ladder.

Entry at signal_observed_at + measured_latency; never uses the actor's own next fill
quantity as our fill. Costs: gas both legs, fee proxy, slippage ceiling haircut.
Trailing is recorded as a failure-control only.
"""
from __future__ import annotations

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


def label_events(panel: pd.DataFrame, resolved: pd.DataFrame) -> pd.DataFrame:
    freeze = load_freeze()
    latency = int(freeze["execution"]["measured_latency_seconds"])
    primary_h = int(freeze["targets"]["primary_horizon_seconds"])
    ladder = list(freeze["targets"]["tp_ladder"])
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    size_usd = float(freeze["execution"]["primary_size_usd"])
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])
    stake_weth = size_usd / eth_usd
    slip_ceil = float(freeze["execution"]["depth_slippage_ceiling"])
    fee = 0.003
    gas_frac = (2 * gas) / max(stake_weth, 1e-12)

    paths: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for pool, g in resolved.sort_values("ts").groupby("pool", sort=False):
        paths[str(pool)] = (
            g["ts"].to_numpy(dtype=np.int64),
            g["price_weth"].to_numpy(dtype=np.float64),
        )

    n = len(panel)
    entry_px = np.full(n, np.nan)
    fill_ts = np.full(n, np.nan)
    ret_30m = np.full(n, np.nan)
    ret_4h = np.full(n, np.nan)
    ret_24h = np.full(n, np.nan)
    ret_primary = np.full(n, np.nan)
    ret_tp = np.full(n, np.nan)
    ret_trail = np.full(n, np.nan)
    buy_status = np.array(["NO_FILL"] * n, dtype=object)
    label_end = np.zeros(n, dtype=np.int64)
    wallet_pnl = np.full(n, np.nan)

    pools = panel["pool"].astype(str).to_numpy()
    dts = panel["decision_time"].astype(int).to_numpy()
    signal_ids = panel["signal_id"].astype(str).to_numpy()

    # Process by pool for cache locality
    order = np.argsort(pools, kind="mergesort")
    i = 0
    while i < n:
        j = i + 1
        p = pools[order[i]]
        while j < n and pools[order[j]] == p:
            j += 1
        ts_arr, px_arr = paths.get(p, (np.array([], dtype=np.int64), np.array([], dtype=np.float64)))
        for k in order[i:j]:
            t0 = int(dts[k])
            entry_t = t0 + latency
            epx, fts = _first_px_after(ts_arr, px_arr, entry_t)
            label_end[k] = entry_t + primary_h
            if epx is None:
                continue
            entry_px_eff = epx * (1.0 + slip_ceil * 0.25)
            entry_px[k] = entry_px_eff
            fill_ts[k] = fts if fts is not None else np.nan
            buy_status[k] = "SUCCESS"
            base_t = int(fts) if fts is not None else entry_t

            def horizon_ret(h: int) -> float:
                px = _px_at(ts_arr, px_arr, base_t + h)
                if px is None or entry_px_eff <= 0:
                    return -1.0
                px_eff = px * (1.0 - slip_ceil * 0.25)
                return px_eff / entry_px_eff - 1.0 - fee - gas_frac

            r30 = horizon_ret(1800)
            r4 = horizon_ret(14400)
            r24 = horizon_ret(86400)
            ret_30m[k] = r30
            ret_4h[k] = r4
            ret_24h[k] = r24
            rp = {1800: r30, 14400: r4, 86400: r24}.get(primary_h, r4)
            ret_primary[k] = rp

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

            # Trailing failure control
            trail = rp
            peak = entry_px_eff
            stopped = None
            for ii in range(i0, i1):
                pxi = px_arr[ii]
                if pxi <= 0:
                    continue
                peak = max(peak, pxi)
                if pxi <= peak * 0.80:
                    stopped = pxi
                    break
            if stopped is not None and entry_px_eff > 0:
                trail = stopped / entry_px_eff - 1.0 - fee - gas_frac
            ret_trail[k] = trail
            wallet_pnl[k] = stake_weth * float(tp_ret)
            label_end[k] = base_t + primary_h
        i = j
        if i % 50000 < (j - i) or i == n:
            print(f"[phase3] labeled {min(i,n):,}/{n:,}", flush=True)

    labels = pd.DataFrame({
        "signal_id": signal_ids,
        "entry_px": entry_px,
        "fill_ts": fill_ts,
        "wallet_pnl_weth": wallet_pnl,
        "ret_primary": ret_primary,
        "ret_30m": ret_30m,
        "ret_4h": ret_4h,
        "ret_24h": ret_24h,
        "ret_tp_ladder": ret_tp,
        "ret_trailing_control": ret_trail,
        "buy_status": buy_status,
        "label_end_time": label_end,
        "stake_weth": stake_weth,
    })
    out = panel.merge(labels, on="signal_id", how="left")
    path = OUT / "panel" / "events_labeled.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)
    save_json(OUT / "phase3" / "label_summary.json", {
        "n": int(len(out)),
        "fill_rate": float(out["buy_status"].eq("SUCCESS").mean()) if len(out) else 0.0,
        "mean_ret_tp_ladder": float(pd.to_numeric(out["ret_tp_ladder"], errors="coerce").mean()) if len(out) else None,
        "mean_wallet_pnl_weth": float(pd.to_numeric(out["wallet_pnl_weth"], errors="coerce").mean()) if len(out) else None,
        "generated_at": utcnow(),
    })
    print(
        f"[phase3] labeled events: {len(out):,} "
        f"fill_rate={out['buy_status'].eq('SUCCESS').mean():.3f}",
        flush=True,
    )
    return out
