"""Repair the one failed cash index without rewriting frozen pilot inputs."""
import json
from collections import defaultdict, Counter
from urllib.parse import urlencode
from evidence import ROOT, read, save, fetch, now
from wallets import stamp
from ledger import classify, address

def main():
    wallet='0x347ffc6db9acc54ac2019795173b9599e8b82bd9'
    original=read('wallet_cash_index/internal_'+wallet+'.json')
    params={}; items=[]; pages=[]; seen=set(); complete=False; status='PAGE_CAP_20'
    for page in range(20):
        url='https://base.blockscout.com/api/v2/addresses/'+wallet+'/internal-transactions'
        if params:url+='?'+urlencode(params)
        m,d=fetch(url,label='repair_failed_internal_index',cache=page!=0)
        pages.append({'key':m['key'],'http_status':m.get('http_status')})
        if not isinstance(d,dict) or not isinstance(d.get('items'),list):
            status='API_ERROR';break
        for x in d['items']:
            identity=x.get('transaction_hash'),x.get('index'),x.get('block_number')
            if identity not in seen and x.get('timestamp') and stamp(x['timestamp'])>=original['cutoff']:
                seen.add(identity);items.append(x)
        if any(x.get('timestamp') and stamp(x['timestamp'])<original['cutoff'] for x in d['items']):
            complete=True;status='CROSSED_QUALIFICATION_START';break
        params=d.get('next_page_params')
        if not params:complete=True;status='INDEX_EXHAUSTED';break
    fixed={**original,'items':items,'pages':pages,'complete':complete,'status':status,'repaired_at':now()}
    save('wallet_cash_index/internal_'+wallet+'.repaired.json',fixed)
    audit={'generated_at':now(),'wallet':wallet,'complete':complete,'status':status,
           'pages':len(pages),'indexed_internal_transfers':len(items),
           'original_inputs_preserved':True,'rows':[]}
    if complete:
        native=defaultdict(int)
        for x in items:
            if x.get('success') is True:
                native[x['transaction_hash']]+=int(x['value'])*(int(address(x['to'])==wallet)-int(address(x['from'])==wallet))
        external={x['hash']:x for x in read('wallet_cash_index/external_'+wallet+'.json')['items']}
        # Reconcile exactly the frozen external transaction universe; do not add
        # later transactions fetched during repair or change the pilot selection.
        for old in read('account_cash_ledger.json')['rows']:
            if old['wallet']!=wallet:continue
            x=external[old['tx_hash']]
            updated=classify({t:int(v) for t,v in old['token_nets'].items()},
                            native[old['tx_hash']]-int(x.get('value') or 0)) if x.get('status')=='ok' else {'status':'FAILED_TRANSACTION'}
            audit['rows'].append({'tx_hash':old['tx_hash'],'after_snapshot':old['after_snapshot'],
                'original':{k:old.get(k) for k in ('side','quote','quote_net_raw','token','token_net_raw','status')},
                'repaired':updated})
        old_buys=[r for r in audit['rows'] if r['after_snapshot'] and r['original'].get('side')=='buy']
        new_buys=[r for r in audit['rows'] if r['after_snapshot'] and r['repaired'].get('side')=='buy']
        audit.update(original_forward_buys=len(old_buys),repaired_forward_buys=len(new_buys),
                     old_buy_cash_changes=sum(r['original'].get('quote_net_raw')!=r['repaired'].get('quote_net_raw') for r in old_buys),
                     old_buys_no_longer_buys=sum(r['repaired'].get('side')!='buy' for r in old_buys),
                     new_classifications=dict(Counter(r['repaired']['status'] for r in audit['rows'])))
    save('internal_index_repair_audit.json',audit)
    print(json.dumps({k:v for k,v in audit.items() if k!='rows'},indent=2))

if __name__=='__main__':main()
