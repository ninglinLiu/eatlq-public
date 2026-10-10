"""Receipt-proven V4 pool keys and multi-hop quote-normalized price paths."""
import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from eth_abi import decode,encode
from eth_utils import keccak
from evidence import ROOT,read,save,fetch,rpc,now
from pilot import req,call,topic,TRANSFER,price_from_state
from ledger import WETH,USDC

MANAGER='0x498581ff718922c3f8e6a244956af099b2652b2b'
STATE_VIEW='0xa3c0c9b65bad0b08107aa264b0f3db444b867a71'
INIT=topic('Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)')
SLOT='0x'+keccak(text='getSlot0(bytes32)')[:4].hex()
ZERO='0x'+'00'*20

def decode_init(l):
    tt=l.get('topics',[])
    if l.get('address','').lower()!=MANAGER or len(tt)!=4 or tt[0]!=INIT:return None
    c0,c1='0x'+tt[2][-40:],'0x'+tt[3][-40:]
    fee,spacing,hooks,sqrt,tick=decode(['uint24','int24','address','uint160','int24'],bytes.fromhex(l['data'][2:]))
    key_hash='0x'+keccak(encode(['address','address','uint24','int24','address'],[c0,c1,fee,spacing,hooks])).hex()
    assert key_hash==tt[1],'pool key hash mismatch'
    return {'pool_id':tt[1],'token0':c0,'token1':c1,'fee':fee,'tick_spacing':spacing,'hooks':hooks,
            'init_block':int(l['blockNumber'],16),'init_tx':l['transactionHash'],'pool_key_hash_verified':True}

def mapping():
    receipts=read('pilot_receipts.json');tokens=set()
    for r in receipts:
        rec,_=req('eth_getTransactionReceipt',[r['tx_hash']],'pilot_source_receipt')
        for l in (rec or {}).get('logs',[]):
            if l.get('topics',[None])[0]==TRANSFER:tokens.add(l['address'].lower())
    tokens-= {WETH,USDC}
    def one(token):
        m,d=fetch('https://base.blockscout.com/api/v2/addresses/'+token,label='v4_token_creation')
        out={'token':token,'address_evidence':m['key'],'pool_keys':[]}
        tx=d.get('creation_transaction_hash') if isinstance(d,dict) else None
        if not tx:out['status']='NO_CREATION_TRANSACTION';return out
        out['creation_tx']=tx
        rec,ev=req('eth_getTransactionReceipt',[tx],'v4_creation_receipt');out['receipt_evidence']=ev
        for l in (rec or {}).get('logs',[]):
            p=decode_init(l)
            if p:out['pool_keys'].append(p)
        out['status']='CREATION_RECEIPT_READ' if rec else 'RECEIPT_UNAVAILABLE'
        return out
    with ThreadPoolExecutor(max_workers=2) as ex:rows=list(ex.map(one,sorted(tokens)))
    keys={p['pool_id']:p for r in rows for p in r['pool_keys']}
    required={l['topics'][1] for r in receipts for l in r['swap_logs'] if l['kind']=='V4'}
    save('v4_pool_keys.json',{'rows':rows,'keys':keys,'n_required':len(required),'n_mapped':len(required&keys.keys()),'missing_ids':sorted(required-keys.keys())})
    print(json.dumps({'tokens':len(tokens),'required':len(required),'mapped':len(required&keys.keys()),'missing':sorted(required-keys.keys())},indent=2))

def route_for(r,keys,v23):
    edges=[]
    for l in r['swap_logs']:
        if l['kind']=='V4':
            m=keys.get(l['topics'][1]);ident=l['topics'][1]
        else:
            m=v23.get((l['pool'],r['block_number']));ident=l['pool']
        if not m or not m.get('token0') or not m.get('token1'):continue
        if m.get('init_block',0)>r['block_number']:continue
        edges.append({'kind':l['kind'],'id':ident,'token0':m['token0'],'token1':m['token1'],
                      'fee':m.get('fee'),'hooks':m.get('hooks')})
    # Only pools witnessed in this source transaction, no outcome-based routing.
    targets={WETH,ZERO} if r['quote']=='ETH' else {USDC}
    found=[]
    def dfs(token,path,visited):
        if token in targets:found.append(path);return
        if len(path)>=4:return
        for edge in edges:
            if token not in (edge['token0'],edge['token1']):continue
            other=edge['token1'] if token==edge['token0'] else edge['token0']
            if other in visited:continue
            dfs(other,path+[edge|{'from_token':token,'to_token':other,'token0_is_from':token==edge['token0']}],visited|{other})
    dfs(r['token'],[],{r['token']})
    if not found:return None
    minlen=min(map(len,found));short=[p for p in found if len(p)==minlen]
    unique={tuple((e['kind'],e['id']) for e in p):p for p in short}
    return next(iter(unique.values())) if len(unique)==1 else None

