#!/usr/bin/env python3
"""Pinned read-only RPC evidence for the ten-conclusion seconds re-audit."""
from __future__ import annotations

import hashlib, importlib.util, json, os, subprocess, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path("/opt/EATLQ"); DIAG=ROOT/"mvp/diagnostics"
PREREG=ROOT/"configs/research/base_reaudit10_seconds_preregistration_20260715.json"
CORE_OUT=DIAG/"base_reaudit10_seconds_rpc_20260715.json"
B4_OUT=DIAG/"base_reaudit10_b4_health_rpc_20260715.json"
RUG_OUT=DIAG/"base_reaudit10_rug_five_evidence_rpc_20260715.json"
LEDGER=DIAG/"base_reaudit10_rpc_full_ledger_20260715.jsonl"
LEDGER_SUMMARY=DIAG/"base_reaudit10_rpc_ledger_summary_20260715.json"
SPEND=10**15; FROM="0x000000000000000000000000000000000000a10d"
OFFSETS=(1,3,5,10,15); AGE_BLOCKS={"2m":0,"5m":90,"10m":240}
WETH="0x4200000000000000000000000000000000000006"
TRANSFER_TOPIC="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ALLOWED={"eth_blockNumber","eth_getBlockByNumber","eth_getTransactionReceipt","eth_getLogs","eth_call","debug_traceTransaction"}
sys.path.insert(0,str(ROOT/"scripts/lib")); sys.path.insert(0,str(ROOT/"scripts/wallet_profile_v0"))
from ch_helper import ch_query  # noqa:E402
from v3_quoterv2_struct import QUOTER_V2_BASE,WETH_BASE,build_quote_exact_input_single_calldata  # noqa:E402

