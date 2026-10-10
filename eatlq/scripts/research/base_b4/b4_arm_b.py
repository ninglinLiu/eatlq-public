#!/usr/bin/env python3
"""B4 v2 arm B. Pinned archive eth_call only; no transaction/signing code."""
from __future__ import annotations

import hashlib, json, math, os, subprocess, sys, threading, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/wallet_profile_v0"))
sys.path.insert(0, str(ROOT / "scripts/lib"))
from ch_helper import ch_query
from v3_quoterv2_struct import build_quote_exact_input_single_calldata, QUOTER_V2_BASE, WETH_BASE

OUT = ROOT / "mvp/diagnostics/base_b4_arm_b_20260714.json"
SEED = "B4v2-armB-instant-hp-20260714-v1"
SPEND = 10**15
SAMPLE_N = {"W1": 120, "W2": 124, "W3": 140}
FROM = "0x000000000000000000000000000000000000b405"
FEE_SELECTOR = "0xddca3f43"
BALANCE_OF = "70a08231"
ALLOWED = {"eth_call", "eth_blockNumber"}


def wilson(k, n):
    if not n: return [None, None]
    z=1.96; p=k/n; d=1+z*z/n
    c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [c-h,c+h]


class Rpc:
    def __init__(self, url):
        self.url=url; self.lock=threading.Lock(); self.calls=0; self.attempts=0
    def call(self, method, params, timeout=120):
        if method not in ALLOWED: raise RuntimeError("RPC method forbidden")
        with self.lock: self.calls += 1; ident=self.calls
        last=None
        for retry in range(3):
            with self.lock: self.attempts += 1
            body=json.dumps({"jsonrpc":"2.0","id":ident,"method":method,"params":params}).encode()
            try:
                req=Request(self.url,data=body,headers={"content-type":"application/json","user-agent":"EATLQ-B4-readonly/1"})
                with urlopen(req,timeout=timeout) as resp: obj=json.load(resp)
                if obj.get("error"): raise RuntimeError(str(obj["error"]))
                raw=obj.get("result")
                return raw, hashlib.sha256(json.dumps(params,separators=(",",":"),sort_keys=True).encode()).hexdigest(), hashlib.sha256(str(raw).encode()).hexdigest()
            except Exception as exc:
                last=exc; time.sleep(.3*(retry+1))
        raise RuntimeError(str(last)[:300])


def compile_creation_bytecode():
    out=Path("/tmp/b4forgeout")
    art=out/"AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run([str(Path.home()/".foundry/bin/forge"),"build","--contracts",
            "scripts/research/base_b4/AtomicRoundTrip.sol","--out",str(out),"--cache-path","/tmp/b4forgecache"],cwd=ROOT,check=True)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_calldata(bytecode, token, fee):
    return "0x"+bytecode+token.lower().removeprefix("0x").rjust(64,"0")+f"{fee:064x}"+f"{SPEND:064x}"


def decode_atomic(raw):
    if not raw or len(raw)<2+16*64: raise ValueError("short atomic return")
    w=[int(raw[2+i*64:2+(i+1)*64],16) for i in range(16)]
    keys=["flags","router_buy_out","target_before","target_after_buy","target_after_sell","router_sell_out",
          "weth_after_deposit","weth_after_buy","weth_after_sell","pin_block","chain_id","parent_block_hash",
          "weth_approve_error_hash","buy_error_hash","target_approve_error_hash","sell_error_hash"]
    return dict(zip(keys,w))


