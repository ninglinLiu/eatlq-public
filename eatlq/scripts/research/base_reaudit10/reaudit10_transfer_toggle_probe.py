#!/usr/bin/env python3
"""Bounded binary transition probe for the two delayed transfer-toggle labels."""
import importlib.util,json,os
from pathlib import Path
ROOT=Path("/opt/EATLQ");D=ROOT/"mvp/diagnostics";base=ROOT/"scripts/research/base_reaudit10/reaudit10_rpc.py"
s=importlib.util.spec_from_file_location("r10_toggle",base);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.LEDGER=D/"base_reaudit10_toggle_rpc_full_ledger_20260715.jsonl";OUT=D/"base_reaudit10_transfer_toggle_probe_20260715.json"
def safe(fn,label):
 try:return fn()
 except Exception as e:return {"status":"unknown","where":label,"error":f"{type(e).__name__}:{str(e)[:220]}"}
def main():
 url=os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
 if not url:raise RuntimeError("FORK_ARCHIVE_RPC required")
 labels=[x for x in m.rug_labels() if x.get("mechanism")=="transfer_toggle"];rpc=m.Rpc(url);head,hm=rpc.call("eth_blockNumber",[]);code=m.compile_bytecode();rows=[]
 for lab in labels:
  pool=lab["pool"].lower();token=lab["token"].lower();lo,hi=map(int,lab["flip_interval"]);fee=int(lab["fee"]);probes=[]
  while lo+1<hi:
   mid=(lo+hi)//2;a=safe(lambda b=mid:m.atomic_at(rpc,code,token,fee,b),"binary_atomic");probes.append({"block":mid,"atomic":a})
   if a.get("buy_ok") and a.get("sell_ok") and a.get("recovery_ratio",0)>.1:lo=mid
   else:hi=mid
  points=[]
  for b in (hi-1,hi):
   raw,meta=rpc.call("eth_getBlockByNumber",[hex(b),True]);token_txs=[t for t in raw.get("transactions",[]) if str(t.get("to") or "").lower()==token];txev=[]
   for tx in token_txs:
    h=tx["hash"];rc=safe(lambda h=h:(lambda x:{"status":"measured","receipt":x[0],**x[1]})(rpc.call("eth_getTransactionReceipt",[h])),"receipt");tr=safe(lambda h=h:(lambda x:{"status":"measured","trace_sha256":m.chash(x[0]),**x[1]})(rpc.call("debug_traceTransaction",[h,{"tracer":"callTracer","timeout":"30s"}],timeout=180)),"trace");txev.append({"tx_hash":h,"from":tx.get("from"),"to":tx.get("to"),"input_selector":str(tx.get("input") or "")[:10],"receipt":rc,"trace":tr})
   points.append({"block":b,"hash":raw.get("hash"),"parent_hash":raw.get("parentHash"),"timestamp":int(raw["timestamp"],16),"block_request_sha256":meta["request_sha256"],"block_response_sha256":meta["response_sha256"],"atomic":safe(lambda b=b:m.atomic_at(rpc,code,token,fee,b),"edge_atomic"),"token_directed_transactions":txev})
  rows.append({"pool":pool,"token":token,"frozen_interval":lab["flip_interval"],"first_bad_block":hi,"binary_probes":probes,"points":points})
 payload={"schema_version":1,"research_only":True,"archive_head":int(head,16),"archive_head_response_sha256":hm["response_sha256"],"rows":rows,"rpc":rpc.summary()};OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n");print(json.dumps({"out":str(OUT),"rows":len(rows),"first_bad":[x["first_bad_block"] for x in rows],"rpc":payload["rpc"]},indent=2))
if __name__=="__main__":main()
