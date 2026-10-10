"""Bounded, same-timestamp sell-route prefix diagnostics; never replaces PnL.

A shortened call can change hook context. A first failing prefix narrows the
adapter/pool investigation; it is not proof of permanent token unsellability.
"""
import json
from evidence import read,save,now
from smartmoney_settlement import simulate

def run():
    rows=read('smartmoney_follower_execution_result.json')['rows']
    routes={r['source_tx']:r for r in read('smartmoney_follower_routes.json')['rows']}
    keys=read('smartmoney_v4_keys_calldata.json')['keys'];out=[]
    for r in rows:
        if r['status']!='SELL_SIMULATION_UNRESOLVED':continue
        route=routes[r['source_tx']]['route'];steps=[]
        for length in range(1,len(route)+1):
            if length==len(route):result=r['sell'];origin='REUSE_ORIGINAL_FULL_ROUTE'
            else:
                result=simulate(route[:length],keys,int(r['buy']['received_raw']),
                    r['sell']['block'],False,r['slot_evidence']['slot'],mixed=True)
                origin='SHORTENED_ROUTE_DIAGNOSTIC_ONLY'
            item={'prefix_length':length,'last_pool':route[length-1]['id'],
                  'last_protocol':route[length-1]['kind'],'origin':origin,'simulation':result}
            steps.append(item)
            if result.get('status')!='HISTORICAL_TRANSFER_SETTLED_SIMULATION':break
        failed=steps[-1] if steps[-1]['simulation'].get('status')!='HISTORICAL_TRANSFER_SETTLED_SIMULATION' else None
        out.append({'source_tx':r['source_tx'],'token':r['token'],'original_status':r['status'],
            'steps':steps,'first_failing_prefix':failed['prefix_length'] if failed else None,
            'first_failing_prefix_last_pool':failed['last_pool'] if failed else None,
            'not_a_replacement_exit_or_profit_result':True})
    result={'generated_at':now(),'n_mature_unresolved_exits':len(out),'rows':out,
        'qualification':'CALL_PREFIX_DIAGNOSTIC_NOT_CAUSAL_TRACE_OR_UNSELLABILITY_PROOF',
        'limits':['same entry quantity, original exit timestamp and caller; no alternate price-selected route',
                  'shorter calldata/route can change hook behavior; cannot identify an exact internal cause',
                  'original unresolved denominator and all cash accounting remain unchanged']}
    save('smartmoney_route_failure_probe.json',result)
    print(json.dumps({'probed':len(out),'results':[{k:v for k,v in r.items() if k!='steps'} for r in out]},indent=2))

if __name__=='__main__':run()
