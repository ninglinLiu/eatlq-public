"""Separately labelled self-built funding routes for the two frozen gaps.

This is not a reconstruction of the source's unknown ETH/USDC conversion.
No outcomes, price rankings, or new candidate selection enter route selection.
Run `execute` only after the main single network collector has completed.
"""
import argparse
import json
from collections import Counter
from evidence import ROOT, read, save, now
from ledger import USDC, WETH
from v4_paths import route_for
from smartmoney_settlement import has_throttle
from smartmoney_expand_settlement import mixed_one, summary

FUNDING_POOL = '0xd0b53d9277642d899df5c87a3966a349a798f224'
FROZEN_GAPS = {
    '0x9dcf0a5c7b7af3ff1db18e27a6c2a0fbbbc7f6328af480d0e2e5bc494ea144eb',
    '0x97b082ffa59e30b22bba7e16d3b65116cd7d4a202592fb95cc0f55bc003da1e9',
}

def attach_funding_leg(path, meta, source_block):
    assert path and path[-1]['to_token'] == USDC
    assert len(path) < 4
    assert meta['pool'] == FUNDING_POOL and meta['fee'] == 500
    assert meta['token0'] == WETH and meta['token1'] == USDC
    assert meta['block_number'] <= source_block, 'future metadata is not permitted'
    return path + [dict(kind='V3', id=FUNDING_POOL, token0=WETH, token1=USDC,
        fee=500, hooks=None, from_token=USDC, to_token=WETH, token0_is_from=False,
        route_origin='FIXED_SELF_BUILT_FUNDING_LEG_NOT_SOURCE_WITNESSED')]

def prepare():
    rows=read('smartmoney_routes_extended.json')['rows']
    recs={r['tx_hash']:r for r in read('smartmoney_receipts_extended.json')['rows']}
    keys=read('smartmoney_v4_keys_calldata.json')['keys']
    metadata=read('smartmoney_pool_metadata_extended.json')
    funding=next(m for m in metadata if m['pool']==FUNDING_POOL)
    assert {r['source_tx'] for r in rows if not r['route']}==FROZEN_GAPS
    out=[]
    for row in rows:
        r=dict(row)
        r['route_origin']='SOURCE_RECEIPT_WITNESSED_POOLS'
        if r['source_tx'] in FROZEN_GAPS:
            rec=recs[r['source_tx']]
            usable={(m['pool'],r['source_block']):m for m in metadata
                    if m['block_number']<=r['source_block'] and m.get('token0') and m.get('token1')}
            path=route_for(rec|{'quote':'USDC'}, keys, usable)
            r.update(route=attach_funding_leg(path,funding,r['source_block']),
                route_origin='SOURCE_USDC_PATH_PLUS_FIXED_SELF_BUILT_FUNDING_LEG',
                source_route_status=row['status'],status='FOLLOWER_ALTERNATIVE_ROUTE_DEFINED',
                funding_metadata_block=funding['block_number'])
        out.append(r)
    result={'generated_at':now(),'n_frozen':len(rows),'rows':out,
        'source_route_recovered':len(rows)-len(FROZEN_GAPS),
        'alternative_funding_routes':len(FROZEN_GAPS),
        'note':'does not relabel unknown source ETH/USDC conversion as reconstructed'}
    save('smartmoney_follower_routes.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))

def execute():
    primary=read('smartmoney_settlement_expanded_result.json')
    routes=read('smartmoney_follower_routes.json')['rows']
    assert len(primary['rows'])==441 and len(routes)==441
    bytx={r['source_tx']:r for r in primary['rows']}
    keys=read('smartmoney_v4_keys_calldata.json')['keys'];out=[]
    for route in routes:
        tx=route['source_tx'];item=dict(bytx[tx])
        if tx in FROZEN_GAPS:
            assert item['status']=='ROUTE_UNRESOLVED'
            filename='smartmoney_funding_supplement/'+tx+'.json'
            if (ROOT/filename).exists() and not has_throttle(read(filename)):
                item=read(filename)
            else:
                item=mixed_one(route,keys)
                item.update(source_route_unresolved=True,
                    route_origin=route['route_origin'],actual_follower_route=route['route'])
                save(filename,item)
            print(json.dumps({'supplement_tx':tx,'status':item['status']}),flush=True)
        else:item['route_origin']=route['route_origin']
        out.append(item)
    result=primary|{'generated_at':now(),'rows':out,
        'statuses':dict(Counter(r['status'] for r in out)),
        'primary_artifact':'smartmoney_settlement_expanded_result.json',
        'alternative_funding_routes':len(FROZEN_GAPS),
        'note':'439 source-witnessed routes + 2 explicit self-built funding alternatives; not 441 source reconstructions'}
    result['remaining']=[x for x in result['remaining'] if 'no synthetic path' not in x]
    result['remaining'].append('two source ETH/USDC conversions unresolved; self-built alternatives modelled separately')
    save('smartmoney_follower_execution_result.json',result)
    summary('smartmoney_follower_execution_result.json')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','execute'])
    {'prepare':prepare,'execute':execute}[parser.parse_args().command]()
