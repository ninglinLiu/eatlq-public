"""Observed source fees and explicitly conditional own-L2-fee stress, offline."""
import argparse
import json
from evidence import ROOT,read,save,now
from smartmoney_routes import raw_receipt

def quantiles(values):
    a=sorted(values)
    return {name:a[min(len(a)-1,int(p*(len(a)-1)))] for name,p in
            [('min',0),('p25',.25),('p50',.5),('p90',.9),('p99',.99),('max',1)]}

def source():
    rows=[]
    for r in read('smartmoney_receipts.json')['rows']:
        raw=raw_receipt(r)
        rows.append({'source_tx':r['tx_hash'],'gas_used':int(raw['gasUsed'],16),
            'effective_gas_price_wei':int(raw['effectiveGasPrice'],16),'source_fee_raw':r['source_fee_raw']})
    out={'generated_at':now(),'n':len(rows),'gas_used':quantiles([r['gas_used'] for r in rows]),
        'effective_gas_price_wei':quantiles([r['effective_gas_price_wei'] for r in rows]),
        'source_fee_raw':{k:str(v) for k,v in quantiles([int(r['source_fee_raw']) for r in rows]).items()},
        'sum_source_fee_raw':str(sum(int(r['source_fee_raw']) for r in rows)),
        'qualification':'source transactions only; not the copier cost or evidence of 10-second inclusion', 'rows':rows}
    save('smartmoney_source_fee_diagnostics.json',out)
    print(json.dumps({k:v for k,v in out.items() if k!='rows'},indent=2))

def stress():
    source_file=('smartmoney_follower_execution_result.json' if (ROOT/'smartmoney_follower_execution_result.json').exists()
                 else 'smartmoney_settlement_expanded_result.json')
    all_rows=read(source_file)['rows']
    rows=[r for r in all_rows
          if r.get('modeled_net_eth_raw') is not None and r.get('intermediate_inventory_status')!='NOT_MEASURED']
    cash_rows=[r for r in all_rows if r.get('cash_recovered_after_modeled_chain_fees_less_entry_raw') is not None
               and r['status'] in ('CONDITIONAL_EXECUTION_COST_MODEL','PARTIAL_EXIT_INVENTORY_REMAINS','INTERMEDIATE_INVENTORY_REMAINS')]
    cases=[]
    for tip in (10**7,10**8,10**9,10**10,2*10**10):
        pnl=[int(r['modeled_net_eth_raw'])-(tip-10**7)*(r['buy']['gas_model_units']+r['sell']['gas_model_units']) for r in rows]
        cash=[int(r['cash_recovered_after_modeled_chain_fees_less_entry_raw'])
              -(tip-10**7)*(r['buy']['gas_model_units']+r['sell']['gas_model_units']) for r in cash_rows]
        cases.append({'tip_wei':tip,'modeled_roundtrips':len(pnl),'positive_roundtrips':sum(x>0 for x in pnl),
            'sum_conditional_net_raw':str(sum(pnl)),
            'closed_and_partial_cash_rows':len(cash),
            'closed_and_partial_cash_delta_raw':str(sum(cash)), 'not_portfolio_profit':True})
    save('smartmoney_l2_fee_stress.json',{'generated_at':now(),'settlement_input_artifact':source_file,'cases':cases,
        'qualification':'AFTER_PRIMARY_RESULTS_DIAGNOSTIC_NOT_PARAMETER_SELECTION',
        'limits':['L2 gas sensitivity only; original L1/operator estimates held fixed',
                  'higher fee does not prove inclusion at the modeled 10-second timestamp',
                  'partial-exit cash is included separately; remaining inventory is not valued',
                  'unresolved or unmatured exits and unknown entry costs are outside these subset sums',
                  'own transaction gas units used, not source transaction fee substituted']})
    print(json.dumps(cases,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['source','stress']);a=p.parse_args()
    {'source':source,'stress':stress}[a.command]()
