#!/usr/bin/env python3
"""Repair B6 weather coverage from factory logs plus bounded atomic probes. Read-only."""
from __future__ import annotations
import bisect, hashlib, json, os, subprocess, sys, threading, time
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path('/opt/EATLQ'); DIAG=ROOT/'mvp/diagnostics'
OUT=DIAG/'base_b6_weather_repair_20260714.json'; SNAPOUT=DIAG/'base_b6_universe_snapshot_corrected_20260714.json'; B4OUT=DIAG/'base_b4_arm_b_weather_augmented_20260714.json'
sys.path.insert(0,str(ROOT/'scripts/lib'))
from v3_quoterv2_struct import build_quote_exact_input_single_calldata,QUOTER_V2_BASE,WETH_BASE
WETH=WETH_BASE.lower(); SPEND=10**15; FROM='0x000000000000000000000000000000000000b608'; MAX_ATTEMPTS=12500
FACTORIES=['0x8909dc15e40173ff4699343b6eb8132c65e18ec6','0x33128a8fc17869897dce68ed026d694621f6fdfd','0x420dd381b31aef6683db6b902084cb0ffece40da','0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a']
UNIV3=FACTORIES[1]
TOPICS=['0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9',
        '0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118',
        # Aerodrome PoolFactory: PoolCreated(address,address,bool,address,uint256)
        '0x2128d88d14c80cb081c1252a5acff7a264671bf199ce226b53788fb26065005e',
        # Aerodrome Slipstream CLFactory: PoolCreated(address,address,int24,address)
        '0xab0d57f0df537bb25e80245ef7748fa62353808c54d6e528a9dd20887aed9ac2']
V3TOP=TOPICS[1]

class Rpc:
 def __init__(self,url): self.url=url; self.lock=threading.Lock(); self.logical=0; self.attempts=0; self.methods=Counter()
 def call(self,method,params,tries=3):
  if method not in {'eth_getLogs','eth_getBlockByNumber','eth_call','eth_blockNumber'}: raise RuntimeError('forbidden method')
  with self.lock: self.logical+=1; ident=self.logical
  last=None
  for retry in range(tries):
   with self.lock:
    if self.attempts>=MAX_ATTEMPTS: raise RuntimeError('RPC_ATTEMPT_HARD_CAP')
    self.attempts+=1; self.methods[method]+=1
   try:
    body=json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params}).encode(); req=Request(self.url,data=body,headers={'content-type':'application/json','user-agent':'EATLQ-B6-weather-repair/1'})
    with urlopen(req,timeout=180) as resp: obj=json.load(resp)
    if obj.get('error'): raise RuntimeError(str(obj['error']))
    return obj.get('result')
   except Exception as e: last=e; time.sleep(.5*(retry+1))
  raise RuntimeError(str(last)[:300])

def ts_block(rpc,block):
 x=rpc.call('eth_getBlockByNumber',[hex(block),False]); return int(x['timestamp'],16),x['hash']
def first_block_at(rpc,target,lo,hi):
 while lo<hi:
  mid=(lo+hi)//2; ts,_=ts_block(rpc,mid)
  if ts<target: lo=mid+1
  else: hi=mid
 return lo
def addr_topic(x): return '0x'+x[-40:].lower()
def parse_log(z):
 topics=z.get('topics') or []
 if len(topics)<3:return None
 t0=topics[0].lower(); token0=addr_topic(topics[1]); token1=addr_topic(topics[2])
 if WETH not in (token0,token1):return None
 data=z.get('data','0x')[2:]; words=[data[i:i+64] for i in range(0,len(data),64) if len(data[i:i+64])==64]
 pool=None; fee=None
 if t0==V3TOP:
  fee=int(topics[3],16) if len(topics)>3 else None
  if len(words)>=2:pool='0x'+words[1][-40:].lower()
 elif words: pool='0x'+words[0][-40:].lower()
 return {'factory':z['address'].lower(),'topic0':t0,'token0':token0,'token1':token1,'target_token':token1 if token0==WETH else token0,'pool':pool,'fee_event':fee,'block':int(z['blockNumber'],16),'block_hash':z.get('blockHash'),'tx_hash':z.get('transactionHash'),'log_index':int(z.get('logIndex','0x0'),16),'timestamp_raw':z.get('blockTimestamp')}

def scan(rpc,start,end):
 out=[]; total=(end-start+10000)//10000
 for i,b in enumerate(range(start,end,10000),1):
  e=min(end-1,b+9999); flt={'fromBlock':hex(b),'toBlock':hex(e),'address':FACTORIES,'topics':[TOPICS]}
  try: logs=rpc.call('eth_getLogs',[flt])
  except Exception:
   logs=[]
   for f in FACTORIES: logs+=rpc.call('eth_getLogs',[{'fromBlock':hex(b),'toBlock':hex(e),'address':f,'topics':[TOPICS]}])
  for z in logs:
   p=parse_log(z)
   if p:out.append(p)
  if i%50==0:print(f'W1 {i}/{total} logs={len(out)} attempts={rpc.attempts}',flush=True)
 ded={ (x['tx_hash'],x['log_index']):x for x in out}; return sorted(ded.values(),key=lambda x:(x['block'],x['log_index']))

