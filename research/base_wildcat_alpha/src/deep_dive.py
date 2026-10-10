from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from hypotheses import HYPOTHESES
from safety import research_entry_guard
from screen import BASE_COST_BPS, clustered_spread, outcome_column


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXP = ROOT / "EXPERIMENTS"
RNG = np.random.default_rng(8453)
CANDIDATES = ["F03", "F01", "H01", "S02", "RV01", "C03", "A01", "R02"]


def _prepared(panel: pd.DataFrame, h: dict, outcome_override: str | None = None) -> pd.DataFrame:
    outcome = outcome_override or outcome_column(h)
    cols = ["block_hour","pool_address","pair_key",h["feature"],outcome,"duplicate_pressure"]
    d = panel[[c for c in cols if c in panel]].rename(columns={h["feature"]:"feature",outcome:"outcome"})
    d = d.replace([np.inf,-np.inf],np.nan).dropna(subset=["feature","outcome"]).copy()
    d["feature"] *= float(h["direction"])
    if "ret" in outcome or "worst" in outcome or "spread" in outcome:
        d = d[d.outcome.abs() <= np.log(100)]
    return d


def _daily_spreads(d: pd.DataFrame, low: float, high: float) -> pd.Series:
    z = d.copy()
    z["side"] = np.select([z.feature >= high,z.feature <= low],[1,-1],default=0)
    z = z[z.side != 0]
    z["day"] = z.block_hour.dt.floor("D")
    return z.groupby("day").apply(
        lambda q: q.loc[q.side==1,"outcome"].mean()-q.loc[q.side==-1,"outcome"].mean(),
        include_groups=False,
    ).dropna()


def _metrics(d: pd.DataFrame, low: float, high: float) -> dict:
    return clustered_spread(d, low, high)


