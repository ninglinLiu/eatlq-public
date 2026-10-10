#!/usr/bin/env python3
"""Finalize the sanitized GL2-A RPC receipt after all read-only phases."""
import hashlib,json
from collections import Counter
from pathlib import Path
ROOT=Path('/opt/EATLQ');D=ROOT/'mvp/diagnostics';FULL=D/'base_gl2_a_rpc_full_ledger_20260715.jsonl';OUT=D/'base_gl2_a_rpc_ledger_20260715.json'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
rows=[json.loads(x) for x in FULL.read_text().splitlines() if x]
methods=Counter(r['method'] for r in rows);errors=Counter(r.get('error_class',r.get('error_type','unknown')) for r in rows if r['status']=='error')
payload={'schema_version':4,'research_only':True,'preregistration_sha256':'331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07','endpoint_alias':'prettiest-blue-aura','posture':'read-only; no send/sign/broadcast method implemented','phases':['all_431_prereg_text_b0_ladder','aborted_trigger_attempt_preserved','all_431_fresh_trigger_ladder','trigger_unresolved_E1_E5','single_oversize_recovery_log_probe_preserved'],'rpc':{'ledger_rows':len(rows),'logical_id_max':max(r['logical_id'] for r in rows),'attempt_id_max':max(r['attempt_id'] for r in rows),'success_rows':sum(r['status']=='ok' for r in rows),'error_rows':sum(r['status']=='error' for r in rows),'methods_attempted':dict(sorted(methods.items())),'errors_by_class':dict(sorted(errors.items())),'full_ledger_path':str(FULL.relative_to(ROOT)),'full_ledger_sha256':sha(FULL)},'outputs':{p.name:sha(p) for p in (D/'base_gl2_a_five_evidence_rpc_20260715.json',D/'base_gl2_a_trigger_five_evidence_rpc_20260715.json',D/'base_gl2_a_trigger_evidence_reviewed_20260715.json')}}
OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
print(json.dumps(payload['rpc'],indent=2,sort_keys=True))
