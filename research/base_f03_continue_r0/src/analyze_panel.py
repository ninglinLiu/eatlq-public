"""Panel-only F03 decomposition. Does not modify wildcat files."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
WILDCAT = Path("/root/base_wildcat_alpha")
OUT = ROOT / "out"
COST_BPS = 120.0
ZERO_SPREAD = 20.0
R2_ZERO = 0.01


def _jsonable(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    if pd.isna(v):
        return None
    return v


def clustered_spread(d: pd.DataFrame, low: float, high: float) -> dict:
    z = d[["block_hour", "feature", "outcome", "pool_address"]].dropna().copy()
    z["side"] = np.select([z.feature >= high, z.feature <= low], [1, -1], default=0)
    z = z[z.side != 0]
    top = z[z.side == 1]
    bottom = z[z.side == -1]
    spread = float(top.outcome.mean() - bottom.outcome.mean()) if len(top) and len(bottom) else float("nan")
    z["day"] = z.block_hour.dt.floor("D")
    daily = z.groupby("day").apply(
        lambda q: q.loc[q.side == 1, "outcome"].mean() - q.loc[q.side == -1, "outcome"].mean(),
        include_groups=False,
    ).dropna()
    if len(daily) >= 3 and daily.std(ddof=1) > 0:
        t_stat = float(daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))))
        p_value = float(2 * stats.t.sf(abs(t_stat), len(daily) - 1))
    else:
        t_stat, p_value = float("nan"), float("nan")

    def side_stats(q: pd.DataFrame) -> dict:
        if not len(q):
            return {"n": 0}
        o = q.outcome
        return {
            "n": int(len(q)),
            "mean_bps": float(o.mean() * 10_000),
            "median_bps": float(o.median() * 10_000),
            "win": float((o > 0).mean()),
            "p10_bps": float(o.quantile(0.10) * 10_000),
            "p90_bps": float(o.quantile(0.90) * 10_000),
            "std_bps": float(o.std() * 10_000) if len(q) > 1 else None,
        }

    return {
        "n": int(len(z)),
        "n_top": int(len(top)),
        "n_bottom": int(len(bottom)),
        "days": int(len(daily)),
        "spread": spread,
        "spread_bps": spread * 10_000 if np.isfinite(spread) else None,
        "net_bps_120": (spread * 10_000 - COST_BPS) if np.isfinite(spread) else None,
        "t_day": t_stat,
        "p_day": p_value,
        "high": side_stats(top),
        "low": side_stats(bottom),
        "threshold_low": float(low),
        "threshold_high": float(high),
    }


def r2_fit(y: np.ndarray, X: np.ndarray) -> dict:
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X = y[mask], X[mask]
    if len(y) < 20:
        return {"n": int(len(y)), "r2": None, "rmse": None}
    X = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(ss_res / len(y)))
    return {"n": int(len(y)), "r2": r2, "rmse": rmse, "coef": [float(c) for c in coef]}


def predict_r2(y: np.ndarray, X: np.ndarray, coef: list[float]) -> dict:
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X = y[mask], X[mask]
    if len(y) < 10:
        return {"n": int(len(y)), "r2": None, "rmse": None}
    X = np.column_stack([np.ones(len(y)), X])
    pred = X @ np.asarray(coef)
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(ss_res / len(y)))
    return {"n": int(len(y)), "r2": r2, "rmse": rmse}


def main() -> None:
    research_entry_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    x = pd.read_parquet(WILDCAT / "data/feature_panel.parquet")
    x["block_hour"] = pd.to_datetime(x.block_hour, utc=True)
    x["sign_ret"] = np.sign(x.ret_0)
    x["abs_ret"] = x.ret_0.abs()
    x["abs_flow"] = x.flow_imbalance.abs()
    x["aligned"] = x.sign_ret * x.flow_imbalance
    x["agree"] = x.sign_ret * np.sign(x.flow_imbalance)

    d = x[
        [
            "block_hour",
            "pool_address",
            "ret_0",
            "sign_ret",
            "abs_ret",
            "flow_imbalance",
            "abs_flow",
            "aligned",
            "agree",
            "direction_persistence",
            "fwd_ret_4",
            "delay1_ret_4",
            "delay2_ret_4",
            "delay4_ret_4",
            "swap_count",
            "unique_senders",
            "token0_symbol",
            "token1_symbol",
            "has_weth_leg",
            "fee_bps",
        ]
    ].copy()
    d = d.replace([np.inf, -np.inf], np.nan)
    base = d.dropna(subset=["direction_persistence", "fwd_ret_4"]).copy()
    base = base[base.fwd_ret_4.abs() <= np.log(100)]
    cutoff = base.block_hour.quantile(0.40)
    cal, rep = base[base.block_hour <= cutoff], base[base.block_hour > cutoff]

    features = {
        "f03_abs": "direction_persistence",
        "aligned_no_abs": "aligned",
        "pure_momentum_sign": "sign_ret",
        "pure_momentum_ret": "ret_0",
        "abs_flow_only": "abs_flow",
        "signed_flow_f01like": "flow_imbalance",
        "abs_ret_only": "abs_ret",
    }

    feature_table = {}
    for name, col in features.items():
        work = base.dropna(subset=[col, "fwd_ret_4"]).copy()
        work = work[work.fwd_ret_4.abs() <= np.log(100)]
        c_cut = work.block_hour.quantile(0.40)
        c_cal, c_rep = work[work.block_hour <= c_cut], work[work.block_hour > c_cut]
        low, high = c_cal[col].quantile([0.2, 0.8]).tolist()
        tmp = c_rep.rename(columns={col: "feature", "fwd_ret_4": "outcome"})
        feature_table[name] = clustered_spread(tmp, low, high)
        feature_table[name]["calibration_cutoff"] = str(c_cut)

    # Residual: among already-up hours, does |flow| still separate?
    residual_sides = {}
    for sign_name, mask in (("up_hours", rep.sign_ret > 0), ("down_hours", rep.sign_ret < 0)):
        sub = rep.loc[mask].dropna(subset=["abs_flow", "fwd_ret_4"])
        cal_sub = cal.loc[cal.sign_ret == (1 if sign_name == "up_hours" else -1)].dropna(subset=["abs_flow"])
        if len(sub) < 50 or len(cal_sub) < 50:
            residual_sides[sign_name] = {"status": "UNDERPOWERED", "n": int(len(sub))}
            continue
        low, high = cal_sub.abs_flow.quantile([0.2, 0.8]).tolist()
        tmp = sub.rename(columns={"abs_flow": "feature", "fwd_ret_4": "outcome"})
        residual_sides[sign_name] = clustered_spread(tmp, low, high)

    # Same, but control |ret_0| by using residual of fwd ~ ret_0 within sign
    residual_after_ret = {}
    for sign_name, sval in (("up_hours", 1.0), ("down_hours", -1.0)):
        cal_s = cal[cal.sign_ret == sval].dropna(subset=["abs_flow", "ret_0", "fwd_ret_4"])
        rep_s = rep[rep.sign_ret == sval].dropna(subset=["abs_flow", "ret_0", "fwd_ret_4"])
        if len(cal_s) < 50 or len(rep_s) < 50:
            residual_after_ret[sign_name] = {"status": "UNDERPOWERED"}
            continue
        # residualize outcome on ret_0 using calibration coefficients
        fit = r2_fit(cal_s.fwd_ret_4.to_numpy(), cal_s[["ret_0"]].to_numpy())
        coef = np.asarray(fit["coef"])
        Xr = np.column_stack([np.ones(len(rep_s)), rep_s.ret_0.to_numpy()])
        resid = rep_s.fwd_ret_4.to_numpy() - Xr @ coef
        tmp = pd.DataFrame(
            {
                "block_hour": rep_s.block_hour.values,
                "pool_address": rep_s.pool_address.values,
                "feature": rep_s.abs_flow.values,
                "outcome": resid,
            }
        )
        low, high = cal_s.abs_flow.quantile([0.2, 0.8]).tolist()
        residual_after_ret[sign_name] = clustered_spread(tmp, low, high)
        residual_after_ret[sign_name]["note"] = "outcome is fwd_ret_4 residual after linear ret_0 fit on calibration"

    # Linear R2 increment on last 30% of replay
    hold_cut = rep.block_hour.quantile(0.70)
    train, hold = rep[rep.block_hour <= hold_cut], rep[rep.block_hour > hold_cut]
    y_tr, y_ho = train.fwd_ret_4.to_numpy(), hold.fwd_ret_4.to_numpy()
    X0_tr = train[["sign_ret", "abs_ret"]].to_numpy()
    X1_tr = train[["sign_ret", "abs_ret", "abs_flow"]].to_numpy()
    X0_ho = hold[["sign_ret", "abs_ret"]].to_numpy()
    X1_ho = hold[["sign_ret", "abs_ret", "abs_flow"]].to_numpy()
    m0_tr = r2_fit(y_tr, X0_tr)
    m1_tr = r2_fit(y_tr, X1_tr)
    m0_ho = predict_r2(y_ho, X0_ho, m0_tr["coef"])
    m1_ho = predict_r2(y_ho, X1_ho, m1_tr["coef"])
    # contemporaneous leak check: same models but y = ret_0 (should be huge for sign/abs_ret)
    leak0 = r2_fit(train.ret_0.to_numpy(), X0_tr)
    leak1 = r2_fit(train.ret_0.to_numpy(), X1_tr)

    r2_block = {
        "holdout_cut": str(hold_cut),
        "train_n": int(len(train)),
        "holdout_n": int(len(hold)),
        "m0_train": m0_tr,
        "m1_train": m1_tr,
        "m0_holdout": m0_ho,
        "m1_holdout": m1_ho,
        "holdout_r2_increment": (
            None
            if m0_ho["r2"] is None or m1_ho["r2"] is None
            else float(m1_ho["r2"] - m0_ho["r2"])
        ),
        "leak_r2_m0_on_ret0": leak0,
        "leak_r2_m1_on_ret0": leak1,
        "zero_threshold": R2_ZERO,
    }

    # Agreement inside F03 tails
    low_f03, high_f03 = cal.direction_persistence.quantile([0.2, 0.8]).tolist()
    high_rows = rep[rep.direction_persistence >= high_f03]
    low_rows = rep[rep.direction_persistence <= low_f03]

    def agree_break(q: pd.DataFrame) -> dict:
        a = q[q.agree > 0]
        b = q[q.agree < 0]
        z = q[q.agree == 0]
        out = {
            "n": int(len(q)),
            "n_agree": int(len(a)),
            "n_disagree": int(len(b)),
            "n_zero": int(len(z)),
            "agree_share": float((q.agree > 0).mean()) if len(q) else None,
            "disagree_share": float((q.agree < 0).mean()) if len(q) else None,
        }
        for label, part in (("agree", a), ("disagree", b)):
            if not len(part):
                out[label] = {"n": 0}
                continue
            o = part.fwd_ret_4
            out[label] = {
                "n": int(len(part)),
                "mean_bps": float(o.mean() * 10_000),
                "median_bps": float(o.median() * 10_000),
                "win": float((o > 0).mean()),
            }
        if len(a) and len(b):
            out["agree_minus_disagree_bps"] = float((a.fwd_ret_4.mean() - b.fwd_ret_4.mean()) * 10_000)
        return out

    agreement = {
        "f03_high": agree_break(high_rows),
        "f03_low": agree_break(low_rows),
        "all_replay": agree_break(rep),
    }

    # Delay stress: F03 vs pure sign momentum, same thresholds from calibration
    delay = {}
    for feat_name, col in (("f03_abs", "direction_persistence"), ("pure_momentum_sign", "sign_ret")):
        low, high = cal[col].quantile([0.2, 0.8]).tolist()
        delay[feat_name] = {}
        for h, ocol in (("0", "fwd_ret_4"), ("1", "delay1_ret_4"), ("2", "delay2_ret_4"), ("4", "delay4_ret_4")):
            tmp = rep.dropna(subset=[col, ocol]).rename(columns={col: "feature", ocol: "outcome"})
            tmp = tmp[tmp.outcome.abs() <= np.log(100)]
            delay[feat_name][h] = clustered_spread(tmp, low, high)["spread_bps"]

    # Concurrency / overlap
    tagged = rep.copy()
    tagged["side"] = np.select(
        [tagged.direction_persistence >= high_f03, tagged.direction_persistence <= low_f03],
        ["HIGH", "LOW"],
        default="MID",
    )
    sig = tagged[tagged.side != "MID"]
    per_hour = sig.groupby("block_hour").size()
    high_hour = sig[sig.side == "HIGH"].groupby("block_hour").size()
    aero = sig.assign(
        aero=lambda q: q.token0_symbol.fillna("").str.upper().eq("AERO")
        | q.token1_symbol.fillna("").str.upper().eq("AERO")
    )
    pair = sig.assign(pair=sig.token0_symbol.fillna("?") + "/" + sig.token1_symbol.fillna("?"))
    top_pairs = pair.groupby("pair").size().sort_values(ascending=False).head(10)

    concurrency = {
        "replay_signal_rows": int(len(sig)),
        "replay_hours_with_any_signal": int(sig.block_hour.nunique()),
        "mean_signals_per_active_hour": float(per_hour.mean()) if len(per_hour) else None,
        "p50_signals_per_active_hour": float(per_hour.median()) if len(per_hour) else None,
        "p90_signals_per_active_hour": float(per_hour.quantile(0.90)) if len(per_hour) else None,
        "max_signals_per_hour": int(per_hour.max()) if len(per_hour) else 0,
        "mean_high_per_active_hour": float(high_hour.mean()) if len(high_hour) else None,
        "share_hours_with_ge_2_signals": float((per_hour >= 2).mean()) if len(per_hour) else None,
        "aero_share_of_signals": float(aero.aero.mean()) if len(aero) else None,
        "top_pairs": {str(k): int(v) for k, v in top_pairs.items()},
    }

    # Monthly F03 vs momentum sign (HIGH-only means)
    monthly = []
    for month, part in tagged.groupby(tagged.block_hour.dt.to_period("M")):
        row = {"month": str(month)}
        for side in ("HIGH", "LOW"):
            q = part[part.side == side]
            row[f"f03_{side}_n"] = int(len(q))
            row[f"f03_{side}_mean_bps"] = float(q.fwd_ret_4.mean() * 10_000) if len(q) else None
        mom_hi = part[part.sign_ret > 0]
        mom_lo = part[part.sign_ret < 0]
        row["mom_up_n"] = int(len(mom_hi))
        row["mom_up_mean_bps"] = float(mom_hi.fwd_ret_4.mean() * 10_000) if len(mom_hi) else None
        row["mom_down_n"] = int(len(mom_lo))
        row["mom_down_mean_bps"] = float(mom_lo.fwd_ret_4.mean() * 10_000) if len(mom_lo) else None
        monthly.append(row)

    # Verdicts (frozen thresholds)
    f03_bps = feature_table["f03_abs"]["spread_bps"]
    mom_bps = feature_table["pure_momentum_sign"]["spread_bps"]
    aligned_bps = feature_table["aligned_no_abs"]["spread_bps"]
    resid_up = residual_sides.get("up_hours", {}).get("spread_bps")
    resid_dn = residual_sides.get("down_hours", {}).get("spread_bps")
    r2_inc = r2_block["holdout_r2_increment"]
    disagree_high = agreement["f03_high"].get("disagree_share") or 0.0

    flow_adds = (
        (resid_up is not None and abs(resid_up) >= ZERO_SPREAD)
        or (resid_dn is not None and abs(resid_dn) >= ZERO_SPREAD)
        or (r2_inc is not None and r2_inc >= R2_ZERO)
    )
    aligned_false = (
        aligned_bps is not None
        and f03_bps is not None
        and aligned_bps <= f03_bps + ZERO_SPREAD
        and disagree_high >= 0.15
    )
    if r2_inc is None and resid_up is None:
        primary = "DATA_INSUFFICIENT"
    elif not flow_adds:
        primary = "MOMENTUM_ONLY"
    else:
        primary = "FLOW_ADDS_IN_TICK_SPACE"

    result = {
        "cutoff": str(cutoff),
        "n_base": int(len(base)),
        "n_cal": int(len(cal)),
        "n_replay": int(len(rep)),
        "feature_spreads": feature_table,
        "residual_abs_flow_within_sign": residual_sides,
        "residual_abs_flow_after_ret0": residual_after_ret,
        "r2": r2_block,
        "agreement": agreement,
        "delay_stress_bps": delay,
        "concurrency": concurrency,
        "monthly": monthly,
        "f03_thresholds": {"low": low_f03, "high": high_f03},
        "verdict": {
            "primary": primary,
            "aligned_story_false": bool(aligned_false),
            "flow_adds_in_tick_space": bool(flow_adds),
            "f03_spread_bps": f03_bps,
            "momentum_sign_spread_bps": mom_bps,
            "aligned_spread_bps": aligned_bps,
            "f03_minus_momentum_bps": (
                None if f03_bps is None or mom_bps is None else float(f03_bps - mom_bps)
            ),
            "holdout_r2_increment": r2_inc,
            "residual_up_spread_bps": resid_up,
            "residual_down_spread_bps": resid_dn,
            "f03_high_disagree_share": disagree_high,
            "usd_capacity": "UNVERIFIED",
            "highest_status": "SHADOW_CANDIDATE",
        },
    }
    payload = json.loads(json.dumps(result, default=_jsonable))
    (OUT / "r0_panel_decomp.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["verdict"], indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
