"""Recover receipt-witnessed V4 PoolKeys by exact hash, never by token suffix.

Enumerated templates are discovery hypotheses, not accepted metadata: a key is
used only when its full ABI hash equals the pool ID in the manager's Swap log.
"""
import argparse
import itertools
import json
import csv
from collections import Counter
from collections import defaultdict
from pathlib import Path
from eth_abi import encode, decode
from eth_utils import keccak
from evidence import ROOT, read, save, now
from pilot import TRANSFER, V4, call
from v4_paths import MANAGER, ZERO, decode_init, route_for
from ledger import WETH, USDC

def pool_id(key):
    return '0x'+keccak(encode(['address','address','uint24','int24','address'],
        [key['token0'],key['token1'],key['fee'],key['tick_spacing'],key['hooks']])).hex()

def raw_receipt(row):
    meta=read('raw/'+row['receipt_evidence']['key']+'.meta.json')
    data=json.loads((ROOT/'raw'/(meta['body_sha256']+'.body')).read_bytes())
    return data.get('result') or {}

def recover(rec, known):
    keys={}; logs=rec.get('logs',[])
    for log in logs:
        p=decode_init(log)
        if p:keys[p['pool_id']]=p|{'key_origin':'RECEIPT_INITIALIZE'}
    swaps=[l for l in logs if l.get('address','').lower()==MANAGER and (l.get('topics') or [None])[0]==V4]
    missing={l['topics'][1] for l in swaps}-known.keys()-keys.keys()
    if not missing:return keys
    tokens={l['address'].lower() for l in logs if (l.get('topics') or [None])[0]==TRANSFER}|{ZERO,WETH,USDC}
    hooks={k['hooks'] for k in known.values()}|{ZERO}|{l['address'].lower() for l in logs}
    fees={0,100,500,3000,10000,8388608}|{k['fee'] for k in known.values()}
    for log in swaps:
        try:fees.add(decode(['int128','int128','uint160','uint128','int24','uint24'],bytes.fromhex(log['data'][2:]))[-1])
        except Exception:pass
    spacing={1,10,60,100,200}|{k['tick_spacing'] for k in known.values()}
    pairs=list(itertools.combinations(sorted(tokens),2))
    # Try witnessed templates first; exact hash prevents false acceptance.
    templates={(k['fee'],k['tick_spacing'],k['hooks']) for k in known.values()}
    templates=sorted(templates)+[t for t in itertools.product(sorted(fees),sorted(spacing),sorted(hooks)) if t not in templates]
    block=int(rec['blockNumber'],16)
    for fee, tick_spacing, hook in templates:
        for a,b in pairs:
            key={'token0':a,'token1':b,'fee':fee,'tick_spacing':tick_spacing,'hooks':hook}
            pid=pool_id(key)
            if pid in missing:
                keys[pid]=key|{'pool_id':pid,'pool_key_hash_verified':True,
                    'key_origin':'EXACT_HASH_FROM_RECEIPT_CURRENCIES_AND_BOUNDED_TEMPLATES',
                    'first_witnessed_block':block,'first_witnessed_tx':rec['transactionHash']}
                missing.remove(pid)
                if not missing:return keys
    return keys

def mapping():
    files=sorted((ROOT/'smartmoney_receipts').glob('*.json'))
    rows=[json.loads(p.read_text()) for p in files]
    keys=dict(read('v4_pool_keys.json')['keys'])
    if (ROOT/'smartmoney_v4_keys.json').exists():keys.update(read('smartmoney_v4_keys.json')['keys'])
    new=0
    for i,r in enumerate(rows):
        if r.get('receipt_status')!='CONFIRMED_SUCCESS':continue
        found=recover(raw_receipt(r),keys)
        for pid,key in found.items():
            if pid not in keys:keys[pid]=key;new+=1
        if (i+1)%50==0:print(json.dumps({'receipts':i+1,'new_keys':new}),flush=True)
    required={l['topics'][1] for r in rows for l in r['swap_logs'] if l['kind']=='V4' and l['pool']==MANAGER}
    out={'generated_at':now(),'n_receipts_considered':len(rows),'n_required':len(required),
         'n_mapped':len(required&keys.keys()),'missing_ids':sorted(required-keys.keys()),'keys':keys}
    save('smartmoney_v4_keys.json',out)
    print(json.dumps({k:v for k,v in out.items() if k not in ('keys','missing_ids')},indent=2))

