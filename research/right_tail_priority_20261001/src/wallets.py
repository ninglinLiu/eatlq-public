"""Frozen Base wallet cohort: bounded complete-index pagination and audit."""
import json
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from collections import Counter, defaultdict
from evidence import ROOT, read, save, fetch, now

def stamp(v):
    return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()

def collect_one(w):
    path = 'wallet_index/base_' + w['address'] + '.json'
    if (ROOT / path).exists(): return read(path)
    # Include a 7-day qualification window BEFORE the saved ranking snapshot,
    # then all later activity. No outcomes used for pagination/sampling decisions.
    cutoff = stamp(w['captured_at']) - 7 * 86400
    params = {'type': 'ERC-20'}
    rows, pages, seen = [], [], set()
    complete, reason = False, 'PAGE_CAP_20'
    for page in range(20):
        url = f"https://base.blockscout.com/api/v2/addresses/{w['address']}/token-transfers?" + urlencode(params)
        m, d = fetch(url, label='base_wallet_index')
        pages.append({'page': page + 1, 'key': m['key'], 'http_status': m.get('http_status')})
        if not isinstance(d, dict) or not isinstance(d.get('items'), list):
            reason = 'API_ERROR'; break
        items = d['items']
        for x in items:
            key = x.get('transaction_hash'), x.get('log_index'), x.get('block_hash')
            if key in seen: continue
            seen.add(key)
            if x.get('timestamp') and stamp(x['timestamp']) >= cutoff: rows.append(x)
        if any(x.get('timestamp') and stamp(x['timestamp']) < cutoff for x in items):
            complete, reason = True, 'CROSSED_QUALIFICATION_START'; break
        nxt = d.get('next_page_params')
        if not nxt:
            complete, reason = True, 'INDEX_EXHAUSTED'; break
        params = {'type': 'ERC-20', **nxt}
    out = {'wallet': w['address'], 'snapshot': w['captured_at'], 'cutoff': cutoff,
           'index_range_complete': complete, 'status': reason, 'pages': pages, 'items': rows,
           'warning': 'Indexed transfers only; native flows, preexisting inventory and swaps require independent reconciliation.'}
    save(path, out)
    return out

def collect():
    ws = [w for w in read('frozen_wallets.json')['rows'] if w['chain'] == 'base']
    with ThreadPoolExecutor(max_workers=3) as ex: results = list(ex.map(collect_one, ws))
    sums = []
    for r in results:
        forward = [x for x in r['items'] if stamp(x['timestamp']) >= stamp(r['snapshot'])]
        sums.append({'wallet':r['wallet'], 'pages':len(r['pages']), 'n_transfers':len(r['items']),
                     'n_forward_transfers':len(forward), 'n_forward_txs':len({x['transaction_hash'] for x in forward}),
                     'status':r['status'], 'index_range_complete':r['index_range_complete']})
    save('wallet_index_summary.json', sums); print(json.dumps(sums,indent=2))

def collect_cash_index(w, kind):
    path='wallet_cash_index/'+kind+'_'+w['address']+'.json'
    if (ROOT/path).exists():return read(path)
    cutoff=stamp(w['captured_at'])-7*86400
    route='transactions' if kind=='external' else 'internal-transactions'
    params={'filter':'from'} if kind=='external' else {}
    rows=[];pages=[];seen=set();reason='PAGE_CAP_20';complete=False
    for page in range(20):
        url=f"https://base.blockscout.com/api/v2/addresses/{w['address']}/{route}"
        if params:url+='?'+urlencode(params)
        m,d=fetch(url,label='base_'+kind+'_index')
        pages.append({'key':m['key'],'http_status':m.get('http_status')})
        if not isinstance(d,dict) or not isinstance(d.get('items'),list):reason='API_ERROR';break
        items=d['items']
        for x in items:
            key=(x.get('hash') or x.get('transaction_hash'),x.get('index'),x.get('block_number'))
            if key in seen:continue
            seen.add(key)
            if x.get('timestamp') and stamp(x['timestamp'])>=cutoff:rows.append(x)
        if any(x.get('timestamp') and stamp(x['timestamp'])<cutoff for x in items):
            complete=True;reason='CROSSED_QUALIFICATION_START';break
        nxt=d.get('next_page_params')
        if not nxt:complete=True;reason='INDEX_EXHAUSTED';break
        params=({'filter':'from'} if kind=='external' else {})|nxt
    out={'wallet':w['address'],'kind':kind,'cutoff':cutoff,'snapshot':w['captured_at'],
         'complete':complete,'status':reason,'pages':pages,'items':rows}
    save(path,out);return out

def cash():
    ws=[w for w in read('frozen_wallets.json')['rows'] if w['chain']=='base']
    jobs=[(w,k) for w in ws for k in ('external','internal')]
    with ThreadPoolExecutor(max_workers=3) as ex:rs=list(ex.map(lambda j:collect_cash_index(*j),jobs))
    ss=[{k:v for k,v in r.items() if k not in ('items','pages')}|{'n':len(r['items']),'n_pages':len(r['pages'])} for r in rs]
    save('wallet_cash_summary.json',ss);print(json.dumps(ss,indent=2))

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['transfers','cash'],default='transfers',nargs='?');a=p.parse_args()
    collect() if a.command=='transfers' else cash()
