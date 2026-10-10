"""Repair a missed official Aerodrome V2 event ABI; preserve raw receipts."""
import json
from collections import Counter
from eth_utils import keccak
from evidence import read,save,now
from smartmoney_routes import raw_receipt,metadata,routes,costs
from smartmoney import receipt_summary

SWAP='0x'+keccak(text='Swap(address,address,uint256,uint256,uint256,uint256)').hex()
ALGEBRA='0x'+keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24,uint24,uint24)').hex()

def extend_receipts():
    original=read('smartmoney_receipts.json');rows=[]
    for r in original['rows']:
        rr=dict(r);rr['swap_logs']=list(r['swap_logs'])
        for l in raw_receipt(r).get('logs',[]):
            if not l['topics'] or l['topics'][0] not in (SWAP,ALGEBRA):continue
            rr['swap_logs'].append(dict(pool=l['address'].lower(),kind='AERO_V2' if l['topics'][0]==SWAP else 'ALGEBRA',
                log_index=int(l['logIndex'],16),data=l['data'],topics=l['topics']))
        rr['swap_logs'].sort(key=lambda l:l['log_index']);rows.append(rr)
    obj={'generated_at':now(),'rows':rows,'summary':receipt_summary(rows),
         'source':'https://github.com/aerodrome-finance/contracts/blob/main/contracts/interfaces/IPool.sol',
         'algebra_source':'https://github.com/cryptoalgebra/Algebra/blob/master/src/core/contracts/interfaces/pool/IAlgebraPoolEvents.sol',
         'qualification':'event signature recognized; canonical factory registration still checked in execution'}
    save('smartmoney_receipts_extended.json',obj);print(json.dumps(obj['summary']))

def run():
    extend_receipts()
    metadata('smartmoney_receipts_extended.json','smartmoney_pool_metadata_extended.json')
    routes('smartmoney_v4_keys_calldata.json','smartmoney_routes_extended.json','smartmoney_receipts_extended.json')
    costs('smartmoney_v4_keys_calldata.json','smartmoney_routes_extended.json',
          'smartmoney_cost_ledger_extended.json','smartmoney_source_per_trade.csv','smartmoney_receipts_extended.json')

if __name__=='__main__':run()
