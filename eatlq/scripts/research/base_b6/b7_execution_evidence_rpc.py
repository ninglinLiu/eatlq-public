#!/usr/bin/env python3
"""Pinned archive eth_call evidence for B5 selected pools. No signing/broadcast."""
from __future__ import annotations
import csv, hashlib, importlib.util, itertools, json, os, subprocess, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path('/opt/EATLQ'); OUT=ROOT/'mvp/diagnostics/base_b7_execution_evidence_20260714.json'
sys.path.insert(0,str(ROOT/'scripts/lib'))
from v3_quoterv2_struct import build_quote_exact_input_single_calldata,QUOTER_V2_BASE,WETH_BASE
SPEND=10**15; FROM='0x000000000000000000000000000000000000b707'; MAX_ATTEMPTS=1100

class Rpc:
 def __init__(self,url): self.url=url; self.lock=threading.Lock(); self.logical=0; self.attempts=0; self.methods=Counter()
 def call(self,method,params):
  if method!='eth_call': raise RuntimeError('forbidden method')
  with self.lock: self.logical+=1; ident=self.logical
  last=None
  for retry in range(3):
   with self.lock:
    if self.attempts>=MAX_ATTEMPTS: raise RuntimeError('RPC_ATTEMPT_HARD_CAP')
    self.attempts+=1; self.methods[method]+=1
   try:
    body=json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params}).encode()
    req=Request(self.url,data=body,headers={'content-type':'application/json','user-agent':'EATLQ-B7-readonly/1'})
    with urlopen(req,timeout=120) as resp: obj=json.load(resp)
    if obj.get('error'): raise RuntimeError(str(obj['error']))
    return obj.get('result'),hashlib.sha256(json.dumps(params,separators=(',',':'),sort_keys=True).encode()).hexdigest(),hashlib.sha256(str(obj.get('result')).encode()).hexdigest()
   except Exception as e: last=e; time.sleep(.4*(retry+1))
  raise RuntimeError(str(last)[:300])

def b5mod():
 p=ROOT/'scripts/research/base_b5/b5_offline_reverify.py'; s=importlib.util.spec_from_file_location('b5b7',p)
 m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def selected_rows():
 m=b5mod(); df,path,_=m.load_base(); df['regime']=df.t0.map(m.regime)
 X,y=m.features(df); prob,cut,_=m.fit_forward(df,X,y,list(X.columns))
 df['selected']=(prob<=cut)&df.fric.notna()&(df.fric>=-.05)&df.regime.notna()
 scoped={r.pool:r for r in df[df.selected].itertuples(index=False)}; sims={}
 def parsed(z):
  pool,block,ms,tx,li,a0,a1,tick=z
  return {'block':int(block),'ms':int(ms),'tx':tx,'log_index':int(li),'a0':int(a0),'a1':int(a1),'tick':int(tick)}
 with open(m.SWAPS) as fh:
  for pool,grp in itertools.groupby(csv.reader(fh,delimiter='\t'),key=lambda z:z[0]):
   if pool in scoped: sims[pool]=m.simulate(scoped[pool],[parsed(z) for z in grp],path,1658.75)
 for pool,r in scoped.items():
  if pool not in sims: sims[pool]=m.simulate(r,[],path,1658.75)
 gaps=sorted([p for p,v in sims.items() if v['reason']!='limit_filled'])
 controls=sorted([p for p,v in sims.items() if v['reason']=='limit_filled'],key=lambda p:hashlib.sha256(('B7-control|'+p).encode()).hexdigest())[:20]
 rows=[]
 for pool in gaps+controls:
  r=scoped[pool]; sim=sims[pool]
  rows.append({'pool':pool,'token':r.alt.lower(),'entry_block':int(r.entry_block),'exit_trigger_block':int(sim.get('exb') or r.legacy_exb),
               'old_reason':sim['reason'],'cohort':'gap' if pool in gaps else 'control','regime':r.regime})
 assert len(gaps)==43 and len(controls)==20 and len(rows)==63,(len(gaps),len(controls))
 return rows

def bytecode():
 out=Path('/tmp/b7forgeout'); art=out/'AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json'
 if not art.exists(): subprocess.run([str(Path.home()/'.foundry/bin/forge'),'build','--contracts','scripts/research/base_b4/AtomicRoundTrip.sol','--out',str(out),'--cache-path','/tmp/b7forgecache'],cwd=ROOT,check=True)
 return json.loads(art.read_text())['bytecode']['object'].removeprefix('0x')

def atomic_data(bc,token,fee): return '0x'+bc+token.removeprefix('0x').rjust(64,'0')+f'{fee:064x}'+f'{SPEND:064x}'
def decode(raw):
 if not raw or len(raw)<2+16*64: raise ValueError('short atomic return')
 w=[int(raw[2+i*64:2+(i+1)*64],16) for i in range(16)]
 ks=['flags','router_buy_out','target_before','target_after_buy','target_after_sell','router_sell_out','weth_after_deposit','weth_after_buy','weth_after_sell','pin_block','chain_id','parent_block_hash','weth_approve_error_hash','buy_error_hash','target_approve_error_hash','sell_error_hash']
 return dict(zip(ks,w))

