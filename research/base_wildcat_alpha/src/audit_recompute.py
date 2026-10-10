from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from safety import research_entry_guard


ROOT = Path(__file__).resolve().parents[1]

# Intentionally independent mapping and arithmetic: this module does not import
# screen.py or deep_dive.py.
SPECS = {
    "F03": ("direction_persistence", 1, "fwd_ret_4"),
    "F01": ("flow_imbalance", 1, "fwd_ret_1"),
    "H01": ("close_location", 1, "fwd_ret_1"),
    "S02": ("unique_sender_ratio", 1, "future_worst_24"),
    "RV01": ("cross_pool_divergence", -1, "relative_spread_1"),
    "C03": ("reactivation", 1, "future_activity_4"),
    "A01": ("breadth_activity_mismatch", 1, "fwd_abs_4"),
    "R02": ("market_dispersion", -1, "fwd_ret_4"),
}


def recompute() -> dict:
    research_entry_guard()
    p = pd.read_parquet(ROOT/"data/feature_panel.parquet")
    p["block_hour"] = pd.to_datetime(p.block_hour,utc=True)
    published = {r["alpha_id"]:r for r in json.loads((ROOT/"KILL_PANEL_FULL.json").read_text())}
    results={}
    for aid,(feature,direction,outcome) in SPECS.items():
        d=p[["block_hour",feature,outcome]].replace([np.inf,-np.inf],np.nan).dropna().copy()
        d["feature"]=d[feature]*direction; d["outcome"]=d[outcome]
        if any(s in outcome for s in ("ret","worst","spread")):
            d=d[d.outcome.abs()<=np.log(100)]
        cutoff=d.block_hour.quantile(.40)
        cal=d[d.block_hour<=cutoff]; replay=d[d.block_hour>cutoff]
        lo,hi=cal.feature.quantile([.2,.8])
        top=replay.loc[replay.feature>=hi,"outcome"]
        bottom=replay.loc[replay.feature<=lo,"outcome"]
        value=float(top.mean()-bottom.mean())
        expected=float(published[aid]["gross_spread"])
        results[aid]={"recomputed":value,"published":expected,"absolute_difference":abs(value-expected),"match":abs(value-expected)<1e-12,"n_top":len(top),"n_bottom":len(bottom)}
    output={"independent_implementation":True,"all_match":all(v["match"] for v in results.values()),"results":results}
    (ROOT/"INDEPENDENT_RECOMPUTE.json").write_text(json.dumps(output,indent=2),encoding="utf-8")
    return output


if __name__=="__main__":
    recompute()