def probe(rpc, bytecode, row):
    pool=row["pool_address"].lower(); token=row["target_token"].lower(); block=int(row["snapshot_120"])
    out={**row,"pool_address":pool,"target_token":token,"block":block}
    try:
        fee_raw,fh,frh=rpc.call("eth_call",[{"to":pool,"data":FEE_SELECTOR},hex(block)])
        fee=int(fee_raw,16); out.update(fee=fee,fee_request_sha256=fh,fee_response_sha256=frh)
        tx={"from":FROM,"data":atomic_calldata(bytecode,token,fee),"value":hex(SPEND),"gas":hex(20_000_000)}
        raw,ah,arh=rpc.call("eth_call",[tx,hex(block),{FROM:{"balance":hex(10**18)}}])
        a=decode_atomic(raw); out.update(atomic=a,atomic_request_sha256=ah,atomic_response_sha256=arh)
        received=a["target_after_buy"]-a["target_before"]
        buy_ok=bool(a["flags"]&2) and received>0
        sell_ok=bool(a["flags"]&8) and a["weth_after_sell"]>a["weth_after_buy"]
        recovered=a["weth_after_sell"]-a["weth_after_buy"] if sell_ok else 0
        severe=buy_ok and (not sell_ok or recovered/SPEND<=.10)
        out.update(received_target=received,buy_ok=buy_ok,sell_ok=sell_ok,recovered_weth=recovered,
                   roundtrip_tax=(1-recovered/SPEND) if sell_ok else 1.0 if buy_ok else None,
                   instant_hp=bool(buy_ok and not sell_ok),severe_entry_adverse=severe,status="measured")
        if received>0:
            qdata=build_quote_exact_input_single_calldata(token,WETH_BASE,received,fee)
            qraw,qh,qrh=rpc.call("eth_call",[{"to":QUOTER_V2_BASE,"data":qdata},hex(block)])
            qout=int(qraw[2:66],16) if qraw and len(qraw)>=66 else 0
            out.update(qv2_amount_out=qout,qv2_request_sha256=qh,qv2_response_sha256=qrh,
                       qv2_reasonable=qout/SPEND>=.90,
                       preregistered_disagreement=bool(qout/SPEND>=.90 and severe))
        else:
            out.update(qv2_amount_out=None,qv2_reasonable=False,preregistered_disagreement=False)
        if sell_ok:
            out["sell_integer_equal"] = a["router_sell_out"] == recovered
        if buy_ok:
            out["standard_buy_integer_equal"] = a["router_buy_out"] == received
    except Exception as exc:
        out.update(status="unknown_adverse",error=f"{type(exc).__name__}:{str(exc)[:240]}",instant_hp=False,
                   severe_entry_adverse=True,preregistered_disagreement=False)
    return out


def balance_of(rpc, token, owner, block):
    data="0x"+BALANCE_OF+owner.lower().removeprefix("0x").rjust(64,"0")
    raw,h,rh=rpc.call("eth_call",[{"to":token,"data":data},hex(block)])
    return int(raw,16),h,rh


def summarize(rows):
    def one(xs):
        n=len(xs); hp=sum(x.get("instant_hp",False) for x in xs); severe=sum(x.get("severe_entry_adverse",False) for x in xs)
        unknown=sum(x["status"]!="measured" for x in xs); buy=sum(x.get("buy_ok",False) for x in xs)
        dis=sum(x.get("preregistered_disagreement",False) for x in xs)
        return {"n":n,"measured":n-unknown,"unknown_adverse":unknown,"buyable":buy,"instant_hp":hp,
                "instant_hp_rate":hp/n,"instant_hp_wilson95":wilson(hp,n),
                "adverse_identification_interval":[hp/n,(hp+unknown)/n],
                "severe_entry_adverse":severe,"preregistered_disagreements":dis,
                "H0a_case_n":hp,"H0a_disagreement_wilson95":wilson(dis,hp),
                "H0a_disagreement_identification_interval":[dis/hp if hp else None,
                    (dis+unknown)/(hp+unknown) if hp+unknown else None]}
    return {"pooled":one(rows),"windows":{w:one([r for r in rows if r["window"]==w]) for w in SAMPLE_N}}