def work(rpc,bc,row):
 out={**row,'points':[]}
 try:
  fr,fh,frh=rpc.call('eth_call',[{'to':row['pool'],'data':'0xddca3f43'},hex(row['entry_block'])]); fee=int(fr,16); out.update(fee=fee,fee_request_sha256=fh,fee_response_sha256=frh)
  entry_qty=None
  for label,block in [('entry',row['entry_block']),('trigger',row['exit_trigger_block']),('plus_30m',row['exit_trigger_block']+900),('plus_60m',row['exit_trigger_block']+1800)]:
   p={'label':label,'block':block}
   try:
    tx={'from':FROM,'data':atomic_data(bc,row['token'],fee),'value':hex(SPEND),'gas':hex(20_000_000)}
    raw,ah,arh=rpc.call('eth_call',[tx,hex(block),{FROM:{'balance':hex(10**18)}}]); a=decode(raw)
    received=a['target_after_buy']-a['target_before']; buy=bool(a['flags']&2) and received>0
    sell=bool(a['flags']&8) and a['weth_after_sell']>a['weth_after_buy']; recovered=a['weth_after_sell']-a['weth_after_buy'] if sell else 0
    p.update(status='measured',buy_ok=buy,sell_ok=sell,received_target=received,recovered_weth=recovered,atomic_response_sha256=arh,atomic_request_sha256=ah)
    if label=='entry' and received>0: entry_qty=received
    if received>0:
     try:
      qd=build_quote_exact_input_single_calldata(row['token'],WETH_BASE,received,fee)
      qr,qh,qrh=rpc.call('eth_call',[{'to':QUOTER_V2_BASE,'data':qd},hex(block)])
      p.update(qv2_amount_out=int(qr[2:66],16) if qr and len(qr)>=66 else 0,qv2_request_sha256=qh,qv2_response_sha256=qrh)
     except Exception as qe: p.update(qv2_amount_out=None,qv2_error=f'{type(qe).__name__}:{str(qe)[:180]}')
    else: p['qv2_amount_out']=None
    if entry_qty:
     if label=='entry': p['held_qty_qv2_amount_out']=p['qv2_amount_out']
     else:
      try:
       hd=build_quote_exact_input_single_calldata(row['token'],WETH_BASE,entry_qty,fee)
       hr,hh,hrh=rpc.call('eth_call',[{'to':QUOTER_V2_BASE,'data':hd},hex(block)])
       p.update(held_qty_qv2_amount_out=int(hr[2:66],16) if hr and len(hr)>=66 else 0,held_qty_qv2_request_sha256=hh,held_qty_qv2_response_sha256=hrh)
      except Exception as he: p.update(held_qty_qv2_amount_out=None,held_qty_qv2_error=f'{type(he).__name__}:{str(he)[:180]}')
   except Exception as e: p.update(status='unknown_adverse',error=f'{type(e).__name__}:{str(e)[:240]}',buy_ok=False,sell_ok=False)
   out['points'].append(p)
 except Exception as e: out.update(status='unknown_adverse',error=f'{type(e).__name__}:{str(e)[:240]}')
 return out

def main():
 url=os.environ.get('FORK_ARCHIVE_RPC') or os.environ.get('LABEL_RPC_URL');
 if not url: raise RuntimeError('FORK_ARCHIVE_RPC required')
 rows=selected_rows(); rpc=Rpc(url); bc=bytecode(); result=[]
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs=[ex.submit(work,rpc,bc,r) for r in rows]
  for i,f in enumerate(as_completed(fs),1):
   result.append(f.result())
   if i%10==0: print(f'E1 {i}/{len(rows)} logical={rpc.logical} attempts={rpc.attempts}',flush=True)
 result.sort(key=lambda x:(x['cohort'],x['pool']))
 summary={'rows':len(result),'gap':sum(x['cohort']=='gap' for x in result),'control':sum(x['cohort']=='control' for x in result),
          'gap_sellable_by_point':{lab:sum(x['cohort']=='gap' and next((p.get('sell_ok',False) for p in x.get('points',[]) if p['label']==lab),False) for x in result) for lab in ('entry','trigger','plus_30m','plus_60m')},
          'unknown_rows':sum(any(p.get('status')!='measured' for p in x.get('points',[])) or x.get('status')=='unknown_adverse' for x in result)}
 payload={'schema_version':1,'research_only':True,'posture':'pinned eth_call only; no sign/send/broadcast','rpc':{'logical_calls':rpc.logical,'http_attempts':rpc.attempts,'api_credits':rpc.attempts*20,'hard_cap_attempts':MAX_ATTEMPTS,'methods':dict(rpc.methods)},'summary':summary,'rows':result}
 OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n'); print(json.dumps({'out':str(OUT),'rpc':payload['rpc'],'summary':summary},indent=2))
if __name__=='__main__': main()
