"""Outcome-independent Pancake V2 adapter repair, after the main collector.

Keeps primary results and original executor source/runtime evidence. The repair
only selects witnessed V2 pools whose historical factory is canonical Pancake.
"""
import argparse
import json
from collections import Counter
from eth_utils import keccak
from evidence import ROOT,read,save,now,digest,fetch
from pilot import call
import smartmoney_settlement as settlement
from smartmoney_expand_settlement import mixed_one,summary

FACTORY='0x02a84c1b3bbd7401a5f7fa98a384ebc70bb5749e'
FACTORY_DOC='https://developer.pancakeswap.finance/contracts/v2/addresses'
PAIR_SOURCE='https://base.blockscout.com/api/v2/smart-contracts/0x0ca83c6816bba50add784a422ea54540e4ca3803'

def archive():
    name='smartmoney_executor_v1_archive.json'
    if (ROOT/name).exists():return
    source=ROOT/'contracts/ResearchMixedExecutor.sol'
    runtime=settlement.runtime(True)
    save(name,{'generated_at':now(),'source_text':source.read_text(),
        'source_sha256':digest(source.read_bytes()),'runtime':runtime,
        'runtime_keccak':'0x'+keccak(bytes.fromhex(runtime.removeprefix('0x'))).hex(),
        'note':'main mixed collector runtime before the separately compiled Pancake extension'})

def prepare():
    rows=read('smartmoney_routes_extended.json')['rows'];earliest={}
    for r in rows:
        for e in r['route'] or []:
            if e['kind']=='V2':earliest[e['id']]=min(earliest.get(e['id'],r['source_block']),r['source_block'])
    meta=[]
    for pool,block in sorted(earliest.items()):
        raw,ev=call(pool,'0xc45a0155',block,'smartmoney_v2_factory_repair')
        factory=('0x'+raw[-40:]).lower() if raw and len(raw)==66 else None
        meta.append(dict(pool=pool,block=block,factory=factory,evidence=ev,
                         canonical_pancake=factory==FACTORY))
    m,_=fetch(FACTORY_DOC,label='smartmoney_pancake_official_factory')
    n,source=fetch(PAIR_SOURCE,label='smartmoney_pancake_verified_pair_source')
    assert source and source.get('is_verified') and source.get('name')=='PancakePair'
    assert 'amount0In.mul(25)' in source['source_code'] and 'balance0.mul(10000)' in source['source_code']
    pools={m['pool'] for m in meta if m['canonical_pancake']}
    affected=[r['source_tx'] for r in rows if any(e['id'] in pools for e in r['route'] or [])]
    result={'generated_at':now(),'factory':FACTORY,'pools':sorted(pools),'pool_metadata':meta,
        'affected_source_txs':affected,'n_affected':len(affected),
        'factory_document_evidence':m['key'],'verified_pair_evidence':n['key'],
        'registered_fee':{'numerator':9975,'denominator':10000},
        'selection':'historical source-witnessed protocol identity, independent of model PnL'}
    save('smartmoney_pancake_repair_manifest.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('pool_metadata','affected_source_txs')},indent=2))

def run():
    manifest=read('smartmoney_pancake_repair_manifest.json')
    primary=read('smartmoney_follower_execution_result.json')
    affected=set(manifest['affected_source_txs'])
    routes={r['source_tx']:r for r in read('smartmoney_follower_routes.json')['rows']}
    keys=read('smartmoney_v4_keys_calldata.json')['keys'];out=[]
    name='smartmoney_follower_execution_before_pancake.json'
    if not (ROOT/name).exists():save(name,primary)
    old_runtime=settlement.runtime
    artifact=read('sol_out_repair/ResearchPancakeV2Executor.sol/ResearchPancakeV2Executor.json')
    repair_runtime=artifact['deployedBytecode']['object']
    settlement.runtime=lambda mixed=False:repair_runtime if mixed else old_runtime(False)
    try:
        for prior in primary['rows']:
            tx=prior['source_tx'];item=dict(prior)
            if tx in affected:
                filename='smartmoney_pancake_repair/'+tx+'.json'
                if (ROOT/filename).exists() and not settlement.has_throttle(read(filename)):
                    item=read(filename)
                else:
                    item=mixed_one(routes[tx],keys)
                    item.update(adapter='MIXED_PLUS_CANONICAL_PANCAKE_V2_25BPS',
                                route_origin=routes[tx]['route_origin'],repaired_prior_status=prior['status'])
                    save(filename,item)
                print(json.dumps({'pancake_repair_tx':tx,'status':item['status']}),flush=True)
            item.update(execution_venue='SELF_BUILT_DIRECT_EXECUTOR',platform_fee_model_raw='0',
                platform_fee_status='NO_THIRD_PARTY_ROUTER_COMMISSION_BY_DESIGN',
                operating_cost_status='RPC_SERVER_DEPLOYMENT_NOT_AMORTIZED',
                custody_note='output measured at research executor; external-wallet sweep/unwrap not included',
                settlement_cash_asset=routes[tx]['route'][-1]['to_token'])
            out.append(item)
    finally:settlement.runtime=old_runtime
    result=primary|{'generated_at':now(),'rows':out,'statuses':dict(Counter(r['status'] for r in out)),
        'pancake_adapter_repaired':len(affected),
        'pre_repair_artifact':name,'repair_manifest':'smartmoney_pancake_repair_manifest.json'}
    result['remaining']=[x for x in result['remaining'] if 'only registered Uniswap' not in x]
    result['remaining'].append('unverified proxy-pool execution interfaces remain unresolved; no guessed constant-product fee used')
    save('smartmoney_follower_execution_result.json',result)
    summary('smartmoney_follower_execution_result.json')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['archive','prepare','run'])
    {'archive':archive,'prepare':prepare,'run':run}[parser.parse_args().command]()
