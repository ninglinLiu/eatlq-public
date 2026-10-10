"""Read-only checks that the published 441-row model does not already contain.

Uses the frozen execution rows. Does not change routes, sizes, or eligibility.
Finite-capital levels are the published per-trade size and the published peak
cash gap, not a bankroll chosen after seeing which level wins.
"""
import json
from collections import defaultdict
from evidence import read, save, now

DAY = 86400


def events_from(rows):
    events = []
    for r in rows:
        buy = r.get('buy') or {}
        fee = (r.get('buy_fees') or {}).get('total_fee_model_raw')
        if not buy.get('received_raw') or int(buy['received_raw']) <= 0 or fee is None:
            continue
        events.append({'timestamp': buy['timestamp'], 'source_tx': r['source_tx'], 'token': r['token'],
                       'leg': 'buy', 'cash_delta_raw': -int(buy['spent_raw']) - int(fee)})
        sell = r.get('sell') or {}
        sfee = (r.get('sell_fees') or {}).get('total_fee_model_raw')
        if sell.get('received_raw') is not None and sfee is not None:
            events.append({'timestamp': sell['timestamp'], 'source_tx': r['source_tx'], 'token': r['token'],
                           'leg': 'sell', 'cash_delta_raw': int(sell['received_raw']) - int(sfee)})
    events.sort(key=lambda e: (e['timestamp'], 0 if e['leg'] == 'buy' else 1, e['source_tx']))
    return events


def constrained(events, capital_raw):
    cash = capital_raw
    skipped = set()
    done_buy = done_sell = 0
    minimum = cash
    for e in events:
        if e['leg'] == 'buy':
            need = -e['cash_delta_raw']
            if cash < need:
                skipped.add(e['source_tx'])
                continue
            cash -= need
            done_buy += 1
        elif e['source_tx'] not in skipped:
            cash += e['cash_delta_raw']
            done_sell += 1
        if cash < minimum:
            minimum = cash
    return {'capital_raw': str(capital_raw),
            'buys_executed': done_buy,
            'buys_not_done_insufficient_cash': len(skipped),
            'sells_executed': done_sell,
            'ending_cash_raw': str(cash),
            'minimum_cash_raw': str(minimum),
            'not_done_source_txs': sorted(skipped)}


def delay_audit(rows, routes):
    delays = []
    block_gaps = []
    for r in rows:
        buy = r.get('buy') or {}
        route = routes.get(r['source_tx'])
        if not buy.get('timestamp') or not route:
            continue
        delays.append(buy['timestamp'] - route['source_ts'])
        block_gaps.append(buy['block'] - route['source_block'])
    return {'rows_with_buy_timestamp': len(delays),
            'entry_delay_seconds_min': min(delays) if delays else None,
            'entry_delay_seconds_max': max(delays) if delays else None,
            'entry_block_gap_min': min(block_gaps) if block_gaps else None,
            'entry_block_gap_max': max(block_gaps) if block_gaps else None,
            'meaning': 'The simulator enters at source_block+5. On these rows that gap is exactly 5 blocks and the recorded block timestamps differ by exactly 10 seconds. That is Base block time, not a measured delay from seeing the signal to our transaction being included.'}


def overlap(rows):
    holds = []
    for r in rows:
        buy = r.get('buy') or {}
        if not buy.get('timestamp'):
            continue
        holds.append((buy['timestamp'], buy['timestamp'] + DAY, r['token'], r['source_tx']))
    by_token = defaultdict(list)
    for start, end, token, tx in holds:
        by_token[token].append((start, end, tx))
    worst = {'token': None, 'max_concurrent': 0, 'at_timestamp': None}
    stacked = []
    for token, spans in by_token.items():
        points = []
        for start, end, tx in spans:
            points.append((start, 1))
            points.append((end, -1))
        points.sort()
        cur = 0
        peak = 0
        when = None
        for ts, delta in points:
            cur += delta
            if cur > peak:
                peak = cur
                when = ts
        stacked.append({'token': token, 'signals_with_a_buy': len(spans), 'max_concurrent_independent_positions': peak})
        if peak > worst['max_concurrent']:
            worst = {'token': token, 'max_concurrent': peak, 'at_timestamp': when}
    stacked.sort(key=lambda r: (-r['max_concurrent_independent_positions'], r['token']))
    return {'tokens_with_a_modeled_buy': len(stacked),
            'tokens_with_overlapping_holds': sum(r['max_concurrent_independent_positions'] > 1 for r in stacked),
            'worst': worst,
            'top': stacked[:8],
            'meaning': 'Each position is assumed to occupy 0.01 ETH for 24h on the historical pool, ignoring our own earlier trade. Concurrent count is the number of those independent assumptions alive at once. It is not a sequential fill.'}


def run():
    rows = read('smartmoney_follower_execution_result.json')['rows']
    routes = {r['source_tx']: r for r in read('smartmoney_follower_routes.json')['rows']}
    ev = events_from(rows)
    peak = int(read('smartmoney_cash_schedule.json')['buy_first_same_timestamp']['minimum_initial_quote_raw'])
    levels = [('one_trade_0_01_eth', 10**16), ('published_peak_cash_gap', peak)]
    capital = {name: constrained(ev, raw) for name, raw in levels}
    for row in capital.values():
        row.pop('not_done_source_txs')
    state = {
        'sell_state_override': 'sell simulation writes only the target-token balance slot and the hypothetical executor code. It does not carry pool reserves, spend authorization, or a cooldown created by our own buy.',
        'witnessed_buy_restriction': '3 buys reverted because the historical spend gate did not authorize this executor. Those 3 stay unresolved. The signature in the source transaction was not reused.',
        'partial_exit_second_sell': '77 partial exits still hold inventory. A second independent sell of the remainder on the untouched historical pool would reuse the same liquidity the first sell already consumed in a separate call. That second call is not a legal continuation and was not run.',
    }
    out = {'generated_at': now(),
           'input': 'smartmoney_follower_execution_result.json',
           'delay': delay_audit(rows, routes),
           'same_token_overlap': overlap(rows),
           'finite_capital_on_independent_fills': capital,
           'state_limits': state,
           'profitability_certified': False,
           'limits': ['no mempool or inclusion trace',
                      'capital test skips a buy when cash on hand is short; it still uses each fill as if the pool never saw our other trades',
                      'unsold inventory stays unpriced']}
    save('smartmoney_constraint_audit.json', out)
    print(json.dumps({k: out[k] for k in ('delay', 'same_token_overlap', 'finite_capital_on_independent_fills')}, indent=2))


if __name__ == '__main__':
    run()