def main():
    url=os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
    if not url: raise RuntimeError("FORK_ARCHIVE_RPC/LABEL_RPC_URL required")
    rpc=Rpc(url); head,_,head_hash=rpc.call("eth_blockNumber",[])
    bytecode=compile_creation_bytecode()
    q="""SELECT lower(u.pool_address) pool_address,lower(u.target_token) target_token,toUInt64(u.snapshot_120) snapshot_120,u.window
      FROM eatlq.base_pool_universe_b3 u FINAL INNER JOIN eatlq.base_pool_2min_snapshot_b3 f FINAL USING (chain_id,pool_address)
      WHERE f.fresh_class='fresh' AND lower(u.pool_type) LIKE '%v3%'"""
    universe=ch_query(q); strata=defaultdict(list)
    for r in universe:
        r["sample_key"]=hashlib.sha256(f"{SEED}|{r['pool_address'].lower()}".encode()).hexdigest(); strata[r["window"]].append(r)
    full_census = os.environ.get("B4_FULL_CENSUS") == "1"
    sample=[]
    for w,n in SAMPLE_N.items():
        ordered=sorted(strata[w],key=lambda x:x["sample_key"])
        sample += ordered if full_census else ordered[:n]
    rows=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        fs={ex.submit(probe,rpc,bytecode,r):r for r in sample}
        for f in as_completed(fs): rows.append(f.result())
    rows.sort(key=lambda x:(x["window"],x["sample_key"]))
    flips=[
      {"name":"transfer_toggle_known_missed","pool":"0x75d2c083d267fd7feebe1e78babcc7478c9b226b","token":"0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb","fee":10000,"pre":45000592,"post":45007796},
      {"name":"lp_pull_PARADAENGI_1","pool":"0x7f72018c642630a998e9d63b87d356d7fc63d93d","token":"0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38","fee":100,"pre":47699311,"post":47699312},]
    flip_rows=[]
    for case in flips:
        c={**case,"points":[]}
        for side in ("pre","post"):
            b=case[side]; r=probe(rpc,bytecode,{"pool_address":case["pool"],"target_token":case["token"],"snapshot_120":b,"window":"golden"})
            depth,dh,drh=balance_of(rpc,WETH_BASE,case["pool"],b); r.update(pool_weth_raw=depth,depth_request_sha256=dh,depth_response_sha256=drh,side=side)
            c["points"].append(r)
        flip_rows.append(c)
    clean=[r for r in rows if r.get("status")=="measured" and r.get("buy_ok") and r.get("sell_ok") and r.get("standard_buy_integer_equal")][:2]
    golden=[]
    for r in clean:
        repeat=probe(rpc,bytecode,{k:r[k] for k in ("pool_address","target_token","snapshot_120","window","sample_key")})
        golden.append({"pool":r["pool_address"],"block":r["block"],"first_atomic_response_sha256":r["atomic_response_sha256"],
                       "repeat_atomic_response_sha256":repeat.get("atomic_response_sha256"),
                       "raw_response_equal":r["atomic_response_sha256"]==repeat.get("atomic_response_sha256"),
                       "router_buy_out":r["atomic"]["router_buy_out"],"received_target":r["received_target"],
                       "buy_integer_equal":r["atomic"]["router_buy_out"]==r["received_target"],
                       "router_sell_out":r["atomic"]["router_sell_out"],"recovered_weth":r["recovered_weth"],
                       "sell_integer_equal":r["atomic"]["router_sell_out"]==r["recovered_weth"]})
    result={"schema_version":1,"posture":"archive pinned eth_call only; no send/sign/broadcast",
      "rpc":{"endpoint_id":"POOL_DISCOVERY_RPC_URL/FORK_ARCHIVE_RPC (secret omitted)","head":head,"head_response_sha256":head_hash,
             "logical_calls":rpc.calls,"attempts":rpc.attempts},
      "estimand":{"all_fresh":3302,"fresh_v3":len(universe),"fresh_v2_not_applicable_to_QuoterV2":3302-len(universe),"sample_n":len(sample),"seed":SEED,"initial_strata":SAMPLE_N,"full_census":full_census,
                   "adaptive_rule":"expand to all fresh V3 iff initial unknown>5% or any window measured<75",
                   "expansion_triggered_by_initial":"unknown=64/384>5%" if full_census else None,
                   "probe_block":"snapshot_120","spend_weth_raw":SPEND},
      "summary":summarize(rows),"rows":rows,"flip_before_after":flip_rows,"golden":golden}
    ps=result["summary"]["pooled"]; hp=ps["instant_hp"]
    if ps["unknown_adverse"]:
        result["H0a_status"]="INCONCLUSIVE_UNKNOWN_ADVERSE"
    elif hp==0:
        result["H0a_status"]="INCONCLUSIVE_NO_DISEASE_CASES"
    else:
        result["H0a_status"]="REJECT_H0a" if ps["H0a_disagreement_wilson95"][0]>.05 else "DO_NOT_REJECT_H0a"
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"output":str(OUT),"rpc":result["rpc"],"estimand":result["estimand"],"summary":result["summary"],"H0a_status":result["H0a_status"]},indent=2))

if __name__=="__main__": main()
