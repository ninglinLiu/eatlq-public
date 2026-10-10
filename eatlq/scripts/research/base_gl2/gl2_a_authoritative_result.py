#!/usr/bin/env python3
"""Render conservative GL2-A result from fresh trigger evidence."""
from __future__ import annotations
import csv,hashlib,json,math
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np

ROOT=Path('/opt/EATLQ'); D=ROOT/'mvp/diagnostics'
CORE=D/'base_reaudit10_seconds_rpc_20260715.json'; EVID=D/'base_gl2_a_trigger_evidence_reviewed_20260715.json'; IMM=D/'base_gl2_a_five_evidence_rpc_20260715.json'; ETH=D/'base_2min_accel_b3_eth_daily_20260714.json'
OUT=D/'base_gl2_a_universe_ev_20260715.json'; REPORT=ROOT/'reports/GL2_A_universe_ev_result_20260715.md'; SPEND=10**15; WINDOWS=(1,5,15)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def pct(v,q):return None if not v else float(np.quantile(v,q,method='linear'))
def swaps():
 out=defaultdict(list)
 for p in (D/'base_b5_real_swaps_20260714.tsv',D/'base_b8_future_swaps_20260714.tsv'):
  with p.open() as f:
   for z in csv.reader(f,delimiter='\t'):out[z[0]].append({'block':int(z[1]),'tx':z[3],'log_index':int(z[4]),'a0':int(z[5]),'a1':int(z[6])})
 for v in out.values():v.sort(key=lambda z:(z['block'],z['log_index']))
 return out
def first_sell(rows,orient,block):
 for s in rows:
  if s['block']<block:continue
  weth,tok=(s['a0'],s['a1']) if orient==-1 else (s['a1'],s['a0'])
  if weth<0 and tok>0:return {**s,'weth_out_raw':str(-weth),'token_in_raw':str(tok)}
 return None
def stats(rows,w):
 c=[r['windows'][str(w)] for r in rows]; p=[z for z in c if z['resolved']]; v=[z['net_return'] for z in p]
 return {'n_total':len(c),'n_resolved':len(p),'n_unresolved':sum(not z['resolved'] for z in c),'n_confirmed_rug':sum(z['evidence']=='CONFIRMED_NONRECOVERING_RUG' for z in c),'resolved_coverage':len(p)/len(c),'evidence_counts':dict(sorted(Counter(z['evidence'] for z in c).items())),'mean':float(np.mean(v)) if v else None,'median':pct(v,.5),'p05':pct(v,.05),'p25':pct(v,.25),'p50':pct(v,.5),'p75':pct(v,.75),'p95':pct(v,.95),'win_rate':sum(x>0 for x in v)/len(v) if v else None,'total_net_weth_raw':str(sum(int(z['net_weth_raw']) for z in p)),'partial_identification':any(not z['resolved'] for z in c)}
