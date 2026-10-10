"""Freeze the next Base selection list before reading any later trade.

Rule, copied from the 2026-09-17 freeze and not refit on that window's winners:
among saved GMGN 7d spot rows, keep the earliest capture of each address, then
take the 12 highest 7-day realized USD profits, breaking ties by address.
The October 1 files are the new snapshot. September winners are not added.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from evidence import ROOT, read, save, now, digest

SNAPSHOTS = [
    Path('/root/gmgn_base_7d_2026-10-01T11-15-45-722Z.json'),
    Path('/root/gmgn_base_7d_2026-10-01T11-19-21-108Z.json'),
    Path('/root/gmgn_base_7d_2026-10-01T11-19-33-510Z.json'),
    Path('/root/gmgn_base_7d_2026-10-01T11-19-45-600Z.json'),
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Origin': 'https://gmgn.ai',
}


def stamp_from_name(path):
    raw = path.name.split('gmgn_base_7d_')[1].removesuffix('.json').removesuffix('Z')
    date, clock = raw.split('T')
    hour, minute, second, millis = clock.split('-')
    return datetime.fromisoformat(f'{date}T{hour}:{minute}:{second}.{millis}+00:00')


def freeze_rows():
    chosen = {}
    for path in sorted(SNAPSHOTS, key=lambda p: stamp_from_name(p)):
        payload = json.loads(path.read_text())
        captured = stamp_from_name(path)
        for row in payload['data']['rank']:
            address = row['address'].lower()
            if address in chosen:
                continue
            chosen[address] = {
                'address': address,
                'captured_at': captured.isoformat(),
                'source_file': path.name,
                'source_sha256': digest(path.read_bytes()),
                'vendor_realized_usd': float(row.get('realized_profit_7d') or 0),
                'tags': row.get('tags') or [],
                'chain': 'base',
                'window': '7d',
            }
    ranked = sorted(chosen.values(), key=lambda r: (-r['vendor_realized_usd'], r['address']))
    top = []
    for i, row in enumerate(ranked[:12], 1):
        top.append(dict(row, frozen_rank=i))
    september = {r['address'].lower() for r in read('frozen_wallets.json')['rows'] if r['chain'] == 'base'}
    return {'frozen_at': now(),
            'chain': 'base',
            'window': '7d',
            'rule': 'earliest October 1 capture per address, then realized_profit_7d descending, address ascending, first 12',
            'september_winners_not_inserted': True,
            'overlap_with_september_frozen_12': sorted(september & {r['address'] for r in top}),
            'n_unique_in_four_files': len(chosen),
            'rows': top}


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.loads(response.read().decode())


def activity_after(address, cutoff_ts):
    rows = []
    cursor = None
    pages = 0
    while pages < 8:
        url = f'https://gmgn.ai/vas/api/v1/wallet_activity/base?wallet={address}&limit=50&type=buy,sell'
        if cursor:
            url += '&cursor=' + urllib.parse.quote(cursor)
        time.sleep(0.9)
        payload = get_json(url)
        pages += 1
        acts = (payload.get('data') or {}).get('activities') or []
        if not acts:
            break
        oldest = min(a['timestamp'] for a in acts)
        for a in acts:
            if a['timestamp'] >= cutoff_ts:
                token = a.get('token') or {}
                rows.append({'tx_hash': a.get('tx_hash'), 'timestamp': a['timestamp'],
                             'event_type': a.get('event_type'), 'token': token.get('address'),
                             'symbol': token.get('symbol')})
        cursor = (payload.get('data') or {}).get('next')
        if oldest < cutoff_ts or not cursor:
            break
    return {'pages': pages, 'truncated': pages >= 8 and cursor is not None, 'rows': rows}


def run():
    frozen_path = ROOT / 'smartmoney_new_window_20261001.json'
    if frozen_path.exists():
        frozen = read('smartmoney_new_window_20261001.json')
    else:
        frozen = freeze_rows()
        save('smartmoney_new_window_20261001.json', frozen)
    signals = []
    for row in frozen['rows']:
        cutoff = datetime.fromisoformat(row['captured_at']).timestamp()
        try:
            book = activity_after(row['address'], cutoff)
            error = None
        except Exception as exc:
            book = {'pages': 0, 'truncated': False, 'rows': []}
            error = str(exc)[:300]
        buys = [r for r in book['rows'] if r['event_type'] == 'buy']
        signals.append({'address': row['address'], 'frozen_rank': row['frozen_rank'],
                        'captured_at': row['captured_at'], 'error': error,
                        'pages': book['pages'], 'truncated': book['truncated'],
                        'post_snapshot_buys': len(buys), 'post_snapshot_sells': sum(r['event_type'] == 'sell' for r in book['rows']),
                        'first_post_buy': min((r['timestamp'] for r in buys), default=None)})
        print(json.dumps(signals[-1]), flush=True)
    # 24h exits cannot exist yet for a snapshot at 11:15 UTC on Oct 1 if read before 11:15 UTC Oct 2.
    out = {'generated_at': now(), 'frozen_file': 'smartmoney_new_window_20261001.json',
           'n_selected': len(frozen['rows']),
           'overlap_with_september_frozen_12': frozen['overlap_with_september_frozen_12'],
           'exit_24h': 'NOT_OBSERVED_HORIZON_HAS_NOT_ELAPSED_FOR_A_FULL_DAY_AFTER_THE_SNAPSHOT',
           'profitability_certified': False,
           'rows': signals}
    save('smartmoney_new_window_20261001_signals.json', out)
    print(json.dumps({k: out[k] for k in out if k != 'rows'}, indent=2))


if __name__ == '__main__':
    run()
