"""Per-candidate explanation of route gaps; not a zero-return imputation."""
import json
from collections import Counter
from evidence import read,save,now
from smartmoney_routes import raw_receipt
from pilot import TRANSFER,V2,V3,V4,topic
from smartmoney_solidly import SWAP

def run():
    rs=read('smartmoney_routes_extended.json')['rows'];keys=read('smartmoney_v4_keys_calldata.json')['keys']
    recs={r['tx_hash']:r for r in read('smartmoney_receipts_extended.json')['rows']}
    meta={r['pool']:r for r in read('smartmoney_pool_metadata_extended.json')};out=[]
    for r in rs:
        if r['route']:continue
        rec=recs[r['source_tx']];edges=[]
        for l in rec['swap_logs']:
            m=keys[l['topics'][1]] if l['kind']=='V4' else meta[l['pool']]
            edges.append(dict(kind=l['kind'],pool_id=l['topics'][1] if l['kind']=='V4' else l['pool'],
                token0=m.get('token0'),token1=m.get('token1')))
        tag=('NO_RECOGNIZED_SWAP' if not edges else 'TARGET_NOT_IN_RECOGNIZED_SWAPS'
             if not any(r['token'] in (e['token0'],e['token1']) for e in edges) else 'DISCONNECTED_OR_AMBIGUOUS_GRAPH')
        unknown=[{'emitter':l['address'],'topic0':l['topics'][0],'data_bytes':(len(l['data'])-2)//2}
            for l in raw_receipt(rec)['logs'] if l['topics'] and l['topics'][0] not in
            (TRANSFER,V2,V3,V4,SWAP,topic('Approval(address,address,uint256)'))]
        algebra=topic('Swap(address,address,int256,int256,uint160,uint128,int24,uint24,uint24)')
        for e in unknown:
            if e['topic0']==algebra:
                e['abi_hint']='ALGEBRA_INTEGRAL_SWAP; pool currencies/factory/context adapter not yet verified'
                e['abi_source']='https://github.com/cryptoalgebra/Algebra/blob/master/src/core/contracts/interfaces/pool/IAlgebraPoolEvents.sol'
        out.append({**r,'gap_class':tag,'known_edges':edges,'other_events':unknown,
            'note':'candidate account receipt reconciles; economic purchase/path classification unresolved; not a failed copier trade'})
    save('smartmoney_route_gap_audit.json',{'generated_at':now(),'frozen_candidates':len(rs),
        'n_route_gaps':len(out),'classes':dict(Counter(r['gap_class'] for r in out)),'rows':out})
    print(json.dumps({'n_route_gaps':len(out),'classes':dict(Counter(r['gap_class'] for r in out))}))

if __name__=='__main__':run()