def add_dates(rpc,rows,start_date,end_date,start_block,end_block):
 have=all(x.get('timestamp_raw') for x in rows)
 boundaries=[]
 if not have:
  for n in range((end_date-start_date).days+2):
   d=start_date+timedelta(days=n); target=int(datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc).timestamp())
   b=first_block_at(rpc,target,start_block,end_block); boundaries.append((d,b))
 blocks=[b for _,b in boundaries]
 for x in rows:
  if have:
   raw=x['timestamp_raw']; ts=int(raw,16) if isinstance(raw,str) and raw.startswith('0x') else int(raw); x['created_ms']=ts*1000; x['day']=str(datetime.fromtimestamp(ts,timezone.utc).date())
  else:
   j=max(0,bisect.bisect_right(blocks,x['block'])-1); x['day']=str(boundaries[j][0]); x['created_ms']=int(datetime.combine(boundaries[j][0],datetime.min.time(),tzinfo=timezone.utc).timestamp()*1000)
 return have,boundaries

def bytecode():
 out=Path('/tmp/b6repairforge'); art=out/'AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json'
 if not art.exists():subprocess.run([str(Path.home()/'.foundry/bin/forge'),'build','--contracts','scripts/research/base_b4/AtomicRoundTrip.sol','--out',str(out),'--cache-path','/tmp/b6repaircache'],cwd=ROOT,check=True)
 return json.loads(art.read_text())['bytecode']['object'].removeprefix('0x')
def atomic_data(bc,token,fee):return '0x'+bc+token.removeprefix('0x').rjust(64,'0')+f'{fee:064x}'+f'{SPEND:064x}'
def adecode(raw):
 if not raw or len(raw)<2+16*64:raise ValueError('short atomic')
 w=[int(raw[2+i*64:2+(i+1)*64],16) for i in range(16)];ks=['flags','router_buy_out','target_before','target_after_buy','target_after_sell','router_sell_out','weth_after_deposit','weth_after_buy','weth_after_sell','pin_block','chain_id','parent_block_hash','we','be','te','se'];return dict(zip(ks,w))

def choose_extra(logrows,oldrows,old_snap,start=date(2026,2,27),end=date(2026,6,8),target=30):
 bypool={x['pool']:x for x in old_snap['b3_rows']}; existing=[]; oldp=set()
 for r in oldrows:
  u=bypool.get(r['pool_address'].lower())
  if u: existing.append((datetime.fromtimestamp(u['created_ms']/1000,timezone.utc),r));oldp.add(r['pool_address'].lower())
 cands=[x for x in logrows if x['factory']==UNIV3 and x['topic0']==V3TOP and x.get('pool') and x['pool'] not in oldp and date.fromisoformat(x['day'])>=start-timedelta(days=7) and date.fromisoformat(x['day'])<=end]
 cands.sort(key=lambda x:hashlib.sha256(('B6-weather-sample|'+x['pool']).encode()).hexdigest())
 selected=[]; selectedp=set();d=start
 while d<=end:
  cut=datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc);a=cut-timedelta(days=7)
  n=sum(a<=t<cut for t,_ in existing)+sum(a<=datetime.fromtimestamp(x['created_ms']/1000,timezone.utc)<cut for x in selected)
  if n<target:
   eligible=[x for x in cands if x['pool'] not in selectedp and a<=datetime.fromtimestamp(x['created_ms']/1000,timezone.utc)<cut]
   for x in eligible[:target-n]:selected.append(x);selectedp.add(x['pool'])
  d+=timedelta(days=1)
 return selected

def probe(rpc,bc,x):
 row={'pool_address':x['pool'],'target_token':x['target_token'],'snapshot_120':x['block']+60,'window':'weather_repair','sample_key':hashlib.sha256(x['pool'].encode()).hexdigest(),'created_ms':x['created_ms']}
 try:
  fr=rpc.call('eth_call',[{'to':x['pool'],'data':'0xddca3f43'},hex(row['snapshot_120'])]);fee=int(fr,16)
  tx={'from':FROM,'data':atomic_data(bc,x['target_token'],fee),'value':hex(SPEND),'gas':hex(20_000_000)};raw=rpc.call('eth_call',[tx,hex(row['snapshot_120']),{FROM:{'balance':hex(10**18)}}]);a=adecode(raw)
  rec=a['target_after_buy']-a['target_before'];buy=bool(a['flags']&2) and rec>0;sell=bool(a['flags']&8) and a['weth_after_sell']>a['weth_after_buy'];recovered=a['weth_after_sell']-a['weth_after_buy'] if sell else 0
  row.update(fee=fee,atomic=a,received_target=rec,buy_ok=buy,sell_ok=sell,recovered_weth=recovered,severe_entry_adverse=bool(buy and(not sell or recovered/SPEND<=.10)),instant_hp=bool(buy and not sell),status='measured')
  if rec>0:
   qd=build_quote_exact_input_single_calldata(x['target_token'],WETH_BASE,rec,fee);qr=rpc.call('eth_call',[{'to':QUOTER_V2_BASE,'data':qd},hex(row['snapshot_120'])]);row['qv2_amount_out']=int(qr[2:66],16) if qr and len(qr)>=66 else 0
  else:row['qv2_amount_out']=None
 except Exception as e:row.update(status='unknown_adverse',error=f'{type(e).__name__}:{str(e)[:220]}',severe_entry_adverse=True,instant_hp=False,qv2_amount_out=None)
 return row

