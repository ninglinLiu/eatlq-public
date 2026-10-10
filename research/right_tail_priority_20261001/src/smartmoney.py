"""RT06 full frozen-candidate validation and explicit universe coverage."""
import argparse
import csv
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from evidence import ROOT, read, save, digest, now
from pilot import one_receipt, req, TRANSFER
from wallets import stamp

def universe_audit():
    source=Path(read('frozen_wallets.json')['source'])
    boards=json.loads(source.read_text())
    rows=[r for r in boards['rows'] if r.get('source')=='gmgn' and r.get('spot_reconcilable')
          and r.get('chain') in ('base','bsc')]
    summary=[]; cohort=[]
    frozen=read('frozen_wallets.json')['rows']
    for chain in ('base','bsc'):
        for window in sorted({r['window'] for r in rows if r['chain']==chain}):
            rr=[r for r in rows if r['chain']==chain and r['window']==window]
            by={}
            for r in sorted(rr,key=lambda r:(r['captured_at'],r.get('tag',''))):
                by.setdefault(r['address'].lower(),r)
            ranked=sorted(by.values(),key=lambda r:(-float(r.get('vendor_realized_usd') or 0),r['address'].lower()))
            cohort.extend({**r,'address':r['address'].lower(),'rank_in_saved_window':i+1} for i,r in enumerate(ranked))
            summary.append({'chain':chain,'window':window,'saved_rows':len(rr),'unique_addresses':len(by),
                'saved_tags':dict(Counter(r['tag'] for r in rr)),
                'n_initial_frozen':sum(w['chain']==chain for w in frozen) if window=='7d' else 0,
                'initial_fraction_of_saved_addresses':12/len(by) if window=='7d' and by else None})
    name='smartmoney_full_saved_cohort.json'
    obj={'frozen_at':now(),'source_sha256':digest(source.read_bytes()),'source':str(source),
         'selection':'ALL saved GMGN Base/BSC spot rows deduplicated by chain/window/address; no new outcome screen',
         'qualification':'SAVED_SOURCE_UNIVERSE_NOT_CHAINWIDE_CENSUS; expansion after original pilot results',
         'rows':cohort}
    if (ROOT/name).exists():assert read(name)['rows']==cohort
    else:save(name,obj)
    ledger=read('account_cash_ledger.json');after=[r for r in ledger['rows'] if r['after_snapshot']]
    buys=read('expanded_candidate_manifest.json')['rows']
    nonown=[]; fwd_transfers=[]
    for w in frozen:
        if w['chain']!='base':continue
        tr=read('wallet_index/base_'+w['address']+'.json')
        own={x['hash'] for x in read('wallet_cash_index/external_'+w['address']+'.json')['items']}
        forward={x['transaction_hash'] for x in tr['items'] if stamp(x['timestamp'])>=stamp(w['captured_at'])}
        fwd_transfers.extend((w['address'],tx) for tx in forward)
        nonown.extend((w['address'],tx) for tx in forward-own)
    out={'generated_at':now(),'saved_source_coverage':summary,
         'total_saved_spot_unique_chain_addresses':len({(r['chain'],r['address'].lower()) for r in rows}),
         'frozen_wallets':len(frozen),'base_frozen_wallets':12,'bsc_candidate_trades_in_this_manifest':0,
         'initial_retained_source_tag_counts':{c:dict(Counter(r['tag'] for r in frozen if r['chain']==c))
             for c in ('base','bsc')},
         'tag_count_note':'tag on retained deduplicated record; not independently validated economic skill or exclusive identity',
         'n_indexed_own_txs':len(ledger['rows']),'n_after_snapshot_own_txs':len(after),
         'after_snapshot_classification':dict(Counter(r.get('side',r['status']) for r in after)),
         'after_snapshot_wallet_transfer_tx_pairs':len(set(fwd_transfers)),
         'after_snapshot_nonown_transfer_tx_pairs':len(set(nonown)),
         'n_buy_candidates':len(buys),'n_candidate_tokens':len({r['token'] for r in buys}),
         'n_candidate_wallets':len({r['wallet'] for r in buys}),
         'candidate_index_complete':dict(Counter(str(r['index_complete']) for r in buys)),
         'candidate_start_utc':datetime.fromtimestamp(min(r['timestamp'] for r in buys),timezone.utc).isoformat(),
         'candidate_end_utc':datetime.fromtimestamp(max(r['timestamp'] for r in buys),timezone.utc).isoformat(),
         'pilot_rule':'first two distinct-token buys per active wallet; NOT a profit eligibility gate',
         'n_pilot':len(read('pilot_entries.json')['rows']),
         'biases':['one supplier, capped ranked category snapshots, not all profitable wallets',
             '12 of 295 saved Base 7d addresses and no BSC activity in 441 candidates',
             'retained Base top-12 rows are 10 kol and 2 smart_degen; not a broad Smart Money label census',
             'one ranking window and about two weeks of subsequent trades, not independent regimes',
             'own-sent transactions only; delegated/AA/non-own activity not classified here',
             'single target and ETH/WETH/USDC opposite cash; multiasset/other quotes not classified',
             'repeated same-wallet/token signals are not independent opportunities',
             'largest historical absolute supplier PnL is a selection hypothesis, not copyability proof']}
    save('smartmoney_universe_audit.json',out)
    print(json.dumps(out,ensure_ascii=False,indent=2))

