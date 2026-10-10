"""One simultaneous size check on the most overlapped token.

Uses the already recorded 0.01 ETH fill at the first signal that was open
when 19 independent positions overlapped. Asks the same registered route,
at that same historical block, what one 0.19 ETH buy would have received.
This is not 19 sequential trades and does not replace any exit.
"""
import json
from evidence import read, save, now
from smartmoney_settlement import simulate

TOKEN = '0xca7a1e31b36779cf32acb18714ab26982cf36b05'
PEAK_TS = 1790188077
ONE = 10 ** 16
MULTIPLE = 19


def main():
    routes = {r['source_tx']: r for r in read('smartmoney_follower_routes.json')['rows']}
    keys = read('smartmoney_v4_keys_calldata.json')['keys']
    rows = read('smartmoney_follower_execution_result.json')['rows']
    open_rows = []
    for row in rows:
        route = routes[row['source_tx']]
        if (route.get('token') or '').lower() != TOKEN:
            continue
        buy = row.get('buy') or {}
        if not buy.get('timestamp') or not buy.get('received_raw'):
            continue
        if buy['timestamp'] <= PEAK_TS < buy['timestamp'] + 86400:
            open_rows.append(row)
    open_rows.sort(key=lambda r: r['buy']['timestamp'])
    if len(open_rows) != MULTIPLE:
        raise RuntimeError(f'expected {MULTIPLE} open buys, found {len(open_rows)}')
    pools = []
    for row in open_rows:
        pools.append(tuple(step['id'].lower() for step in routes[row['source_tx']]['route']))
    first = open_rows[0]
    route = routes[first['source_tx']]
    block = route['source_block'] + 5
    if first['buy']['block'] != block:
        raise RuntimeError('recorded entry block is not source_block+5')
    recorded = int(first['buy']['received_raw'])
    large = simulate(route['route'], keys, MULTIPLE * ONE, block, True, mixed=True)
    out = {
        'generated_at': now(),
        'token': TOKEN,
        'peak_timestamp': PEAK_TS,
        'open_independent_positions': len(open_rows),
        'distinct_registered_pool_paths': len(set(pools)),
        'same_path_as_the_probed_signal': sum(p == pools[0] for p in pools),
        'probed_source_tx': first['source_tx'],
        'entry_block': block,
        'recorded_0_01_eth_tokens_raw': str(recorded),
        'simultaneous_0_19_eth': large,
        'meaning': 'one buy of 0.19 ETH at the first overlapping signal, same registered path and block; not 19 sequential fills and not a new exit',
        'profitability_certified': False,
    }
    if large.get('received_raw'):
        got = int(large['received_raw'])
        linear = recorded * MULTIPLE
        out['tokens_if_nineteen_independent_fills_raw'] = str(linear)
        out['simultaneous_tokens_raw'] = str(got)
        out['tokens_shortfall_versus_linear_raw'] = str(linear - got)
        out['received_per_eth_versus_0_01'] = (got / (MULTIPLE * ONE)) / (recorded / ONE)
    slot = (first.get('slot_evidence') or {}).get('slot')
    sell = first.get('sell') or {}
    if slot and sell.get('received_raw') and sell.get('spent_raw') and sell.get('block'):
        qty = int(sell['spent_raw']) * MULTIPLE
        dumped = simulate(route['route'], keys, qty, sell['block'], False, slot, mixed=True)
        out['simultaneous_exit'] = {
            'block': sell['block'],
            'token_qty_raw': str(qty),
            'status': dumped.get('status'),
            'eth_received_raw': dumped.get('received_raw'),
            'nineteen_times_recorded_0_01_exit_raw': str(int(sell['received_raw']) * MULTIPLE),
            'meaning': 'one sell of 19 times the recorded token amount into the historical pool at the first signal exit block; the pool state does not contain the earlier buys',
        }
        if dumped.get('received_raw'):
            linear_eth = int(sell['received_raw']) * MULTIPLE
            got_eth = int(dumped['received_raw'])
            out['simultaneous_exit']['eth_shortfall_versus_linear_raw'] = str(linear_eth - got_eth)
            out['simultaneous_exit']['eth_per_token_versus_0_01'] = (got_eth / qty) / (int(sell['received_raw']) / int(sell['spent_raw']))
    save('smartmoney_size_bound.json', out)
    print(json.dumps({k: out[k] for k in out if k != 'simultaneous_0_19_eth'}, indent=2))
    print(json.dumps({'large_status': large.get('status'), 'received': large.get('received_raw')}))


if __name__ == '__main__':
    main()
