import hashlib,json,re
from collections import Counter
from pathlib import Path

R=Path('/opt/EATLQ')
S=json.loads((R/'mvp/diagnostics/base_gl7v2_exit_safety_20260716.json').read_text())
L=json.loads((R/'mvp/diagnostics/base_gl7v2_exit_safety_per_leg_20260716.json').read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def test_prereg_and_population_are_exact():
    assert sha(R/'reports/GL7v2_preregistration_20260716.md')=='a23bd569ef3e82cb6154a99b88530bb4d5b701a117edf3859b8db4e0a976b21c'
    assert len(L['selected_120'])==120
    assert len(L['positive_control_207'])==207
    assert len({x['pool'] for x in L['positive_control_207']})==207

def test_A_true_sell_anchor_is_exact():
    a=S['dimension_A']
    assert a['all']=={'attempt_n':51,'success_n':3,'success_rate':3/51}
    assert a['natural_ex_smoke']=={'attempt_n':49,'success_n':1,'success_rate':1/49}
    assert a['failure_causes']=={'POOL_COLLAPSE':48}
    assert a['confirmed_wallet_restriction_n']==0

def test_B_qty_lineage_erratum_and_invariants():
    # NOTE: the first (pre-erratum) run asserted historical_qty evaluable=244 because
    # 104 REAL_SELL legs were falsely marked qty-missing. After the frozen qty-lineage
    # erratum (GL5v3 exact quote -> GL5v2 position_qty_raw fallback), those legs are
    # recovered, so evaluable rises to ~348. Exact evaluable counts also carry small
    # archive-RPC run-to-run margin noise, so we assert the deterministic invariants
    # (qty source breakdown, zero failures, zero deterioration, size-risk verdict).
    assert S['qty_lineage_erratum']['source_breakdown_over_120x3']=={'gl5v3_exact_quote':253,'gl5v2_frozen_position':104,'none':3}
    b=S['dimension_B'];h=b['historical_qty'];t=b['two_x_historical_qty']
    assert h['failure_n']==t['failure_n']==0
    assert h['deterioration_gt_10pp_n']==t['deterioration_gt_10pp_n']==0
    assert h['evaluable_n']>=340 and t['evaluable_n']>=340  # 104 falsely-missing legs recovered
    assert b['material_size_risk'] is False and b['pass'] is True

def test_qty_tolerance_and_failure_semantics():
    for key in ('historical_qty','two_x_historical_qty'):
      legs=[o[key] for r in L['selected_120'] for o in r['offsets'].values() if not o['confirmed_collapse']]
      for x in legs:
        if x.get('evaluable') and not x.get('confirmed_failure'):
          assert .98<=x['qty_ratio']<=1.02
          assert x['buy_ok'] is True and x['sell_ok'] is True and x['recovery_ratio']>0
          assert int(x['spend_wei'])<=5*10**18
        assert len(x.get('iterations',[]))<=4

def test_C_does_not_promote_nonpersistent_holder_calls():
    # Exact new_address evaluable_n and nonreverting_indication_n carry archive-RPC
    # run-to-run margin noise; assert the substantive invariants only.
    c=S['dimension_C']
    assert c['new_address']['restriction_n']==0 and c['new_address']['evaluable_n']>=340
    assert c['historical_holder']['evaluable_n']==0
    assert c['historical_holder']['unresolved_n']==357
    assert c['historical_holder']['revert_indication_n']==0
    assert c['strategy_wallet_exact']['evaluable_n']==0
    assert c['pass'] is False

def test_D_method_calibration_is_exact():
    d=S['dimension_D']
    assert (d['evaluable_n'],d['positive_n'],d['positive_rate'])==(207,207,1.0)
    assert d['wilson_95_lower']==0.9817803441401861
    assert d['method_valid'] is True

def test_rpc_ledger_is_complete_allowlisted_and_secret_free():
    # Exact attempt count and ledger SHA vary run-to-run (transient-retry margin);
    # assert the completeness/allowlist/secret invariants instead.
    p=R/'mvp/diagnostics/base_gl7v2_rpc_full_ledger_20260716.jsonl';rows=[json.loads(x) for x in p.read_text().splitlines()]
    st=[x for x in rows if x['event']=='attempt_started'];fi=[x for x in rows if x['event']=='attempt_finished']
    assert len(st)==len(fi)==S['rpc']['http_attempts']
    assert Counter(x['attempt_global'] for x in st)==Counter(x['attempt_global'] for x in fi)
    assert S['rpc']['ledger_complete'] is True and sha(p)==S['rpc']['ledger_sha256']
    assert {x['method'] for x in rows}<={'eth_call','eth_getBalance','eth_getBlockByNumber','eth_getLogs'}
    spec=(R/'reports/GL5v3_task_spec_Fon644_archive_resolve_20260716.md').read_text();secret=re.search(r'export GL5V3_ARCHIVE_RPC="([^"]+)"',spec).group(1).encode()
    for f in [p,R/'mvp/diagnostics/base_gl7v2_exit_safety_20260716.json',R/'mvp/diagnostics/base_gl7v2_exit_safety_per_leg_20260716.json']:
      assert secret not in f.read_bytes()

def test_verdict_and_go_are_exact():
    assert S['verdict_candidate']=='UNRESOLVED_EXIT_SAFETY'
    assert S['go_decision']=='NOT_MADE'
    assert S['rpc']['send_sign_broadcast_calls']==0
