"""Frozen supplier-wallet trial: receipts and historical pool price references."""
import argparse
import json
import math
from datetime import datetime,timezone
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from statistics import mean, median
from eth_utils import keccak
from eth_abi import decode
from evidence import ROOT, read, save, rpc, now,fetch
from ledger import WETH,USDC

URL='https://mainnet.base.org'
def topic(s):return '0x'+keccak(text=s).hex()
TRANSFER=topic('Transfer(address,address,uint256)')
V3=topic('Swap(address,address,int256,int256,uint160,uint128,int24)')
V2=topic('Swap(address,uint256,uint256,uint256,uint256,address)')
V4=topic('Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)')

def req(method,params,label):
    m,d=rpc(URL,method,params,label)
    # One delayed retry of a previously cached throttling response; never retry
    # authentication failures or cycle providers to defeat a rate limit.
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(m['finished'])).total_seconds()
    if m.get('http_status')==429 and age>=60:
        m,d=fetch(URL,{'jsonrpc':'2.0','id':1,'method':method,'params':params},label,cache=False)
    return d.get('result') if isinstance(d,dict) else None, {'key':m['key'],'http_status':m.get('http_status'),'rpc_error':d.get('error') if isinstance(d,dict) else None}

def call(to,data,block,label):return req('eth_call',[{'to':to,'data':data},hex(block)],label)

def one_receipt(r):
    rec,evidence=req('eth_getTransactionReceipt',[r['tx_hash']],'pilot_source_receipt')
    out={**r,'receipt_evidence':evidence,'swap_logs':[]}
    if not rec:out['receipt_status']='UNAVAILABLE';return out
    nets=defaultdict(int)
    for l in rec.get('logs',[]):
        tt=l.get('topics',[])
        if not tt:continue
        if tt[0]==TRANSFER and len(tt)>=3:
            qty=int(l['data'],16);f='0x'+tt[1][-40:];t='0x'+tt[2][-40:]
            nets[l['address'].lower()]+=qty*(int(t==r['wallet'])-int(f==r['wallet']))
        if tt[0] in (V2,V3,V4):
            out['swap_logs'].append({'pool':l['address'].lower(),'kind':{V2:'V2',V3:'V3',V4:'V4'}[tt[0]],
                'log_index':int(l['logIndex'],16),'data':l['data'],'topics':tt})
    out['receipt_token_net_raw']=str(nets[r['token']])
    out['receipt_token_net_matches_index']=nets[r['token']]==int(r['token_net_raw'])
    out['receipt_from_matches_wallet']=rec.get('from','').lower()==r['wallet']
    out['receipt_status']='CONFIRMED_SUCCESS' if int(rec.get('status','0x0'),16)==1 else 'REVERT'
    out['source_fee_receipt_raw']=str(int(rec['gasUsed'],16)*int(rec.get('effectiveGasPrice','0x0'),16)+int(rec.get('l1Fee','0x0'),16))
    return out

def receipts():
    entries=read('pilot_entries.json')['rows']
    with ThreadPoolExecutor(max_workers=3) as ex:rows=list(ex.map(one_receipt,entries))
    save('pilot_receipts.json',rows)
    print(json.dumps({'n':len(rows),'status':dict(Counter(r['receipt_status'] for r in rows)),
        'index_match':sum(r.get('receipt_token_net_matches_index',False) for r in rows),
        'swap_kinds':dict(Counter(l['kind'] for r in rows for l in r['swap_logs']))},indent=2))

def metadata():
    rows=read('pilot_receipts.json')
    pairs=sorted({(l['pool'],r['block_number']) for r in rows for l in r['swap_logs'] if l['kind'] in ('V2','V3')})
    def one(j):
        p,b=j;out={'pool':p,'block_number':b}
        for name,selector in [('token0','0x0dfe1681'),('token1','0xd21220a7'),('fee','0xddca3f43')]:
            value,ev=call(p,selector,b,'pilot_pool_'+name)
            out[name+'_evidence']=ev
            if value and value!='0x':out[name]=('0x'+value[-40:]).lower() if name.startswith('token') else int(value,16)
        return out
    with ThreadPoolExecutor(max_workers=3) as ex:ms=list(ex.map(one,pairs))
    save('pilot_pool_metadata.json',ms)
    print(json.dumps(ms[:3],indent=2));print('pools',len(ms),'resolved',sum('token0' in m and 'token1' in m for m in ms))