def main():
 core={r['pool']:r for r in json.loads(CORE.read_text())['rows']}; ev=json.loads(EVID.read_text()); im=json.loads(IMM.read_text()); eth=json.loads(ETH.read_text())['closes']; sw=swaps(); rows=[]
 for e in ev['rows']:
  c=core[e['pool']]; usd=float(eth.get(c['signal_time'][:10]) or min(eth.values())); wr={}
  for w in WINDOWS:
   p=e['fresh_trigger_points'][str(w)]; a=p['fresh_atomic_guard']; q=p['fresh_exact_size_quote']; qty=int(e['true_position_token_raw'] or 0); amount=int(q.get('amount_out',0) or 0); real=first_sell(sw[e['pool']],int(e['orient']),int(p['scheduled_block']))
   cost=int(round(2*.075/usd*1e18)); five=e['windows'].get(str(w))
   if not qty: z={'resolved':True,'evidence':'NO_ENTRY_FLAT','exit_weth_raw':str(SPEND),'frozen_cost_weth_raw':'0','net_weth_raw':'0','net_return':0.0}
   elif a.get('status')=='measured' and a.get('sell_ok') and a.get('recovery_ratio',0)>.1 and q.get('status')=='measured' and amount>0:
    net=amount-SPEND-cost;z={'resolved':True,'evidence':'ATOMIC_GUARDED_EXACT_QUOTE','exit_weth_raw':str(amount),'frozen_cost_weth_raw':str(cost),'net_weth_raw':str(net),'net_return':net/SPEND}
   elif q.get('status')=='measured' and amount>0 and real:
    net=amount-SPEND-cost;z={'resolved':True,'evidence':'ATOMIC_GUARDED_EXACT_QUOTE_REAL_SELL_CONFIRMED','exit_weth_raw':str(amount),'frozen_cost_weth_raw':str(cost),'net_weth_raw':str(net),'net_return':net/SPEND,'real_sell':real}
   else:z={'resolved':False,'evidence':(five or {}).get('adjudication','UNRESOLVED_FRESH_TRIGGER_EVIDENCE'),'exit_weth_raw':None,'frozen_cost_weth_raw':None,'net_weth_raw':'0','net_return':None}
   z.update({'entry_weth_raw':str(SPEND),'true_position_token_raw':str(qty) if qty else None,'target_block':int(p['scheduled_block']),'exact_quote':q,'atomic_guard':a,'five_evidence':five});wr[str(w)]=z
  rows.append({'stable_key':hashlib.sha256((e['pool']+'|'+str(c['entry_block'])+'|'+str(c['trigger_block'])).encode()).hexdigest(),'pool':e['pool'],'token':e['token'],'regime':c['regime'],'signal_time':c['signal_time'],'cohort':{'full_universe':True,'keystone_selected':bool(e['selected'])},'windows':wr})
 selected=[r for r in rows if r['cohort']['keystone_selected']]; summaries={}
 for w in WINDOWS:
  fu=stats(rows,w);ks=stats(selected,w);alpha=ks['mean']-fu['mean']; comp={'beta_component_full_universe_mean':fu['mean'],'selection_alpha_component':alpha,'keystone_mean':ks['mean'],'beta_signed_share_of_keystone_mean':fu['mean']/ks['mean'] if not math.isclose(ks['mean'],0) else None,'selection_alpha_signed_share_of_keystone_mean':alpha/ks['mean'] if not math.isclose(ks['mean'],0) else None,'partial_identification':True}
  summaries[str(w)]={'including_confirmed_rugs':{'full_universe':fu,'keystone_subset':ks,'alpha_beta_decomposition':comp},'excluding_confirmed_rugs_survivor_conditioned':{'full_universe':fu,'keystone_subset':ks,'alpha_beta_decomposition':comp}}
 immediate={str(w):{'resolved':sum((not r['true_position_token_raw']) or (next(p for p in r['target_points'] if p['window_blocks_after_entry']==w)['atomic_guard'].get('sell_ok') and next(p for p in r['target_points'] if p['window_blocks_after_entry']==w)['exact_size_quote'].get('status')=='measured' and int(next(p for p in r['target_points'] if p['window_blocks_after_entry']==w)['exact_size_quote'].get('amount_out',0) or 0)>0) for r in im['rows'])} for w in WINDOWS}
 for w in WINDOWS:immediate[str(w)]['unresolved']=431-immediate[str(w)]['resolved']
 payload={'schema_version':3,'alpha_id':'AR-GL2-A-UNIVERSE-EV','sop_applied':True,'research_only':True,'decision_flags':{'training_allowed':False,'live_allowed':False,'self_go_allowed':False},'next_gate':'HUMAN_LINE_REVIEW_ONLY','preregistration':{'path':'reports/GL2_A_prereg_five_evidence_20260715.md','sha256':'331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07','modified':False},'authoritative_estimand':{'entry':'entry_block+1','exit':'trigger_block+1/+5/+15','scope':'GL1-12 4h hold plus seconds exit ladder'},'prereg_text_estimand_defect':{'entry':'b0+1','exit':'b0+2/+6/+16','immediate_ladder_counts':immediate,'valid_for_GL1_12_alpha_beta':False},'population':{'full_universe_n':431,'keystone_subset_n':116,'pricing':'WETH'},'trigger_evidence_counts':ev['summary'],'rug_review':ev['review'],'summaries':summaries,'rows':rows,'rpc_ledger':json.loads((D/'base_gl2_a_rpc_ledger_20260715.json').read_text()),'source_hashes':{str(p.relative_to(ROOT)):sha(p) for p in (CORE,EVID,IMM,ETH,D/'base_gl2_a_rpc_full_ledger_20260715.jsonl')},'review_posture':'A remains partially identified; no grade or GO is self-assigned'}
 OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n');write_report(payload)
