from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from safety import research_entry_guard


ROOT=Path(__file__).resolve().parents[1]


def initialize_zero_order_shadow() -> dict:
    """Freeze F03 signals only; there is intentionally no execution interface."""
    research_entry_guard()
    x=pd.read_parquet(ROOT/"data/feature_panel.parquet")
    x["block_hour"]=pd.to_datetime(x.block_hour,utc=True)
    d=x[["block_hour","pool_address","direction_persistence","fwd_ret_4"]].dropna()
    cutoff=d.block_hour.quantile(.40)
    calibration=d[d.block_hour<=cutoff]
    low,high=calibration.direction_persistence.quantile([.2,.8])
    latest=x.block_hour.max()
    current=x[x.block_hour==latest][["pool_address","direction_persistence","swap_count","unique_senders","duplicate_pressure"]].copy()
    current["signal_side"]=np.select([current.direction_persistence>=high,current.direction_persistence<=low],["HIGH","LOW"],default="NONE")
    signals=current[current.signal_side!="NONE"].copy()
    signals.insert(0,"signal_generated_utc",pd.Timestamp.now(tz="UTC").isoformat())
    signals.insert(1,"market_data_hour",latest.isoformat())
    signals["research_status"]="ZERO_ORDER_SHADOW"
    signals["order_count"]=0
    signals["transaction_count"]=0
    outdir=ROOT/"SHADOW"; outdir.mkdir(parents=True,exist_ok=True)
    signals.to_csv(outdir/"signals.csv",index=False)
    state={
        "candidate":"F03","promotion_status":"SHADOW_CANDIDATE",
        "purpose":"collect unseen outcome and future quote/depth/failure evidence",
        "market_data_hour":latest.isoformat(),"signal_count":int(len(signals)),
        "matured_outcome_count":0,"real_orders_sent":0,"real_transactions_signed":0,"real_capital_at_risk":0,
        "auto_promotion":False,"executor_present":False,
    }
    (outdir/"state.json").write_text(json.dumps(state,indent=2),encoding="utf-8")
    return state


if __name__=="__main__": initialize_zero_order_shadow()
