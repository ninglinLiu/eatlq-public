#!/usr/bin/env python3
"""B3 64-opportunity seconds repricing; pinned read-only calls only."""
import importlib.util,json,os,sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
ROOT=Path("/opt/EATLQ"); DIAG=ROOT/"mvp/diagnostics"
BASE=ROOT/"scripts/research/base_reaudit10/reaudit10_rpc.py"; OUT=DIAG/"base_reaudit10_b3_seconds_rpc_20260715.json"; LEDGER=DIAG/"base_reaudit10_b3_rpc_full_ledger_20260715.jsonl"; OFFSETS=(1,3,5,10,15)
spec=importlib.util.spec_from_file_location("r10rpc_b3",BASE); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.LEDGER=LEDGER
def safe(fn,label):
 try:return fn()
 except Exception as e:return {"status":"unknown","where":label,"error":f"{type(e).__name__}:{str(e)[:220]}"}
def work(rpc,code,opp,meta):
 pool=opp["pool"].lower(); row={"pool":pool,"real":opp["real"],"proxy":opp["proxy"],"window":opp["window"],"frozen_ladder":opp["ladder"]}
 md=meta.get(pool); row["metadata"]=md
 if not md or "v3" not in md["pool_type"].lower():row["status"]="not_v3_atomic_not_applicable";return row
 entry=opp["ladder"].get("entry"); exits=opp["ladder"].get("exits",[])
 if not entry or not exits:row["status"]="missing_frozen_entry_or_exit";return row
 eb=int(entry["block_number"]); xb=int(exits[0]["block_number"]); token=md["target_token"].lower(); fo=safe(lambda:(lambda x:{"status":"measured","fee":x[0],**x[1]})(m.fee_at(rpc,pool,eb)),"fee");row["fee_evidence"]=fo
 if fo.get("status")!="measured":return row
 fee=fo["fee"]; ib=eb+1; ia=safe(lambda:m.atomic_at(rpc,code,token,fee,ib),"entry_atomic"); row["entry_point"]={"offset_blocks":1,"block":ib,"block_evidence":safe(lambda:m.block_at(rpc,ib),"entry_block"),"atomic":ia};qty=int(ia.get("received_target",0));row["exit_points"]=[]
 for off in OFFSETS:
  b=xb+off;a=safe(lambda b=b:m.atomic_at(rpc,code,token,fee,b),"exit_atomic");q={"status":"not_applicable"}
  if qty>0:q=safe(lambda b=b:m.quote_at(rpc,token,fee,qty,b),"exit_quote")
  row["exit_points"].append({"offset_blocks":off,"block":b,"block_evidence":safe(lambda b=b:m.block_at(rpc,b),"exit_block"),"atomic_guard":a,"exact_size_quote":q})
 row["status"]="measured";return row
def main():
 url=os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
 if not url:raise RuntimeError("FORK_ARCHIVE_RPC required")
 base=json.loads((DIAG/"base_2min_accel_b3_v1_20260714.json").read_text())["result"]; pools=sorted({x["pool"].lower() for x in base["opportunities"]}); quoted=",".join("'"+p+"'" for p in pools)
 rows=m.ch_query(f"SELECT lower(pool_address) pool_address,lower(target_token) target_token,pool_type FROM eatlq.base_pool_universe_b3 FINAL WHERE lower(pool_address) IN ({quoted})"); meta={x["pool_address"]:x for x in rows}; rpc=m.Rpc(url);head,hm=rpc.call("eth_blockNumber",[]);code=m.compile_bytecode();out=[]
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs=[ex.submit(work,rpc,code,o,meta) for o in base["opportunities"]]
  for i,f in enumerate(as_completed(fs),1):
   out.append(f.result())
   if i%10==0 or i==len(fs):print(f"[B3 seconds] {i}/{len(fs)} logical={rpc.logical} attempts={rpc.attempts}",flush=True)
 out.sort(key=lambda x:x["pool"]);payload={"schema_version":1,"research_only":True,"preregistration_sha256":m.sha(m.PREREG),"archive_head":int(head,16),"archive_head_response_sha256":hm["response_sha256"],"summary":{"rows":len(out),"status":dict(Counter(x.get("status") for x in out)),"real_signal":sum(x["real"] for x in out),"proxy_signal":sum(x["proxy"] for x in out)},"rpc":rpc.summary(),"rows":out};OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n");print(json.dumps({"out":str(OUT),"summary":payload["summary"],"rpc":payload["rpc"]},indent=2))
if __name__=="__main__":main()
