#!/usr/bin/env python3
import hashlib,json
from pathlib import Path
R=Path('/opt/EATLQ');D=R/'mvp/diagnostics'
test={'schema_version':2,'research_only':True,'command':'python3 -B -m unittest -v tests.test_base_gl2_a','test_module':'tests.test_base_gl2_a','passed':8,'failed':0,'result':'OK','decision_flags':{'training_allowed':False,'live_allowed':False,'self_go_allowed':False}}
(D/'base_gl2_a_test_evidence_20260715.json').write_text(json.dumps(test,indent=2,sort_keys=True)+'\n')
paths=['reports/GL2_A_prereg_five_evidence_20260715.md','mvp/diagnostics/base_gl2_a_preregistration_20260715.sha256','scripts/research/base_gl2/gl2_a_five_evidence_rpc.py','scripts/research/base_gl2/gl2_a_trigger_five_evidence.py','scripts/research/base_gl2/gl2_a_full_tail_review.py','scripts/research/base_gl2/gl2_a_finalize_rpc_ledger.py','scripts/research/base_gl2/gl2_a_authoritative_result.py','scripts/research/base_gl2/gl2_a_finalize_hashes.py','mvp/diagnostics/base_gl2_a_five_evidence_rpc_20260715.json','mvp/diagnostics/base_gl2_a_trigger_five_evidence_rpc_20260715.json','mvp/diagnostics/base_gl2_a_trigger_evidence_reviewed_20260715.json','mvp/diagnostics/base_gl2_a_rpc_full_ledger_20260715.jsonl','mvp/diagnostics/base_gl2_a_rpc_ledger_20260715.json','mvp/diagnostics/base_gl2_a_universe_ev_20260715.json','reports/GL2_A_universe_ev_result_20260715.md','tests/test_base_gl2_a.py','mvp/diagnostics/base_gl2_a_test_evidence_20260715.json']
ledger={'schema_version':2,'alpha_id':'AR-GL2-A-UNIVERSE-EV','research_only':True,'next_gate':'HUMAN_LINE_REVIEW_ONLY','decision_flags':{'training_allowed':False,'live_allowed':False,'self_go_allowed':False},'artifacts':[{'path':p,'sha256':hashlib.sha256((R/p).read_bytes()).hexdigest()} for p in paths]}
(D/'base_gl2_a_hash_ledger_20260715.json').write_text(json.dumps(ledger,indent=2,sort_keys=True)+'\n')
print({'hashed':len(paths)})
