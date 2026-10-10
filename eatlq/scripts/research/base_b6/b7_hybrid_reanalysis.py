#!/usr/bin/env python3
"""Offline B5 conservative vs atomic-gated held-quantity trigger-price scenario."""
from __future__ import annotations
import csv,importlib.util,itertools,json,math,hashlib
from pathlib import Path
import numpy as np
ROOT=Path('/opt/EATLQ');D=ROOT/'mvp/diagnostics';OUT=D/'base_b7_hybrid_reanalysis_20260714.json';REPORT=ROOT/'reports/keystone_execution_gap_reanalysis_20260714.md'
def module():
 p=ROOT/'scripts/research/base_b5/b5_offline_reverify.py';s=importlib.util.spec_from_file_location('b5b7h',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def summ(vals):
 a=np.asarray(vals,float);k=max(1,math.ceil(.01*len(a)));o=np.argsort(a)[::-1];pos=np.maximum(a,0).sum();top=np.maximum(a[o[:k]],0).sum();rem=np.delete(a,o[:k])
 return {'n':len(a),'mean':float(a.mean()),'median':float(np.median(a)),'win_rate':float((a>0).mean()),'fatal_rate':float((a<=-.5).mean()),'top1_contribution':float(top/pos) if pos else 0.0,'top1_removed_mean':float(rem.mean())}
def main():
 m=module();eth_close=json.loads((D/'base_2min_accel_b3_eth_daily_20260714.json').read_text())['closes'];df,path,_=m.load_base();df['regime']=df.t0.map(m.regime);X,y=m.features(df);p,cut,_=m.fit_forward(df,X,y,list(X.columns));df['selected']=(p<=cut)&df.fric.notna()&(df.fric>=-.05)&df.regime.notna();sc={r.pool:r for r in df[df.selected].itertuples(index=False)};si={}
 def z(a):
  pool,b,ms,tx,li,a0,a1,t=a;return {'block':int(b),'ms':int(ms),'tx':tx,'log_index':int(li),'a0':int(a0),'a1':int(a1),'tick':int(t)}
 with open(m.SWAPS) as f:
  for pool,g in itertools.groupby(csv.reader(f,delimiter='\t'),key=lambda x:x[0]):
   if pool in sc:
    r=sc[pool];eth=float(eth_close.get(str(r.t0.date())) or min(eth_close.values()));si[pool]=m.simulate(r,[z(a) for a in g],path,eth)
 for pool,r in sc.items():
  if pool not in si:
   eth=float(eth_close.get(str(r.t0.date())) or min(eth_close.values()));si[pool]=m.simulate(r,[],path,eth)
 e=json.loads((D/'base_b7_execution_evidence_20260714.json').read_text());proxy={};gold=[]
 for x in e['rows']:
  if x['cohort']!='gap':continue
  r=sc[x['pool']];eth=float(eth_close.get(str(r.t0.date())) or min(eth_close.values()));gas=.15/(.001*eth);q=next(q for q in x['points'] if q['label']=='trigger');v=q.get('held_qty_qv2_amount_out')
  proxy[x['pool']]=(v/1e15-1-gas) if q.get('sell_ok') and v is not None else -1-gas
  if len(gold)<5:gold.append({'pool':x['pool'],'trigger_block':q['block'],'atomic_sell_ok':q.get('sell_ok'),'entry_qty':next(p for p in x['points'] if p['label']=='entry').get('received_target'),'held_qty_qv2_amount_out':v,'scenario_ret':proxy[x['pool']]})
 old={w:[] for w in ('W1','W2','W3')};hy={w:[] for w in old}
 for pool,r in sc.items():old[r.regime].append(si[pool]['ret']);hy[r.regime].append(proxy.get(pool,si[pool]['ret']))
 oldp=[v for w in old.values() for v in w];hyp=[v for w in hy.values() for v in w]
 perf={'b5_conservative':{'pooled':summ(oldp),**{w:summ(v) for w,v in old.items()}},'hybrid_trigger_proxy':{'pooled':summ(hyp),**{w:summ(v) for w,v in hy.items()}}}
 payload={'schema_version':1,'research_only':True,'rpc_calls':0,'go_decision':'NOT_MADE','definition':'B5 returns retained for 73 limit_filled; 43 gaps replaced at frozen exit trigger by entry-held quantity QuoterV2 output only when same-block AtomicRoundTrip sell succeeds, otherwise adverse','status':'EXPLORATORY_NOT_PREREGISTERED_CONFIRMATION','performance':perf,'golden':gold,'limitations':['AtomicRoundTrip buyer is contemporaneous helper, not the historical held wallet','QuoterV2 held-quantity price is a counterfactual pool quote, not our mined fill','scenario was defined after B5 gap diagnosis and cannot be called confirmatory PASS']}
 OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
 def pct(x):return f'{100*x:+.2f}%'
 lines=['# KEYSTONE 43笔执行缺口复核','', '> research-only；不作GO。Hybrid为诊断后定义的探索情景，不是预注册PASS。','', '## 结果','', '| 口径 | 窗 | n | mean | median | top1移除 | fatal |', '|---|---|---:|---:|---:|---:|---:|']
 for name in ('b5_conservative','hybrid_trigger_proxy'):
  for w in ('W1','W2','W3','pooled'):
   s=perf[name][w];lines.append(f"| {name} | {w} | {s['n']} | {pct(s['mean'])} | {pct(s['median'])} | {pct(s['top1_removed_mean'])} | {pct(s['fatal_rate'])} |")
 lines+=['','## 解释','','- B5的第三方swap限价证据缺失不等于AMM不能执行我方卖单；43个gap里冻结trigger块有40个同时具备Atomic sell与held-quantity quote。','- 替代情景把73个原limit_filled保持不变，只替换43个gap；若Atomic不可卖或held quote缺失仍按全损。','- 结果强烈说明B5的−100%缺失策略过度保守，但不能反向把hybrid当真实成交，因为历史held-wallet身份无法用纯eth_call完整保存。','- 正确后续是冻结这条新estimand，再做独立OOS/forward shadow；不得用本样本直接宣布PASS。','', '## Hash','','```',f"execution_evidence_sha256={hashlib.sha256((D/'base_b7_execution_evidence_20260714.json').read_bytes()).hexdigest()}",'```','']
 REPORT.write_text('\n'.join(lines));print(json.dumps({'out':str(OUT),'report':str(REPORT),'performance':perf},indent=2))
if __name__=='__main__':main()
