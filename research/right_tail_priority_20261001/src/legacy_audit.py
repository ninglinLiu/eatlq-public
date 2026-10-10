"""Bounded audit of legacy GL2-B qualification, no private endpoints."""
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from evidence import save, digest

DIAG=Path('/opt/EATLQ/mvp/diagnostics')
SAMPLE=Path('/root/copy_cascade_invert_r0/data/raw/golden/base_b3_entity_attribution_sample_v2_20260714.json')
SETTINGS=' SETTINGS readonly=2,max_threads=1,max_execution_time=30,max_memory_usage=536870912'

def ch(sql):
    p=subprocess.run(['docker','exec','eatlq-clickhouse-1','clickhouse-client','--query',sql],capture_output=True,text=True,timeout=35)
    if p.returncode:raise RuntimeError(p.stderr[:1000])
    return p.stdout

def main():
    stage_path=DIAG/'base_gl2_b_stage1_distribution_20260715.json'
    forward_path=DIAG/'base_gl2_b_copy_forward_20260715.json'
    stage=json.loads(stage_path.read_text());forward=json.loads(forward_path.read_text());sample=json.loads(SAMPLE.read_text())
    addresses=forward['eligible_wallets']
    address_sql=','.join("'"+w+"'" for w in addresses)
    sql=f"SELECT tx_hash,block_number,toString(block_time) block_time,venue_address,target_token_address,side,resolved_account,attribution_source,tx_from,account_code_class FROM eatlq.base_cha_wallet_swaps WHERE block_number BETWEEN 46389458 AND 46417105 AND resolved_account IN ({address_sql})"
    explain=ch('EXPLAIN indexes=1 '+sql+SETTINGS)
    save('legacy_query_plan.json',{'sql':sql+SETTINGS,'explain':explain})
    rows=[json.loads(x) for x in ch(sql+SETTINGS+' FORMAT JSONEachRow').splitlines()]
    save('legacy_additional_swaps.json',rows)
    checks=[]
    for w in addresses:
        sampled=[x for x in sample['rows'] if x['resolved_account_or_entity'].lower()==w]
        txs={x['tx_hash'] for x in sampled}
        alltxs={x['tx_hash'] for x in rows if x['resolved_account']==w}
        additional=[x for x in rows if x['resolved_account']==w and x['tx_hash'] not in txs]
        token_set={x['target_token_address'].lower() for x in sampled}
        pool_set={x['venue_address'].lower() for x in sample['rows']}
        m=stage['wallet_metrics'][w]
        checks.append({'wallet':w,'sampled_attribution_txs':len(txs),'observed_wallet_table_txs':len(alltxs),
                       'additional_known_txs_not_sampled':len(alltxs-txs),
                       'additional_eoa_tx_from_confirmed':len({x['tx_hash'] for x in additional if x['tx_from'].lower()==w and x['account_code_class']=='eoa'}),
                       'additional_in_original_17_pool_universe':len({x['tx_hash'] for x in additional if x['venue_address'].lower() in pool_set}),
                       'additional_in_same_sampled_tokens':len({x['tx_hash'] for x in additional if x['target_token_address'].lower() in token_set}),
                       'old_matched_fifo_realized_weth':m['realized_weth'],
                       'oversell_raw':str(m['oversell_raw']),
                       'qualification_status':'NOT_VERIFIED_FULL_INVENTORY'})
    golden=[]
    for g in stage['fifo']['golden_wallets']:
        earliest={}
        for x in sorted(g['balance_of_checks'],key=lambda x:x['block_number']):earliest.setdefault(x['token'],x)
        golden.append({'wallet':g['wallet'],'all_transaction_delta_checks_pass':g['all_balance_of_integer_equal'],
                       'first_observation_positive_inventory_tokens':sum(int(x['balance_before_raw'])>0 for x in earliest.values()),
                       'first_observations':list(earliest.values())})
    out={'scope':stage['population'],'window':stage['window'],'eligible_audit':checks,'golden_audit':golden,
         'database_time_range':{'start':min(r['block_time'] for r in rows),'end':max(r['block_time'] for r in rows)},
         'conclusions':['deterministic transaction subsampling is not an inventory-complete trading history',
                        'zero oversell is insufficient: missing high-cost purchases can coexist with matched profitable sells',
                        'per-transaction balance delta equality does not validate cost basis or complete chronology',
                        'legacy raw-token aggregate totals mix assets; do not treat them as meaningful portfolio inventory units'],
         'source_hashes':{str(p):digest(p.read_bytes()) for p in (stage_path,forward_path,SAMPLE)}}
    save('legacy_wallet_audit.json',out)
    print(json.dumps({'eligible':checks,'golden_with_nonzero_first_inventory':sum(g['first_observation_positive_inventory_tokens']>0 for g in golden)},indent=2))

if __name__=='__main__':main()
