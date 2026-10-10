#!/usr/bin/env python3
"""GL7v2 defensive exit-safety due diligence; archive eth_call only."""
from __future__ import annotations

import hashlib,json,math,sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path

ROOT=Path('/opt/EATLQ')
sys.path.insert(0,str(ROOT/'scripts/research'))
sys.path.insert(0,str(ROOT/'scripts/research/base_gl7'))
from gl_readonly_rpc import Rpc,atomic_probe,balance_of,compile_atomic_bytecode,WETH  # noqa
import gl7_wallet_size_falsification as G7  # noqa

PREREG=ROOT/'reports/GL7v2_preregistration_20260716.md'
PREREG_SHA='a23bd569ef3e82cb6154a99b88530bb4d5b701a117edf3859b8db4e0a976b21c'
V3=ROOT/'mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json'
V2=ROOT/'mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json'
GL4=ROOT/'mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json'
PC=ROOT/'reports/GL7_positive_control_207_frozen_20260716.json'
LEDGER=ROOT/'mvp/diagnostics/base_gl7v2_rpc_full_ledger_20260716.jsonl'
LEDGER_SUM=ROOT/'mvp/diagnostics/base_gl7v2_rpc_ledger_20260716.json'
OUT=ROOT/'mvp/diagnostics/base_gl7v2_exit_safety_20260716.json'
LEGS=ROOT/'mvp/diagnostics/base_gl7v2_exit_safety_per_leg_20260716.json'
SPEND=30_200_000_000_000_000
CAP=5_000_000_000_000_000_000

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

_V2BY={}  # pool -> GL5v2 frozen per-leg row; populated in main() before any qty_for call

def qty_for(row,off):
    # Erratum GL7v2_preregistration_erratum_qty_lineage: historical position qty
    # comes first from the GL5v3 exact-size quote; REAL_SELL legs never built one,
    # so fall back to the frozen GL5v2 position_qty_raw for the same pool/offset.
    v3=int(((((row['arm1'][str(off)].get('archive') or {}).get('exact_size_quote') or {}).get('amount_in_token_raw')) or 0))
    if v3>0:return v3
    r2=_V2BY.get(row['pool'])
    if r2:
        q=((r2.get('arm1') or {}).get(str(off)) or {}).get('position_qty_raw')
        if q and int(q)>0:return int(q)
    return 0

def converge(rpc,bytecode,token,fee,block,target_qty,baseline):
    if target_qty<=0:return {'evaluable':False,'reason':'historical_qty_missing','target_qty_raw':str(target_qty),'iterations':[]}
    got=int(baseline.get('received_target_raw') or 0)
    if not baseline.get('ok') or not baseline.get('buy_ok') or got<=0:
        return {'evaluable':False,'reason':'baseline_buy_unavailable','target_qty_raw':str(target_qty),'iterations':[]}
    spend=max(1,int(SPEND*target_qty/got));iterations=[]
    for _ in range(4):
        if spend>CAP:return {'evaluable':False,'reason':'spend_cap_exceeded','target_qty_raw':str(target_qty),'next_spend_wei':str(spend),'iterations':iterations}
        a=atomic_probe(rpc,bytecode,token,fee,spend,block);iterations.append({'spend_wei':str(spend),**a})
        if not a.get('ok'):
            if a.get('error_class')=='rpc_revert':return {'evaluable':True,'buy_ok':False,'sell_ok':False,'confirmed_failure':True,'reason':'evm_revert','target_qty_raw':str(target_qty),'iterations':iterations}
            return {'evaluable':False,'reason':'rpc_unresolved','target_qty_raw':str(target_qty),'iterations':iterations}
        actual=int(a.get('received_target_raw') or 0)
        if actual<=0:return {'evaluable':True,'buy_ok':False,'sell_ok':False,'confirmed_failure':True,'reason':'zero_buy_receive','target_qty_raw':str(target_qty),'iterations':iterations}
        ratio=actual/target_qty
        if .98<=ratio<=1.02:
            ok=bool(a.get('sell_ok') and int(a.get('recovered_weth_raw') or 0)>0)
            return {'evaluable':True,'buy_ok':bool(a.get('buy_ok')),'sell_ok':bool(a.get('sell_ok')),
                    'confirmed_failure':not ok,'target_qty_raw':str(target_qty),'actual_qty_raw':str(actual),
                    'qty_ratio':ratio,'spend_wei':str(spend),'recovery_ratio':a.get('recovery_ratio'),'iterations':iterations}
        spend=max(1,int(spend*target_qty/actual))
    return {'evaluable':False,'reason':'qty_tolerance_not_converged','target_qty_raw':str(target_qty),'iterations':iterations}

