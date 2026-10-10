"""Sample F03 signal hours and count actual direction switches. Read-only CH."""
from __future__ import annotations

import json
import math
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from ch_readonly import query_tsv
from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
WILDCAT = Path("/root/base_wildcat_alpha")
OUT = ROOT / "out"
RNG = np.random.default_rng(8453)
SAMPLE_N = 200  # per side, 400 total
COST_BPS = 120.0


def clustered_spread(d: pd.DataFrame, low: float, high: float) -> dict:
    z = d[["block_hour", "feature", "outcome", "pool_address"]].dropna().copy()
    z["side"] = np.select([z.feature >= high, z.feature <= low], [1, -1], default=0)
    z = z[z.side != 0]
    top, bottom = z[z.side == 1], z[z.side == -1]
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
    return {
        "n": int(len(z)),
        "n_top": int(len(top)),
        "n_bottom": int(len(bottom)),
        "spread_bps": None if not np.isfinite(spread) else float(spread * 10_000),
        "t_day": t_stat,
        "p_day": p_value,
        "high_mean_bps": None if not len(top) else float(top.outcome.mean() * 10_000),
        "low_mean_bps": None if not len(bottom) else float(bottom.outcome.mean() * 10_000),
    }


def r2_fit(y, X):
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X = y[mask], X[mask]
    if len(y) < 20:
        return {"n": int(len(y)), "r2": None, "coef": None}
    X = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return {"n": int(len(y)), "r2": 1 - ss_res / ss_tot if ss_tot else 0.0, "coef": [float(c) for c in coef]}


def predict_r2(y, X, coef):
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X = y[mask], X[mask]
    X = np.column_stack([np.ones(len(y)), X])
    pred = X @ np.asarray(coef)
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return {"n": int(len(y)), "r2": 1 - ss_res / ss_tot if ss_tot else 0.0}


def flips_from_signs(signs: np.ndarray) -> dict:
    if len(signs) < 2:
        return {"n_swaps": int(len(signs)), "n_flips": 0, "flip_rate": None, "longest_run": int(len(signs))}
    flips = int(np.sum(signs[1:] != signs[:-1]))
    # longest run
    longest = 1
    cur = 1
    for i in range(1, len(signs)):
        if signs[i] == signs[i - 1]:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1
    return {
        "n_swaps": int(len(signs)),
        "n_flips": flips,
        "flip_rate": float(flips / (len(signs) - 1)),
        "longest_run": int(longest),
    }


