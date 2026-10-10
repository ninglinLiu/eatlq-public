"""Sell the 26 signals whose 24h horizon is now in the past.

Writes a separate version. Does not replace smartmoney_observation_cutoff.json,
the 441-row execution result, or any route. Same registered path, same 0.01 ETH,
same source_block+5 entry and +43200-block exit. No new pool is chosen.
"""
import json
from collections import Counter
from evidence import ROOT, fetch, read, save, now
from pilot import URL
from smartmoney_settlement import detect_balance_slot, has_throttle, own_fees, simulate

CUTOFF_NAME = 'smartmoney_observation_cutoff_mature.json'
OUT_DIR = 'smartmoney_settlement_mature'


def fresh_rpc(method, params, label):
    # Block number and the latest header must not reuse an earlier cached head.
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    meta, data = fetch(URL, payload, label, cache=False)
    if not isinstance(data, dict) or data.get('result') is None:
        raise RuntimeError(label + ' unavailable')
    return data['result'], {'key': meta['key'], 'http_status': meta.get('http_status'),
                            'rpc_error': data.get('error')}


def chain_head():
    block_hex, ev = fresh_rpc('eth_blockNumber', [], 'smartmoney_mature_head_number')
    number = int(block_hex, 16)
    header, hev = fresh_rpc('eth_getBlockByNumber', [hex(number), False], 'smartmoney_mature_head_header')
    if not header.get('timestamp'):
        raise RuntimeError('chain head header unavailable')
    return {'chain_block': number, 'chain_timestamp': int(header['timestamp'], 16),
            'number_evidence': ev, 'header_evidence': hev, 'generated_at': now(),
            'replaces_prior_cutoff': False,
            'prior_cutoff': 'smartmoney_observation_cutoff.json'}


def mature_one(execution, route, keys, cutoff_ts):
    item = {k: execution[k] for k in execution if k not in ('status', 'observation_cutoff', 'modeled_net_eth_raw')}
    item.update(status='MATURE_WINDOW_PENDING', modeled_net_eth_raw=None, net_profit_certified=False,
                mature_window_cutoff=cutoff_ts, prior_status=execution['status'],
                route_kept=route['route_origin'], exit_policy='24h', delay_seconds=10)
    scheduled = execution.get('scheduled_exit_timestamp') or (route['source_ts'] + 86410)
    if scheduled > cutoff_ts:
        item.update(status='EXIT_HORIZON_NOT_YET_OBSERVED', scheduled_exit_timestamp=scheduled)
        return item
    buy = execution.get('buy') or {}
    if not buy.get('received_raw') or int(buy['received_raw']) <= 0:
        item.update(status='BUY_SIMULATION_UNRESOLVED_OR_ZERO')
        return item
    exit_block = route['source_block'] + 5 + 43200
    slot = detect_balance_slot(route['token'], exit_block)
    item['slot_evidence'] = slot
    if not slot.get('slot'):
        item.update(status='SELL_SIMULATION_UNRESOLVED', sell={'block': exit_block, 'status': 'TOKEN_BALANCE_SLOT_UNRESOLVED'})
        return item
    sell = simulate(route['route'], keys, int(buy['received_raw']), exit_block, False, slot['slot'], mixed=True)
    item['sell'] = sell
    if has_throttle(sell):
        item.update(status='SELL_SIMULATION_THROTTLED')
        return item
    if not sell.get('received_raw'):
        item.update(status='SELL_SIMULATION_UNRESOLVED')
        return item
    item['sell_fees'] = own_fees(route['route'], keys, int(buy['received_raw']), exit_block, False, sell, mixed=True)
    # cash_account still consults the frozen 12:47 cutoff. These 26 are past that
    # cutoff's horizon only if we account them here; call it only after the sell exists
    # and the new head is already past scheduled exit. Patch by temporarily satisfying
    # the old check: scheduled exit is before the NEW head, but the old function uses
    # the old timestamp. So do not use cash_account. Account inline with the same fields.
    qty = int(buy['received_raw'])
    spent = int(sell['spent_raw'])
    if spent > qty:
        item.update(status='SELL_SIMULATION_UNRESOLVED', sell_spent_exceeds_bought=True)
        return item
    residual = qty - spent
    gas = [execution.get('buy_fees', {}).get('total_fee_model_raw'), item['sell_fees'].get('total_fee_model_raw')]
    cash = int(sell['received_raw']) - int(buy['spent_raw'])
    item.update(target_inventory_remaining_raw=str(residual),
                settled_before_gas_pnl_raw=str(cash),
                residual_inventory_value_raw=None if residual else '0',
                residual_note='cash deficit is not total loss: unsold inventory is not valued at zero',
                horizon_seconds_observed=sell['timestamp'] - buy['timestamp'],
                entry_delay_seconds_observed=buy['timestamp'] - route['source_ts'])
    item['cash_recovered_after_modeled_chain_fees_less_entry_raw'] = (
        str(cash - sum(map(int, gas))) if all(x is not None for x in gas) else None)
    if residual:
        item['status'] = 'PARTIAL_EXIT_INVENTORY_REMAINS'
        item['modeled_net_eth_raw'] = None
    elif all(x is not None for x in gas):
        item['status'] = 'CONDITIONAL_EXECUTION_COST_MODEL'
        item['modeled_net_eth_raw'] = item['cash_recovered_after_modeled_chain_fees_less_entry_raw']
    else:
        item['status'] = 'SETTLEMENT_SIMULATED_FEE_COMPONENT_UNRESOLVED'
    return item


