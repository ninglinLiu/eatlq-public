#!/usr/bin/env python3
"""GL7 four-dimensional wallet/size sellability falsification."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT=Path("/opt/EATLQ")
sys.path.insert(0,str(ROOT/"scripts/research"))
from gl_readonly_rpc import Rpc, atomic_probe, balance_of, compile_atomic_bytecode, WETH  # noqa

PREREG=ROOT/"reports/GL7_preregistration_20260716.md"
PREREG_SHA="60e2387f58b84661aba10cfc585ccbf7fe4f1d7087b0f395b7cfe8f09d7a8edf"
V3=ROOT/"mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
GL4=ROOT/"mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json"
PC=ROOT/"reports/GL7_positive_control_207_frozen_20260716.json"
LEDGER=ROOT/"mvp/diagnostics/base_gl7_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM=ROOT/"mvp/diagnostics/base_gl7_rpc_ledger_20260716.json"
OUT=ROOT/"mvp/diagnostics/base_gl7_wallet_size_falsification_20260716.json"
LEGS=ROOT/"mvp/diagnostics/base_gl7_wallet_size_per_leg_20260716.json"
SPEND=30_200_000_000_000_000
SIZES={"1x":1.0,"1.87x":1.87,"5x":5.0,"14x":14.0}
SINK="0x000000000000000000000000000000000000dead"


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def holder_candidates(pools):
    out=defaultdict(list)
    for i in range(0,len(pools),150):
        ins=",".join(f"'{p}'" for p in pools[i:i+150])
        sql=f"""SELECT pool_address,recipient,sender,max(block_number) b FROM eatlq.base_v3_wallet_swaps_resolved