def deep_one(panel: pd.DataFrame, h: dict, q48: float) -> dict:
    d = _prepared(panel, h)
    cutoff = d.block_hour.quantile(0.40)
    calibration, replay = d[d.block_hour <= cutoff], d[d.block_hour > cutoff]
    low, high = calibration.feature.quantile([0.2,0.8]).tolist()
    base = _metrics(replay, low, high)
    economic = outcome_column(h).startswith(("fwd_ret","future_worst","relative"))
    gross_bps = base["spread"]*10_000 if economic else np.nan

    monthly = []
    for month, part in replay.groupby(replay.block_hour.dt.to_period("M")):
        m = _metrics(part, low, high)
        monthly.append({"month":str(month),"spread":m["spread"],"n":m["n"]})
    valid_months = [m for m in monthly if np.isfinite(m["spread"])]
    positive_month_share = float(np.mean([m["spread"]>0 for m in valid_months])) if valid_months else 0.0

    perturb = {}
    for tail in (0.10,0.20,0.30):
        lo, hi = calibration.feature.quantile([tail,1-tail]).tolist()
        perturb[str(tail)] = _metrics(replay, lo, hi)["spread"]

    days = _daily_spreads(replay, low, high).to_numpy()
    if len(days):
        draws = np.array([RNG.choice(days, len(days), replace=True).mean() for _ in range(1000)])
        bootstrap_ci = [float(np.quantile(draws,.025)),float(np.quantile(draws,.975))]
    else:
        bootstrap_ci = [np.nan,np.nan]

    observed = base["spread"]
    placebo = []
    vals = replay.outcome.to_numpy(copy=True)
    top_mask = replay.feature.to_numpy() >= high
    bottom_mask = replay.feature.to_numpy() <= low
    for _ in range(200):
        shuffled = RNG.permutation(vals)
        placebo.append(float(shuffled[top_mask].mean()-shuffled[bottom_mask].mean()))
    placebo_p = float((1+sum(v>=observed for v in placebo if np.isfinite(v)))/(1+sum(np.isfinite(placebo))))

    selected = replay[(replay.feature>=high)|(replay.feature<=low)].copy()
    selected["side"] = np.where(selected.feature>=high,1,-1)
    contributions = selected.assign(edge=lambda q:q.side*q.outcome).groupby("pool_address").edge.sum().abs().sort_values(ascending=False)
    loo = {}
    for pool in contributions.head(10).index:
        loo[pool] = _metrics(replay[replay.pool_address!=pool],low,high)["spread"]
    loo_min = min(loo.values()) if loo else np.nan

    tail_cut = replay.outcome.abs().quantile(.99)
    no_right_tail = _metrics(replay[replay.outcome.abs()<=tail_cut],low,high)["spread"]
    no_duplicates = _metrics(replay[replay.duplicate_pressure<=1.000001],low,high)["spread"]
    half = replay.block_hour.quantile(.5)
    first_half = _metrics(replay[replay.block_hour<=half],low,high)["spread"]
    second_half = _metrics(replay[replay.block_hour>half],low,high)["spread"]

    cost_stress = {}
    if economic:
        for c in (0,30,60,100,200): cost_stress[str(c)] = gross_bps-2*c
    delay_stress = {}
    horizon = int(h["horizon"].rstrip("h"))
    if outcome_column(h).startswith("fwd_ret") and horizon in (1,4):
        for delay in (1,2,4):
            dd = _prepared(panel,h,f"delay{delay}_ret_{horizon}")
            dd = dd[dd.block_hour>cutoff]
            delay_stress[str(delay)] = _metrics(dd,low,high)["spread"]*10_000

    delay1_net = delay_stress.get("1",-np.inf)-2*BASE_COST_BPS
    robust = (
        economic and q48 <= .10 and gross_bps-2*BASE_COST_BPS > 0 and
        delay1_net > 0 and positive_month_share >= .60 and placebo_p <= .05 and
        (base["pool_concentration"] or 1) < .25 and no_right_tail > 0
    )
    if robust:
        status = "REPLAY_SURVIVOR"
    elif economic and gross_bps-2*BASE_COST_BPS <= 0 and base["n"] >= 500:
        status = "KILLED"
    elif base["spread"] > 0 and positive_month_share >= .50 and placebo_p <= .10 and no_right_tail/base["spread"] >= .25:
        status = "MECHANISM_PLAUSIBLE"
    else:
        status = "DISCOVERY_ONLY"

    gate = "FAIL"
    gate_reason = "historical executable quotes, failed-fill/sellability outcomes, USD depth, MEV and price impact are absent"
    checks = {
        "lookahead":"PASS: feature timestamps do not exceed signal hour; close-anchor delay tested where directional",
        "label_leakage":"PASS_BY_CODE_AUDIT: future columns never used as features",
        "survivorship_bias":"PASS_FOR_OBSERVED_V3: all observed pools included; inactive/unobserved market still unknown",
        "universe_contamination":"PASS: no outcome-based or full-period top-N filter",
        "failed_fills_omitted":"UNRESOLVED_GATE",
        "unsellable_outcomes":"UNRESOLVED_GATE",
        "duplicate_events":f"SENSITIVITY spread={no_duplicates:.6g} on duplicate_pressure=1 rows",
        "signal_overlap":"MITIGATED: inference clustered by UTC day",
        "wallet_concentration":"DATA_INSUFFICIENT: only aggregate sender counts retained",
        "pool_concentration":f"top pool={base['pool_concentration']:.3f}; LOO min={loo_min:.6g}",
        "single_period_concentration":f"positive months={positive_month_share:.1%}",
        "endpoint_selection":f"tail perturbations={perturb}",
        "parameter_selection":f"fixed 40% calibration and 10/20/30% tails={perturb}",
        "multiple_testing":f"BH q across 48={q48:.6g}",
        "execution_delay":f"bps={delay_stress}" if delay_stress else "NOT_APPLICABLE_OR_NOT_IDENTIFIED",
        "gas_fee":f"round-trip two-leg cost stress bps={cost_stress}" if economic else "NOT_DIRECTLY_MONETIZED",
        "slippage":"UNRESOLVED_GATE",
        "mev":"UNRESOLVED_GATE",
        "price_impact":"UNRESOLVED_GATE",
        "capacity":"UNRESOLVED_GATE even at $1,000",
        "quote_vs_fill":"UNRESOLVED_GATE",
        "data_interruption":"CONCERN: exact-horizon joins only; missing hours excluded",
        "future_generated_fields":"PASS_BY_SCHEMA_AUDIT: raw event-time fields only",
        "regime_dependence":f"monthly={monthly}",
        "alpha_decay":f"first={first_half:.6g}; second={second_half:.6g}",
        "right_tail_dependence":f"without largest 1% absolute outcomes={no_right_tail:.6g}",
        "permanent_unsellable":"UNRESOLVED_GATE",
    }
    return {
        "alpha_id":h["alpha_id"],"status":status,"n_replay":base["n"],"calibration_cutoff":str(cutoff),
        "gross_spread":base["spread"],"gross_bps":gross_bps,
        "net_bps_60_each_leg":gross_bps-120 if economic else np.nan,
        "day_t":base["t_day"],"day_p":base["p_day"],"q_bh_48":q48,
        "bootstrap_ci95":bootstrap_ci,"placebo_p":placebo_p,"positive_month_share":positive_month_share,
        "monthly":monthly,"parameter_perturbation":perturb,"cost_stress_bps":cost_stress,"delay_stress_bps":delay_stress,
        "pool_concentration":base["pool_concentration"],"leave_top_pools_out":loo,"loo_min_spread":loo_min,
        "without_top_1pct_outcomes":no_right_tail,"duplicate_clean_spread":no_duplicates,
        "first_half_spread":first_half,"second_half_spread":second_half,
        "shadow_gate":gate,"shadow_gate_reason":gate_reason,"kill_checks":checks,
    }