def calldata_mapping():
    """Decode only a complete PoolKey whose bytes hash to a witnessed pool ID.

    No RPC or future market observations: use the saved source transaction input.
    Search byte offsets because nested router calldata need not be word aligned.
    Keep this adapter repair separate from the original route manifest.
    """
    original=read('smartmoney_v4_keys.json');keys=dict(original['keys'])
    needed=set(original['missing_ids']);found={}
    txs={}
    for p in (ROOT/'wallet_cash_index').glob('external_*.json'):
        txs.update({t['hash']:t for t in json.loads(p.read_text())['items']})
    for r in read('smartmoney_receipts.json')['rows']:
        relevant={l['topics'][1] for l in r['swap_logs'] if l['kind']=='V4' and l['pool']==MANAGER}&needed
        if not relevant:continue
        raw=bytes.fromhex((txs.get(r['tx_hash'],{}).get('raw_input') or '0x')[2:])
        for i in range(max(0,len(raw)-159)):
            # Some source routers insert a pool-address word between currencies
            # and V4 fee/spacing/hooks. Neither layout is accepted without hash.
            candidates=[('contiguous',raw[i:i+160])]
            if i+192<=len(raw):candidates.append(('pool_word_inserted',raw[i:i+64]+raw[i+96:i+192]))
            for layout,b in candidates:
                if b[:12]!=bytes(12) or b[32:44]!=bytes(12) or b[128:140]!=bytes(12):continue
                # Router fields can be input/output order; PoolKey currencies
                # are canonical ascending order. Still require exact full hash.
                if b[:32]>b[32:64]:b=b[32:64]+b[:32]+b[64:];layout+='_sorted'
                pid='0x'+keccak(b).hex()
                if pid not in relevant:continue
                try:a,c,fee,tick,hooks=decode(['address','address','uint24','int24','address'],b)
                except Exception:continue
                key=dict(token0=a,token1=c,fee=fee,tick_spacing=tick,hooks=hooks,pool_id=pid,
                    pool_key_hash_verified=True,key_origin='SOURCE_CALLDATA_EXACT_HASH',calldata_layout=layout,
                    first_witnessed_tx=r['tx_hash'],first_witnessed_block=r['block_number'],calldata_byte_offset=i)
                assert pool_id(key)==pid
                keys[pid]=key;found[pid]=key;needed.remove(pid);relevant.remove(pid)
            if not relevant:break
        if relevant:
            rec=raw_receipt(r)
            b20=[('0x'+raw[i:i+20].hex()) for i in range(max(0,len(raw)-19))
                 if raw[i:i+9]==bytes.fromhex('b20000000000000000')]
            # Packed router address-table members are candidate currencies,
            # not accepted identities; recover() still checks manager pool ID.
            augmented=rec|{'logs':rec['logs']+[{'address':a,'topics':[TRANSFER]} for a in b20]}
            extra=recover(augmented,keys)
            for pid,key in extra.items():
                if pid not in relevant:continue
                key.update(key_origin='PACKED_CALLDATA_CURRENCY_EXACT_HASH',first_witnessed_tx=r['tx_hash'])
                keys[pid]=key;found[pid]=key;needed.remove(pid);relevant.remove(pid)
    save('smartmoney_v4_keys_calldata.json',{'generated_at':now(),'keys':keys,
        'new_exact_keys':found,'missing_ids':sorted(needed),'n_mapped':original['n_mapped']+len(found),
        'n_required':original['n_required']})
    print(json.dumps({'new_exact_keys':found,'remaining':sorted(needed)},indent=2))

