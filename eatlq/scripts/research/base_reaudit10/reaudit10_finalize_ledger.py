#!/usr/bin/env python3
"""Combine the hash-only ledgers from the resumable read-only phases."""
import hashlib,json
from collections import Counter
from pathlib import Path
ROOT=Path("/opt/EATLQ");D=ROOT/"mvp/diagnostics";P=ROOT/"configs/research/base_reaudit10_seconds_preregistration_20260715.json"
LEDS=[D/"base_reaudit10_rpc_full_ledger_20260715.jsonl",D/"base_reaudit10_rug_rpc_full_ledger_20260715.jsonl",D/"base_reaudit10_b3_rpc_full_ledger_20260715.jsonl",D/"base_reaudit10_toggle_rpc_full_ledger_20260715.jsonl"]
OUTS=[D/"base_reaudit10_seconds_rpc_20260715.json",D/"base_reaudit10_b4_health_rpc_20260715.json",D/"base_reaudit10_rug_five_evidence_rpc_20260715.json",D/"base_reaudit10_b3_seconds_rpc_20260715.json",D/"base_reaudit10_transfer_toggle_probe_20260715.json"]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 parts=[];total=Counter();ok=err=0
 for p in LEDS:
  c=Counter();n=0;e=0
  for line in p.read_text().splitlines():
   r=json.loads(line);c[r["method"]]+=1;n+=1;e+=r["status"]!="ok"
  credits=sum(v*(40 if k=="debug_traceTransaction" else 20) for k,v in c.items());parts.append({"path":str(p.relative_to(ROOT)),"sha256":sha(p),"http_attempts":n,"errors":e,"estimated_credits":credits,"methods":dict(c)});total.update(c);ok+=n-e;err+=e
 result={"schema_version":1,"research_only":True,"preregistration_sha256":sha(P),"rpc":{"http_attempts":ok+err,"successful_attempts":ok,"error_attempts":err,"estimated_credits":sum(v*(40 if k=="debug_traceTransaction" else 20) for k,v in total.items()),"methods":dict(total),"parts":parts,"endpoint_id":"FORK_ARCHIVE_RPC_secret_omitted"},"outputs":{str(p.relative_to(ROOT)):sha(p) for p in OUTS}}
 (D/"base_reaudit10_rpc_ledger_summary_20260715.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n");print(json.dumps(result,indent=2))
if __name__=="__main__":main()
