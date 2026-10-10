"""Calendar completeness audit — monthly targets only over covered days."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, load_freeze, save_json, utcnow


def run_calendar_audit(resolved: pd.DataFrame, panel: pd.DataFrame | None = None) -> dict[str, Any]:
    freeze = load_freeze()
    start = pd.Timestamp(freeze["calendar"]["decision_start"])
    end = pd.Timestamp(freeze["calendar"]["decision_end"])
    all_days = pd.date_range(start.normalize(), end.normalize(), freq="D", tz="UTC")

    buys = resolved[resolved["side"] == "buy"].copy()
    buys["day"] = pd.to_datetime(buys["ts"], unit="s", utc=True).dt.normalize()
    u = freeze["universe"]
    scoped = buys[
        (buys["pool_age_s"] >= int(u["pool_age_min_s"]))
        & (buys["pool_age_s"] < int(u["pool_age_max_s"]))
        & (buys["quote_weth"] >= float(u["min_quote_weth"]))
    ]
    scoped_days = set(scoped["day"].unique())
    buy_days = set(buys["day"].unique())

    covered = []
    missing = []
    for d in all_days:
        if d in scoped_days:
            covered.append(str(d.date()))
        else:
            missing.append(str(d.date()))

    daily_n = scoped.groupby(scoped["day"].dt.strftime("%Y-%m-%d")).size()
    # decay: first half vs second half of covered days
    if len(daily_n):
        mid = daily_n.index[len(daily_n) // 2]
        early = daily_n.loc[:mid]
        late = daily_n.loc[mid:]
        decay = {
            "early_mean_signals_per_day": float(early.mean()),
            "late_mean_signals_per_day": float(late.mean()),
            "late_over_early": float(late.mean() / early.mean()) if early.mean() else None,
        }
    else:
        decay = {}

    panel_days = None
    if panel is not None and len(panel):
        panel_days = int(pd.to_datetime(panel["decision_time"], unit="s", utc=True).dt.date.nunique())

    report = {
        "task": "calendar_completeness",
        "window_start": str(start.date()),
        "window_end": str(end.date()),
        "calendar_span_days": int(len(all_days)),
        "days_with_any_buy": int(len(buy_days)),
        "days_with_scoped_buy": int(len(scoped_days)),
        "missing_data_days_n": int(len(missing)),
        "missing_data_days_sample": missing[:40],
        "coverage_frac_scoped": float(len(scoped_days) / max(1, len(all_days))),
        "scoped_buys": int(len(scoped)),
        "signals_per_covered_day": {
            "mean": float(daily_n.mean()) if len(daily_n) else 0,
            "p50": float(daily_n.median()) if len(daily_n) else 0,
            "p90": float(daily_n.quantile(0.9)) if len(daily_n) else 0,
            "max": int(daily_n.max()) if len(daily_n) else 0,
            "min": int(daily_n.min()) if len(daily_n) else 0,
        },
        "decay": decay,
        "panel_unique_decision_days": panel_days,
        "v0_explanation_43_days": (
            "v0 whale-selected 43 unique days arose from: (1) underlying resolved data covering "
            f"only {len(buy_days)} of {len(all_days)} calendar days (true discontinuity), "
            "(2) walk-forward TEST windows only, (3) v0 200k random downsample of scoped buys "
            "(removed in v1), (4) absorbed+quantile selection. Not a date-field bug alone."
        ),
        "monthly_target_rule": "Evaluate TARGET only over months with >=1 covered day; disclose missing days.",
        "generated_at": utcnow(),
    }
    save_json(OUT / "calendar_audit.json", report)
    pd.DataFrame({"missing_day": missing}).to_csv(OUT / "missing_data_days.csv", index=False)
    print(
        f"[calendar] span={len(all_days)} covered_scoped={len(scoped_days)} "
        f"missing={len(missing)} coverage={report['coverage_frac_scoped']:.2%}",
        flush=True,
    )
    return report