def selected_row(rpc,bytecode,row,cands):
    fee=(row.get('fee_evidence') or {}).get('fee');entry=int((row.get('entry_weth_evidence') or {}).get('value_raw') or 0)
    out={k:row[k] for k in ('pool','token','entry_block','signal_time')};out['offsets']={}
    for off in (1,5,15):
        block=int(row['arm1'][str(off)]['target_block']);b=balance_of(rpc,WETH,row['pool'],block);lag=balance_of(rpc,WETH,row['pool'],block+15)
        ratio=int(b['value_raw'])/entry if entry and b.get('ok') else None;lr=int(lag['value_raw'])/entry if entry and lag.get('ok') else None
        collapse=ratio is not None and lr is not None and ratio<=.05 and lr<=.05
        baseline=atomic_probe(rpc,bytecode,row['token'],fee,SPEND,block) if fee else {'ok':False,'error_class':'fee_missing'}
        q=qty_for(row,off);hist=converge(rpc,bytecode,row['token'],fee,block,q,baseline) if fee else {'evaluable':False,'reason':'fee_missing'}
        twice=converge(rpc,bytecode,row['token'],fee,block,2*q,baseline) if fee else {'evaluable':False,'reason':'fee_missing'}
        holder=G7.holder_view(rpc,row['token'],block,q,cands.get(row['pool'],[]))
        holder['adjudicable']=False
        holder['adjudication_reason']='eth_call does not persist/verify sink arrival; candidate list is not a target-time canonical BUY proof'
        out['offsets'][str(off)]={'block':block,'reserve_ratio':ratio,'lag_ratio':lr,'confirmed_collapse':collapse,
          'baseline':baseline,'historical_qty':hist,'two_x_historical_qty':twice,'holder_view':holder}
    return out

def control_row(rpc,bytecode,row,flip,cands):
    fee=(row.get('fee_evidence') or {}).get('fee');entry=int((row.get('entry_weth_evidence') or {}).get('value_raw') or 0)
    b=balance_of(rpc,WETH,row['pool'],flip);lag=balance_of(rpc,WETH,row['pool'],flip+15)
    ratio=int(b['value_raw'])/entry if entry and b.get('ok') else None;lr=int(lag['value_raw'])/entry if entry and lag.get('ok') else None
    collapse=ratio is not None and lr is not None and ratio<=.05 and lr<=.05
    baseline=atomic_probe(rpc,bytecode,row['token'],fee,SPEND,flip) if fee else {'ok':False}
    q=0
    for o in (1,5,15):
        q=qty_for(row,o)
        if q:break
    hist=converge(rpc,bytecode,row['token'],fee,flip,q,baseline) if fee else {'evaluable':False}
    twice=converge(rpc,bytecode,row['token'],fee,flip,2*q,baseline) if fee else {'evaluable':False}
    holder=G7.holder_view(rpc,row['token'],flip,q,cands.get(row['pool'],[]));holder['adjudicable']=False
    holder['adjudication_reason']='non-persistent holder call; no verified sink arrival'
    restriction=(baseline.get('ok') and (not baseline.get('buy_ok') or not baseline.get('sell_ok'))) or (hist.get('evaluable') and hist.get('confirmed_failure')) or (twice.get('evaluable') and twice.get('confirmed_failure'))
    evaluable=collapse or baseline.get('ok') or hist.get('evaluable') or twice.get('evaluable') or holder.get('evaluable')
    return {'pool':row['pool'],'token':row['token'],'flip_block':flip,'reserve_ratio':ratio,'lag_ratio':lr,'confirmed_collapse':collapse,
      'baseline':baseline,'historical_qty':hist,'two_x_historical_qty':twice,'holder_view':holder,'evaluable':bool(evaluable),'positive':bool(collapse or restriction)}

