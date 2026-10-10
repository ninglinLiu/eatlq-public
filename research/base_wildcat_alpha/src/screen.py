from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from hypotheses import HYPOTHESES
from safety import research_entry_guard


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXP = ROOT / "EXPERIMENTS"
BASE_COST_BPS = 60.0


def outcome_column(h: dict) -> str:
    horizon = int(h["horizon"].rstrip("h"))
    obj = h["prediction_object"]
    if obj == "future absolute return":
        return f"fwd_abs_{horizon}"
    if obj in {"future adverse return", "future worst move"}:
        return "future_worst_24" if horizon == 24 else "future_worst_4"
    if obj == "future activity":
        return f"future_activity_{horizon}"
    if obj in {"future inactivity", "future data gap"}:
        return "future_gap_hours"
    if obj == "relative spread":
        return "relative_spread_1"
    return f"fwd_ret_{horizon}"


def clustered_spread(d: pd.DataFrame, low: float, high: float) -> dict:
    z = d[["block_hour", "feature", "outcome", "pool_address"]].dropna().copy()
    z["side"] = np.select([z.feature >= high, z.feature <= low], [1, -1], default=0)
    z = z[z.side != 0]
    top = z[z.side == 1]
    bottom = z[z.side == -1]
    spread = float(top.outcome.mean() - bottom.outcome.mean()) if len(top) and len(bottom) else np.nan
    z["day"] = z.block_hour.dt.floor("D")
    daily = z.groupby("day").apply(
        lambda q: q.loc[q.side == 1, "outcome"].mean() - q.loc[q.side == -1, "outcome"].mean(),
        include_groups=False,
    ).dropna()
    if len(daily) >= 3 and daily.std(ddof=1) > 0:
        t_stat = float(daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))))
        p_value = float(2 * stats.t.sf(abs(t_stat), len(daily) - 1))
    else:
        t_stat, p_value = np.nan, np.nan
    pool_contrib = z.assign(edge=z.side*z.outcome).groupby("pool_address").edge.sum().abs()
    concentration = float(pool_contrib.max() / pool_contrib.sum()) if pool_contrib.sum() else np.nan
    return {
        "n": int(len(z)), "n_top": int(len(top)), "n_bottom": int(len(bottom)),
        "days": int(len(daily)), "spread": spread, "t_day": t_stat, "p_day": p_value,
        "pool_concentration": concentration,
        "tail_p01": float((z.side*z.outcome).quantile(0.01)) if len(z) else np.nan,
    }


def bh_qvalues(pvalues: list[float]) -> list[float]:
    p = np.asarray([v if np.isfinite(v) else 1.0 for v in pvalues])
    order = np.argsort(p)
    ranked = p[order]
    adjusted = np.minimum.accumulate((ranked * len(p) / np.arange(1, len(p)+1))[::-1])[::-1]
    out = np.empty_like(adjusted)
    out[order] = np.clip(adjusted, 0, 1)
    return out.tolist()


def screen_one(panel: pd.DataFrame, h: dict) -> dict:
    feature, outcome = h["feature"], outcome_column(h)
    if feature not in panel or outcome not in panel:
        return {"alpha_id":h["alpha_id"], "family":h["family"], "feature":feature, "outcome":outcome, "status":"DATA_INSUFFICIENT", "reason":"field unavailable", "n":0, "p_day":1.0}
    d = panel[["block_hour","pool_address",feature,outcome]].rename(columns={feature:"feature", outcome:"outcome"}).replace([np.inf,-np.inf],np.nan).dropna()
    d["feature"] *= float(h["direction"])
    # Fixed corruption guard, not a fitted threshold: hourly economic outcomes
    # above 100x are quarantined rather than allowed to dominate a mean.
    if "ret" in outcome or "worst" in outcome or "spread" in outcome:
        d = d[d.outcome.abs() <= np.log(100)]
    if len(d) < 200 or d.feature.nunique() < 5:
        return {"alpha_id":h["alpha_id"], "family":h["family"], "feature":feature, "outcome":outcome, "status":"DATA_INSUFFICIENT", "reason":"<200 usable rows or <5 feature values", "n":int(len(d)), "p_day":1.0}
    split_time = d.block_hour.quantile(0.65)
    train, test = d[d.block_hour <= split_time], d[d.block_hour > split_time]
    low, high = train.feature.quantile([0.2,0.8]).tolist()
    train_m = clustered_spread(train, low, high)
    test_m = clustered_spread(test, low, high)
    full_m = clustered_spread(d, low, high)
    same_sign = np.sign(train_m["spread"]) == np.sign(test_m["spread"])
    if test_m["n_top"] < 30 or test_m["n_bottom"] < 30:
        status, reason = "DATA_INSUFFICIENT", "thin out-of-time tails"
    elif same_sign and test_m["spread"] > 0 and test_m["p_day"] < 0.05:
        status, reason = "GROSS_STRUCTURE", "predefined sign survives temporal split"
    elif test_m["spread"] < 0 and test_m["p_day"] < 0.10 and test_m["n"] >= 500:
        status, reason = "KILLED", "predefined sign contradicted out of time"
    else:
        status, reason = "DISCOVERY_ONLY", "weak, unstable, or not significant out of time"
    economic = outcome.startswith("fwd_ret") or outcome.startswith("future_worst") or outcome.startswith("relative")
    gross_bps = test_m["spread"] * 10_000 if economic and np.isfinite(test_m["spread"]) else np.nan
    net_bps = gross_bps - 2 * BASE_COST_BPS if np.isfinite(gross_bps) else np.nan
    return {
        "alpha_id":h["alpha_id"], "family":h["family"], "feature":feature, "outcome":outcome,
        "status":status, "reason":reason, "n":int(len(d)), "split_time":str(split_time),
        "threshold_low":float(low), "threshold_high":float(high),
        "train_spread":train_m["spread"], "test_spread":test_m["spread"], "full_spread":full_m["spread"],
        "gross_test_bps":gross_bps, "net_test_bps_at_60bps_each_leg":net_bps,
        "t_day":test_m["t_day"], "p_day":test_m["p_day"], "days_test":test_m["days"],
        "pool_concentration":test_m["pool_concentration"], "tail_p01":test_m["tail_p01"],
        "same_sign_train_test":bool(same_sign), "economic_return_metric":economic,
    }


