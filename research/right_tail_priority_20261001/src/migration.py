"""RT09 fixed-window Four.meme event census, public read-only RPC."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from eth_abi import decode
from eth_utils import keccak
from evidence import ROOT, read, save, fetch, rpc, now

URL = 'https://bsc-rpc.publicnode.com'
MANAGER = '0x5c952063c7fc8610ffdb798152d69f0b9550762b'
SIG = 'LiquidityAdded(address,uint256,address,uint256)'
TOPIC = '0x' + keccak(text=SIG).hex()

def result(method, params, label='migration'):
    m, d = rpc(URL, method, params, label)
    if not isinstance(d, dict) or 'result' not in d:
        raise RuntimeError(f'{method}: {d or m.get("error") or m.get("http_status")}')
    return d['result']

def block(n):
    b = result('eth_getBlockByNumber', [hex(n), False], 'migration_block')
    return {'number': int(b['number'], 16), 'timestamp': int(b['timestamp'], 16), 'hash': b['hash']}

def first_block_at(ts, head):
    low, high = max(1, head - 2000000), head
    assert block(low)['timestamp'] < ts <= block(high)['timestamp']
    while low + 1 < high:
        mid = (low + high) // 2
        if block(mid)['timestamp'] >= ts: high = mid
        else: low = mid
    return high

def logs(lo, hi):
    params = [{'address': MANAGER, 'topics': [TOPIC], 'fromBlock': hex(lo), 'toBlock': hex(hi)}]
    m, d = rpc(URL, 'eth_getLogs', params, 'migration_events')
    if isinstance(d, dict) and isinstance(d.get('result'), list):
        return {'lo': lo, 'hi': hi, 'ok': True, 'key': m['key'], 'logs': d['result']}
    return {'lo': lo, 'hi': hi, 'ok': False, 'key': m['key'], 'error': d or m.get('error') or m.get('http_status')}

def census():
    abi_meta, abi = fetch('https://raw.githubusercontent.com/four-meme-community/fourmeme-docs/main/abi/TokenManager2.lite.json', label='official_abi/TokenManager2.lite.json')
    event = next(x for x in abi if x.get('name') == 'LiquidityAdded')
    assert [x['type'] for x in event['inputs']] == ['address', 'uint256', 'address', 'uint256']
    assert not any(x['indexed'] for x in event['inputs'])
    p = ROOT / 'migration_window.json'
    if p.exists(): window = read('migration_window.json')
    else:
        head = int(result('eth_blockNumber', [], 'bsc_head'), 16)
        hb = block(head)
        start_ts, end_ts = hb['timestamp'] - 3 * 86400, hb['timestamp'] - 86400
        lo, hi = first_block_at(start_ts, head), first_block_at(end_ts, head) - 1
        window = {'frozen_at': now(), 'head': hb, 'start_ts': start_ts, 'end_ts_exclusive': end_ts,
                  'from_block': lo, 'to_block': hi, 'manager': MANAGER, 'topic': TOPIC,
                  'abi_sha256': abi_meta['body_sha256']}
        save('migration_window.json', window)
    lo, hi = window['from_block'], window['to_block']
    probe = logs(lo, min(lo + 1999, hi))
    save('migration_probe.json', probe)
    if not probe['ok']:
        save('migration_census.json', {'window': window, 'complete': False, 'probe': probe})
        print(json.dumps(probe)); return
    chunks = [(a, min(a + 19999, hi)) for a in range(lo, hi + 1, 20000)]
    with ThreadPoolExecutor(max_workers=3) as ex:
        responses = list(ex.map(lambda p: logs(*p), chunks))
    # Respect explicit provider range limits; smaller subranges remain same frozen universe.
    bad = [r for r in responses if not r['ok']]
    if bad:
        responses = [r for r in responses if r['ok']]
        retry = [(a, min(a + 1999, r['hi'])) for r in bad for a in range(r['lo'], r['hi'] + 1, 2000)]
        with ThreadPoolExecutor(max_workers=3) as ex:
            responses += list(ex.map(lambda p: logs(*p), retry))
    events, seen = [], set()
    for response in responses:
        for log in response.get('logs', []):
            if log.get('removed'): continue
            key = log['transactionHash'], log['logIndex']
            if key in seen: continue
            seen.add(key)
            token, offers, quote, funds = decode(['address','uint256','address','uint256'], bytes.fromhex(log['data'][2:]))
            events.append({'token': token, 'offers_raw': str(offers), 'quote': quote, 'funds_raw': str(funds),
                           'block_number': int(log['blockNumber'], 16), 'tx_hash': log['transactionHash'],
                           'log_index': int(log['logIndex'], 16), 'block_hash': log['blockHash']})
    events.sort(key=lambda x: (x['block_number'], x['log_index']))
    out = {'window': window, 'complete': all(r['ok'] for r in responses), 'events': events,
           'chunks': [{k:v for k,v in r.items() if k != 'logs'} for r in responses], 'event_count': len(events)}
    save('migration_census.json', out)
    print(json.dumps({k:v for k,v in out.items() if k not in ('events','chunks')}, indent=2))
    print(json.dumps(events[:3], indent=2))

def narrow_probe():
    global URL
    URL='https://1rpc.io/bnb'
    w=read('migration_window.json');lo=w['from_block'];hi=lo+1999
    # Exact original 2,000-block probe, subdivided to respect endpoint's 50-block limit.
    chunks=[(a,min(a+49,hi)) for a in range(lo,hi+1,50)]
    with ThreadPoolExecutor(max_workers=2) as ex:rs=list(ex.map(lambda p:logs(*p),chunks))
    # Some backing nodes enforce 25 rather than 50 blocks. Retain the old
    # failed evidence, and retry only failed intervals once in compliant pieces.
    original_errors=[r for r in rs if not r['ok']]
    if original_errors:
        retry=[(a,min(a+24,r['hi'])) for r in original_errors for a in range(r['lo'],r['hi']+1,25)]
        rs=[r for r in rs if r['ok']]
        for a,b in retry:rs.append(logs(a,b))
    ev=[]
    for r in rs:
        for l in r.get('logs',[]):
            token,offers,quote,funds=decode(['address','uint256','address','uint256'],bytes.fromhex(l['data'][2:]))
            ev.append({'token':token,'offers_raw':str(offers),'quote':quote,'funds_raw':str(funds),
                       'block_number':int(l['blockNumber'],16),'tx_hash':l['transactionHash'],
                       'log_index':int(l['logIndex'],16)})
    out={'scope':'ORIGINAL_2000_BLOCK_PROBE_ONLY','window':w,'from_block':lo,'to_block':hi,
         'complete_probe':all(r['ok'] for r in rs),'main_window_complete':False,
         'main_window_requests_at_50_blocks':(w['to_block']-w['from_block']+50)//50,
         'events':ev,'original_errors':original_errors,
         'chunks':[{k:v for k,v in r.items() if k!='logs'} for r in rs]}
    save('migration_narrow_probe.json',out);print(json.dumps({k:v for k,v in out.items() if k!='chunks'},indent=2))

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['census','narrow']);a=p.parse_args()
    census() if a.command=='census' else narrow_probe()
