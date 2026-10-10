"""Dual-head labels: Risk and Opportunity, separately modeled.

Wallet PnL uses atomic fill fields when audited; never uses future sellability
to filter the entry sample.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import LN1P, WETH, utcnow  # noqa: E402
from src.v3.build_panel import load_freeze, update_deployer_pit_with_labels  # noqa: E402

OUT = ROOT / "out_v3"
WETH_L = WETH.lower()


def _path_mfe_mae(row) -> tuple[float, float, float, float, int]:
    """Return mfe, mae, terminal_ret, time_to_rt25 (seconds proxy), n_ticks."""
    try:
        ticks = np.array(json.loads(row.future_ticks_json) if isinstance(row.future_ticks_json, str) else (row.future_ticks_json or []), dtype=np.float64)
    except Exception:
        ticks = np.array([], dtype=np.float64)
    if len(ticks) == 0 or pd.isna(row.entry_tick):
        # fall back to census
        mfe = float(row.mfe_6h_census) if pd.notna(row.mfe_6h_census) else np.nan
        mae = float(row.mae_6h_census) if pd.notna(row.mae_6h_census) else np.nan
        return mfe, mae, mfe if pd.notna(mfe) else np.nan, np.nan, 0
    sign = -1.0 if str(row.canonical_token0).lower() == WETH_L else 1.0
    rets = sign * (ticks - float(row.entry_tick)) * LN1P
    mfe = float(np.max(rets))
    mae = float(np.min(rets))
    term = float(rets[-1])
    hit = np.where(rets >= 0.25)[0]
    # ticks are unordered in time spacing; use index fraction * horizon as proxy
    ttrt = float(hit[0] / max(len(rets) - 1, 1) * 21600) if len(hit) else np.nan
    return mfe, mae, term, ttrt, int(len(rets))


def _wallet_pnl(row, freeze: dict) -> tuple[float, str]:
    """Atomic wallet delta in WETH for production amount_in."""
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    floor = float(freeze["execution"]["economic_unsellable_recovery_floor"])

    buy = str(row.buy_status)
    if buy == "NOT_AUDITED":
        # path proxy when atomic missing: use terminal path return * stake, haircut for risk
        mfe = float(row.mfe) if pd.notna(row.mfe) else 0.0
        mae = float(row.mae) if pd.notna(row.mae) else 0.0
        term = float(row.terminal_ret) if pd.notna(row.terminal_ret) else 0.0
        # conservative: if deep mae and weak mfe, treat as near-fatal
        if mae <= -0.9 or (pd.notna(row.future_permanent_unsellable) and row.future_permanent_unsellable):
            return -amount * (1.0 - floor) - 2 * gas, "PROXY_FATAL"
        pnl = amount * float(np.clip(term, -1.0, 3.0)) - 2 * gas
        return pnl, "PROXY_PATH"
    if buy == "INFRASTRUCTURE_UNKNOWN":
        return -gas, "INFRA_UNKNOWN"
    if buy != "SUCCESS":
        return -gas, "BUY_FAIL"
    # buy success
    fut = str(row.future_sell_status)
    if fut == "SUCCESS" and pd.notna(row.future_weth_out) and str(row.future_weth_out) not in ("", "None"):
        out_w = int(row.future_weth_out) / 1e18
        return out_w - amount - 2 * gas, "ROUNDTRIP"
    if bool(row.future_permanent_unsellable) or fut == "REVERT":
        return -amount * (1.0 - floor) - 2 * gas, "FATAL_UNSELLABLE"
    if fut == "INFRASTRUCTURE_UNKNOWN" or bool(row.infrastructure_unknown):
        return -gas, "INFRA_UNKNOWN"
    # current sell only fallback
    cur = str(row.current_sell_status)
    if cur == "SUCCESS" and pd.notna(row.current_weth_out) and str(row.current_weth_out) not in ("", "None"):
        out_w = int(row.current_weth_out) / 1e18
        return out_w - amount - 2 * gas, "CURRENT_EXIT_ONLY"
    return -amount * (1.0 - floor) - 2 * gas, "UNRESOLVED_TREAT_FATAL"


def build_labels(panel: pd.DataFrame | None = None, freeze: dict | None = None) -> pd.DataFrame:
    freeze = freeze or load_freeze()
    if panel is None:
        panel = pd.read_parquet(OUT / "panel" / "rescue_panel.parquet")
    d = panel.copy()

    mfe_l, mae_l, term_l, ttrt_l, nt_l = [], [], [], [], []
    for r in d.itertuples(index=False):
        mfe, mae, term, ttrt, nt = _path_mfe_mae(r)
        mfe_l.append(mfe)
        mae_l.append(mae)
        term_l.append(term)
        ttrt_l.append(ttrt)
        nt_l.append(nt)
    d["mfe"] = mfe_l
    d["mae"] = mae_l
    d["terminal_ret"] = term_l
    d["time_to_right_tail"] = ttrt_l
    d["path_n"] = nt_l

    # Opportunity labels
    d["y_right_tail_25"] = (d["mfe"] >= 0.25).astype(int)
    d["y_right_tail_50"] = (d["mfe"] >= 0.50).astype(int)
    d["y_right_tail_100"] = (d["mfe"] >= 1.00).astype(int)
    d["y_tp_before_sl"] = ((d["mfe"] >= 0.50) & (d["mae"] > -0.35)).astype(int)
    # mae_before_mfe proxy: if |mae| path deeper first — use census orientation: mae more negative early
    d["y_mae_before_mfe"] = (d["mae"].abs() > d["mfe"].clip(lower=0)).astype(int)
    d["y_conditional_return"] = d["terminal_ret"]

    # Risk labels (never used to filter entry sample construction — already on full panel)
    d["y_buy_fail"] = d["buy_status"].isin(["REVERT"]).astype(int)
    d["y_current_unsellable"] = (
        (d["buy_status"] == "SUCCESS") & (~d["current_sell_status"].isin(["SUCCESS"]))
    ).astype(int)
    d["y_future_unsellable"] = d["future_permanent_unsellable"].astype(int)
    d["y_liquidity_collapse"] = ((d["mae"] <= -0.90) | (d["liq_drawdown"] <= -0.95)).astype(int)
    d["y_infrastructure_unknown"] = d["infrastructure_unknown"].astype(int)
    # recovery fraction when buy succeeded
    amount = int(freeze["execution"]["amount_in_wei"])
    rec = []
    for r in d.itertuples(index=False):
        if str(r.buy_status) != "SUCCESS":
            rec.append(0.0)
            continue
        if str(r.future_sell_status) == "SUCCESS" and pd.notna(r.future_weth_out) and str(r.future_weth_out) not in ("", "None"):
            rec.append(float(min(1.0, int(r.future_weth_out) / max(amount, 1))))
        elif bool(r.future_permanent_unsellable):
            rec.append(float(freeze["execution"]["economic_unsellable_recovery_floor"]))
        else:
            rec.append(0.0)
    d["y_recovery_fraction"] = rec

    pnl, bucket = [], []
    for r in d.itertuples(index=False):
        p, b = _wallet_pnl(r, freeze)
        pnl.append(p)
        bucket.append(b)
    d["wallet_pnl_weth"] = pnl
    d["pnl_bucket"] = bucket
    d["label_end_time"] = d["decision_time"] + int(freeze["future_horizon_seconds"])
    d["resolved_trade"] = d["pnl_bucket"].isin(["ROUNDTRIP", "PROXY_PATH", "CURRENT_EXIT_ONLY", "FATAL_UNSELLABLE", "PROXY_FATAL"]).astype(int)
    d["fatal_event"] = d["pnl_bucket"].isin(["FATAL_UNSELLABLE", "PROXY_FATAL"]).astype(int)

    d = update_deployer_pit_with_labels(d)

    path = OUT / "labels" / "dual_labels.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.to_parquet(path, index=False)
    summary = {
        "created_utc": utcnow(),
        "n": int(len(d)),
        "right_tail_25_rate": float(d["y_right_tail_25"].mean()),
        "future_unsellable_rate": float(d["y_future_unsellable"].mean()),
        "buy_fail_rate": float(d["y_buy_fail"].mean()),
        "mean_wallet_pnl_weth": float(d["wallet_pnl_weth"].mean()),
        "pnl_bucket_counts": d["pnl_bucket"].value_counts().to_dict(),
        "note": "Entry sample not filtered by future sellability",
    }
    (OUT / "labels" / "LABEL_META.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"[v3.labels] n={len(d)} rt25={summary['right_tail_25_rate']:.3f} fatalish={d['fatal_event'].mean():.3f}", flush=True)
    return d


def main() -> None:
    build_labels()


if __name__ == "__main__":
    main()
