#!/usr/bin/env python3
"""Label-design freeze + atomic/tick path labels for targets A/B/C."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import LN1P, OUT, load_freeze, save_freeze, utcnow  # noqa: E402
from src.build_panel import parse_i256  # noqa: E402

PANEL = OUT / "panel" / "greenfield_panel_15-60m.parquet"
SWAPS = OUT / "panel" / "early_swaps.parquet"
LABEL_DIR = OUT / "labels"


def path_returns(entry_tick: float, fwd_ticks: np.ndarray) -> np.ndarray:
    return (fwd_ticks - entry_tick) * LN1P


def _decode_swaps(swaps: pd.DataFrame) -> pd.DataFrame:
    s = swaps.copy()
    if "tick_i" not in s.columns:
        s["tick_i"] = [parse_i256(x) for x in s["tick"]]
        s["liq_i"] = [parse_i256(x) for x in s["liquidity"]]
    return s


def first_barrier_event(rets: np.ndarray, tp: float, sl: float, fatal_mask: np.ndarray | None) -> str:
    for i, r in enumerate(rets):
        if fatal_mask is not None and i < len(fatal_mask) and fatal_mask[i]:
            return "FATAL_FIRST"
        if r >= tp:
            return "TP_FIRST"
        if r <= -sl:
            return "SL_FIRST"
    return "TIMEOUT"


def attach_forward_paths(panel: pd.DataFrame, swaps: pd.DataFrame) -> pd.DataFrame:
    swaps = _decode_swaps(swaps)
    grouped = {p: g.sort_values("ts") for p, g in swaps.groupby("pool", sort=False)}
    horizons = [1800, 3600, 7200]
    records = []
    for row in panel.itertuples(index=False):
        g = grouped.get(row.pool)
        if g is None:
            continue
        ts = g["ts"].to_numpy(dtype=np.int64)
        ticks = g["tick_i"].to_numpy(dtype=np.float64)
        liqs = g["liq_i"].to_numpy(dtype=np.float64)
        dt = int(row.decision_time)
        i0 = np.searchsorted(ts, dt, side="right")
        entry_tick = float(row.entry_tick)
        entry_liq = float(row.entry_liq)
        out = {"signal_id": row.signal_id}
        for H in horizons:
            i1 = np.searchsorted(ts, dt + H, side="right")
            if i1 <= i0:
                out[f"ret_h{H}"] = np.nan
                out[f"mfe_h{H}"] = np.nan
                out[f"mae_h{H}"] = np.nan
                out[f"path_n_h{H}"] = 0
                out[f"liq_collapse_h{H}"] = 0
                out[f"event_TB_A_h{H}"] = "TIMEOUT"
                out[f"event_TB_B_h{H}"] = "TIMEOUT"
                continue
            ft = ticks[i0:i1]
            fl = liqs[i0:i1]
            valid = ~np.isnan(ft)
            ft = ft[valid]
            fl = fl[valid] if len(fl) == len(valid) else fl[: len(ft)]
            if len(ft) == 0:
                out[f"ret_h{H}"] = np.nan
                out[f"mfe_h{H}"] = np.nan
                out[f"mae_h{H}"] = np.nan
                out[f"path_n_h{H}"] = 0
                out[f"liq_collapse_h{H}"] = 0
                out[f"event_TB_A_h{H}"] = "TIMEOUT"
                out[f"event_TB_B_h{H}"] = "TIMEOUT"
                continue
            rets = path_returns(entry_tick, ft)
            mfe = float(np.max(rets))
            mae = float(np.min(rets))
            # fatal proxy: liquidity collapses >95% and never recovers before end
            fatal_mask = (fl < max(entry_liq * 0.05, 1.0)) if len(fl) == len(rets) else np.zeros(len(rets), dtype=bool)
            # also mark terminal if last liq collapsed and ret <= -0.9
            atomic_fail_proxy = bool(len(fl) and fl[-1] < max(entry_liq * 0.05, 1.0) and rets[-1] <= -0.90)
            out[f"ret_h{H}"] = float(rets[-1])
            out[f"mfe_h{H}"] = mfe
            out[f"mae_h{H}"] = mae
            out[f"path_n_h{H}"] = int(len(rets))
            out[f"liq_collapse_h{H}"] = int(atomic_fail_proxy)
            out[f"mae_before_mfe_h{H}"] = int(np.argmin(rets) < np.argmax(rets))
            out[f"event_TB_A_h{H}"] = first_barrier_event(rets, 0.50, 0.35, fatal_mask)
            out[f"event_TB_B_h{H}"] = first_barrier_event(rets, 1.00, 0.50, fatal_mask)
            # expected shortfall proxy: mean of worst 5% path points
            q = np.quantile(rets, 0.05) if len(rets) >= 5 else float(np.min(rets))
            out[f"es_h{H}"] = float(rets[rets <= q].mean()) if len(rets) else np.nan
            # time to sellable: first forward swap after decision where sell side exists — approx first swap
            out[f"time_to_sellable_h{H}"] = int(ts[i0] - dt) if i0 < len(ts) else H
        records.append(out)
    lab = pd.DataFrame(records)
    return panel.merge(lab, on="signal_id", how="left")


def freeze_label_design(df: pd.DataFrame) -> dict:
    freeze = load_freeze()
    ld = df[df["split_role"] == "LABEL_DESIGN"].copy()
    report = {"n_label_design": int(len(ld)), "horizon_scores": {}, "barrier_scores": {}}
    if ld.empty:
        raise RuntimeError("LABEL_DESIGN empty")

    best_h, best_h_score = None, -1e9
    for H in freeze["label_design_candidates"]["horizons_seconds"]:
        col = f"ret_h{H}"
        sub = ld.dropna(subset=[col])
        if len(sub) < 50:
            report["horizon_scores"][str(H)] = {"n": int(len(sub)), "score": None, "reject": "insufficient_n"}
            continue
        # cross-sectional IC: correlate early_ret rank feature with future return rank within decision_time
        ics = []
        for _, g in sub.groupby("decision_time"):
            if len(g) < 5:
                continue
            r1 = g["early_ret"].rank(pct=True)
            r2 = g[col].rank(pct=True)
            if r1.std() == 0 or r2.std() == 0:
                continue
            ics.append(float(r1.corr(r2)))
        mean_ic = float(np.mean(ics)) if ics else 0.0
        # prefer horizons with non-degenerate barrier event mix later
        score = mean_ic + 0.01 * np.log(len(sub))
        report["horizon_scores"][str(H)] = {
            "n": int(len(sub)),
            "mean_xs_ic_price_vs_fwd": mean_ic,
            "n_xs_slices": int(len(ics)),
            "score": score,
        }
        if score > best_h_score:
            best_h_score, best_h = score, H

    if best_h is None:
        best_h = 3600  # fallback freeze
        report["horizon_fallback"] = True

    best_b, best_b_score = None, -1e9
    for bname, bdef in freeze["label_design_candidates"]["barriers"].items():
        col = f"event_{bname}_h{best_h}"
        vc = ld[col].value_counts(normalize=True).to_dict() if col in ld.columns else {}
        # require all four events present or at least TP/SL/TIMEOUT with FATAL optional
        n_tp = float(vc.get("TP_FIRST", 0))
        n_sl = float(vc.get("SL_FIRST", 0))
        n_to = float(vc.get("TIMEOUT", 0))
        n_fa = float(vc.get("FATAL_FIRST", 0))
        # score: entropy-like diversity, penalize >90% TIMEOUT
        probs = np.array([n_tp, n_sl, n_to, n_fa])
        probs = probs[probs > 0]
        ent = float(-(probs * np.log(probs + 1e-12)).sum()) if len(probs) else 0.0
        score = ent - (2.0 if n_to > 0.9 else 0.0)
        report["barrier_scores"][bname] = {"dist": vc, "score": score, "params": bdef}
        if score > best_b_score:
            best_b_score, best_b = score, bname

    if best_b is None:
        best_b = "TB_A"

    freeze["status"] = "FROZEN_AFTER_LABEL_DESIGN"
    freeze["primary_horizon_seconds"] = int(best_h)
    freeze["primary_barrier"] = best_b
    freeze["primary_barrier_params"] = freeze["label_design_candidates"]["barriers"][best_b]
    freeze["label_design_report"] = report
    freeze["frozen_utc"] = utcnow()
    save_freeze(freeze)
    return freeze


def finalize_labels(df: pd.DataFrame, freeze: dict) -> pd.DataFrame:
    H = int(freeze["primary_horizon_seconds"])
    B = freeze["primary_barrier"]
    tp = float(freeze["primary_barrier_params"]["tp"])
    sl = float(freeze["primary_barrier_params"]["sl"])
    out = df.copy()
    out["label_end_time"] = out["decision_time"] + H
    out["atomic_net_return"] = out[f"ret_h{H}"]
    # economic unsellable override
    collapse = out[f"liq_collapse_h{H}"].fillna(0).astype(int)
    out.loc[collapse == 1, "atomic_net_return"] = -1.0
    out["xs_return_pctile"] = out.groupby("decision_time")["atomic_net_return"].rank(pct=True)
    out["barrier_event"] = out[f"event_{B}_h{H}"]
    out.loc[collapse == 1, "barrier_event"] = "FATAL_FIRST"
    out["atomic_sell_failure"] = collapse
    out["mae_before_mfe"] = out.get(f"mae_before_mfe_h{H}", 0)
    out["expected_shortfall"] = out[f"es_h{H}"]
    out["time_to_sellable"] = out[f"time_to_sellable_h{H}"]
    out["primary_tp"] = tp
    out["primary_sl"] = sl
    out["primary_H"] = H
    return out


def sample_atomic_calibration(df: pd.DataFrame, max_calls: int = 40) -> dict:
    """Optional RPC calibration smoke — does not change frozen labels if RPC fails."""
    try:
        from src.atomic_sim import ArchiveSim
    except Exception as e:
        return {"status": "SKIP", "reason": str(e)}
    sim = ArchiveSim()
    # stratified sample from LABEL_DESIGN
    ld = df[df["split_role"] == "LABEL_DESIGN"].dropna(subset=["decision_block", "alt", "fee"])
    if ld.empty:
        return {"status": "SKIP", "reason": "no_ld"}
    sample = ld.sample(n=min(max_calls, len(ld)), random_state=0)
    rows = []
    for r in sample.itertuples(index=False):
        # probe tiny amount
        try:
            res = sim.atomic_sell(str(r.alt), int(r.fee), amount=10**15, block=int(r.decision_block))
            rows.append({"signal_id": r.signal_id, **res})
        except Exception as e:
            rows.append({"signal_id": r.signal_id, "simulation_status": "INFRASTRUCTURE_UNKNOWN", "error": str(e)[:120]})
    cal = pd.DataFrame(rows)
    path = LABEL_DIR / "atomic_calibration_sample.csv"
    cal.to_csv(path, index=False)
    return {
        "status": "OK",
        "n": int(len(cal)),
        "status_counts": cal["simulation_status"].value_counts().to_dict() if len(cal) else {},
        "path": str(path),
        "rpc_calls": sim.calls,
    }


def main():
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(PANEL)
    swaps = pd.read_parquet(SWAPS)
    print(f"[labels] panel={len(panel)} swaps={len(swaps)}", flush=True)
    labeled = attach_forward_paths(panel, swaps)
    freeze = freeze_label_design(labeled)
    print(f"[labels] frozen H={freeze['primary_horizon_seconds']} barrier={freeze['primary_barrier']}", flush=True)
    final = finalize_labels(labeled, freeze)
    # xs pctile only among labeled
    final.to_parquet(LABEL_DIR / "labels_full.parquet", index=False)
    # seal holdout labels path but models must not use for tuning — still write for completeness
    hold = final[final["split_role"] == "SEALED_HOLDOUT"]
    hold.to_parquet(LABEL_DIR / "sealed_holdout_labels.parquet", index=False)
    cal = sample_atomic_calibration(final, max_calls=30)
    summary = {
        "created_utc": utcnow(),
        "n_rows": int(len(final)),
        "primary_horizon_seconds": freeze["primary_horizon_seconds"],
        "primary_barrier": freeze["primary_barrier"],
        "barrier_dist_dev": final.loc[final["split_role"] == "DEV", "barrier_event"].value_counts().to_dict(),
        "mean_ret_dev": float(final.loc[final["split_role"] == "DEV", "atomic_net_return"].mean()),
        "atomic_calibration": cal,
        "label_note": "Primary path returns from PIT forward ticks; FATAL via liquidity-collapse proxy; atomic RPC sample is calibration-only",
    }
    (LABEL_DIR / "label_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
