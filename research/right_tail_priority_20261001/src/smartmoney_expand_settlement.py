"""Extend frozen settlement denominator to registered V2/V3/V4 mixed routes.

Run after the pure-V4 collector has stopped (one network collector at a time).
No new signals, sizes, delays, or outcome-based replacement of missing routes.
"""
import argparse
import json
import csv
from decimal import Decimal
from collections import Counter,defaultdict
from evidence import ROOT,read,save,now
from smartmoney_settlement import simulate,own_fees,detect_balance_slot,has_throttle

def exit_not_observed(r):
    name='smartmoney_observation_cutoff.json'
    return (ROOT/name).exists() and r['source_ts']+86410>read(name)['chain_timestamp']

def cash_account(item,r,keys,mixed):
    buy=item.get('buy',{});sell=item.get('sell',{})
    item.update(execution_venue='SELF_BUILT_DIRECT_EXECUTOR',platform_fee_model_raw='0',
        platform_fee_status='NO_THIRD_PARTY_ROUTER_COMMISSION_BY_DESIGN',
        operating_cost_status='RPC_SERVER_DEPLOYMENT_NOT_AMORTIZED',net_profit_certified=False)
    item['settlement_cash_asset']=(r['route'][-1]['to_token'] if r.get('route') and 'to_token' in r['route'][-1] else None)
    item['custody_note']='output measured at research executor; external-wallet sweep/unwrap not included'
    if exit_not_observed(r):
        item.update(status='EXIT_HORIZON_NOT_YET_OBSERVED',modeled_net_eth_raw=None,
                    scheduled_exit_timestamp=r['source_ts']+86410,
                    observation_cutoff=read('smartmoney_observation_cutoff.json')['chain_timestamp'])
        return item
    if not sell.get('received_raw') or not buy.get('received_raw'):return item
    qty=int(buy['received_raw']);spent=int(sell['spent_raw'])
    assert spent<=qty
    if not item.get('sell_fees') or has_throttle(item['sell_fees']):
        item['sell_fees']=own_fees(r['route'],keys,qty,sell['block'],False,sell,mixed=mixed)
    residual=qty-spent;item['target_inventory_remaining_raw']=str(residual)
    gas=[item.get('buy_fees',{}).get('total_fee_model_raw'),item['sell_fees'].get('total_fee_model_raw')]
    cash=int(sell['received_raw'])-int(buy['spent_raw'])
    item['settled_before_gas_pnl_raw']=str(cash)
    item['cash_recovered_after_modeled_chain_fees_less_entry_raw']=str(cash-sum(map(int,gas))) if all(x is not None for x in gas) else None
    item['residual_inventory_value_raw']=None if residual else '0'
    item['residual_note']='cash deficit is not total loss: unsold inventory is not valued at zero'
    intermediate=[]
    for sim in (buy,sell):intermediate.extend(sim.get('per_leg_unspent_input_raw',[])[1:])
    has_intermediate=any(int(v)>0 for v in intermediate)
    item['intermediate_inventory_status']=('MEASURED_LEFTOVER' if has_intermediate else 'MEASURED_NO_LEFTOVER') if mixed else ('NOT_MEASURED' if len(r['route'])>1 else 'NO_INTERMEDIATE_LEG')
    if residual or has_intermediate:
        item['status']='PARTIAL_EXIT_INVENTORY_REMAINS' if residual else 'INTERMEDIATE_INVENTORY_REMAINS'
        item['modeled_net_eth_raw']=None
    elif all(x is not None for x in gas):
        item['status']='CONDITIONAL_EXECUTION_COST_MODEL'
        item['modeled_net_eth_raw']=item['cash_recovered_after_modeled_chain_fees_less_entry_raw']
    else:item['status']='SETTLEMENT_SIMULATED_FEE_COMPONENT_UNRESOLVED'
    item['horizon_seconds_observed']=sell['timestamp']-buy['timestamp']
    item['entry_delay_seconds_observed']=buy['timestamp']-r['source_ts']
    return item

def mixed_one(r,keys):
    item={k:r[k] for k in ('source_tx','wallet','token','source_block')}
    item.update(input_eth_raw=str(10**16),exit_policy='24h',delay_seconds=10,
                modeled_net_eth_raw=None,net_profit_certified=False,adapter='MIXED_REGISTERED_FACTORY_V1')
    block=r['source_block']+5;exit_block=block+43200
    buy=simulate(r['route'],keys,10**16,block,True,mixed=True);item['buy']=buy
    if not buy.get('received_raw') or int(buy['received_raw'])==0:
        return item|{'status':'BUY_SIMULATION_UNRESOLVED_OR_ZERO'}
    item['buy_fees']=own_fees(r['route'],keys,10**16,block,True,buy,mixed=True)
    if exit_not_observed(r):return cash_account(item,r,keys,True)
    slot=detect_balance_slot(r['token'],exit_block);item['slot_evidence']=slot
    sell=simulate(r['route'],keys,int(buy['received_raw']),exit_block,False,slot['slot'],mixed=True);item['sell']=sell
    if not sell.get('received_raw'):return item|{'status':'SELL_SIMULATION_UNRESOLVED'}
    return cash_account(item,r,keys,True)

