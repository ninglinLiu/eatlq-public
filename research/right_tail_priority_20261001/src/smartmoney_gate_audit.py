"""Diagnose the witnessed Flaunch spend gate using public data and view calls.

Never reuses a source-wallet signature, impersonates its buyer, changes a gate,
or treats an authorisation-dependent source fill as freely copyable.
"""
import json
from eth_abi import encode,decode
from eth_utils import keccak
from evidence import ROOT,read,save,now,fetch
from pilot import call
from smartmoney_routes import raw_receipt
from smartmoney_settlement import EXECUTOR

TOKEN='0x2f5ab61870c9c5254eebd3e6361d3eb40181ac8e'
CALCULATOR='0x91938a66323725252043c03ad3066542ac793057'
ABI_SOURCE='https://raw.githubusercontent.com/flayerlabs/flaunchgg-contracts/main/abi/SpendGatedSignerFeeCalculator.json'
DEPLOYMENT_SOURCE='https://raw.githubusercontent.com/flayerlabs/flaunchgg-contracts/main/README.md'
KEY='(address,address,uint24,int24,address)'

def source_authorization(tx, wallet, pid):
    raw=bytes.fromhex(tx['raw_input'][2:])
    expected=keccak(text='swapExactInput('+KEY+', (bool,int256,uint160),bytes,uint256,uint256)'.replace(' ',''))[:4]
    if raw[:4]!=expected:return {'status':'UNSUPPORTED_SOURCE_CALL_ENCODING'}
    _,_,hook,_,_=decode([KEY,'(bool,int256,uint160)','bytes','uint256','uint256'],raw[4:])
    if not hook:return {'status':'EMPTY_HOOK_DATA'}
    try:
        ref,auth=decode(['address','(address,bytes32,uint256,uint256,bytes)'],hook)
    except Exception:return {'status':'OTHER_HOOK_DATA_ENCODING','bytes':len(hook)}
    buyer,pool,deadline,ceiling,sig=auth
    return {'status':'DECODED_SOURCE_AUTHORIZATION_FIELDS', 'buyer':buyer,
        'pool_id':'0x'+pool.hex(),'buyer_matches_source_wallet':buyer==wallet,
        'pool_id_matches_route':pool.hex()==pid[2:],'deadline':deadline,
        'spend_ceiling_raw':str(ceiling),'signature_bytes':len(sig),
        'signature_validity':'source transaction succeeded; not independently cryptographically verified',
        'copier_signature_available':False}

def run():
    # Preserve the official ABI and address evidence in the same bounded ledger.
    abi_meta,abi=fetch(ABI_SOURCE,label='smartmoney_gate_official_abi')
    deployment_meta,_=fetch(DEPLOYMENT_SOURCE,label='smartmoney_gate_official_deployment')
    assert any(x.get('name')=='spendGateSettings' for x in abi['abi'])
    rows=[r for r in read('smartmoney_routes_extended.json')['rows'] if r['token']==TOKEN]
    receipts={r['tx_hash']:r for r in read('smartmoney_receipts.json')['rows']}
    external={}
    for p in (ROOT/'wallet_cash_index').glob('external_*.json'):
        external.update({r['hash']:r for r in json.loads(p.read_text())['items']})
    out=[];spend_topic='0x'+keccak(text='SpendRecorded(bytes32,address,uint256)').hex()
    for r in rows:
        tx=r['source_tx'];pid=r['route'][0]['id'];block=r['source_block']+5
        source=external[tx];router=source['to']['hash'].lower()
        calls=[(CALCULATOR,True,keccak(text='spendGateSettings(bytes32)')[:4]+bytes.fromhex(pid[2:])),
               (CALCULATOR,True,keccak(text='approvedRouters(address)')[:4]+encode(['address'],[EXECUTOR])),
               (CALCULATOR,True,keccak(text='approvedRouters(address)')[:4]+encode(['address'],[router])),
               (CALCULATOR,True,keccak(text='trustedPoolKeySigner(bytes32)')[:4]+bytes.fromhex(pid[2:]))]
        data='0x'+keccak(text='aggregate3((address,bool,bytes)[])')[:4].hex()+encode(['(address,bool,bytes)[]'],[calls]).hex()
        raw,ev=call('0xca11bde05977b3631167028862be2a173976ca11',data,block,'smartmoney_gate_historical_view')
        decoded=decode(['(bool,bytes)[]'],bytes.fromhex(raw[2:]))[0] if raw and raw!='0x' else []
        item={k:r[k] for k in ('source_tx','wallet','token','source_block')}
        item.update(entry_block=block,pool_id=pid,calculator=CALCULATOR,evidence=ev,
            source_authorization=source_authorization(source,r['wallet'],pid),
            source_spend_recorded_events=[{'emitter':l['address'],'buyer':'0x'+l['topics'][1][-40:],
                'data':l['data']} for l in raw_receipt(receipts[tx])['logs']
                if l['topics'] and l['topics'][0]==spend_topic],
            gate_active_at_modeled_entry=None)
        if decoded and decoded[0][0]:
            enabled,cap,settler,ends=decode(['bool','uint256','address','uint256'],decoded[0][1])
            item.update(enabled=enabled,wallet_cap_raw=str(cap),settler=settler,ends_at=ends,
                gate_active_at_modeled_entry=bool(enabled and r['source_ts']+10<ends))
        for i,name in [(1,'research_executor_approved'),(2,'source_router_approved')]:
            item[name]=decode(['bool'],decoded[i][1])[0] if len(decoded)>i and decoded[i][0] else None
        if len(decoded)>3 and decoded[3][0]:
            signer,enabled=decode(['address','bool'],decoded[3][1])
            item.update(trusted_signer=signer,trusted_signer_enabled=enabled)
        item['classification']=('AUTHORIZATION_CONTEXT_REQUIRED_NOT_AVAILABLE_TO_THIS_EXECUTOR'
            if item['gate_active_at_modeled_entry'] else 'GATE_NOT_ACTIVE_OR_VIEW_UNRESOLVED')
        out.append(item)
    result={'generated_at':now(),'n':len(out),'rows':out,
        'official_abi_evidence':abi_meta['key'],'official_deployment_evidence':deployment_meta['key'],
        'official_sources':[ABI_SOURCE,DEPLOYMENT_SOURCE],
        'n_active_gate':sum(r['gate_active_at_modeled_entry'] is True for r in out),
        'limits':'no copier authorisation acquired, no signature reuse, no gate modified, no alternative entry timing searched'}
    save('smartmoney_gate_audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))

if __name__=='__main__':run()