def price_from_state(kind,data,token0_is_target):
    if not data or data=='0x':return None
    raw=bytes.fromhex(data[2:])
    if kind=='V3':
        sqrt=int.from_bytes(raw[:32],'big')
        if sqrt<=0:return None
        ratio=(sqrt/(2**96))**2
    else:
        if len(raw)<64:return None
        a=int.from_bytes(raw[:32],'big');b=int.from_bytes(raw[32:64],'big')
        if not a or not b:return None
        ratio=b/a
    return ratio if token0_is_target else 1/ratio

def paths():
    rows=read('pilot_receipts.json');ms={(m['pool'],m['block_number']):m for m in read('pilot_pool_metadata.json')}
    ledger=read('account_cash_ledger.json')['rows']
    outputs=[]
    for r in rows:
        out={'wallet':r['wallet'],'token':r['token'],'source_tx':r['tx_hash'],'source_block':r['block_number'],
             'source_ts':r['timestamp'],'index_complete':r['index_complete'],'price_kind':'HISTORICAL_POOL_MID_NOT_OWN_FILL'}
        matches=[]
        for l in r['swap_logs']:
            m=ms.get((l['pool'],r['block_number']),{})
            if {m.get('token0'),m.get('token1')}=={r['token'],WETH if r['quote']=='ETH' else USDC}:
                matches.append((l,m))
        if len(matches)!=1:
            out.update(status='UNRESOLVED_NO_UNIQUE_DIRECT_QUOTE_POOL',swap_kinds=[l['kind'] for l in r['swap_logs']]);outputs.append(out);continue
        l,m=matches[0];out.update(pool=l['pool'],kind=l['kind'],quote=r['quote'],fee_units=m.get('fee'))
        out['token0_is_target']=m['token0']==r['token']
        source_sells=sorted([x for x in ledger if x['wallet']==r['wallet'] and x.get('token')==r['token'] and x.get('side')=='sell' and x['block_number']>r['block_number']],key=lambda x:x['block_number'])
        sell_block=source_sells[0]['block_number']+5 if source_sells else None
        # Verify 2-second blocktime at source and at longest horizon, not assume from timestamps alone.
        source_block,bev=req('eth_getBlockByNumber',[hex(r['block_number']),False],'pilot_source_block')
        end_block,eev=req('eth_getBlockByNumber',[hex(r['block_number']+43205),False],'pilot_end_block')
        if not source_block or not end_block or int(end_block['timestamp'],16)-int(source_block['timestamp'],16)!=86410:
            out.update(status='UNRESOLVED_BLOCK_TIME_MAPPING',source_block_evidence=bev,end_block_evidence=eev);outputs.append(out);continue
        checkpoints={f'entry_{d}s':r['block_number']+math.ceil(d/2) for d in (1,10,30,60)}
        # Hold from primary copied entry; +100% policy needs a full monitored path,
        # so no high/low or sampled endpoint is used to fabricate a threshold exit.
        for h,seconds in [('30m',1800),('2h',7200),('24h',86400)]:checkpoints['exit_'+h]=r['block_number']+5+seconds//2
        if sell_block:checkpoints['exit_source_first_sell']=sell_block
        state=[]
        for name,b in checkpoints.items():
            raw,ev=call(l['pool'],'0x3850c7bd' if l['kind']=='V3' else '0x0902f1ac',b,'pilot_historical_state')
            price=price_from_state(l['kind'],raw,out['token0_is_target'])
            state.append({'name':name,'block':b,'price_raw_quote_per_target':price,'evidence':ev})
        out.update(status='PRICE_PROXY_PATH' if any(s['price_raw_quote_per_target'] for s in state) else 'UNRESOLVED_HISTORICAL_STATE',states=state,
                   threshold_half_exit='UNRESOLVED_NO_COMPLETE_THRESHOLD_PATH',source_sell_observed=bool(source_sells))
        outputs.append(out)
    save('pilot_paths.json',outputs)
    print(json.dumps({'n':len(outputs),'status':dict(Counter(x['status'] for x in outputs))},indent=2))

