"""RT08 locked contracts: coverage audit and end-of-hour attachment replay."""
import argparse
import csv
import json
import time
from datetime import datetime, timezone
from collections import defaultdict, Counter
from pathlib import Path
from statistics import mean, median
from concurrent.futures import ThreadPoolExecutor
from evidence import ROOT, save, read, fetch, digest, now

OLD = Path('/root/mimic_meme_score_r0/data')

def universe():
    return list(csv.DictReader((OLD/'tables/contracts_all.csv').open()))

def load_series():
    series = defaultdict(dict)
    files = defaultdict(list)
    for p in sorted((OLD/'raw').glob('gt_ohlcv_*.json')):
        try: d = json.loads(p.read_text())
        except (ValueError, UnicodeError): continue
        data = d.get('data', {})
        if not isinstance(data, dict): continue
        attrs = data.get('attributes', {})
        bars = attrs.get('ohlcv_list', [])
        # Metadata identifies actual priced asset; never infer price direction by filename alone.
        base = d.get('meta', {}).get('base', {})
        token = base.get('address', '').lower()
        net = p.name.split('_')[2]
        if not token or not bars: continue
        pool = p.name.split('_')[3].lower()
        key = net, pool, token
        for b in bars: series[key][int(b[0])] = b
        files[key].append({'path':str(p),'sha256':digest(p.read_bytes())})
    return series, files

def collect():
    # All saved BSC/Base contracts, not just the old primary-chain top-eight.
    rows = [r for r in universe() if r['chain'] in ('bsc','base')]
    old, files = load_series()
    jobs = []
    for r in rows:
        key = r['chain'], r['pair_address'].lower(), r['address'].lower()
        born = int(float(r['pair_created_at_unix'] or 0))
        have = old.get(key,{})
        # Request fixed 14 days from saved pool creation for missing/late histories.
        # New data are not used to swap pools or drop tokens.
        if not have or (born and min(have) > born + 3600):
            jobs.append((r, born + 14*86400 if born else 1789603200))
    def one(j):
        r, before = j
        url = f"https://api.geckoterminal.com/api/v2/networks/{r['chain']}/pools/{r['pair_address']}/ohlcv/hour?aggregate=1&limit=1000&currency=usd&before_timestamp={before}"
        m,d = fetch(url,label='narrative_locked_pool_ohlcv')
        return {'contract_id':r['contract_id'],'chain':r['chain'],'pool':r['pair_address'].lower(),
                'token':r['address'].lower(),'before':before,'key':m['key'], 'http_status':m.get('http_status'),
                'data':d}
    with ThreadPoolExecutor(max_workers=2) as ex: results=list(ex.map(one,jobs))
    save('narrative_new_series.json',results)
    print(json.dumps({'requested':len(results),'http_status':dict(Counter(r['http_status'] for r in results))}))

def retry_throttled():
    """Retry the same frozen 429 requests, paced; never retry an auth denial."""
    rows = read('narrative_new_series.json')
    backup = 'narrative_new_series_before_retry.json'
    if not (ROOT/backup).exists():
        save(backup, rows)
    attempts = []
    for i, r in enumerate(rows):
        if r.get('http_status') != 429:
            continue
        old = read('raw/'+r['key']+'.meta.json')
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(old['finished'])).total_seconds()
        try: cooldown = max(60, float(old.get('retry_after') or 0))
        except ValueError: cooldown = 120
        if age < cooldown:
            continue
        # A sequential retry with 6-second spacing, well below the earlier rate.
        if attempts:
            time.sleep(6)
        m, d = fetch(old['url'], label='narrative_locked_pool_ohlcv_retry', cache=False)
        r['prior_http_status'] = r['http_status']
        r['http_status'], r['data'], r['key'] = m.get('http_status'), d, m['key']
        attempts.append({'contract_id':r['contract_id'], 'http_status':m.get('http_status'), 'key':m['key']})
        save('narrative_new_series.json', rows)
        save('narrative_retry_audit.json', {'at':now(), 'attempts':attempts,
             'http_status':dict(Counter(x['http_status'] for x in rows))})
        print(json.dumps(attempts[-1]), flush=True)
        # A fresh throttle means stop this pass, not rotate IPs/providers.
        if m.get('http_status') == 429:
            break
    print(json.dumps({'retried':len(attempts), 'http_status':dict(Counter(r['http_status'] for r in rows))}))