def run():
    prior = read('smartmoney_observation_cutoff.json')
    head = chain_head()
    latest = max(r['scheduled_exit_timestamp'] for r in read('smartmoney_follower_execution_result.json')['rows']
                 if r['status'] == 'EXIT_HORIZON_NOT_YET_OBSERVED')
    head['latest_scheduled_exit_timestamp'] = latest
    head['all_26_horizons_elapsed'] = head['chain_timestamp'] > latest
    save(CUTOFF_NAME, head)
    if not head['all_26_horizons_elapsed']:
        raise RuntimeError('mature window is not open')
    keys = read('smartmoney_v4_keys_calldata.json')['keys']
    routes = {r['source_tx']: r for r in read('smartmoney_follower_routes.json')['rows']}
    pending = [r for r in read('smartmoney_follower_execution_result.json')['rows']
               if r['status'] == 'EXIT_HORIZON_NOT_YET_OBSERVED']
    out = []
    for i, execution in enumerate(pending, 1):
        name = OUT_DIR + '/' + execution['source_tx'] + '.json'
        if (ROOT / name).exists() and not has_throttle(read(name)):
            out.append(read(name))
            continue
        item = mature_one(execution, routes[execution['source_tx']], keys, head['chain_timestamp'])
        save(name, item)
        out.append(item)
        print(json.dumps({'done': i, 'total': len(pending), 'status': item['status'],
                          'tx': execution['source_tx']}), flush=True)
    full = [r for r in out if r.get('modeled_net_eth_raw') is not None]
    partial = [r for r in out if r['status'] == 'PARTIAL_EXIT_INVENTORY_REMAINS']
    result = {
        'generated_at': now(),
        'prior_cutoff_timestamp': prior['chain_timestamp'],
        'mature_cutoff': CUTOFF_NAME,
        'n_prior_unmatured': len(pending),
        'statuses': dict(Counter(r['status'] for r in out)),
        'complete_roundtrips': len(full),
        'sum_conditional_net_raw': str(sum(int(r['modeled_net_eth_raw']) for r in full)),
        'partial_rows': len(partial),
        'partial_cash_delta_raw': str(sum(int(r['cash_recovered_after_modeled_chain_fees_less_entry_raw'])
                                          for r in partial if r.get('cash_recovered_after_modeled_chain_fees_less_entry_raw') is not None)),
        'qualification': 'MATURE_WINDOW_OF_THE_SAME_26_SIGNALS_NOT_A_NEW_SAMPLE',
        'unchanged': ['smartmoney_observation_cutoff.json', 'smartmoney_follower_execution_result.json',
                      'smartmoney_execution_per_trade.csv', 'registered routes'],
        'rows': out,
    }
    save('smartmoney_mature_window_result.json', result)
    print(json.dumps({k: result[k] for k in result if k != 'rows'}, indent=2))


if __name__ == '__main__':
    run()