def metadata(receipt_file='smartmoney_receipts.json',output='smartmoney_pool_metadata.json'):
    rows=read(receipt_file)['rows']
    old=read('pilot_pool_metadata.json')
    # Immutable currency identity may be reused once verified at or before the
    # source block; pool fee/tick/state still require their relevant epoch.
    earliest={}
    for r in rows:
        for l in r['swap_logs']:
            if l['kind'] in ('V2','V3','AERO_V2','ALGEBRA'):
                key=l['pool'];earliest[key]=min(earliest.get(key,r['block_number']),r['block_number'])
    out=[]
    for i,(pool,block) in enumerate(sorted(earliest.items())):
        name='smartmoney_pool_metadata/'+pool+'.json'
        if (ROOT/name).exists():out.append(read(name));continue
        matches=[m for m in old if m['pool']==pool and m['block_number']<=block and m.get('token0') and m.get('token1')]
        if matches:
            row=min(matches,key=lambda m:m['block_number']);save(name,row);out.append(row);continue
        row={'pool':pool,'block_number':block}
        fields=[('token0','0x0dfe1681'),('token1','0xd21220a7'),('fee','0xddca3f43'),('factory','0xc45a0155')]
        selector=keccak(text='aggregate3((address,bool,bytes)[])')[:4]
        data='0x'+selector.hex()+encode(['(address,bool,bytes)[]'],
            [[(pool,True,bytes.fromhex(s[2:])) for _,s in fields]]).hex()
        result,ev=call('0xca11bde05977b3631167028862be2a173976ca11',data,block,'smartmoney_pool_metadata_multicall')
        values=[]
        if result and result!='0x':
            try:values=decode(['(bool,bytes)[]'],bytes.fromhex(result[2:]))[0]
            except Exception:pass
        for (field,_),(success,value) in zip(fields,values):
            row[field+'_evidence']=ev|{'subcall_success':success}
            if success and len(value)>=32:
                row[field]=('0x'+value[-20:].hex()).lower() if field!='fee' else int.from_bytes(value,'big')
        save(name,row);out.append(row)
        print(json.dumps({'metadata_pools':i+1,'total':len(earliest)}),flush=True)
    save(output,out)

def routes(key_file='smartmoney_v4_keys.json',output='smartmoney_routes.json',receipt_file='smartmoney_receipts.json'):
    rows=read(receipt_file)['rows'];keys=read(key_file)['keys']
    ms=read('pilot_pool_metadata.json')
    if (ROOT/'smartmoney_pool_metadata.json').exists():ms+=read('smartmoney_pool_metadata.json')
    if (ROOT/'smartmoney_pool_metadata_extended.json').exists():ms+=read('smartmoney_pool_metadata_extended.json')
    out=[]
    for r in rows:
        usable={}
        for m in sorted(ms,key=lambda m:m['block_number']):
            if m['block_number']<=r['block_number'] and m.get('token0') and m.get('token1'):
                usable[(m['pool'],r['block_number'])]=m
        trusted_logs=[l for l in r['swap_logs'] if l['kind']!='V4' or l['pool']==MANAGER]
        route=route_for(r|{'swap_logs':trusted_logs},keys,usable)
        out.append({'source_tx':r['tx_hash'],'wallet':r['wallet'],'token':r['token'],
            'source_block':r['block_number'],'source_ts':r['timestamp'],'quote':r['quote'],
            'index_complete':r['index_complete'],'route':route,
            'status':'ROUTE_CURRENCIES_IDENTIFIED' if route else 'UNRESOLVED_POOL_KEY_OR_ROUTE'})
    result={'generated_at':now(),'n_frozen':len(rows),'rows':out,
            'summary':dict(Counter(r['status'] for r in out)),
            'note':'V4 identity is manager + exact hash; V2/V3 factory trust and router execution remain separate checks'}
    save(output,result);print(json.dumps(result['summary']))