WHERE lower(pool_address) IN ({ins}) GROUP BY pool_address,recipient,sender ORDER BY pool_address,b DESC FORMAT TabSeparated"""
        r=subprocess.run(["docker","exec","eatlq-clickhouse-1","clickhouse-client","--query",sql],capture_output=True,text=True,timeout=300)
        if r.returncode: continue
        for ln in r.stdout.splitlines():
            p,rcp,snd,_=ln.split("\t")
            for a in (rcp.lower(),snd.lower()):
                if a.startswith("0x") and len(a)==42 and a not in out[p.lower()] and len(out[p.lower()])<12: out[p.lower()].append(a)
    return out


def transfer_probe(rpc,token,holder,qty,block):
    bal=balance_of(rpc,token,holder,block)
    if not bal.get("ok") or int(bal.get("value_raw") or 0)<qty or qty<=0:
        return {"evaluable":False,"reason":"holder_balance_insufficient","holder":holder,"balance":bal}
    data="0xa9059cbb"+SINK[2:].rjust(64,"0")+f"{qty:064x}"
    try:
        raw,qh,rh=rpc.call("eth_call",[{"from":holder,"to":token,"data":data},hex(block)])
        accepted=(raw=="0x") or int(raw,16)!=0
        return {"evaluable":True,"transfer_ok":accepted,"raw_no_return":raw=="0x","holder":holder,
                "qty_raw":str(qty),"request_sha256":qh,"response_sha256":rh,
                "arrival_amount_verified":False,"caveat":"eth_call success exercises holder transfer logic but cannot persist sink balance"}
    except Exception as exc:
        err=str(exc)
        if err=="rpc_revert":
            return {"evaluable":True,"transfer_ok":False,"holder":holder,"qty_raw":str(qty),"error_class":err}
        return {"evaluable":False,"transfer_ok":None,"holder":holder,"qty_raw":str(qty),
                "reason":"rpc_unresolved","error_class":err}


def holder_view(rpc,token,block,qty,cands):
    tried=[]
    for h in cands[:12]:
        p=transfer_probe(rpc,token,h,qty,block);tried.append(p)
        if p.get("evaluable"): return {**p,"candidates_tried":len(tried)}
    return {"evaluable":False,"transfer_ok":None,"reason":"no_archive_holder_with_sufficient_balance","candidates_tried":len(tried)}


def selected_probe(rpc,bytecode,row,cands):
    out={k:row[k] for k in ("pool","token","entry_block","signal_time")};out["offsets"]={}
    fee=(row.get("fee_evidence") or {}).get("fee")
    for off in (1,5,15):
        old=row["arm1"][str(off)];block=int(old["target_block"]);arms={}
        for name,m in SIZES.items(): arms[name]=atomic_probe(rpc,bytecode,row["token"],fee,int(SPEND*m),block) if fee else {"ok":False,"error_class":"fee_missing"}
        q=((old.get("archive") or {}).get("exact_size_quote") or {}).get("amount_in_token_raw")
        hv=holder_view(rpc,row["token"],block,int(q or 0),cands.get(row["pool"],[]))
        entry=int((row.get("entry_weth_evidence") or {}).get("value_raw") or 0)
        tb=balance_of(rpc,WETH,row["pool"],block);lag=balance_of(rpc,WETH,row["pool"],block+15)
        ratio=int(tb["value_raw"])/entry if entry and tb.get("ok") else None; lr=int(lag["value_raw"])/entry if entry and lag.get("ok") else None
        collapse=ratio is not None and lr is not None and ratio<=.05 and lr<=.05
        out["offsets"][str(off)]={"block":block,"reserve_ratio":ratio,"lag_ratio":lr,"confirmed_collapse":collapse,
            "size_atomic":arms,"new_address_hook_candidate":bool(arms["1x"].get("buy_ok") and not arms["1x"].get("sell_ok")),"holder_view":hv}
    return out


def pc_probe(rpc,bytecode,row,flip,cands):
    fee=(row.get("fee_evidence") or {}).get("fee");entry=int((row.get("entry_weth_evidence") or {}).get("value_raw") or 0)
    tb=balance_of(rpc,WETH,row["pool"],flip);lag=balance_of(rpc,WETH,row["pool"],flip+15)
    ratio=int(tb["value_raw"])/entry if entry and tb.get("ok") else None;lr=int(lag["value_raw"])/entry if entry and lag.get("ok") else None
    one=atomic_probe(rpc,bytecode,row["token"],fee,SPEND,flip) if fee else {"ok":False};big=atomic_probe(rpc,bytecode,row["token"],fee,int(SPEND*14),flip) if fee else {"ok":False}
    # Frozen 1x counterfactual quantity from any GL5v3 exact-size leg.
    qty=0
    for o in ("1","5","15"):
        qty=int((((row["arm1"][o].get("archive") or {}).get("exact_size_quote") or {}).get("amount_in_token_raw") or 0))
        if qty: break
    hv=holder_view(rpc,row["token"],flip,qty,cands.get(row["pool"],[]))
    collapse=ratio is not None and lr is not None and ratio<=.05 and lr<=.05
    evaluable=collapse or one.get("ok") or big.get("ok") or hv.get("evaluable")
    positive=collapse or (one.get("ok") and (not one.get("buy_ok") or not one.get("sell_ok"))) or (big.get("ok") and (not big.get("buy_ok") or not big.get("sell_ok"))) or (hv.get("evaluable") and not hv.get("transfer_ok"))
    return {"pool":row["pool"],"token":row["token"],"flip_block":flip,"reserve_ratio":ratio,"lag_ratio":lr,
            "confirmed_collapse":collapse,"atomic_1x":one,"atomic_14x":big,"holder_view":hv,"evaluable":evaluable,"positive":positive}


def actual_sell_A(rpc,gl4):
    pos={p["trade_name"]:p for p in gl4["open_positions"]};rows=[]
    txs=[t for t in gl4["transactions"] if t.get("classification")=="FAILED_SELL_ATTEMPT_NON_FILL" or (t.get("classification")=="REAL_SUCCESSFUL_FILL" and t.get("side")=="SELL")]
    for t in txs:
        r={k:t.get(k) for k in ("tx_hash","block","timestamp_utc","wallet","wallet_label","classification","receipt_status","segment","side","trade_name","linked_trade")}
        if t["classification"]=="REAL_SUCCESSFUL_FILL": r.update(success=True,cause="CANONICAL_FILL")
        else:
            p=pos.get(t.get("linked_trade"));ratio=lagratio=None
            if p:
                entry=int(p["pool_weth_entry_raw"]);b=balance_of(rpc,WETH,p["pool"],int(t["block"]));l=balance_of(rpc,WETH,p["pool"],int(t["block"])+15)
                ratio=int(b["value_raw"])/entry if b.get("ok") and entry else None;lagratio=int(l["value_raw"])/entry if l.get("ok") and entry else None
            collapse=ratio is not None and lagratio is not None and ratio<=.05 and lagratio<=.05
            r.update(success=False,pool=p.get("pool") if p else None,reserve_ratio=ratio,lag_ratio=lagratio,
                     cause="POOL_COLLAPSE" if collapse else "UNRESOLVED_FAILURE",confirmed_wallet_hook=False)
        rows.append(r)
    return rows


def wilson_lower(k,n,z=1.959963984540054):
    if not n:return 0.0
    p=k/n;return (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def main():
    assert sha(PREREG)==PREREG_SHA
    allrows=json.loads(V3.read_text())["rows"];selected=[r for r in allrows if r["selected_Fon"]]
    pc=json.loads(PC.read_text());assert pc["expected_n"]==len(pc["pools"])==207 and len(set(pc["pools"]))==207
    bypool={r["pool"]:r for r in allrows};assert set(pc["pools"])<=set(bypool)
    labels=json.loads((ROOT/"scripts/research/base_lefttail/_labeled_universe.json").read_text())
    flip={r["pool"]:int(r["flip_block"]) for r in labels["rug_pools"]}
    pools=sorted(set([r["pool"] for r in selected]+pc["pools"]));cands=holder_candidates(pools)
    rpc=Rpc(LEDGER,user_agent="EATLQ-GL7-readonly/1");bytecode=compile_atomic_bytecode()
    sel=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut=[ex.submit(selected_probe,rpc,bytecode,r,cands) for r in selected]
        for f in as_completed(fut):sel.append(f.result())
    controls=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        fut=[ex.submit(pc_probe,rpc,bytecode,bypool[p],flip[p],cands) for p in pc["pools"]]
        for f in as_completed(fut):controls.append(f.result())
    A=actual_sell_A(rpc,json.loads(GL4.read_text()))
    # Aggregate B/C over selected offset legs.
    b={};material_size=False
    for name in SIZES:
        ev=fail=unres=0
        for r in sel:
            for x in r["offsets"].values():
                if x["confirmed_collapse"]:continue
                a=x["size_atomic"][name]
                if a.get("ok"):ev+=1;fail+=not(a.get("buy_ok") and a.get("sell_ok"))
                else:unres+=1
        b[name]={"evaluable_n":ev,"failure_n":fail,"failure_rate":fail/ev if ev else None,"unresolved_n":unres}
        if name!="1x" and ev and (fail/ev>.02 or fail>=2):material_size=True
    holder_ev=holder_fail=0
    new_ev=new_fail=0
    for r in sel:
        for x in r["offsets"].values():
            if x["confirmed_collapse"]:continue
            a=x["size_atomic"]["1x"]
            if a.get("ok") and a.get("buy_ok"):new_ev+=1;new_fail+=not a.get("sell_ok")
            h=x["holder_view"]
            if h.get("evaluable"):holder_ev+=1;holder_fail+=not h.get("transfer_ok")
    c={"new_address":{"evaluable_n":new_ev,"hook_n":new_fail},"historical_holder":{"evaluable_n":holder_ev,"hook_n":holder_fail},
       "strategy_wallet_exact": {"evaluable_n":0,"reason":"strategy wallets did not hold counterfactual F-on tokens; token storage was not forged"}}
    material_hook=(new_ev and (new_fail/new_ev>.01 or new_fail>=1)) or (holder_ev and (holder_fail/holder_ev>.01 or holder_fail>=1))
    pc_ev=sum(x["evaluable"] for x in controls);pc_pos=sum(x["positive"] for x in controls if x["evaluable"]);rate=pc_pos/pc_ev if pc_ev else 0
    pc_valid=pc_ev/207>=.9 and rate>=.8 and wilson_lower(pc_pos,pc_ev)>=.75
    failed=[r for r in A if not r["success"]];succ=[r for r in A if r["success"]]
    natural=[r for r in A if "smoke" not in str(r.get("segment") or "")]
    awallet=sum(r.get("confirmed_wallet_hook",False) for r in A)
    # Holder and exact strategy-wallet coverage are necessarily unresolved, so four dimensions cannot all pass.
    if not pc_valid: verdict="PROBE_INVALID"
    elif awallet or material_size or material_hook: verdict="WALLET_SIZE_RISK_CONFIRMED"
    else: verdict="INCONCLUSIVE_WALLET_LEVEL"
    rs=rpc.summary();LEDGER_SUM.write_text(json.dumps(rs,indent=2,sort_keys=True)+"\n")
    result={"schema_version":1,"task_id":"GL7","preregistration_sha256":PREREG_SHA,"research_only":True,
      "dimension_A":{"all":{"attempt_n":len(A),"success_n":len(succ),"success_rate":len(succ)/len(A)},
        "natural_ex_smoke":{"attempt_n":len(natural),"success_n":sum(r["success"] for r in natural),"success_rate":sum(r["success"] for r in natural)/len(natural)},
        "failure_causes":dict(Counter(r.get("cause") for r in failed)),"confirmed_wallet_hook_n":awallet},
      "dimension_B":{"sizes":b,"material_size_failure":bool(material_size)},"dimension_C":{**c,"material_hook":bool(material_hook)},
      "dimension_D":{"expected_n":207,"evaluable_n":pc_ev,"positive_n":pc_pos,"evaluable_rate":pc_ev/207,"positive_rate":rate,
        "wilson_95_lower":wilson_lower(pc_pos,pc_ev),"probe_valid":pc_valid},
      "verdict_candidate":verdict,"boundary":"Pool/new-address/third-party-holder eth_call does not prove the strategy wallet can sell; only a separately authorized tiny real trade can close that gap.",
      "go_decision":"NOT_MADE","rpc":rs}
    LEGS.write_text(json.dumps({"dimension_A_attempts":A,"selected_120":sorted(sel,key=lambda x:x["pool"]),"positive_control_207":sorted(controls,key=lambda x:x["pool"])},indent=2,sort_keys=True)+"\n")
    OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"A":result["dimension_A"],"B":b,"C":c,"D":result["dimension_D"],"verdict":verdict,"rpc":rs},indent=2))


if __name__=="__main__":main()