def chash(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

class Rpc:
    def __init__(self,url):
        self.url=url; self.lock=threading.Lock(); self.logical=0; self.attempts=0; self.methods=Counter()
        LEDGER.parent.mkdir(parents=True,exist_ok=True); LEDGER.write_text("")
    def _write(self,row):
        with self.lock:
            with LEDGER.open("a") as f: f.write(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n")
    def call(self,method,params,timeout=150):
        if method not in ALLOWED: raise RuntimeError(f"FORBIDDEN_RPC_METHOD:{method}")
        with self.lock: self.logical+=1; lid=self.logical
        rh=chash({"method":method,"params":params}); last=None
        for retry in range(1,4):
            with self.lock: self.attempts+=1; aid=self.attempts; self.methods[method]+=1
            t=time.time()
            try:
                body=json.dumps({"jsonrpc":"2.0","id":lid,"method":method,"params":params}).encode()
                req=Request(self.url,data=body,headers={"content-type":"application/json","user-agent":"EATLQ-reaudit10-readonly/1"})
                with urlopen(req,timeout=timeout) as resp: obj=json.load(resp)
                if obj.get("error"): raise RuntimeError(str(obj["error"]))
                result=obj.get("result"); sh=chash(result)
                self._write({"attempt_id":aid,"logical_id":lid,"method":method,"retry":retry,"request_sha256":rh,"response_sha256":sh,"status":"ok","elapsed_ms":round((time.time()-t)*1000,3)})
                return result,{"request_sha256":rh,"response_sha256":sh}
            except Exception as exc:
                last=exc; self._write({"attempt_id":aid,"logical_id":lid,"method":method,"retry":retry,"request_sha256":rh,"error_sha256":hashlib.sha256(str(exc).encode()).hexdigest(),"error_type":type(exc).__name__,"status":"error","elapsed_ms":round((time.time()-t)*1000,3)})
                time.sleep(.3*retry)
        raise RuntimeError(f"{type(last).__name__}:{str(last)[:240]}")
    def summary(self):
        return {"logical_calls":self.logical,"http_attempts":self.attempts,"estimated_credits":sum(n*(40 if m=="debug_traceTransaction" else 20) for m,n in self.methods.items()),"methods_attempted":dict(self.methods),"ledger_path":str(LEDGER.relative_to(ROOT)),"ledger_sha256":sha(LEDGER),"endpoint_id":"FORK_ARCHIVE_RPC_secret_omitted"}

def module(path,name):
    s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
def compile_bytecode():
    out=Path("/tmp/reaudit10forgeout"); art=out/"AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    if not art.exists(): subprocess.run([str(Path.home()/".foundry/bin/forge"),"build","--contracts","scripts/research/base_b4/AtomicRoundTrip.sol","--out",str(out),"--cache-path","/tmp/reaudit10forgecache"],cwd=ROOT,check=True)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")
def atomic_data(code,token,fee,spend=SPEND): return "0x"+code+token.removeprefix("0x").rjust(64,"0")+f"{fee:064x}"+f"{spend:064x}"
def decode_atomic(raw):
    if not raw or len(raw)<2+16*64: raise ValueError("short atomic response")
    w=[int(raw[2+i*64:2+(i+1)*64],16) for i in range(16)]
    keys=["flags","router_buy_out","target_before","target_after_buy","target_after_sell","router_sell_out","weth_after_deposit","weth_after_buy","weth_after_sell","pin_block","chain_id","parent_block_hash","weth_approve_error_hash","buy_error_hash","target_approve_error_hash","sell_error_hash"]
    return dict(zip(keys,w))
def safe(fn,label):
    try: return fn()
    except Exception as exc: return {"status":"unknown","where":label,"error":f"{type(exc).__name__}:{str(exc)[:220]}"}
def fee_at(rpc,pool,block):
    raw,meta=rpc.call("eth_call",[{"to":pool,"data":"0xddca3f43"},hex(block)]); return int(raw,16),meta
def block_at(rpc,block):
    raw,meta=rpc.call("eth_getBlockByNumber",[hex(block),False]); return {"status":"measured","number":block,"hash":raw.get("hash"),"parent_hash":raw.get("parentHash"),"timestamp":int(raw["timestamp"],16),**meta}
def atomic_at(rpc,code,token,fee,block):
    tx={"from":FROM,"data":atomic_data(code,token,fee),"value":hex(SPEND),"gas":hex(20_000_000)}
    raw,meta=rpc.call("eth_call",[tx,hex(block),{FROM:{"balance":hex(10**18)}}]); a=decode_atomic(raw)
    received=a["target_after_buy"]-a["target_before"]; buy=bool(a["flags"]&2) and received>0
    sell=bool(a["flags"]&8) and a["weth_after_sell"]>a["weth_after_buy"]; recovered=a["weth_after_sell"]-a["weth_after_buy"] if sell else 0
    return {"status":"measured","buy_ok":buy,"sell_ok":sell,"received_target":str(received),"recovered_weth":str(recovered),"recovery_ratio":recovered/SPEND if sell else 0.,"flags":a["flags"],"router_buy_out":str(a["router_buy_out"]),"router_sell_out":str(a["router_sell_out"]),"buy_integer_equal":bool(buy and a["router_buy_out"]==received),"sell_integer_equal":bool(sell and a["router_sell_out"]==recovered),"pin_block_returned":a["pin_block"],"parent_block_hash_returned":f"0x{a['parent_block_hash']:064x}",**meta}
def quote_at(rpc,token,fee,qty,block):
    data=build_quote_exact_input_single_calldata(token,WETH_BASE,qty,fee); raw,meta=rpc.call("eth_call",[{"to":QUOTER_V2_BASE,"data":data},hex(block)])
    amount=int(raw[2:66],16) if raw and len(raw)>=66 else 0; return {"status":"measured","amount_in":str(qty),"amount_out":str(amount),**meta}
def balance_at(rpc,token,owner,block):
    data="0x70a08231"+owner.removeprefix("0x").rjust(64,"0"); raw,meta=rpc.call("eth_call",[{"to":token,"data":data},hex(block)]); return {"status":"measured","raw":str(int(raw,16)),**meta}

def core_row(rpc,code,row):
    keys=("pool","token","orient","regime","signal_time","entry_block","trigger_block","trigger_source","selected")
    out={k:row[k] for k in keys}; out["legacy_real_entry"]=row.get("entry")
    fo=safe(lambda:(lambda x:{"status":"measured","fee":x[0],**x[1]})(fee_at(rpc,row["pool"],row["entry_block"])),"fee"); out["fee_evidence"]=fo
    if fo.get("status")!="measured": return out
    fee=fo["fee"]; out["entry_points"]=[]
    for off in OFFSETS:
        b=int(row["entry_block"])+off; out["entry_points"].append({"offset_blocks":off,"scheduled_block":b,"block":safe(lambda b=b:block_at(rpc,b),"entry_block"),"atomic":safe(lambda b=b:atomic_at(rpc,code,row["token"],fee,b),"entry_atomic")})
    qtys={str(p["offset_blocks"]):int(p["atomic"].get("received_target",0)) for p in out["entry_points"] if p["atomic"].get("buy_ok")}
    out["exit_points"]=[]
    for off in OFFSETS:
        b=int(row["trigger_block"])+off; p={"offset_blocks":off,"scheduled_block":b,"block":safe(lambda b=b:block_at(rpc,b),"exit_block"),"atomic_guard":safe(lambda b=b:atomic_at(rpc,code,row["token"],fee,b),"exit_atomic"),"exact_size_quotes_by_entry_offset":{}}
        for eo,qty in qtys.items(): p["exact_size_quotes_by_entry_offset"][eo]=safe(lambda q=qty,b=b:quote_at(rpc,row["token"],fee,q,b),"exit_quote")
        out["exit_points"].append(p)
    return out
def run_core(rpc,code):
    rows=module(ROOT/"scripts/research/base_b8/b8_rpc_evidence.py","b8_for_reaudit10").prepare_rows(); done=[]
    with ThreadPoolExecutor(max_workers=10) as ex:
        fs=[ex.submit(core_row,rpc,code,r) for r in rows]
        for i,f in enumerate(as_completed(fs),1):
            done.append(f.result())
            if i%20==0 or i==len(rows): print(f"[core] {i}/{len(rows)} logical={rpc.logical} attempts={rpc.attempts}",flush=True)
    done.sort(key=lambda x:x["pool"]); payload={"schema_version":1,"research_only":True,"posture":"pinned archive read-only; no send/sign/broadcast path","preregistration_sha256":sha(PREREG),"offset_blocks":list(OFFSETS),"summary":{"rows":len(done),"selected":sum(x["selected"] for x in done),"entry_atomic_measured":sum(p["atomic"].get("status")=="measured" for x in done for p in x.get("entry_points",[])),"exit_atomic_measured":sum(p["atomic_guard"].get("status")=="measured" for x in done for p in x.get("exit_points",[]))},"rows":done}
    CORE_OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n"); return payload

def b4_row(rpc,code,row):
    out={k:row[k] for k in ("pool_address","target_token","snapshot_120","window")}; out["pool_address"]=out["pool_address"].lower(); out["target_token"]=out["target_token"].lower()
    fo=safe(lambda:(lambda x:{"status":"measured","fee":x[0],**x[1]})(fee_at(rpc,out["pool_address"],int(out["snapshot_120"]))),"b4_fee"); out["fee_evidence"]=fo
    if fo.get("status")!="measured": return out
    fee=fo["fee"]; out["points"]=[]
    for age,extra in AGE_BLOCKS.items():
        b=int(out["snapshot_120"])+extra; a=safe(lambda b=b:atomic_at(rpc,code,out["target_token"],fee,b),"b4_atomic"); q={"status":"not_applicable"}
        if a.get("buy_ok") and int(a.get("received_target",0))>0: q=safe(lambda b=b,qty=int(a["received_target"]):quote_at(rpc,out["target_token"],fee,qty,b),"b4_qv2")
        out["points"].append({"age":age,"block":b,"block_evidence":safe(lambda b=b:block_at(rpc,b),"b4_block"),"atomic":a,"qv2_sell_exact_received":q})
    return out
def run_b4(rpc,code):
    q="""SELECT lower(u.pool_address) pool_address,lower(u.target_token) target_token,toUInt64(u.snapshot_120) snapshot_120,u.window FROM eatlq.base_pool_universe_b3 u FINAL INNER JOIN eatlq.base_pool_2min_snapshot_b3 f FINAL USING (chain_id,pool_address) WHERE f.fresh_class='fresh' AND lower(u.pool_type) LIKE '%v3%' ORDER BY u.window,u.pool_address"""
    universe=ch_query(q,timeout=900); rows=[]
    with ThreadPoolExecutor(max_workers=10) as ex:
        fs=[ex.submit(b4_row,rpc,code,r) for r in universe]
        for i,f in enumerate(as_completed(fs),1):
            rows.append(f.result())
            if i%50==0 or i==len(universe): print(f"[B4] {i}/{len(universe)} logical={rpc.logical} attempts={rpc.attempts}",flush=True)
    rows.sort(key=lambda x:(x["window"],x["pool_address"])); payload={"schema_version":1,"research_only":True,"posture":"full fresh Base V3 census; pinned read-only calls","preregistration_sha256":sha(PREREG),"ages":AGE_BLOCKS,"summary":{"rows":len(rows),"windows":dict(Counter(x["window"] for x in rows))},"rows":rows}; B4_OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n"); return payload

def rug_labels():
    rows={}
    for path in sorted((ROOT/"scripts/research/base_lefttail/_uni_shards").glob("_uni_s*.json")):
        for r in json.loads(path.read_text()).get("pools",[]):
            if r.get("label")=="delayed_rug": rows[r["pool"].lower()]=r
    return sorted(rows.values(),key=lambda x:x["pool"])
def logval(log):
    try: return int(log.get("data","0x0"),16)
    except Exception: return 0
def logs_at(rpc,address,block,topics=None,to_block=None):
    f={"fromBlock":hex(block),"toBlock":hex(block if to_block is None else to_block),"address":address}
    if topics is not None: f["topics"]=topics
    raw,meta=rpc.call("eth_getLogs",[f]); return {"status":"measured","logs":raw,**meta}
def rug_row(rpc,code,label):
    pool=label["pool"].lower(); token=label["token"].lower(); flip=int(label["flip_block"]); out={"pool":pool,"token":token,"flip_block":flip,"label_source":label}
    fo=safe(lambda:(lambda x:{"status":"measured","fee":x[0],**x[1]})(fee_at(rpc,pool,max(1,flip-1))),"rug_fee"); out["fee_evidence"]=fo
    if fo.get("status")!="measured": return out
    fee=fo["fee"]; out["points"]=[]
    for d in (-1,0,1,3,5,10,15):
        b=flip+d; out["points"].append({"delta_blocks":d,"block":b,"block_evidence":safe(lambda b=b:block_at(rpc,b),"rug_block"),"pool_weth_balance":safe(lambda b=b:balance_at(rpc,WETH,pool,b),"rug_depth"),"atomic":safe(lambda b=b:atomic_at(rpc,code,token,fee,b),"rug_atomic")})
    topic_pool="0x"+"0"*24+pool[2:]; interval=label.get("flip_interval") or [flip-1,flip]; begin=max(0,int(interval[0])+1); end=flip
    pl=safe(lambda:logs_at(rpc,pool,begin,to_block=end),"rug_pool_logs"); wo=safe(lambda:logs_at(rpc,WETH,begin,[TRANSFER_TOPIC,topic_pool],end),"rug_weth_out"); wi=safe(lambda:logs_at(rpc,WETH,begin,[TRANSFER_TOPIC,None,topic_pool],end),"rug_weth_in"); tl=safe(lambda:logs_at(rpc,token,begin,[TRANSFER_TOPIC],end),"rug_token_logs")
    out["event_evidence"]={"pool_logs":pl,"weth_out_logs":wo,"weth_in_logs":wi,"token_transfer_logs":tl}; cand=[]
    for src,obj in (("weth_out",wo),("pool",pl),("token",tl)):
        for log in obj.get("logs",[]): cand.append((src,logval(log),log.get("transactionHash")))
    cand=[x for x in cand if x[2]]; cand.sort(key=lambda x:(x[0]!="weth_out",-x[1],x[2])); tx=cand[0][2] if cand else None; out["candidate_search_range"]=[begin,end]; out["candidate_cause_tx"]=tx
    if tx:
        out["candidate_receipt"]=safe(lambda:(lambda x:{"status":"measured","receipt":x[0],**x[1]})(rpc.call("eth_getTransactionReceipt",[tx])),"rug_receipt")
        out["candidate_trace"]=safe(lambda:(lambda x:{"status":"measured","trace_sha256":chash(x[0]),"trace_type":type(x[0]).__name__,**x[1]})(rpc.call("debug_traceTransaction",[tx,{"tracer":"callTracer","timeout":"30s"}],timeout=180)),"rug_trace")
        rc=out["candidate_receipt"].get("receipt") or {}; cause_block=int(rc.get("blockNumber","0x0"),16) if rc else 0; out["candidate_cause_block"]=cause_block or None; out["cause_points"]=[]
        if cause_block:
            for d in (-1,0,1,3,5,10,15):
                b=cause_block+d; out["cause_points"].append({"delta_blocks":d,"block":b,"block_evidence":safe(lambda b=b:block_at(rpc,b),"cause_block"),"pool_weth_balance":safe(lambda b=b:balance_at(rpc,WETH,pool,b),"cause_depth"),"atomic":safe(lambda b=b:atomic_at(rpc,code,token,fee,b),"cause_atomic")})
    else: out["candidate_receipt"]={"status":"unknown","reason":"no_candidate_tx"}; out["candidate_trace"]={"status":"unknown","reason":"no_candidate_tx"}
    return out
def run_rugs(rpc,code):
    labels=rug_labels(); rows=[]
    with ThreadPoolExecutor(max_workers=6) as ex:
        fs=[ex.submit(rug_row,rpc,code,r) for r in labels]
        for i,f in enumerate(as_completed(fs),1):
            rows.append(f.result())
            if i%20==0 or i==len(labels): print(f"[rugs] {i}/{len(labels)} logical={rpc.logical} attempts={rpc.attempts}",flush=True)
    rows.sort(key=lambda x:x["pool"]); payload={"schema_version":1,"research_only":True,"posture":"five-source pinned rug mechanics; read-only","preregistration_sha256":sha(PREREG),"summary":{"delayed_rug_rows":len(rows)},"rows":rows}; RUG_OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n"); return payload

def main():
    url=os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
    if not url: raise RuntimeError("FORK_ARCHIVE_RPC or LABEL_RPC_URL required")
    rpc=Rpc(url); head,hm=rpc.call("eth_blockNumber",[]); print(json.dumps({"posture":"READ_ONLY_NO_SEND_SIGN_BROADCAST","executor_strategy_containers":"verified_absent_before_run","preregistration_sha256":sha(PREREG),"archive_head":int(head,16),"archive_head_response_sha256":hm["response_sha256"]},sort_keys=True),flush=True)
    code=compile_bytecode(); core=run_core(rpc,code); b4=run_b4(rpc,code); rugs=run_rugs(rpc,code)
    result={"schema_version":1,"research_only":True,"preregistration_sha256":sha(PREREG),"archive_head_at_start":int(head,16),"archive_head_response_sha256":hm["response_sha256"],"outputs":{str(CORE_OUT.relative_to(ROOT)):sha(CORE_OUT),str(B4_OUT.relative_to(ROOT)):sha(B4_OUT),str(RUG_OUT.relative_to(ROOT)):sha(RUG_OUT)},"row_counts":{"core":len(core["rows"]),"b4":len(b4["rows"]),"rugs":len(rugs["rows"])},"rpc":rpc.summary()}; LEDGER_SUMMARY.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n"); print(json.dumps(result,indent=2,sort_keys=True))
if __name__=="__main__": main()
