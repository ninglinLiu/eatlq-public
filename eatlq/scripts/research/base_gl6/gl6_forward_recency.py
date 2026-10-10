#!/usr/bin/env python3
"""GL6 frozen F-on forward-recency audit (research-only)."""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research"))
sys.path.insert(0, str(ROOT / "scripts/lib"))
sys.path.insert(0, str(ROOT / "scripts/research/base_lefttail"))
from gl_readonly_rpc import Rpc, atomic_probe, balance_of, compile_atomic_bytecode, pool_fee, WETH  # noqa
from v3_quoterv2_struct import build_quote_exact_input_single_calldata  # noqa
import keystone_live as K  # noqa
import risk_rebacktest as R  # noqa
import live_config_rebacktest as L  # noqa
import aggressive_live_rebacktest as A  # noqa

PREREG = ROOT / "reports/GL6_preregistration_20260716.md"
PREREG_SHA = "fa216ded00bd36420f907f529793049fc2dae7e8d286bf0496f6903d34c8c64a"
ERRATUM_SHA = "5cf1a7da94ceddc341692466e5811bf1adffa54b16d66fc03cf52490573bb9d2"
REPRO_GRID_ERRATUM_SHA = "7e21db08cfd0d8412b9b5c3e673528354de947bc27b89476a5ba24d70a1a276c"
LEDGER = ROOT / "mvp/diagnostics/base_gl6_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl6_rpc_ledger_20260716.json"
OUT = ROOT / "mvp/diagnostics/base_gl6_forward_recency_20260716.json"
LEGS = ROOT / "mvp/diagnostics/base_gl6_forward_per_leg_20260716.json"
CENSUS = ROOT / "mvp/diagnostics/base_gl6_forward_feature_census_20260716.json"
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
START = "2026-05-15 16:56:54"
END = "2026-07-16 09:24:07"
F_READY = pd.Timestamp("2026-06-24T11:47:16Z")
W = 600
H = 14400
SPEND = 30_200_000_000_000_000
OFFSETS = (1, 5, 15)
LN1P = math.log(1.0001)
HARD_STOP = math.log(0.5)
TRAIL = 0.35
MAX_HOLD_BLOCKS = 7200
SIZE_USD = 50.09425
PESS = -1.0 - 0.15 / SIZE_USD - 0.02


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ch(sql: str, timeout=600) -> str:
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[:3000])
    return r.stdout


def universe_coverage() -> dict:
    sql = f"""
SET max_memory_usage=12000000000,max_execution_time=500,max_threads=4;
WITH cand AS (
 SELECT lower(pool_address) pool_address FROM eatlq.discovered_pools
 WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
), fs AS (
 SELECT lower(pool_address) pool_address,min(block_time) t0,
        argMin(block_number,tuple(block_time,block_number,tx_hash,log_index)) first_block
 FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND block_time IS NOT NULL
 GROUP BY pool_address
)
SELECT count(),max(t0),argMax(first_block,tuple(t0,first_block))
FROM fs INNER JOIN cand USING(pool_address)
WHERE t0>='{START}' AND t0<='{END}' FORMAT TabSeparated
"""
    n, latest, block = ch(sql).strip().split("\t")
    return {"raw_universe_n": int(n), "latest_first_swap_time": latest + "Z", "latest_first_swap_block": int(block)}