def f(v):return 'NA' if v is None else f'{v:+.4%}'
def write_report(x):
 lines=['---','sop_applied: true','alpha_id: AR-GL2-A-UNIVERSE-EV','family: F1/F4/F7','next_gate: HUMAN_LINE_REVIEW_ONLY','decision_flags:','  training_allowed: false','  live_allowed: false','  self_go_allowed: false','---','','# GL2-A：GL1-12 权威 trigger 锚五证据复核','','结论候选：A 仍为部分识别，不能定级或 GO。冻结预注册文字把退出写成 b0+2/+6/+16，但 GL1-12 的 +56% 来自 trigger_block+1/+5/+15（约4小时持有后秒级退出）；两者 estimand 不同。立即 round-trip 只作缺陷旁证，绝不用于解释 +56%。','','全人群=431同龄候选；KEYSTONE=其中冻结116；原生WETH；入场entry_block+1，退出trigger_block+1/+5/+15。','','|窗|人群|resolved/total|unresolved|确认rug|mean|p05/p25/p50/p75/p95|win rate|','|---|---|---:|---:|---:|---:|---|---:|']
 for w in WINDOWS:
  d=x['summaries'][str(w)]['including_confirmed_rugs']
  for k,n in [('full_universe','全宇宙'),('keystone_subset','KEYSTONE')]:
   s=d[k];lines.append(f"|+{w}|{n}|{s['n_resolved']}/{s['n_total']}|{s['n_unresolved']}|{s['n_confirmed_rug']}|{f(s['mean'])}|{'/'.join(f(s[q]) for q in ('p05','p25','p50','p75','p95'))}|{f(s['win_rate'])}|")
 lines+=['','|窗|beta(全宇宙)|条件alpha|KEYSTONE均值|部分识别|','|---|---:|---:|---:|---|']
 for w in WINDOWS:
  a=x['summaries'][str(w)]['including_confirmed_rugs']['alpha_beta_decomposition'];lines.append(f"|+{w}|{f(a['beta_component_full_universe_mean'])}|{f(a['selection_alpha_component'])}|{f(a['keystone_mean'])}|是|")
 lines+=['','## 五证据与 rug 降级','','fresh trigger RPC 后，E3 又对153个未决池从 block 42,699,129 到 cutoff 46,865,124 查询 `uniswap_v3_swaps_decoded FINAL`，得到11,287条事件且各窗没有后续真实卖出。但 cutoff 单点 atomic 不能排除中间曾恢复，E4 不能写 true；因此初筛141/143/145个 rug 全部降回 unresolved，最终确认rug=0，未使用−100%。','','|窗|fresh五证据前未决|最终未决|确认rug|','|---|---:|---:|---:|']
 for w in WINDOWS:lines.append(f"|+{w}|{x['trigger_evidence_counts']['unresolved_before_five_evidence'][str(w)]}|{x['summaries'][str(w)]['including_confirmed_rugs']['full_universe']['n_unresolved']}|0|")
 lines+=['','## 立即 ladder 旁证（不回答 GL1-12）','']
 for w in WINDOWS:lines.append(f"- b0+{1+w}：resolved {x['prereg_text_estimand_defect']['immediate_ladder_counts'][str(w)]['resolved']}/431，unresolved {x['prereg_text_estimand_defect']['immediate_ladder_counts'][str(w)]['unresolved']}；不得与4小时 +56% 混用。")
 lines+=['','真实第三方卖单只确认可卖；策略金额始终来自同块真实持仓整数 exact quote。没有 quote 就不按第三方单价缩放。纯只读，不训练、不下单、不签名、不广播、不写live/signals/Redis；最终分级由审查agent决定。','']
 REPORT.write_text('\n'.join(lines))
if __name__=='__main__':main()
