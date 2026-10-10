import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT=Path('/opt/EATLQ')
G6=json.loads((ROOT/'mvp/diagnostics/base_gl6_forward_recency_20260716.json').read_text())
G6L=json.loads((ROOT/'mvp/diagnostics/base_gl6_forward_per_leg_20260716.json').read_text())
G7=json.loads((ROOT/'mvp/diagnostics/base_gl7_wallet_size_falsification_20260716.json').read_text())
G7L=json.loads((ROOT/'mvp/diagnostics/base_gl7_wallet_size_per_leg_20260716.json').read_text())


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_preregistration_and_corrected_model_identity_are_exact():
    assert sha(ROOT/'reports/GL6_preregistration_20260716.md')=='fa216ded00bd36420f907f529793049fc2dae7e8d286bf0496f6903d34c8c64a'
    assert sha(ROOT/'reports/GL6_preregistration_erratum_model_identity_20260716.md')=='5cf1a7da94ceddc341692466e5811bf1adffa54b16d66fc03cf52490573bb9d2'
    assert sha(ROOT/'reports/GL6_preregistration_erratum_reproducibility_grid_20260716.md')=='7e21db08cfd0d8412b9b5c3e673528354de947bc27b89476a5ba24d70a1a276c'
    assert sha(ROOT/'reports/GL7_preregistration_20260716.md')=='60e2387f58b84661aba10cfc585ccbf7fe4f1d7087b0f395b7cfe8f09d7a8edf'
    assert G6['step0_anchor']=={'labeled_n':644,'f_on_pass':120,'aggressive_gate':339}
    assert G6['frozen_config']['forward_rows_in_fit']==0


def test_gl6_full_funnel_and_pit_f_are_exact():
    assert G6['funnel']=={'raw_universe':4794,'min_pre_block':4513,'min_pre_pass':281,'model_pass':125,'depth_block':117,'gate_unresolved':1,'gate_pass':7,'definite_selected':0,'possible_selected_gate_identified':7,'outer_possible_if_gate_and_f_resolve':8,'f_unresolved_possible':7}
    passed=[r for r in G6L['rows'] if r.get('gate_status')=='GATE_PASS']
    assert len(passed)==7
    assert Counter(r['f_status'] for r in passed)=={'F_NOT_AVAILABLE_PIT':6,'F_IDENTITY_UNRESOLVED':1}
    assert G6['verdict_candidate']=='INSUFFICIENT_FORWARD_N'


def test_gl6_census_grid_return_bounds_and_collapse_are_exact():
    census=ROOT/'mvp/diagnostics/base_gl6_forward_feature_census_20260716.json'
    assert sha(census)=='e7e5ea30d1da7e472c7303c2d9d89baabcae707eccda0b6a17b2021eca3fa8d7'
    c=json.loads(census.read_text())
    assert len(c['rows'])==len({r['pool'] for r in c['rows']})==281
    assert sum(r['p_fatal']<=.27 for r in c['rows'])==125
    assert c['total_order']==['block_time','block_number','tx_hash','log_index']
    m=G6['possible_selected_gate_identified_metrics']
    assert m['1']['mean_interval']==[0.19927253168497863,0.19927253168497863]
    assert m['5']['mean_interval']==[0.19927253168497863,0.19927253168497863]
    assert m['15']['mean_interval']==[-0.24526923296196443,-0.24526923296196443]
    assert m['15']['branches']=={'ALIVE_ATOMIC_OK':3,'CONFIRMED_COLLAPSE':4}
    assert m['15']['median_interval']==[-1.0229943556396193,-1.0229943556396193]
    assert m['15']['right_tail_pass'] is False
    for r in [x for x in G6L['rows'] if x.get('gate_status')=='GATE_PASS']:
        for leg in r['offsets'].values():
            t=leg['target_block']
            assert set(map(int,leg['reserve_grid']))=={t-1,t,t+1,t+5,t+15}
    outer=G6['same_age_baseline_gate_selection_outer_bound']['1']
    assert outer['mean_lower_bound']==0.0464891707694039
    assert outer['mean_upper_bound'] is None
    assert outer['upper_status']=='UNBOUNDED_NO_EXIT_EVIDENCE'
    assert G6['same_age_baseline_gate_selection_outer_bound']['15']['possible_n']==8


