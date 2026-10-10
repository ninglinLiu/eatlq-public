import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from evidence import ROOT, save, read, digest, now, fetch, rpc, summary

BOARDS = Path('/root/copy_cascade_invert_r0/data/normalized/boards.json')
RPCS = {'bsc': ['https://bsc-rpc.publicnode.com', 'https://bsc-dataseed.binance.org'],
        'base': ['https://base-rpc.publicnode.com', 'https://mainnet.base.org']}

def freeze():
    p = ROOT / 'frozen_wallets.json'
    if p.exists():
        return read('frozen_wallets.json')
    src = json.loads(BOARDS.read_text())
    selected = []
    for chain in ('bsc', 'base'):
        by = {}
        rows = [r for r in src['rows'] if r.get('chain') == chain
                and r.get('source') == 'gmgn' and r.get('window') == '7d'
                and r.get('spot_reconcilable')]
        # One frozen supplier claim per address: first capture wins, tag tie-break.
        for r in sorted(rows, key=lambda r: (r['captured_at'], r.get('tag', ''))):
            by.setdefault(r['address'].lower(), r)
        ranked = sorted(by.values(), key=lambda r: (-float(r.get('vendor_realized_usd') or 0),
                                                   r['address'].lower()))[:12]
        for i, r in enumerate(ranked, 1):
            selected.append({**r, 'address': r['address'].lower(), 'frozen_rank': i})
    out = {'frozen_at': now(), 'source': str(BOARDS), 'source_sha256': digest(BOARDS.read_bytes()),
           'dedup': 'earliest capture, tag tie-break; rank realized USD descending/address ascending',
           'claims_verified': False, 'rows': selected}
    save('frozen_wallets.json', out)
    return out

def probe():
    frozen = freeze()
    jobs = []
    for chain, endpoints in RPCS.items():
        for url in endpoints:
            jobs.append((url, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_blockNumber', 'params': []}, chain + '_head'))
        w = next(x for x in frozen['rows'] if x['chain'] == chain)
        jobs.append((f"https://gmgn.ai/vas/api/v1/wallet_activity/{chain}?wallet={w['address']}&limit=50&type=buy,sell", None, chain + '_gmgn_activity'))
        if chain == 'base':
            jobs.append((f"https://base.blockscout.com/api/v2/addresses/{w['address']}/token-transfers?type=ERC-20", None, 'base_blockscout_transfers'))
    for name in ('abi/TokenManager2.lite.json', 'docs/trade-guide.md'):
        jobs.append(('https://raw.githubusercontent.com/four-meme-community/fourmeme-docs/main/' + name, None, 'official_' + name))
    jobs.append(('https://api.geckoterminal.com/api/v2/networks/bsc/pools/0xc91d0280d92e2c89f2c6a984ddafa6cca39c7643/ohlcv/hour?aggregate=1&limit=1000&currency=usd', None, 'gt_bsc_hour'))
    def one(j):
        m, d = fetch(j[0], j[1], j[2]); return summary(m, d)
    with ThreadPoolExecutor(max_workers=4) as ex:
        result = list(ex.map(one, jobs))
    save('probe.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('command', choices=['freeze', 'probe'])
    a = p.parse_args()
    if a.command == 'freeze': print(json.dumps(freeze(), ensure_ascii=False, indent=2))
    else: probe()
