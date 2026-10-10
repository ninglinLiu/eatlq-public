"""Conditional cash timing from already-modelled fills, not a live portfolio."""
import json
from evidence import read,save,now

def cash_path(events,buy_first=True):
    order={'buy':0,'sell':1} if buy_first else {'sell':0,'buy':1}
    balance=0;minimum=0;minimum_ts=None;out=[]
    for e in sorted(events,key=lambda e:(e['timestamp'],order[e['leg']],e['source_tx'])):
        balance+=int(e['cash_delta_raw'])
        if balance<minimum:minimum=balance;minimum_ts=e['timestamp']
        out.append(e|{'cumulative_cash_without_initial_capital_raw':str(balance)})
    return {'minimum_initial_quote_raw':str(-minimum),'peak_deficit_timestamp':minimum_ts,
            'ending_cash_delta_raw':str(balance),'events':out}

def run():
    rows=read('smartmoney_follower_execution_result.json')['rows']
    cutoff=read('smartmoney_observation_cutoff.json')['chain_timestamp']
    events=[];excluded=[];open_positions=[];entries=exits=0
    for r in rows:
        b=r.get('buy',{});bf=r.get('buy_fees',{}).get('total_fee_model_raw')
        if not b.get('received_raw') or int(b['received_raw'])<=0 or bf is None:
            excluded.append({'source_tx':r['source_tx'],'status':r['status'],'reason':'entry fill or own fee not modelled'})
            continue
        assert b['timestamp']<=cutoff
        entries+=1
        events.append(dict(timestamp=b['timestamp'],source_tx=r['source_tx'],token=r['token'],leg='buy',
                           cash_delta_raw=str(-int(b['spent_raw'])-int(bf))))
        s=r.get('sell',{});sf=r.get('sell_fees',{}).get('total_fee_model_raw')
        has_exit=(s.get('received_raw') is not None and sf is not None and s.get('timestamp',cutoff+1)<=cutoff)
        if has_exit:
            exits+=1
            events.append(dict(timestamp=s['timestamp'],source_tx=r['source_tx'],token=r['token'],leg='sell',
                               cash_delta_raw=str(int(s['received_raw'])-int(sf))))
        if not has_exit or r['status'] in ('PARTIAL_EXIT_INVENTORY_REMAINS','INTERMEDIATE_INVENTORY_REMAINS'):
            open_positions.append({'source_tx':r['source_tx'],'token':r['token'],'status':r['status'],
                                   'inventory_value_raw':None})
    cautious=cash_path(events,True);alternative=cash_path(events,False)
    result={'generated_at':now(),'frozen_candidates':len(rows),'observation_cutoff':cutoff,
        'entries_with_fill_and_fee':entries,'exits_with_cash_and_fee':exits,
        'excluded_entries':excluded,'unpriced_open_positions':open_positions,
        'buy_first_same_timestamp':cautious,
        'sell_first_same_timestamp':{k:v for k,v in alternative.items() if k!='events'},
        'qualification':'CONDITIONAL_INDEPENDENT_FILL_CASH_SCHEDULE_NOT_PORTFOLIO_PROFIT_CERTIFICATION',
        'limits':['ETH/WETH treated 1:1; no extra sweep/unwrap included',
                  'no own cumulative pool impact, ordering/inclusion failures, retries or operating costs',
                  'unresolved entries omitted explicitly, not assigned winning or losing PnL',
                  'unknown/unmatured exits have no invented cash receipt; holdings not marked to zero',
                  'cash deficit is not mark-to-market drawdown or a probability of ruin']}
    assert entries+len(excluded)==len(rows)
    save('smartmoney_cash_schedule.json',result)
    print(json.dumps({'entries':entries,'exits':exits,'unpriced_open_positions':len(open_positions),
        'excluded_entries':len(excluded),'cash_schedule':{k:v for k,v in cautious.items() if k!='events'}},indent=2))

if __name__=='__main__':run()
