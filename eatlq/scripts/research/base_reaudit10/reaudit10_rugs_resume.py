#!/usr/bin/env python3
"""Resume only the rug phase after the core and B4 checkpoints exist."""
import importlib.util,json,os
from pathlib import Path
ROOT=Path("/opt/EATLQ"); path=ROOT/"scripts/research/base_reaudit10/reaudit10_rpc.py"
spec=importlib.util.spec_from_file_location("reaudit10_rug_resume",path); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.LEDGER=ROOT/"mvp/diagnostics/base_reaudit10_rug_rpc_full_ledger_20260715.jsonl"
def main():
 url=os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
 if not url:raise RuntimeError("FORK_ARCHIVE_RPC required")
 rpc=m.Rpc(url);head,hm=rpc.call("eth_blockNumber",[]);rows=m.run_rugs(rpc,m.compile_bytecode()); result={"schema_version":1,"research_only":True,"archive_head":int(head,16),"archive_head_response_sha256":hm["response_sha256"],"preregistration_sha256":m.sha(m.PREREG),"row_counts":{"rugs":len(rows["rows"])},"output_sha256":m.sha(m.RUG_OUT),"rpc":rpc.summary()}; (m.DIAG/"base_reaudit10_rug_rpc_ledger_summary_20260715.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n");print(json.dumps(result,indent=2))
if __name__=="__main__":main()