def candidates() -> pd.DataFrame:
    sql = f"""
SET max_memory_usage=12000000000,max_execution_time=500,max_threads=4;
WITH cand AS (
 SELECT lower(pool_address) pool_address,lower(token0_address) t0a,lower(token1_address) t1a
 FROM eatlq.discovered_pools WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
), fs AS (
 SELECT lower(pool_address) pool_address,min(block_time) t0
 FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND block_time IS NOT NULL
 GROUP BY pool_address HAVING t0>='{START}' AND t0<='{END}'
), sw AS (
 SELECT lower(s.pool_address) pool,s.tick tick,s.block_time bt,s.block_number bn,
        s.tx_hash tx,s.log_index li,
        lower(s.recipient) rcp,lower(s.sender) snd,toFloat64(s.liquidity) liq,
        dateDiff('second',fs.t0,s.block_time) sec,fs.t0 t0
 FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON lower(s.pool_address)=fs.pool_address
 WHERE s.chain_id=8453 AND s.block_time IS NOT NULL AND dateDiff('second',fs.t0,s.block_time) BETWEEN 0 AND {W}
)
SELECT sw.pool,
 if(c.t0a='{WETH}',-1,1) orient,if(c.t0a='{WETH}',c.t1a,c.t0a) token,any(t0) t0,
 count() ns_pre,uniqExact(rcp) buyers_pre,uniqExact(snd) senders_pre,
 argMin(tick,tuple(bt,bn,tx,li)) first_tick,argMax(tick,tuple(bt,bn,tx,li)) last_tick_pre,min(tick) min_tick_pre,max(tick) max_tick_pre,
 argMin(liq,tuple(bt,bn,tx,li)) liq_first,argMax(liq,tuple(bt,bn,tx,li)) liq_last_pre,min(liq) liq_min_pre,
 countIf(sec<={W//2}) ns_h1,countIf(sec>{W//2}) ns_h2,argMaxIf(tick,tuple(bt,bn,tx,li),sec<={W//2}) last_tick_h1,
 argMax(bn,tuple(bt,bn,tx,li)) entry_block
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address GROUP BY sw.pool,orient,token
HAVING ns_pre>={5} ORDER BY t0 FORMAT TabSeparatedWithNames
"""
    return pd.read_csv(StringIO(ch(sql)), sep="\t")