def costs(key_file='smartmoney_v4_keys.json',route_file='smartmoney_routes.json',output='smartmoney_cost_ledger.json',csv_file='smartmoney_per_trade.csv',receipt_file='smartmoney_receipts.json'):
    receipts=read(receipt_file)['rows']
    routes={r['source_tx']:r for r in read(route_file)['rows']}
    keys=read(key_file)['keys']
    ms=read('pilot_pool_metadata.json')
    if (ROOT/'smartmoney_pool_metadata.json').exists():ms+=read('smartmoney_pool_metadata.json')
    if (ROOT/'smartmoney_pool_metadata_extended.json').exists():ms+=read('smartmoney_pool_metadata_extended.json')
    rows=[]
    for r in receipts:
        cash=defaultdict(int);unknown=[];fee_units=[]
        for l in r['swap_logs']:
            if l['kind']=='V4' and l['pool']==MANAGER:
                k=keys.get(l['topics'][1])
                if not k:unknown.append(l['topics'][1]);continue
                vals=decode(['int128','int128','uint160','uint128','int24','uint24'],bytes.fromhex(l['data'][2:]))
                cash[k['token0']]+=vals[0];cash[k['token1']]+=vals[1]
                fee_units.append(vals[-1])
            elif l['kind'] in ('V2','V3','AERO_V2','ALGEBRA'):
                mm=[m for m in ms if m['pool']==l['pool'] and m['block_number']<=r['block_number'] and m.get('token0') and m.get('token1')]
                if not mm:unknown.append(l['pool']);continue
                k=max(mm,key=lambda m:m['block_number'])
                if l['kind'] in ('V3','ALGEBRA'):
                    vals=decode(['int256','int256','uint160','uint128','int24'],bytes.fromhex(l['data'][2:]))
                    cash[k['token0']]-=vals[0];cash[k['token1']]-=vals[1]
                else:
                    a,b,c,d=decode(['uint256']*4,bytes.fromhex(l['data'][2:]))
                    cash[k['token0']]+=c-a;cash[k['token1']]+=d-b
            else:unknown.append(l['pool'])
        root=cash[WETH]+cash[ZERO] if r['quote']=='ETH' else cash[USDC]
        # This is source accounting, explicitly not the copier's gas bill.
        route=routes[r['tx_hash']]['route']
        row={'source_tx':r['tx_hash'],'wallet':r['wallet'],'token':r['token'],'source_block':r['block_number'],
             'receipt_status':r['receipt_status'],'receipt_account_token_match':r.get('receipt_token_net_matches_index',False),
             'source_fee_raw':r.get('source_fee_receipt_raw'),'source_fee_reconciled':r.get('source_fee_receipt_raw')==r['source_fee_raw'],
             'source_wallet_cash_spent_raw':str(-int(r['quote_net_raw'])),
             'recognized_swap_logs':len(r['swap_logs']),
             'source_target_net_pool_output_raw':str(cash[r['token']]) if not unknown and r['swap_logs'] else None,
             'source_amm_buy_direction_supported':bool(route and not unknown and root<0 and cash[r['token']]>0),
             'source_cash_index_complete':r['index_complete'],'source_root_pool_input_raw':str(-root) if not unknown and r['swap_logs'] else None,
             'source_cash_less_pool_input_raw':str(-int(r['quote_net_raw'])+root) if not unknown and r['index_complete'] and r['swap_logs'] else None,
             'cash_less_pool_note':'difference can include router/hook charges or unmodelled flows; not automatically pure fee',
             'source_observed_v4_swap_fees_ppm':fee_units,'unknown_swap_metadata':unknown,
             'route_status':routes[r['tx_hash']]['status'],'route_hops':len(route) if route else None,
             'route_kinds':','.join(e['kind'] for e in route) if route else None,
             'follower_net_pnl':None,'follower_cost_status':'PENDING_HISTORICAL_TRANSFER_SETTLEMENT_AND_OWN_GAS'}
        rows.append(row)
    out={'generated_at':now(),'n_frozen':len(receipts),'rows':rows,
         'summary':{'source_fee_reconciled':sum(r['source_fee_reconciled'] for r in rows),
             'identified_routes':sum(r['route_hops'] is not None for r in rows),
             'source_swap_currency_flows_parsed':sum(not r['unknown_swap_metadata'] and r['recognized_swap_logs']>0 for r in rows),
             'source_cash_and_swap_delta_comparable':sum(r['source_cash_less_pool_input_raw'] is not None for r in rows),
             'source_amm_buy_direction_supported':sum(r['source_amm_buy_direction_supported'] for r in rows),
             'own_net_profit_certified':0}}
    save(output,out)
    fields=['source_tx','wallet','token','source_block','receipt_status','receipt_account_token_match',
        'source_fee_raw','source_fee_reconciled','source_wallet_cash_spent_raw','source_cash_index_complete',
        'source_root_pool_input_raw','source_cash_less_pool_input_raw','route_status','route_hops','route_kinds',
        'follower_cost_status']
    with (ROOT/csv_file).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    print(json.dumps(out['summary'],indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['mapping','calldata','metadata','routes','routes_extended','costs','costs_extended']);a=p.parse_args()
    {'mapping':mapping,'calldata':calldata_mapping,'metadata':metadata,'routes':routes,
     'routes_extended':lambda:routes('smartmoney_v4_keys_calldata.json','smartmoney_routes_extended.json',
         'smartmoney_receipts_extended.json' if (ROOT/'smartmoney_receipts_extended.json').exists() else 'smartmoney_receipts.json'),'costs':costs,
     'costs_extended':lambda:costs('smartmoney_v4_keys_calldata.json','smartmoney_routes_extended.json','smartmoney_cost_ledger_extended.json','smartmoney_source_per_trade.csv',
         'smartmoney_receipts_extended.json' if (ROOT/'smartmoney_receipts_extended.json').exists() else 'smartmoney_receipts.json')}[a.command]()