def run():
    keys=read('smartmoney_v4_keys_calldata.json')['keys'];rows=read('smartmoney_routes_extended.json')['rows']
    original_routes={r['source_tx']:r['route'] for r in read('smartmoney_routes.json')['rows']}
    out=[]
    for i,r in enumerate(rows):
        name='smartmoney_settlement_expanded/'+r['source_tx']+'.json'
        if (ROOT/name).exists() and not has_throttle(read(name)):
            out.append(read(name));continue
        pure=read('smartmoney_settlement/'+r['source_tx']+'.json')
        if r['route'] and (pure['status'] in ('MIXED_PROTOCOL_ADAPTER_PENDING','ROUTE_UNRESOLVED') or
                           original_routes[r['source_tx']]!=r['route'] or len(r['route'])>1):item=mixed_one(r,keys)
        else:item=cash_account(dict(pure),r,keys,False)
        save(name,item);out.append(item)
        if (i+1)%10==0 or i+1==len(rows):
            p={'done':i+1,'total':len(rows),'statuses':dict(Counter(x['status'] for x in out))}
            save('smartmoney_settlement_expanded_progress.json',p);print(json.dumps(p),flush=True)
    save('smartmoney_settlement_expanded_result.json',{'generated_at':now(),'n_frozen':len(rows),'rows':out,
        'statuses':dict(Counter(x['status'] for x in out)),
        'qualification':'CONDITIONAL_HISTORICAL_EXECUTION_MODEL_NOT_LIVE_PROFIT_PROOF',
        'remaining':['unresolved source routes retained; no synthetic path added',
          'only registered Uniswap V2/V3, Aerodrome V2/CL, verified Algebra deployments and official V4 supported',
          'empty hookData / hypothetical executor context, not source-router equivalence',
          'entry impact not propagated into later historical state',
          'inclusion/MEV/slippage tolerance/failed attempts and portfolio capital not modeled',
          'self-built RPC/server/deployment costs not amortized']})
    summary()