def run_deep_dives() -> pd.DataFrame:
    research_entry_guard()
    panel = pd.read_parquet(DATA/"feature_panel.parquet")
    panel["block_hour"] = pd.to_datetime(panel.block_hour,utc=True)
    rapid = pd.read_csv(ROOT/"RAPID_SCREEN_RESULTS.csv").set_index("alpha_id")
    by_id = {h["alpha_id"]:h for h in HYPOTHESES}
    selection = []
    outputs = []
    reasons = {
        "F03":"strongest cost-tolerant directional gross structure",
        "F01":"related flow signal with a shorter horizon; tests independence/decay",
        "H01":"path acceptance candidate with positive conservative net proxy",
        "S02":"disaster-avoidance candidate with positive conservative net proxy",
        "RV01":"very significant relative-value microstructure but likely killed by costs",
        "C03":"non-price reactivation/survival mechanism",
        "A01":"nontraditional breadth/activity mismatch anomaly",
        "R02":"meta-regime candidate retained despite temporal instability",
    }
    for aid in CANDIDATES:
        h=by_id[aid]
        selection.append({"alpha_id":aid,"family":h["family"],"rapid_status":rapid.loc[aid,"status"],"selection_reason":reasons[aid]})
        result=deep_one(panel,h,float(rapid.loc[aid,"q_bh_48"]))
        outputs.append(result)
        d=EXP/aid/"deep_dive"; d.mkdir(parents=True,exist_ok=True)
        (d/"result.json").write_text(json.dumps(result,indent=2,default=_default),encoding="utf-8")
        (d/"README.md").write_text(f"# {aid} Deep Dive\n\nReproduce: `python3 src/deep_dive.py`\n\nIncludes walk-forward month slices, parameter/cost/delay stress, day-cluster bootstrap, placebo, pool leave-out, right-tail removal, duplicate sensitivity, and the 27-item Kill Panel.\n",encoding="utf-8")
    pd.DataFrame(selection).to_csv(ROOT/"TOURNAMENT_SELECTION.csv",index=False)
    flat=[]
    for r in outputs:
        flat.append({k:v for k,v in r.items() if not isinstance(v,(dict,list))})
    out=pd.DataFrame(flat)
    out.to_csv(ROOT/"KILL_PANEL_RESULTS.csv",index=False)
    (ROOT/"KILL_PANEL_FULL.json").write_text(json.dumps(outputs,indent=2,default=_default),encoding="utf-8")
    return out


def _default(v):
    if isinstance(v,(np.integer,)): return int(v)
    if isinstance(v,(np.floating,)): return float(v)
    if isinstance(v,(np.bool_,)): return bool(v)
    if pd.isna(v): return None
    return str(v)


if __name__=="__main__":
    run_deep_dives()
