"""Amount-specific historical V4 pool quotes, NOT settled wallet net returns.

No signing, token approval, balance injection, or transaction submission. The
quoter is called through historical eth_call and all state changes are discarded.
"""
import json
from collections import Counter, defaultdict
from statistics import mean
from eth_abi import encode, decode
from eth_utils import keccak
from evidence import ROOT, read, save, now, digest
from pilot import call

QUOTER = '0x0d5e0f971ed27fbff6c2837bf31316121532048d'
PARAM_TYPE = '((address,address,uint24,int24,address),bool,uint128,bytes)'
SELECTOR = keccak(text='quoteExactInputSingle('+PARAM_TYPE+')')[:4]
SIZES = (10**16, 2*10**16)

def calldata(key, input_token, amount):
    if input_token not in (key['token0'], key['token1']):
        raise ValueError('input token not in verified pool key')
    if not 0 < amount < 2**127:
        raise ValueError('amount outside positive signed-int128 range')
    pool = (key['token0'], key['token1'], key['fee'], key['tick_spacing'], key['hooks'])
    params = (pool, input_token == key['token0'], amount, b'')
    return '0x'+SELECTOR.hex()+encode([PARAM_TYPE], [params]).hex()

def parse_quote(raw):
    if not raw or raw == '0x':
        return None
    try:
        amount, gas = decode(['uint256', 'uint256'], bytes.fromhex(raw[2:]))
    except (ValueError, OverflowError):
        return None
    return {'amount_out_raw':str(amount), 'quoter_gas_units':gas}

def quote(key, token_in, amount, block):
    raw, ev = call(QUOTER, calldata(key, token_in, amount), block, 'amount_specific_pool_quote')
    try: parsed = parse_quote(raw)
    except Exception as exc:
        parsed = None
        ev['decode_error'] = type(exc).__name__
    return {'block':block, 'token_in':token_in, 'amount_in_raw':str(amount),
            'evidence':ev, 'quote':parsed,
            'status':'POOL_QUOTE_RETURNED' if parsed else 'NO_POOL_QUOTE'}

def freeze_expanded_candidates():
    ledger = read('account_cash_ledger.json')
    rows = sorted((r for r in ledger['rows'] if r.get('side') == 'buy' and r['after_snapshot']),
                  key=lambda r:(r['timestamp'], r['wallet'], r['tx_hash']))
    assert len({(r['wallet'],r['tx_hash']) for r in rows}) == len(rows)
    name = 'expanded_candidate_manifest.json'
    out = {'frozen_at':now(), 'source_sha256':digest((ROOT/'account_cash_ledger.json').read_bytes()),
           'selection':'ALL existing after-snapshot single-target opposite-cash buy candidates; no outcome filter',
           'qualification':'EXPLORATORY_EXTENSION_AFTER_PILOT_RESULTS; NOT ALL RECEIPTS VALIDATED',
           'n_candidates':len(rows), 'n_tokens':len({r['token'] for r in rows}),
           'n_wallets':len({r['wallet'] for r in rows}), 'rows':rows}
    if (ROOT/name).exists():
        assert read(name)['rows'] == rows, 'expanded frozen universe changed'
    else:
        save(name, out)
    return {k:out[k] for k in ('n_candidates','n_tokens','n_wallets')}

def summarize(rows):
    groups = defaultdict(list)
    for r in rows:
        for horizon in ('30m','2h','24h','source_first_sell'):
            ex = r.get('exits', {}).get(horizon, {})
            groups[(r['input_quote_raw'],horizon)].append(ex.get('pool_quote_return'))
    return [{'input_eth':int(size)/10**18, 'exit_policy':h,
             'n_frozen':len(vals), 'n_quoted_roundtrips':sum(v is not None for v in vals),
             'n_unresolved':sum(v is None for v in vals),
             'pool_quote_mean_return':mean([v for v in vals if v is not None]) if any(v is not None for v in vals) else None,
             'n_positive':sum(v is not None and v > 0 for v in vals),
             'net_pnl':None}
            for (size,h),vals in groups.items()]

def main():
    extension = freeze_expanded_candidates()
    paths = read('pilot_paths.json')
    keys = read('v4_pool_keys.json')['keys']
    rows = []
    for p in paths:
        for size in SIZES:
            r = {'source_tx':p['source_tx'], 'wallet':p['wallet'], 'token':p['token'],
                 'input_quote_raw':str(size), 'quote_asset':'ETH_OR_WETH',
                 'index_complete':p['index_complete'], 'net_pnl':None, 'exits':{}}
            route = p.get('route')
            if not route:
                r['status'] = 'UNRESOLVED_POOL_KEY_OR_ROUTE'
            elif p['quote'] != 'ETH' or len(route) != 1 or route[0]['kind'] != 'V4':
                r['status'] = 'ENGINEERING_PENDING_MIXED_ROUTE_ADAPTER'
            else:
                e = route[0]; key = keys[e['id']]
                points = {s['name']:s['block'] for s in p['states']}
                entry_block = points['entry_10s']
                entry = quote(key, e['to_token'], size, entry_block)
                r['entry'] = entry
                qty = int(entry['quote']['amount_out_raw']) if entry['quote'] else 0
                if qty <= 0:
                    r['status'] = 'ENTRY_ZERO_OUTPUT' if entry['quote'] else 'ENTRY_QUOTE_UNAVAILABLE'
                else:
                    r['status'] = 'AMOUNT_SPECIFIC_POOL_QUOTE_NOT_SETTLED_FILL'
                    for h in ('30m','2h','24h','source_first_sell'):
                        block = points.get('exit_'+h)
                        if block is None or block <= entry_block:
                            continue
                        ex = quote(key, e['from_token'], qty, block)
                        ex['pool_quote_return'] = (int(ex['quote']['amount_out_raw'])/size-1) if ex['quote'] else None
                        r['exits'][h] = ex
            rows.append(r)
            save('amount_quote_progress.json', {'generated_at':now(), 'rows':rows})
        print(json.dumps({'source_tx':p['source_tx'], 'status':rows[-1]['status'], 'processed_entries':len(rows)//len(SIZES)}), flush=True)
    out = {'generated_at':now(), 'expanded_candidate_universe':extension,
           'method':'Historical V4 Quoter with empty hookData; fixed original 10-second entry and horizons',
           'sources':['https://docs.uniswap.org/contracts/v4/deployments',
                      'https://github.com/Uniswap/v4-periphery/blob/main/src/interfaces/IV4Quoter.sol',
                      'https://github.com/Uniswap/v4-periphery/blob/main/src/lens/V4Quoter.sol'],
           'limitations':['pool quote is not token transfer settlement or transfer-tax verification',
               'quoter gas is not full transaction gas; own gas and L1 fee remain unpriced',
               'empty hookData and quoter sender are not a validated actual router context',
               'does not propagate counterfactual entry impact into later historical state',
               'exact-input quoter returns output, not consumed input; full consumption unverified',
               'fixed native-quote sizes are not historical $25/$50 and do not certify a $1000 sleeve',
               'incomplete source indexes remain flagged; not certified Smart Money qualification'],
           'status_counts':dict(Counter(r['status'] for r in rows)),
           'rows':rows, 'summaries':summarize(rows)}
    save('amount_quote_result.json', out)
    print(json.dumps({'extension':extension,'status_counts':out['status_counts'],'summaries':out['summaries']}, indent=2))

if __name__ == '__main__':
    main()
