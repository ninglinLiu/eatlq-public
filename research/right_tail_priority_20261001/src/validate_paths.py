"""Depth availability and opening inventory checks for frozen pilot entries."""
import json
from collections import Counter
from eth_utils import keccak
from evidence import read,save
from pilot import call
from v4_paths import STATE_VIEW

def main():
    paths=read('pilot_paths.json');entries={r['tx_hash']:r for r in read('pilot_entries.json')['rows']}
    out=[]
    for r in paths:
        checks=[];source=entries[r['source_tx']]
        # At the block BEFORE the observed source buy, token balance identifies
        # whether this entry starts from zero inventory. This does not certify
        # wallet-wide cost basis or missing transfer history.
        raw,ev=call(source['token'],'0x70a08231'+source['wallet'][2:].rjust(64,'0'),source['block_number']-1,'pilot_inventory_before_source')
        opening_evidence=ev
        opening=int(raw,16) if raw and raw!='0x' else None
        for s in r.get('states',[]):
            if s['name'] not in ('entry_10s','exit_30m','exit_2h','exit_24h','exit_source_first_sell'):continue
            legs=[]
            for e in r.get('route') or []:
                if e['kind']=='V2':
                    # Reserves already checked for nonzero in the price decoder.
                    legs.append({'id':e['id'],'kind':'V2','liquidity_status':'RESERVE_RATIO_ONLY'});continue
                to=STATE_VIEW if e['kind']=='V4' else e['id']
                data=('0x'+keccak(text='getLiquidity(bytes32)')[:4].hex()+e['id'][2:]) if e['kind']=='V4' else '0x1a686502'
                raw,ev=call(to,data,s['block'],'pilot_active_liquidity')
                amount=int(raw,16) if raw and raw!='0x' else None
                legs.append({'id':e['id'],'kind':e['kind'],'liquidity_raw':str(amount) if amount is not None else None,
                             'liquidity_status':'NONZERO' if amount else 'ZERO' if amount==0 else 'UNKNOWN','evidence':ev})
            checks.append({'point':s['name'],'legs':legs})
        item={'source_tx':r['source_tx'],'wallet':r['wallet'],'token':r['token'],
              'before_source_balance_raw':str(opening) if opening is not None else None,'inventory_evidence':opening_evidence,
              'checks':checks,'caveat':'NONZERO_ACTIVE_LIQUIDITY_IS_NOT_EXECUTABLE_DEPTH_OR_SELLABILITY_PROOF'}
        out.append(item)
    save('pilot_inventory_liquidity.json',out)
    print(json.dumps({'n':len(out),'opening_balance_known':sum(r['before_source_balance_raw'] is not None for r in out),
        'nonzero_opening':sum(int(r['before_source_balance_raw'] or 0)>0 for r in out),
        'liquidity_status':dict(Counter(l['liquidity_status'] for r in out for s in r['checks'] for l in s['legs']))},indent=2))

if __name__=='__main__':main()
