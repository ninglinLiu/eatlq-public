"""Offline, reproducible evidence/package verification."""
import json
import subprocess
import sys
from pathlib import Path
from collections import Counter
from evidence import ROOT,read,save,digest,now,REQUEST_BUDGET

def main():
    failures=[]
    test=subprocess.run([sys.executable,str(ROOT/'src/test_research.py')],capture_output=True,text=True)
    compile_result=subprocess.run([sys.executable,'-m','compileall','-q',str(ROOT/'src')],capture_output=True,text=True)
    if test.returncode:failures.append('unit tests failed')
    if compile_result.returncode:failures.append('compile check failed')
    lines=[json.loads(x) for x in (ROOT/'request_ledger.jsonl').read_text().splitlines()]
    if len(lines)>REQUEST_BUDGET:failures.append('request budget exceeded')
    metadata=list((ROOT/'raw').glob('*.meta.json'))+list((ROOT/'raw').glob('*.prior.*.json'))
    for p in metadata:
        m=json.loads(p.read_text());b=ROOT/'raw'/(m['body_sha256']+'.body')
        if not b.exists() or digest(b.read_bytes())!=m['body_sha256']:failures.append('bad raw body: '+p.name)
    wallets=read('frozen_wallets.json')
    if digest(Path(wallets['source']).read_bytes())!=wallets['source_sha256']:failures.append('supplier snapshot changed')
    if Counter(r['chain'] for r in wallets['rows'])!={'bsc':12,'base':12}:failures.append('frozen cohort shape')
    entries=read('pilot_entries.json')['rows'];receipts=read('pilot_receipts.json');paths=read('pilot_paths.json')
    if {r['tx_hash'] for r in entries}!={r['tx_hash'] for r in receipts}:failures.append('receipt denominator lost')
    if {r['tx_hash'] for r in entries}!={r['source_tx'] for r in paths}:failures.append('price denominator lost')
    if not all(r['receipt_token_net_matches_index'] and r['receipt_from_matches_wallet'] for r in receipts):failures.append('account receipt mismatch')
    if not all(r['source_fee_receipt_raw']==r['source_fee_raw'] for r in receipts):failures.append('source fee reconciliation')
    for s in read('pilot_comparison.json')['summaries']:
        if s['n_resolved']+s['n_unresolved']!=len(entries):failures.append('comparison denominator lost')
        if s['net_pnl'] is not None or s['n_executable_returns_verified']!=0:failures.append('unsupported net claim')
    amount_summary = None
    if (ROOT/'amount_quote_result.json').exists():
        amount = read('amount_quote_result.json')
        expected = {r['tx_hash'] for r in entries}
        for size in (10**16, 2*10**16):
            arm = [r for r in amount['rows'] if int(r['input_quote_raw'])==size]
            if len(arm)!=len(expected) or {r['source_tx'] for r in arm}!=expected:
                failures.append('amount quote denominator lost')
        for r in amount['rows']:
            if r['net_pnl'] is not None:failures.append('pool quote called net profit')
            entry = r.get('entry') or {}
            for ex in r['exits'].values():
                if ex['block']<=entry['block']:failures.append('amount quote exit chronology')
                if ex['amount_in_raw']!=entry['quote']['amount_out_raw']:
                    failures.append('exit sells a different quantity from entry quote')
                if ex.get('quote'):
                    expected_return=int(ex['quote']['amount_out_raw'])/int(r['input_quote_raw'])-1
                    if ex['pool_quote_return']!=expected_return:failures.append('quote return reconciliation')
        manifest = read('expanded_candidate_manifest.json')
        if manifest['source_sha256']!=digest((ROOT/'account_cash_ledger.json').read_bytes()):
            failures.append('expanded manifest source changed')
        want = {(r['wallet'],r['tx_hash']) for r in read('account_cash_ledger.json')['rows']
                if r.get('side')=='buy' and r['after_snapshot']}
        have = {(r['wallet'],r['tx_hash']) for r in manifest['rows']}
        if have!=want or len(have)!=len(manifest['rows']):failures.append('expanded candidate denominator changed')
        amount_summary = {'expanded_candidates':len(have), 'size_cases':len(amount['rows']),
                          'status_counts':amount['status_counts']}
    smartmoney_summary = None
    if (ROOT/'smartmoney_receipts.json').exists():
        sm=read('smartmoney_receipts.json');manifest=read('expanded_candidate_manifest.json')['rows']
        expected={r['tx_hash'] for r in manifest}
        if len(sm['rows'])!=len(manifest) or {r['tx_hash'] for r in sm['rows']}!=expected:
            failures.append('full Smart Money receipt denominator lost')
        if not all(r.get('receipt_status')=='CONFIRMED_SUCCESS' and r.get('receipt_from_matches_wallet')
                   and r.get('receipt_token_net_matches_index') for r in sm['rows']):
            failures.append('full candidate source receipt/account reconciliation failed')
        if not all(r.get('source_fee_receipt_raw')==r['source_fee_raw'] for r in sm['rows']):
            failures.append('full candidate source fee reconciliation failed')
        smartmoney_summary={'receipts':sm['summary']}
        if (ROOT/'smartmoney_receipts_extended.json').exists():
            er=read('smartmoney_receipts_extended.json')['rows']
            if len(er)!=len(manifest) or {r['tx_hash'] for r in er}!=expected:
                failures.append('extended receipt denominator lost')
        if (ROOT/'smartmoney_v4_keys.json').exists():
            from smartmoney_routes import pool_id
            keys=read('smartmoney_v4_keys.json')['keys']
            if any(pool_id(k)!=pid for pid,k in keys.items()):failures.append('invalid recovered V4 pool key')
        if (ROOT/'smartmoney_v4_keys_calldata.json').exists():
            keys=read('smartmoney_v4_keys_calldata.json')['keys']
            if any(pool_id(k)!=pid for pid,k in keys.items()):failures.append('invalid calldata V4 pool key')
        for name in ('smartmoney_routes.json','smartmoney_cost_ledger.json','smartmoney_routes_extended.json',
                     'smartmoney_cost_ledger_extended.json','smartmoney_settlement_result.json','smartmoney_settlement_expanded_result.json',
                     'smartmoney_follower_routes.json','smartmoney_follower_execution_result.json'):
            if (ROOT/name).exists():
                artifact=read(name)
                if len(artifact['rows'])!=len(manifest) or {r['source_tx'] for r in artifact['rows']}!=expected:
                    failures.append(name+' denominator lost')
                smartmoney_summary[name]=artifact.get('summary')
                if 'settlement' in name or 'follower_execution' in name:
                    smartmoney_summary[name]=artifact.get('statuses')
                    for r in artifact['rows']:
                        if r.get('net_profit_certified'):failures.append('unsupported live net profit certification')
                        if r.get('buy',{}).get('spent_raw') is not None:
                            if not 0<=int(r['buy']['spent_raw'])<=int(r['input_eth_raw']):
                                failures.append('entry spend outside prescribed amount')
                        if r.get('modeled_net_eth_raw') is not None:
                            b,s=r['buy'],r['sell']
                            if s['spent_raw']!=b['received_raw']:failures.append('full net with residual target inventory')
                            modeled=int(s['received_raw'])-int(b['spent_raw'])-int(r['buy_fees']['total_fee_model_raw'])-int(r['sell_fees']['total_fee_model_raw'])
                            if modeled!=int(r['modeled_net_eth_raw']):failures.append('own net accounting mismatch')
                            if s['timestamp']-b['timestamp']!=86400:failures.append('settlement horizon changed')
                            if name!='smartmoney_settlement_result.json' and r.get('intermediate_inventory_status') in ('MEASURED_LEFTOVER','NOT_MEASURED'):
                                failures.append('full net claim with unpriced intermediate inventory')
                        if r['status']=='EXIT_HORIZON_NOT_YET_OBSERVED':
                            if r['scheduled_exit_timestamp']<=r['observation_cutoff'] or r.get('modeled_net_eth_raw') is not None:
                                failures.append('unmatured horizon incorrectly classified')
        if (ROOT/'smartmoney_follower_routes.json').exists():
            from smartmoney_funding import FROZEN_GAPS,FUNDING_POOL
            follower=read('smartmoney_follower_routes.json')['rows']
            original={r['source_tx']:r for r in read('smartmoney_routes_extended.json')['rows']}
            for r in follower:
                if r['source_tx'] in FROZEN_GAPS:
                    if original[r['source_tx']]['route'] or r['route'][-1]['id']!=FUNDING_POOL or r['funding_metadata_block']>r['source_block']:
                        failures.append('self-built funding supplement violates registered scope')
                elif r['route']!=original[r['source_tx']]['route']:
                    failures.append('supplement changed a source-witnessed route')
        if (ROOT/'smartmoney_execution_summary.json').exists():
            s=read('smartmoney_execution_summary.json')
            if s['frozen_candidates']!=len(expected) or s['complete_conditionally_modeled_roundtrips']+s['unresolved_or_inventory_remaining']!=len(expected):
                failures.append('execution summary denominator mismatch')
            if s['portfolio_pnl'] is not None or s['full_441_pnl'] is not None or s['profitability_certified']:
                failures.append('unsupported portfolio/full-universe profitability claim')
            if 'inventory_remaining_cash_delta_raw' in s and int(s['cash_delta_closed_and_partial_raw'])!=int(s['sum_modeled_eth_raw'])+int(s['inventory_remaining_cash_delta_raw']):
                failures.append('closed/partial cash reconciliation failed')
        if (ROOT/'smartmoney_follower_execution_result.json').exists():
            final=read('smartmoney_follower_execution_result.json')['rows']
            routes={r['source_tx']:r for r in read('smartmoney_follower_routes.json')['rows']}
            for r in final:
                if r.get('buy',{}).get('timestamp') is not None and r['buy']['timestamp']-routes[r['source_tx']]['source_ts']!=10:
                    failures.append('follower entry delay changed')
                if r.get('platform_fee_status')!='NO_THIRD_PARTY_ROUTER_COMMISSION_BY_DESIGN':
                    failures.append('self-built venue fee status not propagated')
            if (ROOT/'smartmoney_pancake_repair_manifest.json').exists():
                allowed=set(read('smartmoney_pancake_repair_manifest.json')['affected_source_txs'])
                repaired={r['source_tx'] for r in final if r.get('adapter')=='MIXED_PLUS_CANONICAL_PANCAKE_V2_25BPS'}
                if repaired!=allowed:failures.append('protocol repair scope changed')
            if (ROOT/'smartmoney_execution_diagnostics.json').exists():
                diagnostics=read('smartmoney_execution_diagnostics.json')
                if len(diagnostics['rows'])!=len(expected) or {r['source_tx'] for r in diagnostics['rows']}!=expected:
                    failures.append('diagnostic denominator lost')
                if sum(diagnostics['class_counts'].values())!=len(expected):failures.append('diagnostic counts mismatch')
            if (ROOT/'smartmoney_cash_schedule.json').exists():
                schedule=read('smartmoney_cash_schedule.json')
                if schedule['entries_with_fill_and_fee']+len(schedule['excluded_entries'])!=len(expected):
                    failures.append('cash schedule entry denominator lost')
                events=schedule['buy_first_same_timestamp']['events']
                if any(e['timestamp']>schedule['observation_cutoff'] for e in events):failures.append('cash schedule uses future event')
                total=sum(int(e['cash_delta_raw']) for e in events)
                if total!=int(schedule['buy_first_same_timestamp']['ending_cash_delta_raw']):
                    failures.append('cash schedule terminal reconciliation failed')
                if total!=int(schedule['sell_first_same_timestamp']['ending_cash_delta_raw']):
                    failures.append('cash schedule tie convention changes terminal cash')
                if len({(e['source_tx'],e['leg']) for e in events})!=len(events):failures.append('duplicate cash event')
                wallets=read('smartmoney_execution_summary.json')['wallet_breakdown']
                if sum(int(w['known_cash_delta_at_cutoff_raw']) for w in wallets)!=total:
                    failures.append('wallet cash breakdown does not reconcile to schedule')
            if (ROOT/'smartmoney_route_failure_probe.json').exists():
                probes=read('smartmoney_route_failure_probe.json')['rows']
                if {r['source_tx'] for r in probes}!={r['source_tx'] for r in final if r['status']=='SELL_SIMULATION_UNRESOLVED'}:
                    failures.append('route-prefix diagnostic denominator lost')
            if (ROOT/'smartmoney_l2_fee_stress.json').exists():
                baseline=read('smartmoney_l2_fee_stress.json')['cases'][0]
                s=read('smartmoney_execution_summary.json')
                if baseline['sum_conditional_net_raw']!=s['sum_modeled_eth_raw'] or baseline.get('closed_and_partial_cash_delta_raw')!=s['cash_delta_closed_and_partial_raw']:
                    failures.append('fee stress baseline does not reconcile')
    solidity=None
    if (ROOT/'contracts/ResearchV4Executor.sol').exists():
        solidity=subprocess.run(['forge','test','--offline','--root',str(ROOT),'--contracts','contracts',
            '--out','sol_out','--cache-path','sol_cache','--use','0.8.26','--optimizer-runs','200','--via-ir'],
            capture_output=True,text=True)
        if solidity.returncode:failures.append('settlement contract unit tests failed')
    migration=read('migration_narrow_probe.json')
    covered=sorted((r['lo'],r['hi']) for r in migration['chunks'] if r['ok'])
    cursor=migration['from_block']
    for a,b in covered:
        if a!=cursor:failures.append('migration probe coverage gap or overlap')
        cursor=b+1
    if cursor!=migration['to_block']+1:failures.append('migration probe coverage incomplete')
    if not read('migration_positive_control.json')['n_logs']:failures.append('migration positive control missing')
    hashes={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sorted(ROOT.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and 'raw' not in p.relative_to(ROOT).parts
            and p.name not in ('verification.json','artifact_hashes.json')}
    save('artifact_hashes.json',hashes)
    result={'verified_at':now(),'passed':not failures,'failures':failures,'unit_tests_passed':test.returncode==0,
        'unit_test_output':test.stdout+test.stderr,'compile_passed':compile_result.returncode==0,
        'public_requests':len(lines),'request_budget':REQUEST_BUDGET,'raw_metadata_verified':len(metadata),
        'hashed_artifacts':len(hashes),'frozen_entries':len(entries),'frozen_tokens':len({r['token'] for r in entries}),
        'price_path_status':dict(Counter(r['status'] for r in paths)),
        'amount_quote_instrument':amount_summary,
        'smartmoney_full_candidate_audit':smartmoney_summary,
        'settlement_contract_tests_passed':solidity.returncode==0 if solidity else None,
        'settlement_contract_test_output':solidity.stdout+solidity.stderr if solidity else None,
        'qualification':'PACKAGE_AND_ACCOUNTING_INSTRUMENT_VERIFICATION_NOT_PROFITABILITY_CERTIFICATION'}
    save('verification.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='unit_test_output'},indent=2))
    if failures:raise SystemExit(1)

if __name__=='__main__':main()