def summarize():
    rows=read('pilot_paths.json');by=defaultdict(list);records=[]
    validation=read('pilot_inventory_liquidity.json') if (ROOT/'pilot_inventory_liquidity.json').exists() else []
    zero_entry={r['source_tx'] for r in validation for s in r['checks'] if s['point']=='entry_10s'
                and any(l['liquidity_status']=='ZERO' for l in s['legs'])}
    for r in rows:
        states={s['name']:s['price_raw_quote_per_target'] for s in r.get('states',[])}
        blocks={s['name']:s['block'] for s in r.get('states',[])}
        for delay in (1,10,30,60):
            for policy in ('30m','2h','24h','source_first_sell','double_sell_half'):
                a,b=states.get(f'entry_{delay}s'),states.get('exit_'+policy)
                chronology=blocks.get('exit_'+policy,0)>blocks.get(f'entry_{delay}s',float('inf'))
                value=b/a-1 if a and b and chronology else None
                item={'wallet':r['wallet'],'token':r['token'],'source_tx':r['source_tx'],'delay_s':delay,
                      'exit_policy':policy,'gross_quote_return':value,'status':'PRICE_PROXY' if value is not None else 'UNRESOLVED',
                      'zero_active_liquidity_at_primary_entry':r['source_tx'] in zero_entry,
                      'executable_return_verified':False}
                records.append(item);by[(delay,policy)].append(item)
    sums=[]
    for (d,p),rr in by.items():
        xx=[x['gross_quote_return'] for x in rr if x['gross_quote_return'] is not None]
        sums.append({'delay_s':d,'exit_policy':p,'n_frozen':len(rr),'n_resolved':len(xx),'n_unresolved':len(rr)-len(xx),
                     'gross_mean':mean(xx) if xx else None,'gross_median':median(xx) if xx else None,
                     'gross_winrate':mean(x>0 for x in xx) if xx else None,'gross_max':max(xx) if xx else None,
                     'n_reference_entries_zero_active_liquidity':sum(x['source_tx'] in zero_entry for x in rr if x['gross_quote_return'] is not None),
                     'n_executable_returns_verified':0,
                     'net_pnl':None,'reason':'NO_OWN_EXECUTION_COST_OR_INVENTORY_QUALIFICATION'})
    paired=[]
    primary=[r for r in records if r['delay_s']==10]
    base={r['source_tx']:r['gross_quote_return'] for r in primary if r['exit_policy']=='30m' and r['gross_quote_return'] is not None}
    for policy in ('2h','24h','source_first_sell'):
        vals={r['source_tx']:r['gross_quote_return'] for r in primary if r['exit_policy']==policy and r['gross_quote_return'] is not None}
        overlap=base.keys()&vals.keys()
        paired.append({'vs_30m':policy,'n_paired':len(overlap),'mean_gross_difference':mean(vals[k]-base[k] for k in overlap) if overlap else None})
    save('pilot_comparison.json',{'summaries':sums,'paired':paired,'rows':records,'qualification':'UNVERIFIED_SUPPLIER_WALLETS','economic_claim':'NONE',
        'sensitivity_note':'1/30/60s change entry only; fixed exit timestamps anchored to primary 10s entry. Source-first-sale is not proportional full-source liquidation.'})
    print(json.dumps([s for s in sums if s['delay_s']==10],indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['receipts','metadata','paths','summary']);a=p.parse_args()
    {'receipts':receipts,'metadata':metadata,'paths':paths,'summary':summarize}[a.command]()