def main() -> None:
    research_entry_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    x = pd.read_parquet(WILDCAT / "data/feature_panel.parquet")
    x["block_hour"] = pd.to_datetime(x.block_hour, utc=True)
    d = x.dropna(subset=["direction_persistence", "fwd_ret_4"]).copy()
    d = d[d.fwd_ret_4.abs() <= np.log(100)]
    cutoff = d.block_hour.quantile(0.40)
    cal, rep = d[d.block_hour <= cutoff], d[d.block_hour > cutoff]
    low, high = cal.direction_persistence.quantile([0.2, 0.8]).tolist()
    hi = rep[rep.direction_persistence >= high]
    lo = rep[rep.direction_persistence <= low]
    hi_s = hi.sample(n=min(SAMPLE_N, len(hi)), random_state=8453)
    lo_s = lo.sample(n=min(SAMPLE_N, len(lo)), random_state=8453)
    sample = pd.concat([hi_s, lo_s], ignore_index=True)
    sample["f03_side"] = np.where(sample.direction_persistence >= high, "HIGH", "LOW")
    keys = sample[["pool_address", "block_hour", "f03_side", "direction_persistence", "flow_imbalance", "ret_0", "fwd_ret_4", "swap_count"]].copy()
    keys.to_csv(OUT / "r0_flip_sample_keys.csv", index=False)

    tuples = []
    for _, r in keys.iterrows():
        hour = pd.Timestamp(r.block_hour).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S")
        tuples.append(f"(lower('{r.pool_address}'), toDateTime('{hour}', 'UTC'))")
    in_list = ",\n    ".join(tuples)
    sql = f"""
SELECT
    lower(pool_address) AS pool_address,
    toStartOfHour(block_time) AS block_hour,
    block_number,
    log_index,
    tx_hash,
    sender,
    startsWith(substring(amount0_raw, 3), 'f') AS amount0_neg
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0 AND length(amount0_raw) = 66
  AND (lower(pool_address), toStartOfHour(block_time)) IN (
    {in_list}
  )
ORDER BY pool_address, block_hour, block_number, log_index
"""
    raw = query_tsv(sql)
    (OUT / "r0_flip_swaps.tsv").write_text(raw, encoding="utf-8")
    sw = pd.read_csv(StringIO(raw), sep="\t")
    sw["block_hour"] = pd.to_datetime(sw.block_hour, utc=True)
    sw["pool_address"] = sw.pool_address.str.lower()
    keys["pool_address"] = keys.pool_address.str.lower()
    keys["block_hour"] = pd.to_datetime(keys.block_hour, utc=True)

    rows = []
    for (pool, hour), g in sw.groupby(["pool_address", "block_hour"], sort=False):
        g = g.sort_values(["block_number", "log_index"])
        # unique event key
        g = g.drop_duplicates(["tx_hash", "log_index"], keep="first")
        signs = g.amount0_neg.to_numpy().astype(int)
        stats_obs = flips_from_signs(signs)
        # placebo: shuffle order 50 times
        placebo_rates = []
        for _ in range(50):
            sh = RNG.permutation(signs)
            placebo_rates.append(flips_from_signs(sh)["flip_rate"] or 0.0)
        stats_obs["placebo_flip_rate_mean"] = float(np.mean(placebo_rates)) if placebo_rates else None
        stats_obs["repeat_sender_share"] = float(1 - g.sender.nunique() / max(len(g), 1))
        stats_obs["unique_senders"] = int(g.sender.nunique())
        stats_obs["pool_address"] = pool
        stats_obs["block_hour"] = pd.Timestamp(hour)
        rows.append(stats_obs)
    flips = pd.DataFrame(rows)
    m = keys.merge(flips, on=["pool_address", "block_hour"], how="left")
    m["scarcity"] = 1 - m.flip_rate  # high = few switches
    m.to_csv(OUT / "r0_flip_joined.csv", index=False)

    usable = m.dropna(subset=["flip_rate", "fwd_ret_4"])
    # within-sample tails of scarcity (no peeking at full panel — sample is already F03 tails)
    # pre-reg: incremental after |imbalance|
    # split sample 70/30 by time
    usable = usable.sort_values("block_hour")
    cut = usable.block_hour.quantile(0.70)
    train, hold = usable[usable.block_hour <= cut], usable[usable.block_hour > cut]
    # signed outcome in F03 direction: HIGH wants +fwd, LOW wants -fwd
    usable = usable.copy()
    usable["signed_fwd"] = np.where(usable.f03_side == "HIGH", usable.fwd_ret_4, -usable.fwd_ret_4)

    # quintile of scarcity vs signed_fwd (calibration on train)
    low_s, high_s = train.scarcity.quantile([0.2, 0.8]).tolist() if len(train) else (np.nan, np.nan)
    tmp = hold.rename(columns={"scarcity": "feature", "fwd_ret_4": "outcome"}) if len(hold) else hold
    # use signed outcome for economic meaning
    hold2 = hold.copy()
    hold2["outcome"] = np.where(hold2.f03_side == "HIGH", hold2.fwd_ret_4, -hold2.fwd_ret_4)
    train2 = train.copy()
    train2["outcome"] = np.where(train2.f03_side == "HIGH", train2.fwd_ret_4, -train2.fwd_ret_4)
    all2 = usable.copy()
    all2["outcome"] = all2.signed_fwd
    spread_all = clustered_spread(
        all2.rename(columns={"scarcity": "feature"}),
        *usable.scarcity.quantile([0.2, 0.8]).tolist(),
    ) if len(usable) else {}

    # residual: among similar |flow|, does scarcity add?
    y = usable.signed_fwd.to_numpy()
    X0 = usable[["direction_persistence"]].abs().to_numpy()
    X1 = np.column_stack([usable.direction_persistence.abs().to_numpy(), usable.scarcity.to_numpy()])
    # time holdout
    y_tr, y_ho = train2.outcome.to_numpy(), hold2.outcome.to_numpy()
    X0_tr = train2.direction_persistence.abs().to_numpy().reshape(-1, 1)
    X1_tr = np.column_stack([train2.direction_persistence.abs().to_numpy(), train2.scarcity.to_numpy()])
    X0_ho = hold2.direction_persistence.abs().to_numpy().reshape(-1, 1)
    X1_ho = np.column_stack([hold2.direction_persistence.abs().to_numpy(), hold2.scarcity.to_numpy()])
    m0 = r2_fit(y_tr, X0_tr)
    m1 = r2_fit(y_tr, X1_tr)
    h0 = predict_r2(y_ho, X0_ho, m0["coef"]) if m0["coef"] else {"n": 0, "r2": None}
    h1 = predict_r2(y_ho, X1_ho, m1["coef"]) if m1["coef"] else {"n": 0, "r2": None}
    r2_inc = None if h0["r2"] is None or h1["r2"] is None else float(h1["r2"] - h0["r2"])

    # correlation flip_rate vs |flow| (should be negative: more imbalance → fewer flips mechanically)
    corr = float(usable.flip_rate.corr(usable.flow_imbalance.abs())) if len(usable) > 3 else None

    placebo_gap = float((usable.placebo_flip_rate_mean - usable.flip_rate).mean()) if usable.placebo_flip_rate_mean.notna().any() else None

    verdict = "UNDERPOWERED"
    if len(usable) < 80:
        verdict = "UNDERPOWERED"
    elif (spread_all.get("spread_bps") is not None and abs(spread_all["spread_bps"]) < 20) and (
        r2_inc is None or r2_inc < 0.01
    ):
        verdict = "FLIP_ADDS_NOTHING"
    elif r2_inc is not None and r2_inc >= 0.01:
        verdict = "FLIP_ADDS_IN_TICK_SPACE"
    elif spread_all.get("spread_bps") is not None and abs(spread_all["spread_bps"]) >= 20:
        verdict = "FLIP_SPREAD_ONLY_NO_R2" if (r2_inc is None or r2_inc < 0.01) else "FLIP_ADDS_IN_TICK_SPACE"

    payload = {
        "sample_n_requested": SAMPLE_N * 2,
        "sample_n": int(len(keys)),
        "swap_rows": int(len(sw)),
        "hours_with_flips": int(len(usable)),
        "mean_flip_rate": float(usable.flip_rate.mean()) if len(usable) else None,
        "mean_placebo_flip_rate": float(usable.placebo_flip_rate_mean.mean()) if len(usable) else None,
        "mean_placebo_minus_obs": placebo_gap,
        "corr_flip_rate_abs_flow": corr,
        "scarcity_spread_signed_fwd": spread_all,
        "holdout_r2_m0": h0,
        "holdout_r2_m1": h1,
        "holdout_r2_increment": r2_inc,
        "train_n": int(len(train2)),
        "holdout_n": int(len(hold2)),
        "by_side": {
            side: {
                "n": int(len(q)),
                "mean_flip_rate": float(q.flip_rate.mean()),
                "mean_scarcity": float(q.scarcity.mean()),
                "mean_signed_fwd_bps": float(q.signed_fwd.mean() * 10_000),
            }
            for side, q in usable.groupby("f03_side")
        },
        "verdict": verdict,
        "note": "scarcity = 1 - flip_rate; signed_fwd = fwd_ret_4 for HIGH, -fwd_ret_4 for LOW",
    }
    (OUT / "r0_event_flips.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