def summary(result_file='smartmoney_settlement_expanded_result.json'):
    result=read(result_file);rows=result['rows']
    full=[r for r in rows if r.get('modeled_net_eth_raw') is not None and r.get('intermediate_inventory_status')!='NOT_MEASURED']
    vals=[int(r['modeled_net_eth_raw']) for r in full]
    inventory_rows=[r for r in rows if r['status'] in ('PARTIAL_EXIT_INVENTORY_REMAINS','INTERMEDIATE_INVENTORY_REMAINS')]
    inventory_cash=[int(r['cash_recovered_after_modeled_chain_fees_less_entry_raw']) for r in inventory_rows
                    if r.get('cash_recovered_after_modeled_chain_fees_less_entry_raw') is not None]
    unresolved_exits=[r for r in rows if r['status']=='SELL_SIMULATION_UNRESOLVED'
                      and r.get('buy',{}).get('spent_raw') is not None
                      and r.get('buy_fees',{}).get('total_fee_model_raw') is not None]
    open_entry_debit=sum(int(r['buy']['spent_raw'])+int(r['buy_fees']['total_fee_model_raw']) for r in unresolved_exits)
    positives=sorted([x for x in vals if x>0],reverse=True)
    bytoken=defaultdict(list);bywallet=defaultdict(list)
    for r in full:bytoken[r['token']].append(r);bywallet[r['wallet']].append(r)
    grouped=[]
    for token,rr in bytoken.items():
        grouped.append({'token':token,'n_independent_simulations':len(rr),
            'sum_conditional_net_raw':str(sum(int(r['modeled_net_eth_raw']) for r in rr)),
            'positive_trades':sum(int(r['modeled_net_eth_raw'])>0 for r in rr),
            'not_independent_events':True})
    grouped.sort(key=lambda r:int(r['sum_conditional_net_raw']),reverse=True)
    wallet_rows=[]
    for wallet in sorted({r['wallet'] for r in rows}):
        rr=[r for r in rows if r['wallet']==wallet];ff=bywallet[wallet]
        pp=[r for r in rr if r['status'] in ('PARTIAL_EXIT_INVENTORY_REMAINS','INTERMEDIATE_INVENTORY_REMAINS')]
        partial_cash=sum(int(r['cash_recovered_after_modeled_chain_fees_less_entry_raw']) for r in pp
                         if r.get('cash_recovered_after_modeled_chain_fees_less_entry_raw') is not None)
        unliquidated=[r for r in rr if r['status'] in ('SELL_SIMULATION_UNRESOLVED','EXIT_HORIZON_NOT_YET_OBSERVED')
                      and r.get('buy',{}).get('spent_raw') is not None
                      and r.get('buy_fees',{}).get('total_fee_model_raw') is not None]
        debit=sum(int(r['buy']['spent_raw'])+int(r['buy_fees']['total_fee_model_raw']) for r in unliquidated)
        full_cash=sum(int(r['modeled_net_eth_raw']) for r in ff)
        wallet_rows.append({'wallet':wallet,'n_frozen':len(rr),'n_modeled':len(ff),
            'n_unresolved_or_inventory_remaining':len(rr)-len(ff),
            'statuses':dict(Counter(r['status'] for r in rr)),
            'sum_conditional_net_raw':str(full_cash),
            'partial_exit_rows':len(pp),'partial_exit_cash_delta_raw':str(partial_cash),
            'known_open_entry_debit_raw':str(debit),
            'known_cash_delta_at_cutoff_raw':str(full_cash+partial_cash-debit),
            'qualification':'descriptive only; open inventory unpriced and unknown entry costs omitted; not source-wallet profitability or new eligibility screen'})
    s={'generated_at':now(),'settlement_input_artifact':result_file,
       'alternative_funding_routes':result.get('alternative_funding_routes',0),
       'frozen_candidates':len(rows),'statuses':dict(Counter(r['status'] for r in rows)),
       'complete_conditionally_modeled_roundtrips':len(full),'tokens_in_complete_roundtrips':len({r['token'] for r in full}),
       'wallets_in_complete_roundtrips':len({r['wallet'] for r in full}),
       'unresolved_or_inventory_remaining':len(rows)-len(full),
       'sum_modeled_eth_raw':str(sum(vals)),'positive_roundtrips':len(positives),
       'full_roundtrip_actual_entry_spend_raw':str(sum(int(r['buy']['spent_raw']) for r in full)),
       'full_roundtrip_chain_fees_raw':str(sum(int(r[leg+'_fees']['total_fee_model_raw']) for r in full for leg in ('buy','sell'))),
       'inventory_remaining_rows':len(inventory_rows),'inventory_remaining_cash_modeled_rows':len(inventory_cash),
       'inventory_remaining_cash_delta_raw':str(sum(inventory_cash)),
       'cash_delta_closed_and_partial_raw':str(sum(vals)+sum(inventory_cash)),
       'mature_unresolved_exit_with_known_entry_debit_rows':len(unresolved_exits),
       'mature_unresolved_exit_entry_debit_raw':str(open_entry_debit),
       'mature_known_cash_delta_with_unresolved_exits_left_unliquidated_raw':str(sum(vals)+sum(inventory_cash)-open_entry_debit),
       'unliquidated_cash_note':'no liquidation credit assigned where exit has not been simulated; held tokens are not marked to zero; reverted-transaction gas and unknown entries remain unmodeled',
       'inventory_cash_note':'cash change only; residual inventories unpriced, neither zero-valued nor profit-certified',
       'modeled_win_rate':len(positives)/len(vals) if vals else None,
       'sum_positive_pnl_raw':str(sum(positives)),'sum_negative_pnl_raw':str(sum(x for x in vals if x<0)),
       'largest_winner_raw':str(max(positives)) if positives else None,
       'largest_winner_share_of_positive_pnl':max(positives)/sum(positives) if positives else None,
       'positive_token_clusters':sum(int(r['sum_conditional_net_raw'])>0 for r in grouped),
       'token_clusters':grouped,'wallet_breakdown':wallet_rows,
       'top_five_trades':[{'source_tx':r['source_tx'],'token':r['token'],'modeled_net_eth_raw':r['modeled_net_eth_raw']}
           for r in sorted(full,key=lambda r:int(r['modeled_net_eth_raw']),reverse=True)[:5]],
       'portfolio_pnl':None,'full_441_pnl':None,'profitability_certified':False,
       'limitations':'resolved subset only; repeated tokens/signals correlated; low win rate or concentrated gains not rejection criteria'}
    save('smartmoney_execution_summary.json',s)
    source={r['source_tx']:r for r in read('smartmoney_cost_ledger_extended.json')['rows']}
    route_file='smartmoney_follower_routes.json' if (ROOT/'smartmoney_follower_routes.json').exists() else 'smartmoney_routes_extended.json'
    route_rows={r['source_tx']:r for r in read(route_file)['rows']}
    diagnostics=({r['source_tx']:r for r in read('smartmoney_execution_diagnostics.json')['rows']}
                 if (ROOT/'smartmoney_execution_diagnostics.json').exists() else {})
    failure_probes=({r['source_tx']:r for r in read('smartmoney_route_failure_probe.json')['rows']}
                    if (ROOT/'smartmoney_route_failure_probe.json').exists() else {})
    def eth_text(value):return format(Decimal(value)/Decimal(10**18),'f') if value is not None else None
    labels={'CONDITIONAL_EXECUTION_COST_MODEL':'完整卖出；条件净额',
        'PARTIAL_EXIT_INVENTORY_REMAINS':'部分卖出；仍持目标币',
        'INTERMEDIATE_INVENTORY_REMAINS':'仍持中间币；未估值',
        'EXIT_HORIZON_NOT_YET_OBSERVED':'24小时退出尚未到期',
        'BUY_SIMULATION_UNRESOLVED_OR_ZERO':'买入未成功模拟；具体原因另列',
        'SELL_SIMULATION_UNRESOLVED':'卖出未成功模拟；不等于归零',
        'ROUTE_UNRESOLVED':'路径未恢复',
        'SETTLEMENT_SIMULATED_FEE_COMPONENT_UNRESOLVED':'已模拟结算；费用项缺失'}
    flat=[]
    for r in rows:
        row=source[r['source_tx']]|{k:v for k,v in r.items() if not isinstance(v,(dict,list))}
        row['follower_cost_status']=r['status']
        row['diagnostic_class']=diagnostics.get(r['source_tx'],{}).get('diagnostic_class')
        detail=diagnostics.get(r['source_tx'],{}).get('diagnostic_details',{})
        row['failure_leg']=detail.get('leg')
        row['failure_rpc_message']=detail.get('rpc_message')
        row['failure_solidity_reason']=detail.get('solidity_reason')
        row['associated_unresolved_factory_pools']=' | '.join(detail.get('associated_source_pools_with_unresolved_factory_interface',[]))
        row['diagnostic_preceding_prefix_returned_zero']=detail.get('preceding_prefix_returned_zero')
        row['diagnostic_first_failing_prefix']=failure_probes.get(r['source_tx'],{}).get('first_failing_prefix')
        row['diagnostic_first_failing_prefix_last_pool']=failure_probes.get(r['source_tx'],{}).get('first_failing_prefix_last_pool')
        for leg in ('buy','sell'):
            for field in ('spent_raw','received_raw','gas_model_units','base_fee_wei','block'):
                row[leg+'_'+field]=r.get(leg,{}).get(field)
            for field in ('l2_fee_model_raw','l1_fee_model_raw','operator_fee_model_raw','total_fee_model_raw'):
                row[leg+'_'+field]=r.get(leg+'_fees',{}).get(field)
        row.update(status_cn=labels.get(r['status'],r['status']),
            planned_entry_eth=eth_text(r.get('input_eth_raw')),
            actual_entry_eth=eth_text(r.get('buy',{}).get('spent_raw')),
            exit_cash_eth_equivalent=eth_text(r.get('sell',{}).get('received_raw')),
            entry_fee_eth=eth_text(r.get('buy_fees',{}).get('total_fee_model_raw')),
            exit_fee_eth=eth_text(r.get('sell_fees',{}).get('total_fee_model_raw')),
            conditional_net_eth=eth_text(r.get('modeled_net_eth_raw')),
            cash_change_eth=eth_text(r.get('cash_recovered_after_modeled_chain_fees_less_entry_raw')),
            source_tx_url='https://basescan.org/tx/'+r['source_tx'])
        path=route_rows[r['source_tx']]['route'] or []
        row['buy_pool_path']=' | '.join(e['kind']+':'+e['id'] for e in reversed(path))
        row['sell_pool_path']=' | '.join(e['kind']+':'+e['id'] for e in path)
        if r.get('target_inventory_remaining_raw') is not None and int(r.get('buy',{}).get('received_raw','0'))>0:
            row['target_remaining_fraction']=format(Decimal(r['target_inventory_remaining_raw'])/Decimal(r['buy']['received_raw']),'f')
        flat.append(row)
    fields=sorted({k for r in flat for k,v in r.items() if not isinstance(v,(dict,list))})
    with (ROOT/'smartmoney_execution_per_trade.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(flat)
    print(json.dumps(s,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['all','summary']);a=p.parse_args()
    {'all':run,'summary':summary}[a.command]()
