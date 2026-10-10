"""Native/ERC20 account cash reconciliation; unknowns never become zero-cost buys."""
import json
from collections import defaultdict, Counter
from pathlib import Path
from evidence import ROOT, read, save, now
from wallets import stamp

WETH='0x4200000000000000000000000000000000000006'
USDC='0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'

def address(x):return (x or {}).get('hash','').lower()

def classify(nets, native):
    nets={t:v for t,v in nets.items() if v}
    quotes={'ETH':native+nets.get(WETH,0),'USDC':nets.get(USDC,0)}
    quotes={q:v for q,v in quotes.items() if v}
    targets={t:v for t,v in nets.items() if t not in (WETH,USDC) and v}
    if len(targets)!=1 or len(quotes)!=1:return {'status':'UNRESOLVED_MULTIASSET_OR_NO_CASH'}
    token,qty=next(iter(targets.items()));quote,cash=next(iter(quotes.items()))
    if qty*cash>=0:return {'status':'UNRESOLVED_NO_OPPOSITE_CASH'}
    return {'status':'SINGLE_TARGET_OPPOSITE_ACCOUNT_CASH','token':token,'token_net_raw':str(qty),
            'quote':quote,'quote_net_raw':str(cash),'side':'buy' if qty>0 else 'sell'}

def main():
    wallets=[w for w in read('frozen_wallets.json')['rows'] if w['chain']=='base']
    allrows=[];sums=[];freeze=[]
    for w in wallets:
        addr=w['address'];tr=read('wallet_index/base_'+addr+'.json')
        ext=read('wallet_cash_index/external_'+addr+'.json');intr=read('wallet_cash_index/internal_'+addr+'.json')
        token_nets=defaultdict(lambda:defaultdict(int));native=defaultdict(int);meta={}
        for x in tr['items']:
            token=x['token']['address_hash'].lower();tx=x['transaction_hash']
            qty=int((x.get('total') or {}).get('value') or 0)
            token_nets[tx][token]+=qty*(int(address(x['to'])==addr)-int(address(x['from'])==addr))
            meta[tx]={'block_number':x['block_number'],'timestamp':stamp(x['timestamp'])}
        for x in intr['items']:
            if x.get('success') is not True:continue
            native[x['transaction_hash']]+=int(x['value'])*(int(address(x['to'])==addr)-int(address(x['from'])==addr))
        own={x['hash']:x for x in ext['items']}
        rows=[]
        for tx,x in own.items():
            success=x.get('status')=='ok'
            eth=(native[tx]-int(x.get('value') or 0)) if success else 0
            cls=classify(token_nets.get(tx,{}),eth) if success else {'status':'FAILED_TRANSACTION'}
            row={'wallet':addr,'tx_hash':tx,'timestamp':stamp(x['timestamp']),'block_number':x['block_number'],
                 'native_net_raw':str(eth),'token_nets':{t:str(v) for t,v in token_nets.get(tx,{}).items()},
                 'source_fee_raw':str((x.get('fee') or {}).get('value') or 0),
                 'index_complete':tr['index_range_complete'] and ext['complete'] and intr['complete'],
                 'after_snapshot':stamp(x['timestamp'])>=stamp(w['captured_at']),**cls}
            if not intr['complete']:row['cash_reconciliation']='INTERNAL_INDEX_INCOMPLETE'
            rows.append(row)
        rows.sort(key=lambda x:(x['block_number'],x['tx_hash']))
        buys=[r for r in rows if r.get('side')=='buy' and r['after_snapshot']]
        chosen=set()
        for r in buys:
            if r['token'] in chosen:continue
            chosen.add(r['token'])
            # Keep incomplete-index signals in manifest with explicit nonqualification.
            freeze.append({**r,'pilot_order_in_wallet':len(chosen)})
            if len(chosen)==2:break
        sums.append({'wallet':addr,'vendor_rank':w['frozen_rank'],
                     'n_indexed_transfers':len(tr['items']),'n_own_txs':len(rows),
                     'n_nonown_transfer_txs':len(set(token_nets)-set(own)),
                     'n_forward_buy_candidates':len(buys),'n_pilot_entries':min(2,len(chosen)),
                     'classification':dict(Counter(r['status'] for r in rows)),
                     'qualification':'PENDING_OPENING_INVENTORY_AND_RECEIPT_VALIDATION',
                     'complete_indexes':tr['index_range_complete'] and ext['complete'] and intr['complete']})
        allrows+=rows
    save('account_cash_ledger.json',{'rows':allrows,'wallets':sums,'note':'CASH_RECONCILIATION_NOT_SWAP_PROOF; all unmatched flows retained'})
    # Idempotence: signal manifest cannot silently change after outcomes are queried.
    p=ROOT/'pilot_entries.json'
    out={'frozen_at':now(),'selection':'first two distinct-token own buys per frozen wallet after supplier snapshot',
         'qualification':'SUPPLIER_CLAIM_ONLY','rows':freeze}
    if p.exists():assert read('pilot_entries.json')['rows']==freeze,'frozen entries would change'
    else:save('pilot_entries.json',out)
    print(json.dumps({'wallets':sums,'pilot_entries':len(freeze),'pilot_tokens':len({r['token'] for r in freeze})},indent=2))

if __name__=='__main__':main()