def test_gl7_actual_sell_inventory_is_exact():
    a=G7['dimension_A']
    assert a['all']=={'attempt_n':51,'success_n':3,'success_rate':3/51}
    assert a['natural_ex_smoke']=={'attempt_n':49,'success_n':1,'success_rate':1/49}
    assert a['failure_causes']=={'POOL_COLLAPSE':48}
    assert a['confirmed_wallet_hook_n']==0
    assert len(G7L['dimension_A_attempts'])==51


def test_gl7_size_arms_are_exact_and_no_failure_is_hidden():
    exp={'1x':(345,0,12),'1.87x':(349,0,8),'5x':(347,0,10),'14x':(349,0,8)}
    for k,(ev,fail,unres) in exp.items():
        x=G7['dimension_B']['sizes'][k]
        assert (x['evaluable_n'],x['failure_n'],x['unresolved_n'])==(ev,fail,unres)
        assert x['failure_rate']==0.0
    assert G7['dimension_B']['material_size_failure'] is False


def test_gl7_original_holder_claim_is_superseded_not_promoted():
    review=json.loads((ROOT/'mvp/diagnostics/base_gl7_independent_review_20260716.json').read_text())
    assert review['artifact_verdict']=='FAIL'
    assert any('holder' in str(x).lower() for x in review['substantive_blockers'])
    v2=json.loads((ROOT/'mvp/diagnostics/base_gl7v2_exit_safety_20260716.json').read_text())
    assert v2['dimension_C']['historical_holder']['evaluable_n']==0
    assert v2['dimension_C']['strategy_wallet_exact']['evaluable_n']==0


def test_positive_control_is_exact_and_probe_valid():
    pc=json.loads((ROOT/'reports/GL7_positive_control_207_frozen_20260716.json').read_text())
    assert pc['expected_n']==len(pc['pools'])==len(set(pc['pools']))==207
    d=G7['dimension_D']
    assert (d['evaluable_n'],d['positive_n'],d['positive_rate'])==(207,207,1.0)
    assert d['wilson_95_lower']==0.9817803441401861
    assert d['probe_valid'] is True


def _assert_ledger(task, expected_attempts, expected_sha):
    p=ROOT/f'mvp/diagnostics/base_{task}_rpc_full_ledger_20260716.jsonl'
    rows=[json.loads(x) for x in p.read_text().splitlines()]
    starts=[r for r in rows if r['event']=='attempt_started']; finishes=[r for r in rows if r['event']=='attempt_finished']
    assert len(starts)==len(finishes)==expected_attempts
    assert Counter(r['attempt_global'] for r in starts)==Counter(r['attempt_global'] for r in finishes)
    assert sha(p)==expected_sha
    assert {r['method'] for r in rows}<={'eth_call','eth_getBalance','eth_getBlockByNumber','eth_getLogs'}


def test_complete_rpc_ledgers_and_no_secret_leak():
    _assert_ledger('gl6',469,'7893281024a3880ad7f4632f4108c7c83df6c70d70a8826d34ff845875a8d8c7')
    _assert_ledger('gl7',9811,'9e088c7a86909a9463a108cc7db0c29dc4220e15c08a701fe8983acc8dc64e01')
    spec=(ROOT/'reports/GL5v3_task_spec_Fon644_archive_resolve_20260716.md').read_text()
    secret=re.search(r'export GL5V3_ARCHIVE_RPC="([^"]+)"',spec).group(1).encode()
    for p in [ROOT/'mvp/diagnostics/base_gl6_forward_recency_20260716.json',ROOT/'mvp/diagnostics/base_gl6_forward_per_leg_20260716.json',ROOT/'mvp/diagnostics/base_gl6_rpc_full_ledger_20260716.jsonl',ROOT/'mvp/diagnostics/base_gl7_wallet_size_falsification_20260716.json',ROOT/'mvp/diagnostics/base_gl7_wallet_size_per_leg_20260716.json',ROOT/'mvp/diagnostics/base_gl7_rpc_full_ledger_20260716.jsonl']:
        assert secret not in p.read_bytes()


def test_formal_verdicts_and_go_are_exact():
    assert G6['verdict_candidate']=='INSUFFICIENT_FORWARD_N'
    assert G7['verdict_candidate']=='INCONCLUSIVE_WALLET_LEVEL'
    assert G6['go_decision']==G7['go_decision']=='NOT_MADE'