def main():
 url=os.environ.get('FORK_ARCHIVE_RPC') or os.environ.get('LABEL_RPC_URL');
 if not url:raise RuntimeError('FORK_ARCHIVE_RPC required')
 rpc=Rpc(url);head=int(rpc.call('eth_blockNumber',[]),16);ht,_=ts_block(rpc,head)
 start_dt=datetime(2026,1,23,tzinfo=timezone.utc);end_dt=datetime(2026,6,9,tzinfo=timezone.utc)
 approx_lo=max(1,head-int((datetime.fromtimestamp(ht,timezone.utc)-start_dt).total_seconds()/2)-100000);start_block=first_block_at(rpc,int(start_dt.timestamp()),approx_lo,head)
 approx_end=max(start_block,head-int((datetime.fromtimestamp(ht,timezone.utc)-end_dt).total_seconds()/2)-100000);end_block=first_block_at(rpc,int(end_dt.timestamp()),approx_end,head)
 logs=scan(rpc,start_block,end_block);timestamp_in_logs,bounds=add_dates(rpc,logs,start_dt.date(),end_dt.date(),start_block,end_block)
 oldb4=json.loads((DIAG/'base_b4_arm_b_20260714.json').read_text());oldsnap=json.loads((DIAG/'base_b6_universe_snapshot_20260714.json').read_text())
 extra=choose_extra(logs,oldb4['rows'],oldsnap);print(f'W3 selected_extra={len(extra)} attempts={rpc.attempts}',flush=True)
 bc=bytecode();new=[]
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs=[ex.submit(probe,rpc,bc,x) for x in extra]
  for i,f in enumerate(as_completed(fs),1):
   new.append(f.result())
   if i%50==0:print(f'W3 {i}/{len(extra)} attempts={rpc.attempts}',flush=True)
 new.sort(key=lambda x:x['sample_key']);combined=oldb4['rows']+new
 # Synthetic B3 rows let frozen weather_by_day timestamp the added probes.
 synth=[]
 existing={x['pool'] for x in oldsnap['b3_rows']}
 for r in new:
  if r['pool_address'] not in existing:synth.append({'pool':r['pool_address'],'pool_type':'v3_weather_repair','token':r['target_token'],'created_block':r['snapshot_120']-60,'created_ms':r['created_ms'],'snapshot_120':r['snapshot_120'],'window':'weather_repair'})
 issuance_ms=[x['created_ms'] for x in logs]
 snap={'schema_version':2,'research_only':True,'source':'complete approved-factory getLogs census; UTC 2026-01-23..2026-06-09','rpc_calls':rpc.logical,'b3_rows':oldsnap['b3_rows']+synth,'issuance_created_ms':issuance_ms,'issuance_start':'2026-01-23T00:00:00Z'}
 b4aug={**oldb4,'rows':combined,'weather_repair':{'new_rows':len(new),'population':'all newly created Uniswap V3 WETH pools in fixed-hash gap sample; not restricted to B3 fresh class'}}
 SNAPOUT.write_text(json.dumps(snap,separators=(',',':'),sort_keys=True)+'\n');B4OUT.write_text(json.dumps(b4aug,indent=2,sort_keys=True)+'\n')
 perday=Counter(x['day'] for x in logs);summary={'start_block':start_block,'end_block_exclusive':end_block,'factory_weth_events':len(logs),'days_with_events':len(perday),'timestamp_in_log_response':timestamp_in_logs,'extra_proxy_rows':len(new),'extra_unknown':sum(x['status']!='measured' for x in new),'per_day':dict(sorted(perday.items()))}
 payload={'schema_version':1,'research_only':True,'posture':'getLogs/getBlockByNumber/eth_call only; no sign/send/broadcast','rpc':{'logical_calls':rpc.logical,'http_attempts':rpc.attempts,'api_credits':rpc.attempts*20,'hard_cap_attempts':MAX_ATTEMPTS,'methods':dict(rpc.methods)},'summary':summary,'source_hashes':{'old_b4':hashlib.sha256((DIAG/'base_b4_arm_b_20260714.json').read_bytes()).hexdigest(),'old_snapshot':hashlib.sha256((DIAG/'base_b6_universe_snapshot_20260714.json').read_bytes()).hexdigest()},'factory_events':logs}
 OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n');print(json.dumps({'out':str(OUT),'snapshot':str(SNAPOUT),'b4':str(B4OUT),'rpc':payload['rpc'],'summary':{k:v for k,v in summary.items() if k!='per_day'}},indent=2))
if __name__=='__main__':main()
