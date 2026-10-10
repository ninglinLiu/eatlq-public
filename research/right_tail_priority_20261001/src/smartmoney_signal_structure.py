"""Offline concentration and scheduled overlap; not a portfolio backtest."""
import json
from collections import Counter,defaultdict
from evidence import read,save,now

def run():
    rows=read('smartmoney_routes_extended.json')['rows']
    tokens=Counter(r['token'] for r in rows);wallets=Counter(r['wallet'] for r in rows)
    token_wallets=defaultdict(set);events=[]
    for r in rows:
        token_wallets[r['token']].add(r['wallet'])
        entry=int(r['source_ts'])+10
        # At a coincident timestamp, the scheduled exit frees the old position
        # before the new entry. This is a schedule convention, not block ordering.
        events.extend([(entry,1,r['token']),(entry+86400,-1,r['token'])])
    active=0;peak=0;peak_ts=None;bytoken=Counter();token_peak=Counter()
    for ts,delta,token in sorted(events):
        active+=delta;bytoken[token]+=delta
        token_peak[token]=max(token_peak[token],bytoken[token])
        if active>peak:peak=active;peak_ts=ts
    grouped=[{'token':t,'signals':tokens[t],'distinct_wallets':len(token_wallets[t]),
              'maximum_scheduled_concurrent_positions':token_peak[t]} for t in tokens]
    grouped.sort(key=lambda x:(-x['signals'],x['token']))
    result={'generated_at':now(),'n_signals':len(rows),'n_tokens':len(tokens),'n_wallets':len(wallets),
        'wallet_signal_counts':dict(wallets),'single_signal_tokens':sum(n==1 for n in tokens.values()),
        'multiple_signal_tokens':sum(n>1 for n in tokens.values()),
        'multiple_wallet_tokens':sum(len(v)>1 for v in token_wallets.values()),
        'maximum_scheduled_concurrent_positions':peak,'peak_scheduled_timestamp':peak_ts,
        'peak_scheduled_gross_allocation_eth_raw':str(peak*10**16),
        'token_signal_counts':grouped,
        'qualification':'descriptive frozen 24h schedule only; assumes all signals open until fixed exit',
        'not_claimed':['not actual capital required after fees, cash recycling and incomplete fills',
                       'not independent-event count','not cumulative-impact or portfolio PnL simulation']}
    save('smartmoney_signal_structure.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='token_signal_counts'},indent=2))

if __name__=='__main__':run()
