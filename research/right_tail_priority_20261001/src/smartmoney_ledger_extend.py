"""Extend the September frozen ledgers by a bounded post-window read.

Base: the original 12 addresses, GMGN buy/sell rows after the last candidate
timestamp already in the 441-row batch. BSC: one activity request for the
frozen rank-1 address. A 403 stays a gap. No October winner is inserted.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from evidence import read, save, now

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Origin': 'https://gmgn.ai',
    'Referer': 'https://gmgn.ai/',
}
AFTER = datetime(2026, 9, 30, 22, 2, 47, tzinfo=timezone.utc)


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as response:
        return response.status, json.loads(response.read().decode())


def activity(chain, address, cutoff_ts, pages_cap=4):
    rows = []
    cursor = None
    pages = 0
    status = None
    while pages < pages_cap:
        url = f'https://gmgn.ai/vas/api/v1/wallet_activity/{chain}?wallet={address}&limit=50&type=buy,sell'
        if cursor:
            url += '&cursor=' + urllib.parse.quote(cursor)
        time.sleep(1.0)
        status, payload = get_json(url)
        pages += 1
        acts = (payload.get('data') or {}).get('activities') or []
        if not acts:
            break
        oldest = min(a['timestamp'] for a in acts)
        for a in acts:
            if a['timestamp'] > cutoff_ts:
                token = a.get('token') or {}
                rows.append({'tx_hash': a.get('tx_hash'), 'timestamp': a['timestamp'],
                             'event_type': a.get('event_type'), 'token': token.get('address'),
                             'symbol': token.get('symbol')})
        cursor = (payload.get('data') or {}).get('next')
        if oldest <= cutoff_ts or not cursor:
            break
    return {'http_status': status, 'pages': pages, 'truncated': pages >= pages_cap and cursor is not None,
            'rows': rows, 'error': None}


def one(chain, address, cutoff_ts):
    try:
        return activity(chain, address, cutoff_ts)
    except urllib.error.HTTPError as exc:
        return {'http_status': exc.code, 'pages': 0, 'truncated': False, 'rows': [], 'error': f'HTTP {exc.code}'}
    except Exception as exc:
        return {'http_status': None, 'pages': 0, 'truncated': False, 'rows': [], 'error': type(exc).__name__ + ': ' + str(exc)[:200]}


def run():
    frozen = read('frozen_wallets.json')['rows']
    base = [r for r in frozen if r['chain'] == 'base']
    bsc = [r for r in frozen if r['chain'] == 'bsc']
    cutoff = int(AFTER.timestamp())
    base_rows = []
    for row in base:
        book = one('base', row['address'], cutoff)
        buys = [r for r in book['rows'] if r['event_type'] == 'buy']
        item = {'address': row['address'], 'frozen_rank': row['frozen_rank'],
                'http_status': book['http_status'], 'error': book['error'],
                'pages': book['pages'], 'truncated': book['truncated'],
                'post_window_buys': len(buys),
                'post_window_sells': sum(r['event_type'] == 'sell' for r in book['rows']),
                'first_post_buy': min((r['timestamp'] for r in buys), default=None)}
        base_rows.append(item)
        print(json.dumps(item), flush=True)
    bsc_rows = []
    for row in bsc:
        book = one('bsc', row['address'], cutoff)
        item = {'address': row['address'], 'frozen_rank': row['frozen_rank'],
                'http_status': book['http_status'], 'error': book['error'],
                'pages': book['pages'], 'truncated': book['truncated'],
                'post_window_buys': sum(r['event_type'] == 'buy' for r in book['rows']),
                'post_window_sells': sum(r['event_type'] == 'sell' for r in book['rows'])}
        bsc_rows.append(item)
        print(json.dumps({'bsc': item}), flush=True)
    out = {
        'generated_at': now(),
        'cutoff_utc': AFTER.isoformat(),
        'cutoff_timestamp': cutoff,
        'meaning': 'GMGN buy/sell rows after the September candidate window for the original frozen 12 per chain. Vendor activity, not our copy PnL. The other saved addresses, 366 Base and 520 BSC, were not paged.',
        'base_original_12': base_rows,
        'base_buys': sum(r['post_window_buys'] for r in base_rows),
        'base_sells': sum(r['post_window_sells'] for r in base_rows),
        'bsc_original_12': bsc_rows,
        'bsc_buys': sum(r['post_window_buys'] for r in bsc_rows),
        'bsc_sells': sum(r['post_window_sells'] for r in bsc_rows),
        'bsc_http_errors': sum(r['error'] is not None for r in bsc_rows),
        'october_winners_not_inserted': True,
        'copy_pnl': 'NOT_OBSERVED',
        'profitability_certified': False,
    }
    save('smartmoney_ledger_extension_20261002.json', out)


if __name__ == '__main__':
    run()
