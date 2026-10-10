#!/usr/bin/env python3
"""Independent archive verification of B5 decoded swap golden; read-only RPC."""
import hashlib,json,os,time
from pathlib import Path
from urllib.request import Request,urlopen
ROOT=Path('/opt/EATLQ'); SRC=ROOT/'mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json';OUT=ROOT/'mvp/diagnostics/base_b5_golden_rpc_20260714.json'
ALLOWED={'eth_getTransactionReceipt','eth_getBlockByNumber'}
class Rpc:
 def __init__(self,url):self.url=url;self.calls=0;self.attempts=0
 def call(self,m,p):
  if m not in ALLOWED:raise RuntimeError('forbidden method')
  self.calls+=1
  for j in range(3):
   self.attempts+=1
   try:
    q=Request(self.url,data=json.dumps({'jsonrpc':'2.0','id':self.calls,'method':m,'params':p}).encode(),headers={'content-type':'application/json','user-agent':'EATLQ-B5-golden-readonly/1'})
    with urlopen(q,timeout=90) as r:o=json.load(r)
    if o.get('error'):raise RuntimeError(str(o['error']))
    raw=o['result'];return raw,hashlib.sha256(json.dumps(p,separators=(',',':')).encode()).hexdigest(),hashlib.sha256(json.dumps(raw,separators=(',',':'),sort_keys=True).encode()).hexdigest()
   except Exception as e:last=e;time.sleep(.3*(j+1))
  raise RuntimeError(str(last)[:240])
def sint(word):
 x=int(word,16);return x-(1<<256) if x>=1<<255 else x
def verify_leg(rpc,pool,tx,block,a0,a1):
 rec,rh,rrh=rpc.call('eth_getTransactionReceipt',[tx]); blk,bh,brh=rpc.call('eth_getBlockByNumber',[hex(block),False])
 hits=[]
 for log in rec.get('logs',[]):
  if log.get('address','').lower()!=pool.lower():continue
  raw=log.get('data','0x')[2:]
  if len(raw)>=128:hits.append((sint(raw[:64]),sint(raw[64:128]),int(log['logIndex'],16)))
 exact=[x for x in hits if x[0]==int(a0) and x[1]==int(a1)]
 return {'tx':tx,'block':block,'receipt_block':int(rec['blockNumber'],16),'block_hash':blk['hash'],'parent_hash':blk['parentHash'],
   'receipt_request_sha256':rh,'receipt_response_sha256':rrh,'block_request_sha256':bh,'block_response_sha256':brh,
   'decoded_candidates':len(hits),'exact_matches':len(exact),'matched_log_index':exact[0][2] if exact else None,
   'amount0_raw_expected':str(a0),'amount1_raw_expected':str(a1),'integer_equal':len(exact)==1 and int(rec['blockNumber'],16)==block}
def main():
 url=os.environ.get('FORK_ARCHIVE_RPC') or os.environ.get('LABEL_RPC_URL')
 if not url:raise SystemExit('approved archive env required')
 rpc=Rpc(url);src=json.load(open(SRC));rows=[]
 for g in src['golden']:
  rows.append({'pool':g['pool'],'entry':verify_leg(rpc,g['pool'],g['entry_tx'],g['entry_block'],g['entry_amount0_raw'],g['entry_amount1_raw']),
    'exit':verify_leg(rpc,g['pool'],g['exit_tx'],g['exit_block'],g['exit_amount0_raw'],g['exit_amount1_raw'])})
 out={'schema_version':1,'posture':'archive readonly receipts/blocks; no send/sign/broadcast','rpc':{'endpoint_id':'FORK_ARCHIVE_RPC secret omitted','logical_calls':rpc.calls,'attempts':rpc.attempts},
  'all_integer_equal':all(x[k]['integer_equal'] for x in rows for k in ('entry','exit')),'rows':rows}
 OUT.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n');print(json.dumps({'out':str(OUT),'rpc':out['rpc'],'golden_pools':len(rows),'all_integer_equal':out['all_integer_equal']},indent=2))
if __name__=='__main__':main()