def arm_stats(rows,key):
    ev=fail=unres=0;deter=[];tokens=set()
    for r in rows:
      for x in r['offsets'].values():
        if x['confirmed_collapse']:continue
        z=x[key]
        if z.get('evaluable'):
          ev+=1
          if z.get('confirmed_failure'):fail+=1;tokens.add(r['token'])
          br=x['baseline'].get('recovery_ratio');sr=z.get('recovery_ratio')
          if br is not None and sr is not None:deter.append(br-sr)
        else:unres+=1
    bad=sum(v>.10 for v in deter)
    return {'evaluable_n':ev,'failure_n':fail,'failure_token_n':len(tokens),'failure_rate':fail/ev if ev else None,'unresolved_n':unres,
      'deterioration_n':len(deter),'deterioration_gt_10pp_n':bad,'deterioration_gt_10pp_rate':bad/len(deter) if deter else None,
      'median_deterioration':sorted(deter)[len(deter)//2] if deter else None}

def main():
    assert sha(PREREG)==PREREG_SHA
    assert sha(ROOT/'reports/GL7v2_preregistration_erratum_qty_lineage_20260716.md')=='735f08a70e63b3f052c2a7556b77e51ff3c6114cf909a1c2a8146407edf4b70a'
    global _V2BY
    _V2BY={r['pool']:r for r in json.loads(V2.read_text())['rows']}
    allrows=json.loads(V3.read_text())['rows'];sel0=[r for r in allrows if r['selected_Fon']];by={r['pool']:r for r in allrows}
    assert all(r['pool'] in _V2BY for r in sel0),'GL5v2 lineage missing for a selected pool'
    _qsrc=Counter()
    for r in sel0:
        for off in (1,5,15):
            v3q=int(((((r['arm1'][str(off)].get('archive') or {}).get('exact_size_quote') or {}).get('amount_in_token_raw')) or 0))
            if v3q>0:_qsrc['gl5v3_exact_quote']+=1
            elif int((((_V2BY.get(r['pool']) or {}).get('arm1') or {}).get(str(off)) or {}).get('position_qty_raw') or 0)>0:_qsrc['gl5v2_frozen_position']+=1
            else:_qsrc['none']+=1
    pc=json.loads(PC.read_text());assert len(pc['pools'])==len(set(pc['pools']))==pc['expected_n']==207
    lab=json.loads((ROOT/'scripts/research/base_lefttail/_labeled_universe.json').read_text());flip={r['pool']:int(r['flip_block']) for r in lab['rug_pools']}
    cands=G7.holder_candidates(sorted(set([r['pool'] for r in sel0]+pc['pools'])))
    rpc=Rpc(LEDGER,user_agent='EATLQ-GL7v2-readonly/1');bytecode=compile_atomic_bytecode();selected=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
      fs=[ex.submit(selected_row,rpc,bytecode,r,cands) for r in sel0]
      for f in as_completed(fs):selected.append(f.result())
    controls=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
      fs=[ex.submit(control_row,rpc,bytecode,by[p],flip[p],cands) for p in pc['pools']]
      for f in as_completed(fs):controls.append(f.result())
    A=G7.actual_sell_A(rpc,json.loads(GL4.read_text()))
    astat={'all':{'attempt_n':len(A),'success_n':sum(x['success'] for x in A)},'natural_ex_smoke':{}}
    nat=[x for x in A if 'smoke' not in str(x.get('segment') or '')];astat['all']['success_rate']=astat['all']['success_n']/len(A);astat['natural_ex_smoke']={'attempt_n':len(nat),'success_n':sum(x['success'] for x in nat),'success_rate':sum(x['success'] for x in nat)/len(nat)}
    astat['failure_causes']=dict(Counter(x.get('cause') for x in A if not x['success']));astat['confirmed_wallet_restriction_n']=sum(x.get('confirmed_wallet_hook',False) for x in A)
    b_hist=arm_stats(selected,'historical_qty');b_two=arm_stats(selected,'two_x_historical_qty')
    size_risk=any(x['failure_token_n']>=2 or (x['failure_rate'] or 0)>.02 or ((x['deterioration_gt_10pp_rate'] or 0)>.10 and x['deterioration_gt_10pp_n']>=5) for x in (b_hist,b_two))
    base_ev=base_fail=base_unres=0;holder_calls=holder_nonrevert=holder_revert=0
    for r in selected:
      for x in r['offsets'].values():
        if x['confirmed_collapse']:continue
        a=x['baseline']
        if a.get('ok'):base_ev+=1;base_fail+=not(a.get('buy_ok') and a.get('sell_ok'))
        else:base_unres+=1
        h=x['holder_view']
        if h.get('evaluable'):
          holder_calls+=1;holder_nonrevert+=bool(h.get('transfer_ok'));holder_revert+=not bool(h.get('transfer_ok'))
    material_transfer=(base_fail>=1)  # holder calls are indications only, never adjudicable negatives/positives
    pc_ev=sum(x['evaluable'] for x in controls);pc_pos=sum(x['positive'] for x in controls if x['evaluable']);pc_rate=pc_pos/pc_ev if pc_ev else 0;wilson=G7.wilson_lower(pc_pos,pc_ev);valid=pc_ev/207>=.9 and pc_rate>=.8 and wilson>=.75
    a_pass=astat['confirmed_wallet_restriction_n']==0 and astat['failure_causes'].get('UNRESOLVED_FAILURE',0)/51<=.10
    b_unres=max(b_hist['unresolved_n'],b_two['unresolved_n'])/357
    b_pass=not size_risk and b_unres<=.10
    c_pass=False # exact strategy-wallet token-balance evidence is absent by construction
    if not valid:verdict='METHOD_INVALID'
    elif astat['confirmed_wallet_restriction_n'] or size_risk or material_transfer:verdict='EXIT_RISK_CONFIRMED'
    elif a_pass and b_pass and c_pass:verdict='RESIDUAL_RISK_LOW'
    else:verdict='UNRESOLVED_EXIT_SAFETY'
    rs=rpc.summary();LEDGER_SUM.write_text(json.dumps(rs,indent=2,sort_keys=True)+'\n')
    result={'schema_version':1,'task_id':'GL7-v2','preregistration_sha256':PREREG_SHA,'research_only':True,
      'qty_lineage_erratum':{'rule':'gl5v3_exact_quote_then_gl5v2_frozen_position_qty_raw','erratum_sha256':sha(ROOT/'reports/GL7v2_preregistration_erratum_qty_lineage_20260716.md'),'source_breakdown_over_120x3':dict(_qsrc)},
      'dimension_A':astat,
      'dimension_B':{'baseline':{'evaluable_n':base_ev,'failure_n':base_fail,'unresolved_n':base_unres},'historical_qty':b_hist,'two_x_historical_qty':b_two,'material_size_risk':size_risk,'pass':b_pass},
      'dimension_C':{'new_address':{'evaluable_n':base_ev,'restriction_n':base_fail},
        'historical_holder':{'evaluable_n':0,'unresolved_n':357,'non_adjudicable_call_n':holder_calls,'nonreverting_indication_n':holder_nonrevert,'revert_indication_n':holder_revert,'status':'UNRESOLVED_NO_PERSISTENT_ARRIVAL_VERIFICATION'},
        'strategy_wallet_exact':{'evaluable_n':0,'status':'UNRESOLVED_NO_COUNTERFACTUAL_TOKEN_BALANCE'},'material_transfer_risk':material_transfer,'pass':c_pass},
      'dimension_D':{'expected_n':207,'evaluable_n':pc_ev,'positive_n':pc_pos,'evaluable_rate':pc_ev/207,'positive_rate':pc_rate,'wilson_95_lower':wilson,'method_valid':valid},
      'verdict_candidate':verdict,'boundary':'read-only simulation omits MEV/competition/signing and does not guarantee strategy-wallet real execution','go_decision':'NOT_MADE','rpc':rs}
    LEGS.write_text(json.dumps({'dimension_A_attempts':A,'selected_120':sorted(selected,key=lambda x:x['pool']),'positive_control_207':sorted(controls,key=lambda x:x['pool'])},indent=2,sort_keys=True)+'\n');OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'A':astat,'B':result['dimension_B'],'C':result['dimension_C'],'D':result['dimension_D'],'verdict':verdict,'rpc':rs},indent=2))

if __name__=='__main__':main()
