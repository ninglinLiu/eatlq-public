"""Attach net-of-real-cost outcome (kappa + LP fee) to the alpha panel."""
from __future__ import annotations

import pandas as pd
import numpy as np

from ..execution_depth import compute_pool_kappa, WETH_USD
from .common_alpha import ALPHA_OUT, ensure_alpha_out


def attach_pnl_net(panel: pd.DataFrame, kappa: pd.DataFrame | None = None) -> pd.DataFrame:
    ensure_alpha_out()
    d = panel.copy()
    d["pool"] = d["pool"].astype(str).str.lower()
    stake = float(d["stake_weth"].iloc[0]) if "stake_weth" in d.columns and len(d) else 0.0166667
    if kappa is None:
        kappa = compute_pool_kappa()
    d = d.merge(kappa[["pool", "kappa_med"]], on="pool", how="left")
    # pools without kappa: treat as untradable → pnl_net = nan (excluded from scoring)
    d["fee_frac_roundtrip"] = 2.0 * pd.to_numeric(d.get("fee_bps", 3000), errors="coerce").fillna(3000) / 1e6
    d["oneway_frac"] = pd.to_numeric(d["kappa_med"], errors="coerce") * stake
    d["slip_frac_roundtrip"] = 2.0 * d["oneway_frac"]
    d["roundtrip_bps"] = (d["slip_frac_roundtrip"].fillna(np.inf) + d["fee_frac_roundtrip"]) * 1e4
    d["cost_weth"] = (d["slip_frac_roundtrip"].fillna(0) + d["fee_frac_roundtrip"]) * stake
    d["pnl"] = pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    d["pnl_net"] = np.where(d["kappa_med"].notna(), d["pnl"] - d["cost_weth"], np.nan)
    d["cost_known"] = d["kappa_med"].notna()
    print(
        f"[alpha/cost] cost_known={d['cost_known'].mean():.1%} "
        f"mean_pnl_net={d.loc[d['cost_known'], 'pnl_net'].mean():.6f} "
        f"median_rt_bps={d.loc[d['cost_known'], 'roundtrip_bps'].median():.0f}",
        flush=True,
    )
    return d
