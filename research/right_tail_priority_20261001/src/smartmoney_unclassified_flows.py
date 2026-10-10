"""Describe excluded indexed flows without inventing additional purchases."""
import json
from collections import Counter
from evidence import ROOT,read,save,now

def run():
    rows=[r for r in read('account_cash_ledger.json')['rows'] if r['after_snapshot']
          and r['status']=='UNRESOLVED_MULTIASSET_OR_NO_CASH']
    external={}
    for p in (ROOT/'wallet_cash_index').glob('external_*.json'):
        external.update({r['hash']:r for r in json.loads(p.read_text())['items']})
    out=[]
    for r in rows:
        method=external[r['tx_hash']].get('method');nets=[int(v) for v in r['token_nets'].values() if int(v)]
        native=int(r['native_net_raw'])
        if method in ('approve','0x095ea7b3') and not nets and native==0:
            kind='APPROVE_SELECTOR_AND_NO_INDEXED_ASSET_NET_MOVEMENT'
        elif method=='0xeffbec13' and len(nets)==1 and nets[0]<0 and native==0:
            kind='AGGREGATOR_SINGLE_TOKEN_OUT_NO_IDENTIFIED_CASH_RECEIPT'
        else:kind='OTHER_UNRESOLVED_FLOW'
        out.append({**r,'source_method':method,'descriptive_group':kind,
                    'economic_side_not_reclassified':True})
    result={'generated_at':now(),'n':len(rows),'rows':out,
        'groups':dict(Counter(r['descriptive_group'] for r in out)),
        'qualification':'INDEX_LEVEL_DIAGNOSTIC_NOT_RECEIPT_OR_ECONOMIC_SIDE_CERTIFICATION',
        'note':'No record added to or removed from the frozen 441. A missing cash leg must not become a zero-cost buy or assumed loss.'}
    save('smartmoney_unclassified_flow_audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))

if __name__=='__main__':run()