def paths():
    rows=read('pilot_receipts.json');keys=read('v4_pool_keys.json')['keys']
    v23={(m['pool'],m['block_number']):m for m in read('pilot_pool_metadata.json')}
    ledger=read('account_cash_ledger.json')['rows'];outputs=[]
    for i,r in enumerate(rows):
        name='route_paths/'+r['tx_hash']+'.json'
        route=route_for(r,keys,v23)
        if (ROOT/name).exists():
            prior=read(name)
            if prior.get('route') or not route:outputs.append(prior);continue
            save('route_paths/'+r['tx_hash']+'.previous.json',prior)
        out={'wallet':r['wallet'],'token':r['token'],'source_tx':r['tx_hash'],'source_block':r['block_number'],
             'source_ts':r['timestamp'],'quote':r['quote'],'route':route,'index_complete':r['index_complete'],
             'price_kind':'MULTIHOP_HISTORICAL_POOL_MID_NOT_OWN_FILL','states':[]}
        if not route:
            out['status']='UNRESOLVED_POOL_KEY_OR_ROUTE';save(name,out);outputs.append(out);continue
        # Endpoint check validates deterministic Base two-second timestamp mapping.
        sb,bev=req('eth_getBlockByNumber',[hex(r['block_number']),False],'pilot_source_block')
        eb,eev=req('eth_getBlockByNumber',[hex(r['block_number']+43205),False],'pilot_end_block')
        out['block_time_evidence']=[bev,eev]
        if not sb or not eb or int(eb['timestamp'],16)-int(sb['timestamp'],16)!=86410:
            out['status']='UNRESOLVED_BLOCK_TIME_MAPPING';save(name,out);outputs.append(out);continue
        sells=sorted([x for x in ledger if x['wallet']==r['wallet'] and x.get('token')==r['token'] and x.get('side')=='sell' and x['block_number']>r['block_number']],key=lambda x:x['block_number'])
        points={f'entry_{d}s':r['block_number']+(d+1)//2 for d in (1,10,30,60)}
        points.update({'exit_30m':r['block_number']+905,'exit_2h':r['block_number']+3605,'exit_24h':r['block_number']+43205})
        # Source first sale only, not falsely described as all proportional exits.
        if sells:points['exit_source_first_sell']=sells[0]['block_number']+5
        for point,b in points.items():
            product=1.0;legs=[]
            for edge in route:
                to=STATE_VIEW if edge['kind']=='V4' else edge['id']
                calldata=SLOT+edge['id'][2:] if edge['kind']=='V4' else ('0x3850c7bd' if edge['kind']=='V3' else '0x0902f1ac')
                raw,ev=call(to,calldata,b,'pilot_route_state')
                p=price_from_state('V3' if edge['kind']=='V4' else edge['kind'],raw,edge['token0_is_from'])
                legs.append({'edge':edge['id'],'price':p,'evidence':ev})
                if p is None:product=None;break
                product*=p
            out['states'].append({'name':point,'block':b,'price_raw_quote_per_target':product,'legs':legs})
        out['status']='PRICE_PROXY_PATH' if any(x['price_raw_quote_per_target'] for x in out['states']) else 'UNRESOLVED_HISTORICAL_STATE'
        out['threshold_half_exit']='UNRESOLVED_NO_COMPLETE_THRESHOLD_PATH'
        out['source_sell_observed']=bool(sells)
        save(name,out);outputs.append(out)
        print(json.dumps({'done':i+1,'total':len(rows),'route_hops':len(route),'status':out['status']}),flush=True)
    save('pilot_paths.json',outputs)
    print(json.dumps({'n':len(outputs),'status':dict(Counter(x['status'] for x in outputs))},indent=2))

def repair_throttled_points():
    # A single cooled-down retry for already frozen observations; do not alter
    # entries, routes, horizons, or replace missing assets with available ones.
    rows=read('pilot_paths.json');attempts=0;recovered=0
    for r in rows:
        for s in r.get('states',[]):
            if s['price_raw_quote_per_target'] is not None:continue
            if not any(l['evidence'].get('http_status')==429 for l in s.get('legs',[])):continue
            attempts+=1;product=1.;legs=[]
            for e in r.get('route') or []:
                to=STATE_VIEW if e['kind']=='V4' else e['id']
                calldata=SLOT+e['id'][2:] if e['kind']=='V4' else ('0x3850c7bd' if e['kind']=='V3' else '0x0902f1ac')
                raw,ev=call(to,calldata,s['block'],'pilot_route_state')
                price=price_from_state('V3' if e['kind']=='V4' else e['kind'],raw,e['token0_is_from'])
                legs.append({'edge':e['id'],'price':price,'evidence':ev})
                if price is None:product=None;break
                product*=price
            s['prior_legs']=s['legs'];s['legs']=legs;s['price_raw_quote_per_target']=product
            recovered+=product is not None
        save('route_paths/'+r['source_tx']+'.json',r)
    save('pilot_paths.json',rows);print(json.dumps({'retry_points':attempts,'recovered':recovered}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['mapping','paths','repair']);a=p.parse_args()
    {'mapping':mapping,'paths':paths,'repair':repair_throttled_points}[a.command]()