def receipt_summary(rows):
    checked=[r for r in rows if r.get('receipt_status')=='CONFIRMED_SUCCESS']
    return {'n_rows':len(rows),'status':dict(Counter(r.get('receipt_status') for r in rows)),
        'token_net_matches':sum(r.get('receipt_token_net_matches_index',False) for r in rows),
        'from_matches':sum(r.get('receipt_from_matches_wallet',False) for r in rows),
        'source_fee_matches':sum(r.get('source_fee_receipt_raw')==r['source_fee_raw'] for r in checked),
        'with_recognized_swap_log':sum(bool(r['swap_logs']) for r in rows),
        'swap_logs_by_kind':dict(Counter(l['kind'] for r in rows for l in r['swap_logs'])),
        'cash_index_incomplete':sum(not r['index_complete'] for r in rows)}

def receipts():
    manifest=read('expanded_candidate_manifest.json')['rows']
    def one(r):
        name='smartmoney_receipts/'+r['tx_hash']+'.json'
        if (ROOT/name).exists():
            old=read(name)
            if old.get('receipt_status')=='CONFIRMED_SUCCESS':return old
        out=one_receipt(r)
        # A recognized event topic is not, on its own, proof of a trusted pool.
        out['validation_layer']='ACCOUNT_RECEIPT_AND_LOGS; PROTOCOL/ROUTE MUST BE VERIFIED SEPARATELY'
        save(name,out)
        return out
    done={}
    with ThreadPoolExecutor(max_workers=3) as ex:
        jobs={ex.submit(one,r):r for r in manifest}
        for f in as_completed(jobs):
            r=f.result();done[r['tx_hash']]=r
            if len(done)%20==0 or len(done)==len(manifest):
                save('smartmoney_receipt_progress.json',{'generated_at':now(),'total_frozen':len(manifest),**receipt_summary(list(done.values()))})
                print(json.dumps({'done':len(done),'total':len(manifest),'ok':sum(x.get('receipt_status')=='CONFIRMED_SUCCESS' for x in done.values())}),flush=True)
    rows=[done[r['tx_hash']] for r in manifest]
    out={'generated_at':now(),'manifest_sha256':digest((ROOT/'expanded_candidate_manifest.json').read_bytes()),
         'summary':receipt_summary(rows),'rows':rows}
    save('smartmoney_receipts.json',out)
    print(json.dumps(out['summary'],indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['universe','receipts']);a=p.parse_args()
    {'universe':universe_audit,'receipts':receipts}[a.command]()