def run_screen() -> pd.DataFrame:
    research_entry_guard()
    panel = pd.read_parquet(DATA / "feature_panel.parquet")
    panel["block_hour"] = pd.to_datetime(panel.block_hour, utc=True)
    results = [screen_one(panel, h) for h in HYPOTHESES]
    qvals = bh_qvalues([r.get("p_day",1.0) for r in results])
    for r, q in zip(results, qvals):
        r["q_bh_48"] = q
    out = pd.DataFrame(results)
    out.to_csv(ROOT / "RAPID_SCREEN_RESULTS.csv", index=False)

    atlas = pd.read_csv(ROOT / "ALPHA_ATLAS.csv")
    metrics = out.drop(columns=[c for c in ("family", "feature") if c in out.columns])
    merged = atlas.drop(columns=["status","sample_size","gross_result","cost_adjusted_result","capacity","concentration","tail_risk","next_action"]).merge(metrics, on="alpha_id", how="left")
    merged["sample_size"] = merged.n.fillna(0).astype(int)
    merged["gross_result"] = merged.apply(lambda r: f"OOT spread={r.test_spread:.6g}; q48={r.q_bh_48:.3g}" if pd.notna(r.test_spread) else r.reason, axis=1)
    merged["cost_adjusted_result"] = merged.apply(lambda r: f"{r.net_test_bps_at_60bps_each_leg:.1f} bps @60bps/leg" if pd.notna(r.net_test_bps_at_60bps_each_leg) else "not a directly monetized return target", axis=1)
    merged["capacity"] = "$1,000 UNVERIFIED: no historical executable USD quote/depth snapshots"
    merged["concentration"] = merged.pool_concentration.map(lambda v: f"top-pool abs contribution={v:.1%}" if pd.notna(v) else "unknown")
    merged["tail_risk"] = merged.tail_p01.map(lambda v: f"selected P01={v:.6g}" if pd.notna(v) else "unknown")
    merged["next_action"] = merged.status.map({"GROSS_STRUCTURE":"Kill Panel", "DISCOVERY_ONLY":"retain as spark; acquire stronger data", "KILLED":"archive unless redefined", "DATA_INSUFFICIENT":"acquire missing coverage"}).fillna("audit")
    desired = list(atlas.columns)
    extra = [c for c in merged.columns if c not in desired]
    merged[desired + extra].to_csv(ROOT / "ALPHA_ATLAS.csv", index=False)

    EXP.mkdir(parents=True, exist_ok=True)
    for h, r in zip(HYPOTHESES, results):
        d = EXP / h["alpha_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(h, indent=2), encoding="utf-8")
        (d / "result.json").write_text(json.dumps(r, indent=2, default=_json_default), encoding="utf-8")
        (d / "README.md").write_text(
            f"# {h['alpha_id']}\n\n{h['hypothesis']}\n\nReproduce from workspace root:\n\n```bash\npython3 src/screen.py\n```\n\nThe shared immutable experiment code is `src/screen.py`; this directory freezes configuration and result.\n",
            encoding="utf-8",
        )
    return out


def _json_default(value):
    if isinstance(value, (np.integer,)): return int(value)
    if isinstance(value, (np.floating,)): return float(value)
    if isinstance(value, (np.bool_,)): return bool(value)
    return str(value)


if __name__ == "__main__":
    run_screen()