def replay_group(rr, series):
    priced={}
    for r in rr:
        net={'ethereum':'eth'}.get(r['chain'],r['chain'])
        k=net,r['pair_address'].lower(),r['address'].lower()
        priced[r['contract_id']]=series.get(k,{})
    born={r['contract_id']:int(float(r['pair_created_at_unix'] or 0)) for r in rr}
    times=sorted({t for d in priced.values() for t in d})
    qualified, audited, missing_hours = [], 0, 0
    previous_winner = None
    for t in times:
        # Creation timestamp is pool creation, not guaranteed token birth.
        active=[r['contract_id'] for r in rr if born[r['contract_id']]<=t+3599]
        if len(active)<2: continue
        if not all(t in priced[c] for c in active):
            missing_hours+=1; previous_winner=None; continue
        audited+=1
        vols=sorted([(float(priced[c][t][5]),c) for c in active],reverse=True)
        total=sum(v for v,c in vols)
        winner=None
        if total>=500 and (vols[0][0]/total>=.5 or (vols[0][0]/total>=.35 and vols[0][0]>=1.4*vols[1][0])):
            winner=vols[0][1]
        if winner and winner!=previous_winner:
            # First qualifying covered hour is left censored unless previous hour
            # had complete competitors AND no dominant winner.
            prev=t-3600
            prev_active=[c for c in active if born[c]<=prev+3599]
            prev_complete=len(prev_active)>=2 and all(prev in priced[c] for c in prev_active)
            prev_qualified=False
            if prev_complete:
                vv=sorted([float(priced[c][prev][5]) for c in prev_active],reverse=True);ss=sum(vv)
                prev_qualified=ss>=500 and (vv[0]/ss>=.5 or (vv[0]/ss>=.35 and vv[0]>=1.4*vv[1]))
            state='OBSERVED_DOMINANCE_TRANSITION' if prev_complete and not prev_qualified else 'LEFT_CENSORED_OR_WINNER_SWITCH'
            signal={'hour_start':t,'available_at':t+3600,'winner':winner,'share':vols[0][0]/total,
                    'n_competitors':len(active),'status':state,'price_kind':'HOURLY_REFERENCE_NOT_EXECUTABLE_FILL','returns':{}}
            # One whole-bar extra lag ensures no completed-hour close is treated as own fill.
            entry_t=t+7200
            for h in (1,4,24):
                exits={}
                for c in active:
                    entry=priced[c].get(entry_t);ex=priced[c].get(entry_t+h*3600)
                    if entry and ex and entry[1]>0 and entry[5]>0 and ex[5]>0:
                        exits[c]=float(ex[1])/float(entry[1])-1
                earliest=min(active,key=lambda c:(born[c],c))
                signal['returns'][str(h)]={'winner_gross':exits.get(winner),
                    'equal_weight_gross':mean(exits.values()) if len(exits)==len(active) else None,
                    'earliest_gross':exits.get(earliest),'resolved_competitors':len(exits)}
            qualified.append(signal)
        previous_winner=winner
    return {'n_contracts':len(rr),'n_series':sum(bool(x) for x in priced.values()),
            'n_full_coverage_hours':audited,'n_missing_competitor_hours':missing_hours,
            'signals':qualified,'status':'NO_COMPETITION' if len(rr)<2 else
                'INCOMPLETE_COMPETITOR_HISTORY' if audited==0 else 'CONDITIONAL_REPLAY_ONLY'}

def analyze():
    cs=universe();series,files=load_series()
    if (ROOT/'narrative_new_series.json').exists():
        for r in read('narrative_new_series.json'):
            d=r.get('data') or {}; token=d.get('meta',{}).get('base',{}).get('address','').lower()
            if token!=r['token']: continue
            key=r['chain'],r['pool'],token
            attrs=(d.get('data') or {}).get('attributes',{})
            for b in attrs.get('ohlcv_list',[]):series[key][int(b[0])]=b
    events=list(csv.DictReader((OLD/'tables/events.csv').open()))
    groups=[]
    for e in events:
        # All BSC/Base groups plus original primary chain as a labelled supplemental arm.
        nets=sorted({'bsc','base',e['chain_primary']})
        for net in nets:
            rr=[r for r in cs if r['event_id']==e['event_id'] and r['chain']==net]
            if not rr and net!=e['chain_primary']:continue
            groups.append({'event_id':e['event_id'],'chain':net,'primary_scope':net in ('bsc','base'),**replay_group(rr,series)})
    out={'generated_at':now(),'locked_events':len(events),'locked_contract_rows':len(cs),
         'rh_excluded':sum(r['chain']=='robinhood' for r in cs),
         'biases':['retrospectively named events','DEX search incomplete for disappeared contracts',
                   'saved pool selected at collection, not proven available at historical signal',
                   'strict absence-as-unknown, not zero-volume assumption','hour bars cannot test 10-second execution'],
         'groups':groups,'source_files':dict((str(k),v) for k,v in files.items())}
    save('narrative_result.json',out)
    print(json.dumps([{k:v for k,v in g.items() if k!='signals'}|{'signals':len(g['signals'])} for g in groups],indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['collect','retry','analyze']);a=p.parse_args()
    {'collect':collect,'retry':retry_throttled,'analyze':analyze}[a.command]()
