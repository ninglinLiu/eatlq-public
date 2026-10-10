#!/usr/bin/env python3
"""Pinned archive WETH-depth audit for B5. eth_call only."""
import glob, hashlib, json, os, threading, time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path('/opt/EATLQ'); LEFT=ROOT/'scripts/research/base_lefttail'
OUT=ROOT/'mvp/diagnostics/base_b5_lp_monitor_20260714.json'
WETH='0x4200000000000000000000000000000000000006'; SEL='70a08231'

class Rpc:
 def __init__(self,url): self.url=url; self.calls=0; self.attempts=0; self.lock=threading.Lock()
 def call(self,to,data,block):
  with self.lock: self.calls+=1; ident=self.calls
  params=[{'to':to,'data':data},hex(int(block))]; last=None
  for j in range(3):
   self.attempts+=1
   try:
    q=Request(self.url,data=json.dumps({'jsonrpc':'2.0','id':ident,'method':'eth_call','params':params}).encode(),headers={'content-type':'application/json','user-agent':'EATLQ-B5-readonly/1'})
    with urlopen(q,timeout=90) as r: o=json.load(r)
    if o.get('error'): raise RuntimeError(str(o['error']))
    raw=o['result']; return int(raw,16),hashlib.sha256(json.dumps(params,separators=(',',':'),sort_keys=True).encode()).hexdigest(),hashlib.sha256(raw.encode()).hexdigest()
   except Exception as e: last=e;time.sleep(.3*(j+1))
  raise RuntimeError(str(last)[:240])

def main():
 url=os.environ.get('FORK_ARCHIVE_RPC') or os.environ.get('LABEL_RPC_URL')
 if not url: raise SystemExit('approved archive env required')
 rpc=Rpc(url); rows=[]
 for p in sorted(glob.glob(str(LEFT/'_uni_shards/_uni_s*.json'))): rows += json.load(open(p))['pools']
 priced=json.load(open(LEFT/'_priced_live.json'))
 rugs=[r for r in rows if r.get('label')=='delayed_rug']
 selected=[]
 for r in rugs:
  early=int(r.get('flip_offset') or 10**18)<=300
  exb=(priced.get(r['pool']) or {}).get('exb'); loss=bool(exb and int(r['flip_block'])<=int(exb))
  if early or loss: selected.append((r,early,loss,exb))
 out=[]
 for r,early,loss,exb in selected:
  pool=r['pool'].lower(); data='0x'+SEL+pool[2:].rjust(64,'0')
  rec={'pool':pool,'entry_block':int(r['entry_block']),'flip_block_checkpoint':int(r['flip_block']),
       'flip_interval':r.get('flip_interval'),'flip_offset':int(r['flip_offset']),'early_le_300':early,
       'before_or_at_exit':loss,'exit_block':exb,'mechanism':r.get('mechanism'),'status':'measured'}
  try:
   entry,eh,erh=rpc.call(WETH,data,r['entry_block']); rec.update(entry_weth_raw=entry,entry_request_sha256=eh,entry_response_sha256=erh)
   interval=r.get('flip_interval') or [r['entry_block'],r['flip_block']]; lo,hi=map(int,interval)
   dhi,_,_=rpc.call(WETH,data,hi)
   threshold=entry//2
   if dhi>threshold:
    rec.update(first_depth_below_50pct=None,pre_signal_weth_raw=None,signal_weth_raw=dhi,
               pre_signal_below_80pct=False,rescue_proven=False,note='no_50pct_depth_collapse_in_interval')
   else:
    while hi-lo>1:
     mid=(lo+hi)//2; dm,_,_=rpc.call(WETH,data,mid)
     if dm<=threshold: hi=mid
     else: lo=mid
    pre,ph,prh=rpc.call(WETH,data,hi-1); sig,sh,srh=rpc.call(WETH,data,hi)
    prewarn=pre*5 < entry*4
    rec.update(first_depth_below_50pct=hi,pre_signal_block=hi-1,pre_signal_weth_raw=pre,signal_weth_raw=sig,
      pre_signal_request_sha256=ph,pre_signal_response_sha256=prh,signal_request_sha256=sh,signal_response_sha256=srh,
      pre_signal_below_80pct=prewarn,
      rescue_proven=bool(loss and prewarn and hi-1<int(r['flip_block'])),
      prewarning_but_already_exited=bool((not loss) and prewarn))
  except Exception as e: rec.update(status='unknown_adverse',error=f'{type(e).__name__}:{str(e)[:200]}',rescue_proven=False)
  out.append(rec)
 result={'schema_version':1,'posture':'pinned archive eth_call only; no send/sign/broadcast','rpc':{'endpoint_id':'FORK_ARCHIVE_RPC secret omitted','logical_calls':rpc.calls,'attempts':rpc.attempts},
   'selection':{'all_delayed_rug':len(rugs),'early_le_300_or_real_loss_unique':len(selected)},'thresholds':{'collapse':0.5,'prewarning':0.8},
   'summary':{'measured':sum(x['status']=='measured' for x in out),'unknown_adverse':sum(x['status']!='measured' for x in out),
      'early_n':sum(x['early_le_300'] for x in out),'real_loss_n':sum(x['before_or_at_exit'] for x in out),
      'prewarning_n':sum(x.get('pre_signal_below_80pct',False) for x in out),
      'prewarning_among_real_loss_n':sum(x.get('pre_signal_below_80pct',False) and x.get('before_or_at_exit',False) for x in out),
      'prewarning_but_already_exited_n':sum(x.get('prewarning_but_already_exited',False) for x in out),
      'rescue_proven_n':sum(x.get('rescue_proven',False) for x in out)},'rows':out}
 OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n');print(json.dumps({'out':str(OUT),'rpc':result['rpc'],'selection':result['selection'],'summary':result['summary']},indent=2))
if __name__=='__main__':main()