def feature_matrix(df: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    o = df.orient.astype(float)
    lp_first = df.first_tick * LN1P * o
    lp_last = df.last_tick_pre * LN1P * o
    a = df.min_tick_pre * LN1P * o
    b = df.max_tick_pre * LN1P * o
    lp_lo, lp_hi = np.minimum(a, b), np.maximum(a, b)
    lp_h1 = df.last_tick_h1.where(df.ns_h1 > 0, df.first_tick) * LN1P * o
    ns = df.ns_pre.clip(lower=1); buyers = df.buyers_pre.clip(lower=1); senders = df.senders_pre.clip(lower=1)
    liq0 = df.liq_first.clip(lower=1); nsh1 = df.ns_h1.clip(lower=1)
    vals = np.column_stack([df.ns_pre, df.buyers_pre, df.senders_pre, ns/buyers, df.buyers_pre/senders,
        lp_last-lp_first, lp_hi-lp_lo, lp_hi-lp_last, lp_last-lp_lo,
        np.log1p(df.liq_last_pre.clip(lower=0)), df.liq_last_pre/liq0, df.liq_min_pre/liq0,
        df.ns_h2/nsh1, lp_h1-lp_first, lp_last-lp_h1])
    return pd.DataFrame(vals, columns=names)


def load_model():
    historical, _ = K.load()
    model, X, _train, _test = R.fit_model(historical)
    return model, list(X.columns)


def step0_anchor():
    old=L.run_lens(L.load_base_df(),"qv2")
    dep,skel,creators,token_skel=L._load_blacklists()
    masks=L.gate_masks(old,dep,skel,creators,token_skel)
    fon=masks["live_full"] & old.gate_fric & old.tradeable
    aggressive=A.aggressive_gate(old,creators)
    anchor={"labeled_n":len(old),"f_on_pass":int(fon.sum()),"aggressive_gate":int(aggressive.sum())}
    assert anchor=={"labeled_n":644,"f_on_pass":120,"aggressive_gate":339},anchor
    return anchor


def fetch_paths(pools: list[str]) -> dict[str, list[tuple[int,int,int]]]:
    out = {p: [] for p in pools}
    for i in range(0, len(pools), 250):
        chunk = pools[i:i+250]
        ins = ",".join(f"'{p}'" for p in chunk)
        sql = f"""
SET max_memory_usage=8000000000,max_execution_time=300,max_threads=4;
WITH fs AS (SELECT lower(pool_address) pool,min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
 WHERE chain_id=8453 AND lower(pool_address) IN ({ins}) AND block_time IS NOT NULL GROUP BY pool),
sw AS (SELECT lower(s.pool_address) pool,s.tick,s.block_number bn,s.tx_hash tx,s.log_index li,dateDiff('second',fs.t0,s.block_time) sec
 FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON lower(s.pool_address)=fs.pool
 WHERE s.chain_id=8453 AND s.block_time IS NOT NULL AND dateDiff('second',fs.t0,s.block_time) BETWEEN {W} AND {W+H})
SELECT pool,intDiv(sec-{W},600) bkt,argMax(tick,tuple(bn,tx,li)) ltick,max(bn) lblock FROM sw
GROUP BY pool,bkt ORDER BY pool,bkt FORMAT TabSeparated
"""
        for line in ch(sql).splitlines():
            p,b,t,lb=line.split("\t"); out[p].append((int(b),int(t),int(lb)))
    return out


def exit_block(row, path):
    entry_lp = row.last_tick_pre * LN1P * int(row.orient)
    peak = entry_lp
    for _, tick, block in path:
        lp = tick * LN1P * int(row.orient); peak=max(peak,lp)
        if lp-entry_lp <= HARD_STOP:
            return block,"hard_stop"
        if lp-peak <= math.log(1-TRAIL):
            return block,"trail"
        if block-int(row.entry_block) >= MAX_HOLD_BLOCKS:
            return block,"max_hold"
    if path:
        return min(path[-1][2], int(row.entry_block)+MAX_HOLD_BLOCKS),"path_end"
    return int(row.entry_block)+MAX_HOLD_BLOCKS,"path_missing"


def qv2(rpc: Rpc, token_in: str, token_out: str, qty: int, fee: int, block: int) -> dict:
    data=build_quote_exact_input_single_calldata(token_in,token_out,qty,fee)
    try:
        raw,qh,rh=rpc.call("eth_call",[{"to":"0x3d4e44eb1374240ce5f1B871ab261CD16335B76a","data":data},hex(block)])
        val=int(raw[2:66],16) if raw and len(raw)>=66 else 0
        return {"ok":val>0,"value_raw":str(val),"request_sha256":qh,"response_sha256":rh}
    except Exception as exc:
        return {"ok":False,"value_raw":None,"error_class":str(exc)}


def f_status(row, creators, token_skel, dep, skels):
    if pd.Timestamp(row.t0, tz="UTC") < F_READY:
        return "F_NOT_AVAILABLE_PIT"
    tok=row.token.lower(); c=(creators.get(tok) or {}).get("creator","").lower(); s=token_skel.get(tok)
    if (c and c in dep) or (s and s in skels): return "F_BLOCK"
    if c and s: return "F_PASS"
    return "F_IDENTITY_UNRESOLVED"


def resolve_row(rpc: Rpc, bytecode: str, rec: dict) -> dict:
    pool,token,eb=int(rec["entry_block"]),rec["token"],int(rec["entry_block"])
    fee=pool_fee(rpc,rec["pool"],eb); depth=balance_of(rpc,WETH,rec["pool"],eb)
    rec["fee_evidence"]=fee; rec["entry_weth_evidence"]=depth
    if not fee.get("ok") or not depth.get("ok"):
        rec["gate_status"]="GATE_UNRESOLVED"; return rec
    rec["depth_eth"]=int(depth["value_raw"])/1e18
    if rec["depth_eth"]<3:
        rec["gate_status"]="DEPTH_BLOCK"; return rec
    buy=qv2(rpc,WETH,token,SPEND,fee["fee"],eb)
    sell=qv2(rpc,token,WETH,int(buy["value_raw"]),fee["fee"],eb) if buy.get("ok") else {"ok":False}
    rec["entry_buy_quote"]=buy; rec["entry_sell_quote"]=sell
    rec["friction"]=int(sell["value_raw"])/SPEND-1 if sell.get("ok") else None
    if rec["friction"] is None:
        rec["gate_status"]="GATE_UNRESOLVED"; return rec
    if rec["friction"] < -0.05:
        rec["gate_status"]="FRICTION_BLOCK"; return rec
    rec["gate_status"]="GATE_PASS"
    rec["position_qty_raw"]=buy["value_raw"]
    rec["offsets"]={}
    entry_bal=depth
    for off in OFFSETS:
        target=int(rec["legacy_exit_block"])+off
        grid_blocks=sorted({target-1,target,target+1,target+5,target+15})
        grid={str(b):balance_of(rpc,WETH,rec["pool"],b) for b in grid_blocks}
        tb=grid[str(target)]; lag=grid[str(target+15)]
        ratio=int(tb["value_raw"])/int(entry_bal["value_raw"]) if tb.get("ok") and int(entry_bal["value_raw"]) else None
        lagratio=int(lag["value_raw"])/int(entry_bal["value_raw"]) if lag.get("ok") and int(entry_bal["value_raw"]) else None
        atom=atomic_probe(rpc,bytecode,token,fee["fee"],SPEND,target)
        quote=qv2(rpc,token,WETH,int(rec["position_qty_raw"]),fee["fee"],target)
        if ratio is not None and atom.get("sell_ok") and quote.get("ok"):
            ret=int(quote["value_raw"])/SPEND-1-0.15/SIZE_USD-0.02
            branch="ALIVE_ATOMIC_OK" if ratio>0.05 else "IMPAIRED_ATOMIC_OK"
            resolved=True
        elif ratio is not None and lagratio is not None and ratio<=0.05 and lagratio<=0.05 and not atom.get("sell_ok") and not quote.get("ok"):
            ret=PESS; branch="CONFIRMED_COLLAPSE"; resolved=True
        else:
            ret=None; branch="UNRESOLVED"; resolved=False
        optimistic=(int(quote["value_raw"])/SPEND-1-0.15/SIZE_USD-0.02) if quote.get("ok") else 0.0
        rec["offsets"][str(off)]={"target_block":target,"reserve_grid":grid,"reserve_ratio":ratio,"lag_ratio":lagratio,
            "atomic":atom,"exact_quote":quote,"branch":branch,"resolved":resolved,"ret":ret,
            "lower":ret if resolved else PESS,"upper":ret if resolved else optimistic}
    return rec


def metrics(rows, off):
    legs=[r["offsets"][str(off)] for r in rows if r.get("gate_status")=="GATE_PASS"]
    if not legs: return {"n":0,"mean_interval":[None,None],"median_interval":[None,None],"win_rate_interval":[None,None],"unresolved_n":0,"right_tail_pass":False}
    lo=np.array([x["lower"] for x in legs]); hi=np.array([x["upper"] for x in legs])
    resolved=[x["ret"] for x in legs if x["resolved"]]
    pos_lo=np.maximum(lo,0); pos_hi=np.maximum(hi,0)
    top1_lo=float(pos_lo.max()/pos_hi.sum()) if pos_hi.sum()>0 else None
    top1_hi=float(min(1.0,pos_hi.max()/pos_lo.sum())) if pos_lo.sum()>0 else None
    return {"n":len(legs),"resolved_n":len(resolved),"unresolved_n":sum(not x["resolved"] for x in legs),
        "mean_interval":[float(lo.mean()),float(hi.mean())],"resolved_mean":float(np.mean(resolved)) if resolved else None,
        "median_interval":[float(np.median(lo)),float(np.median(hi))],
        "win_rate_interval":[float(np.mean(lo>0)),float(np.mean(hi>0))],
        "branches":dict(Counter(x["branch"] for x in legs)),"confirmed_rug_interval":[sum(x["branch"]=="CONFIRMED_COLLAPSE" for x in legs)/len(legs),sum(x["branch"] in ("CONFIRMED_COLLAPSE","UNRESOLVED") for x in legs)/len(legs)],
        "top1_positive_contribution_interval":[top1_lo,top1_hi],
        "top1_removed_lower_mean":float(np.sort(lo)[:-1].mean()) if len(lo)>1 else None,
        "right_tail_pass":False,
        "right_tail_reason":"N<30 and/or point-in-time F selection unresolved; rolling right-tail not adjudicable"}


def gate_selection_outer_bound(rows, gate_unresolved_n, off):
    legs=[r["offsets"][str(off)] for r in rows if r.get("gate_status")=="GATE_PASS"]
    n=len(legs)+gate_unresolved_n
    if not n:return {"definite_n":0,"possible_n":0,"mean_interval":[None,None]}
    lo=sum(x["lower"] for x in legs)+gate_unresolved_n*PESS
    return {"definite_n":len(legs),"gate_unresolved_n":gate_unresolved_n,"possible_n":n,
            "mean_lower_bound":lo/n,"mean_upper_bound":None,
            "upper_status":"UNBOUNDED_NO_EXIT_EVIDENCE",
            "lower_substitution":PESS}


def main():
    assert sha(PREREG)==PREREG_SHA
    assert sha(ROOT/"reports/GL6_preregistration_erratum_model_identity_20260716.md")==ERRATUM_SHA
    assert sha(ROOT/"reports/GL6_preregistration_erratum_reproducibility_grid_20260716.md")==REPRO_GRID_ERRATUM_SHA
    anchor=step0_anchor()
    coverage=universe_coverage()
    df=candidates(); model,names=load_model(); df["p_fatal"]=model.predict_proba(feature_matrix(df,names))[:,1]
    census_rows=json.loads(df.sort_values(["t0","pool"]).to_json(orient="records",date_format="iso"))
    CENSUS.write_text(json.dumps({"schema_version":1,"total_order":["block_time","block_number","tx_hash","log_index"],"rows":census_rows},indent=2,sort_keys=True)+"\n")
    census_sha=sha(CENSUS)
    scored=df[(df.ns_pre>=5)&(df.p_fatal<=0.27)].copy()
    creators=json.loads((ROOT/"mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json").read_text()).get("token_creators",{})
    token_skel=json.loads((ROOT/"scripts/research/base_lefttail/_token_skeleton_644.json").read_text())
    dep=set(json.loads((ROOT/"services/executor/fixtures/safe_rug_deployers.json").read_text())["deployers"])
    dep|={"0x715c98cedbd0c242e257986535bbf6e1fba764be","0xc8512b5324402be9ab986d011c9adef8992f076f"}
    skels=set(json.loads((ROOT/"scripts/research/base_lefttail/_rug_seed_validated.json").read_text())["safe_skeletons"])
    paths=fetch_paths(scored.pool.str.lower().tolist())
    base=[]
    for r in scored.itertuples(index=False):
        ex,reason=exit_block(r,paths.get(r.pool.lower(),[]))
        d={k:(v.item() if hasattr(v,"item") else v) for k,v in r._asdict().items()}
        d["t0"]=str(pd.Timestamp(r.t0,tz="UTC")); d["legacy_exit_block"]=ex; d["exit_reason"]=reason
        d["f_status"]=f_status(r,creators,token_skel,dep,skels); base.append(d)
    rpc=Rpc(LEDGER,user_agent="EATLQ-GL6-readonly/1"); bytecode=compile_atomic_bytecode(); rows=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut=[ex.submit(resolve_row,rpc,bytecode,d) for d in base]
        for f in as_completed(fut): rows.append(f.result())
    rows.sort(key=lambda x:(x["t0"],x["pool"]))
    possible=[r for r in rows if r.get("gate_status")=="GATE_PASS" and r["f_status"]!="F_BLOCK"]
    definite=[r for r in possible if r["f_status"]=="F_PASS"]
    baseline=[r for r in rows if r.get("gate_status")=="GATE_PASS"]
    gate_unresolved_n=sum(r.get("gate_status")=="GATE_UNRESOLVED" for r in rows)
    f_unres=sum(r["f_status"] in ("F_NOT_AVAILABLE_PIT","F_IDENTITY_UNRESOLVED") for r in possible)
    verdict="INSUFFICIENT_FORWARD_N" if len(definite)<30 or (len(possible) and f_unres/len(possible)>0.2) else "UNADJUDICATED"
    rpc_summary=rpc.summary(); LEDGER_SUM.write_text(json.dumps(rpc_summary,indent=2,sort_keys=True)+"\n")
    result={"schema_version":1,"task_id":"GL6","preregistration_sha256":PREREG_SHA,"model_identity_erratum_sha256":ERRATUM_SHA,"reproducibility_grid_erratum_sha256":REPRO_GRID_ERRATUM_SHA,"research_only":True,
      "step0_anchor":anchor,
      "frozen_config":{"model":"deterministic reconstruction via risk_rebacktest.fit_model on frozen historical _pools.tsv only","forward_rows_in_fit":0,"min_pre":5,"p_cut":0.27,"min_depth_eth":3,"friction":-0.05,"trail":0.35,"hard_stop":0.5,"max_hold_blocks":7200,"stake_eth":0.0302},
      "window":{"start":START+"Z","end":END+"Z","f_ready":str(F_READY),"observed_coverage":coverage},
      "feature_census":{"min_pre_pass_n":len(df),"sha256":census_sha,"path":str(CENSUS.relative_to(ROOT)),"total_order":["block_time","block_number","tx_hash","log_index"]},
      "funnel":{"raw_universe":coverage["raw_universe_n"],"min_pre_block":coverage["raw_universe_n"]-len(df),"min_pre_pass":len(df),"model_pass":len(scored),"depth_block":sum(r.get("gate_status")=="DEPTH_BLOCK" for r in rows),"gate_unresolved":gate_unresolved_n,"gate_pass":len(baseline),"definite_selected":len(definite),"possible_selected_gate_identified":len(possible),"outer_possible_if_gate_and_f_resolve":len(possible)+gate_unresolved_n,"f_unresolved_possible":f_unres},
      "possible_selected_gate_identified_metrics":{str(o):metrics(possible,o) for o in OFFSETS},
      "possible_selected_gate_selection_outer_bound":{str(o):gate_selection_outer_bound(possible,gate_unresolved_n,o) for o in OFFSETS},
      "definite_selected_metrics":{str(o):metrics(definite,o) for o in OFFSETS},
      "same_age_baseline":{str(o):metrics(baseline,o) for o in OFFSETS},
      "same_age_baseline_gate_selection_outer_bound":{str(o):gate_selection_outer_bound(baseline,gate_unresolved_n,o) for o in OFFSETS},
      "historical_gl5v3":{"selected_return_intervals":{"1":[1.3672943342260926,1.4770342541250798],"5":[1.3960784104486632,1.4881408064429673],"15":[1.4771676388156305,1.489564076021217]},"rug_interval_1":[0,0.025],"alpha_interval_1":[0.8645812810831521,1.2521102848436372]},
      "conditional_alpha":{"identified":False,"reason":"point-in-time F selection is not identified for the forward tokens"},
      "verdict_candidate":verdict,"verdict_reason":"definite F-on forward population is below preregistered N and/or F-unresolved exceeds 20%","go_decision":"NOT_MADE","rpc":rpc_summary}
    LEGS.write_text(json.dumps({"rows":rows},indent=2,sort_keys=True,default=str)+"\n")
    OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"funnel":result["funnel"],"verdict":verdict,"rpc":rpc_summary},indent=2))


if __name__=="__main__": main()
