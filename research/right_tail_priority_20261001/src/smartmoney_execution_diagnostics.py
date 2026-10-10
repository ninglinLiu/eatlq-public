"""Offline reasons, denominator and cash/inventory diagnostics after settlement."""
import json
from collections import Counter
from decimal import Decimal
from eth_abi import decode
from evidence import ROOT,read,save,now

def failure_class(row,gates):
    status=row['status']
    if status=='EXIT_HORIZON_NOT_YET_OBSERVED':return 'EXIT_TIME_NOT_YET_OBSERVED',{}
    if status in ('PARTIAL_EXIT_INVENTORY_REMAINS','INTERMEDIATE_INVENTORY_REMAINS'):
        return 'MEASURED_CASH_AND_UNPRICED_REMAINING_INVENTORY',{}
    if status=='CONDITIONAL_EXECUTION_COST_MODEL':return 'COMPLETE_CONDITIONAL_ROUNDTRIP',{}
    if gates.get(row['source_tx'],{}).get('gate_active_at_modeled_entry') is True:
        return 'HISTORICAL_PURCHASE_AUTHORIZATION_NOT_AVAILABLE',{'gate_audit':'smartmoney_gate_audit.json'}
    sim=row.get('buy',{}) if status.startswith('BUY') else row.get('sell',{})
    ev=sim.get('evidence',{});err=ev.get('rpc_error') or {};data=err.get('data','')
    details={'leg':'buy' if status.startswith('BUY') else 'sell','evidence':ev,
             'rpc_message':err.get('message'),'selector':data[:10] if isinstance(data,str) else None}
    if ev.get('http_status')==429:return 'PROVIDER_THROTTLE_NOT_TRADE_FAILURE',details
    if isinstance(data,str) and data.startswith('0x08c379a0'):
        try:details['solidity_reason']=decode(['string'],bytes.fromhex(data[10:]))[0]
        except Exception:pass
    reason=details.get('solidity_reason','')
    if 'unsupported' in reason or 'unregistered' in reason:return 'FACTORY_OR_REGISTRY_ADAPTER_GAP',details
    if reason=='quantity':return 'INTERMEDIATE_QUANTITY_GUARD_REVERT',details
    if isinstance(data,str) and data.startswith('0x7c9c6e8f'):
        values=decode(['uint160','uint160'],bytes.fromhex(data[10:]))
        details['sqrt_price_error_args']=list(map(str,values))
        return 'FIXED_ROUTE_PRICE_LIMIT_ALREADY_REACHED',details
    if isinstance(data,str) and data.startswith('0x90bfb865'):
        try:
            target,selector,nested,context=decode(['address','bytes4','bytes','bytes'],bytes.fromhex(data[10:]))
            details.update(wrapped_target=target,wrapped_call_selector='0x'+selector.hex(),
                           nested_reason='0x'+nested.hex(),wrapped_context='0x'+context.hex())
        except Exception:pass
        return 'HOOK_OR_EXTERNAL_CALL_CONTEXT_UNRESOLVED',details
    if err.get('code')==3:return 'EVM_REVERT_CAUSE_NOT_YET_ISOLATED',details
    if sim.get('status')=='TOKEN_BALANCE_SLOT_UNRESOLVED':return 'BALANCE_OVERRIDE_INSTRUMENT_UNRESOLVED',details
    return 'UNRESOLVED_REQUIRES_DIAGNOSIS',details

def run():
    result=read('smartmoney_follower_execution_result.json');rows=result['rows']
    gates={r['source_tx']:r for r in read('smartmoney_gate_audit.json')['rows']}
    routes={r['source_tx']:r for r in read('smartmoney_follower_routes.json')['rows']}
    unknown_factories={r['pool'] for r in read('smartmoney_pancake_repair_manifest.json')['pool_metadata']
                       if r['factory'] is None}
    probes=({r['source_tx']:r for r in read('smartmoney_route_failure_probe.json')['rows']}
            if (ROOT/'smartmoney_route_failure_probe.json').exists() else {})
    out=[]
    for r in rows:
        kind,details=failure_class(r,gates)
        associated=[e['id'] for e in routes[r['source_tx']]['route'] if e['id'] in unknown_factories]
        if associated:
            details.update(associated_source_pools_with_unresolved_factory_interface=associated,
                           association_is_not_a_causal_trace=True)
        if r['source_tx'] in probes:
            p=probes[r['source_tx']]
            details.update(first_failing_prefix=p['first_failing_prefix'],
                first_failing_prefix_last_pool=p['first_failing_prefix_last_pool'],
                preceding_prefix_returned_zero=(len(p['steps'])>1 and p['steps'][-2]['simulation'].get('received_raw')=='0'),
                prefix_probe='smartmoney_route_failure_probe.json')
        entry=r.get('buy',{});exit=r.get('sell',{})
        item={k:r[k] for k in ('source_tx','wallet','token','status')}
        item.update(diagnostic_class=kind,diagnostic_details=details,
            entry_simulated=entry.get('status')=='HISTORICAL_TRANSFER_SETTLED_SIMULATION' and int(entry.get('received_raw','0'))>0,
            source_route_origin=routes[r['source_tx']]['route_origin'],
            own_buy_chain_fee_known=r.get('buy_fees',{}).get('total_fee_model_raw') is not None,
            own_sell_chain_fee_known=r.get('sell_fees',{}).get('total_fee_model_raw') is not None,
            route_hops=len(routes[r['source_tx']]['route']))
        if entry.get('received_raw') and exit.get('spent_raw'):
            qty=int(entry['received_raw']);residual=qty-int(exit['spent_raw'])
            item['residual_fraction']=str(Decimal(residual)/Decimal(qty))
        out.append(item)
    partial=[r for r in out if r['status']=='PARTIAL_EXIT_INVENTORY_REMAINS' and r.get('residual_fraction') is not None]
    fractions=sorted(Decimal(r['residual_fraction']) for r in partial)
    diagnostics={'generated_at':now(),'n_frozen':len(rows),'rows':out,
        'class_counts':dict(Counter(r['diagnostic_class'] for r in out)),
        'entry_simulated':sum(r['entry_simulated'] for r in out),
        'own_buy_chain_fee_known':sum(r['own_buy_chain_fee_known'] for r in out),
        'own_sell_chain_fee_known':sum(r['own_sell_chain_fee_known'] for r in out),
        'partial_exit_target_residual_fraction':{
            'n':len(fractions),'min':str(fractions[0]) if fractions else None,
            'median':str(fractions[len(fractions)//2]) if fractions else None,
            'max':str(fractions[-1]) if fractions else None,
            'above_99_percent':sum(x>=Decimal('.99') for x in fractions)},
        'not_a_profitability_gate':True,
        'limits':'failures scoped to this historical caller, fixed path and quantity; not proof no other legal route exists'}
    save('smartmoney_execution_diagnostics.json',diagnostics)
    print(json.dumps({k:v for k,v in diagnostics.items() if k!='rows'},indent=2))

if __name__=='__main__':run()
