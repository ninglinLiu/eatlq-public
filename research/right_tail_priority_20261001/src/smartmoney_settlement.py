"""Historical transfer-settling V4 simulation. No deployment or real transaction.

The hypothetical research executor is injected only into eth_call state. Token
storage is changed only after a balance slot has been confirmed by an independent
balanceOf probe. Missing context is recorded, never converted to a total loss.
"""
import argparse
import json
import rlp
from collections import Counter
from eth_abi import encode,decode
from eth_utils import keccak
from evidence import ROOT,read,save,now
from pilot import req
from v4_paths import ZERO
from ledger import WETH
from pilot import call

EXECUTOR='0x00000000000000000000000000000000c0de0606'
CALLER='0x00000000000000000000000000000000c0de0707'
KEY_TYPE='(address,address,uint24,int24,address)'
SIG='run('+KEY_TYPE+'[],bool[],uint128,bool)'
SELECTOR=keccak(text=SIG)[:4]
SLOT_DETECTOR_VERSION=2
B20_NAMESPACE=int.from_bytes(keccak(encode(['uint256'],[int.from_bytes(keccak(text='base.b20'),'big')-1])),'big') & ~255
B20_STORAGE_SOURCE='https://github.com/base/base-std/blob/main/test/lib/mocks/MockB20Storage.sol'
EIP1967={'0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc',
         '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103'}

def has_throttle(value):
    if isinstance(value,dict):
        return value.get('http_status')==429 or any(has_throttle(v) for v in value.values())
    return isinstance(value,list) and any(has_throttle(v) for v in value)

def runtime(mixed=False):
    name='ResearchMixedExecutor' if mixed else 'ResearchV4Executor'
    return read('sol_out/'+name+'.sol/'+name+'.json')['deployedBytecode']['object']

def run_data(route, keys, amount, buy):
    edges=list(reversed(route)) if buy else route
    params=[];directions=[]
    for e in edges:
        k=keys[e['id']]
        params.append((k['token0'],k['token1'],k['fee'],k['tick_spacing'],k['hooks']))
        tin=e['to_token'] if buy else e['from_token']
        directions.append(tin==k['token0'])
    wrap=buy and route[-1]['to_token']==WETH
    return '0x'+SELECTOR.hex()+encode([KEY_TYPE+'[]','bool[]','uint128','bool'],[params,directions,amount,wrap]).hex()

def balance_data(token_holder):
    return '0x70a08231'+encode(['address'],[token_holder]).hex()

def b20_balance_slot(holder):
    return '0x'+keccak(encode(['address','uint256'],[holder,B20_NAMESPACE+4])).hex()

def needs_slot_repair(row):
    ev=row.get('slot_evidence',{})
    return (row.get('status')=='SELL_SIMULATION_UNRESOLVED' and
            row.get('token','').startswith('0xb2000000000000000000') and
            not ev.get('slot') and ev.get('detector_version',1)<SLOT_DETECTOR_VERSION)

def detect_balance_slot(token,block):
    name='smartmoney_balance_slots/'+token+'.json'
    is_b20=token.startswith('0xb2000000000000000000')
    if (ROOT/name).exists():
        old=read(name)
        if old.get('slot'):return old
        if not has_throttle(old) and not (is_b20 and old.get('detector_version',1)<SLOT_DETECTOR_VERSION):return old
    data=balance_data(EXECUTOR)
    # B20's native implementation does not expose EVM SLOADs in an access list.
    # The documented namespace is a hypothesis until balanceOf confirms it.
    al,ev=(None,None) if is_b20 else req('eth_createAccessList',[{'to':token,'data':data,'from':CALLER},hex(block)],'smartmoney_balance_access_list')
    candidates=[]
    if is_b20:candidates.append(b20_balance_slot(EXECUTOR))
    if al:
        for row in al.get('accessList',[]):
            if row['address'].lower()==token:candidates += [s for s in row.get('storageKeys',[]) if s not in EIP1967]
    # Narrow layout hypotheses are acceptable only with a successful balanceOf
    # sentinel check; no token balance is assumed to live in a guessed slot.
    candidates+=['0x'+keccak(encode(['address','uint256'],[EXECUTOR,i])).hex() for i in range(4)]
    sentinel=12345678987654321
    out={'token':token,'block':block,'access_list_evidence':ev,'slot':None,'attempts':[],
         'detector_version':SLOT_DETECTOR_VERSION,'layout_source':B20_STORAGE_SOURCE if is_b20 else None}
    for slot in dict.fromkeys(candidates):
        val,proof=req('eth_call',[{'to':token,'data':data},hex(block),
                      {token:{'stateDiff':{slot:'0x'+sentinel.to_bytes(32,'big').hex()}}}], 'smartmoney_balance_slot_probe')
        out['attempts'].append(proof)
        if val and val!='0x' and int(val,16)==sentinel:
            out['slot']=slot;break
    save(name,out)
    return out

def simulate(route,keys,amount,block,buy,slot=None,mixed=False):
    data=mixed_data(route,keys,amount,buy) if mixed else run_data(route,keys,amount,buy)
    tx={'from':CALLER,'to':EXECUTOR,'data':data,'gas':hex(15000000),'value':hex(amount if buy else 0),
        'maxFeePerGas':hex(10**9),'maxPriorityFeePerGas':hex(10**7)}
    overrides={CALLER:{'balance':hex(100*10**18)},EXECUTOR:{'code':runtime(mixed),'balance':'0x0'}}
    if not buy:
        if not slot:return {'status':'TOKEN_BALANCE_SLOT_UNRESOLVED'}
        overrides[route[0]['from_token']]={'stateDiff':{slot:'0x'+amount.to_bytes(32,'big').hex()}}
    raw,ev=req('eth_call',[tx,hex(block),overrides],'smartmoney_transfer_settlement')
    out={'block':block,'amount_raw':str(amount),'direction':'buy' if buy else 'sell','evidence':ev}
    if not raw or raw=='0x':return out|{'status':'SIMULATION_UNAVAILABLE_OR_REVERT'}
    parsed=decode(['uint256']*5+(['uint256[]'] if mixed else []),bytes.fromhex(raw[2:]))
    spent,received,gas,basefee,ts=parsed[:5]
    if mixed:out['per_leg_unspent_input_raw']=[str(x) for x in parsed[5]]
    # Some clients zero BASEFEE during zero-price eth_call. Restore the actual
    # historical header value instead of silently treating execution gas as free.
    header_ev=None
    if basefee==0:
        header,header_ev=req('eth_getBlockByNumber',[hex(block),False],'smartmoney_execution_block_header')
        basefee=int(header['baseFeePerGas'],16) if header and header.get('baseFeePerGas') else None
    intrinsic=21000+sum(4 if b==0 else 16 for b in bytes.fromhex(data[2:]))
    out.update(status='HISTORICAL_TRANSFER_SETTLED_SIMULATION',spent_raw=str(spent),received_raw=str(received),
        measured_internal_gas=gas,intrinsic_gas=intrinsic,base_fee_wei=basefee,base_fee_header_evidence=header_ev,timestamp=ts,
        gas_model_units=gas+intrinsic+5000,
        gas_model_note='measured internal execution + intrinsic calldata + explicit 5000 dispatcher/return buffer; not a mined receipt',
        simulation_context='hypothetical executor with empty hookData; no persistent state change')
    return out

def own_fees(route,keys,amount,block,buy,simulation,mixed=False):
    """Model own gas separately; never substitute the source trader's fee."""
    gas=simulation['gas_model_units'];tip=10**7
    if simulation['base_fee_wei'] is None:
        return {'total_fee_model_raw':None,'status':'HISTORICAL_BASE_FEE_UNAVAILABLE'}
    data=mixed_data(route,keys,amount,buy) if mixed else run_data(route,keys,amount,buy)
    unsigned=b'\x02'+rlp.encode([8453,0,tip,10**9,15000000,
        bytes.fromhex(EXECUTOR[2:]),amount if buy else 0,bytes.fromhex(data[2:]),[]])
    oracle='0x420000000000000000000000000000000000000f'
    calls=[(oracle,True,keccak(text='getL1Fee(bytes)')[:4]+encode(['bytes'],[unsigned])),
           (oracle,True,keccak(text='getOperatorFee(uint256)')[:4]+encode(['uint256'],[gas]))]
    cd='0x'+keccak(text='aggregate3((address,bool,bytes)[])')[:4].hex()+encode(['(address,bool,bytes)[]'],[calls]).hex()
    raw,ev=call('0xca11bde05977b3631167028862be2a173976ca11',cd,block,'smartmoney_own_fee_model')
    values=[]
    if raw and raw!='0x':
        try:values=decode(['(bool,bytes)[]'],bytes.fromhex(raw[2:]))[0]
        except Exception:pass
    parsed=[int.from_bytes(value,'big') if success and len(value)==32 else None for success,value in values]
    while len(parsed)<2:parsed.append(None)
    l2=gas*(simulation['base_fee_wei']+tip)
    return {'evidence':ev,'gas_model_units':gas,'priority_fee_assumption_wei':tip,
        'l2_fee_model_raw':str(l2),'l1_fee_model_raw':str(parsed[0]) if parsed[0] is not None else None,
        'operator_fee_model_raw':str(parsed[1]) if parsed[1] is not None else None,
        'total_fee_model_raw':str(l2+sum(parsed)) if all(x is not None for x in parsed) else None,
        'assumptions':'historical base fee; 0.01 gwei tip; measured gas+intrinsic+5000; unsigned type-2 nonce 0 L1 fee estimate; excludes deployment and adverse transaction ordering'}

STEP_TYPE='(uint8,address,'+KEY_TYPE+',bool)'
def mixed_data(route,keys,amount,buy):
    steps=[]
    for e in reversed(route) if buy else route:
        k=keys[e['id']] if e['kind']=='V4' else dict(token0=e['token0'],token1=e['token1'],fee=0,tick_spacing=0,hooks=ZERO)
        key=(k['token0'],k['token1'],k['fee'],k['tick_spacing'],k['hooks'])
        tin=e['to_token'] if buy else e['from_token']
        kind={'AERO_V2':5,'ALGEBRA':6}.get(e['kind'])
        if kind is None:kind=int(e['kind'][1:])
        steps.append((kind,ZERO if e['kind']=='V4' else e['id'],key,tin==k['token0']))
    sig=keccak(text='run('+STEP_TYPE+'[],uint128,bool)')[:4]
    return '0x'+sig.hex()+encode([STEP_TYPE+'[]','uint128','bool'],[steps,amount,buy and route[-1]['to_token']==WETH]).hex()

def probe():
    p=next(p for p in read('pilot_paths.json') if p.get('route') and all(e['kind']=='V4' for e in p['route']))
    keys=read('smartmoney_v4_keys.json')['keys']
    b=simulate(p['route'],keys,10**16,p['source_block']+5,True)
    out={'source_tx':p['source_tx'],'buy':b,'sell':None,'live_allowed':False}
    if b.get('received_raw') and int(b['received_raw'])>0:
        out['buy_fees']=own_fees(p['route'],keys,10**16,p['source_block']+5,True,b)
        slot=detect_balance_slot(p['token'],p['source_block']+43205)
        out['slot']=slot
        if slot['slot']:
            out['sell']=simulate(p['route'],keys,int(b['received_raw']),p['source_block']+43205,False,slot['slot'])
            if out['sell'].get('received_raw'):
                out['sell_fees']=own_fees(p['route'],keys,int(b['received_raw']),p['source_block']+43205,False,out['sell'])
    save('smartmoney_settlement_probe.json',out)
    print(json.dumps(out,indent=2))

def all_entries():
    rows=read('smartmoney_routes.json')['rows'];keys=read('smartmoney_v4_keys.json')['keys'];out=[]
    for i,r in enumerate(rows):
        name='smartmoney_settlement/'+r['source_tx']+'.json'
        if (ROOT/name).exists():
            old=read(name)
            if not has_throttle(old) and not needs_slot_repair(old):out.append(old);continue
            if needs_slot_repair(old):save('smartmoney_settlement_prior/'+r['source_tx']+'.json',old)
        item={k:r[k] for k in ('source_tx','wallet','token','source_block')}
        item.update(input_eth_raw=str(10**16),exit_policy='24h',delay_seconds=10,
                    platform_fee_status='NOT_SPECIFIED_SEPARATE_FROM_ONCHAIN_MODEL',
                    modeled_net_eth_raw=None,net_profit_certified=False)
        route=r['route']
        if not route:
            item['status']='ROUTE_UNRESOLVED'
        elif r['quote']!='ETH' or any(e['kind']!='V4' for e in route):
            item['status']='MIXED_PROTOCOL_ADAPTER_PENDING'
        else:
            entry_block=r['source_block']+5;exit_block=entry_block+43200
            buy=simulate(route,keys,10**16,entry_block,True);item['buy']=buy
            if not buy.get('received_raw') or int(buy['received_raw'])==0:
                item['status']='BUY_SIMULATION_UNRESOLVED_OR_ZERO'
            else:
                item['buy_fees']=own_fees(route,keys,10**16,entry_block,True,buy)
                slot=detect_balance_slot(r['token'],exit_block);item['slot_evidence']=slot
                sell=simulate(route,keys,int(buy['received_raw']),exit_block,False,slot['slot']);item['sell']=sell
                if not sell.get('received_raw'):
                    item['status']='SELL_SIMULATION_UNRESOLVED'
                elif int(sell['spent_raw'])!=int(buy['received_raw']):
                    item['status']='PARTIAL_EXIT_INVENTORY_REMAINS'
                else:
                    item['sell_fees']=own_fees(route,keys,int(buy['received_raw']),exit_block,False,sell)
                    profit=int(sell['received_raw'])-int(buy['spent_raw'])
                    item['settled_before_gas_pnl_raw']=str(profit)
                    fees=[item['buy_fees']['total_fee_model_raw'],item['sell_fees']['total_fee_model_raw']]
                    if all(f is not None for f in fees):
                        item['modeled_net_eth_raw']=str(profit-sum(map(int,fees)))
                        item['status']='CONDITIONAL_EXECUTION_COST_MODEL'
                    else:item['status']='SETTLEMENT_SIMULATED_FEE_COMPONENT_UNRESOLVED'
                    item['horizon_seconds_observed']=sell['timestamp']-buy['timestamp']
                    item['entry_delay_seconds_observed']=buy['timestamp']-r['source_ts']
        save(name,item);out.append(item)
        if (i+1)%10==0 or i+1==len(rows):
            save('smartmoney_settlement_progress.json',{'generated_at':now(),'done':i+1,'total':len(rows),
                 'statuses':dict(Counter(x['status'] for x in out))})
            print(json.dumps({'done':i+1,'total':len(rows),'statuses':dict(Counter(x['status'] for x in out))}),flush=True)
    result={'generated_at':now(),'n_frozen':len(rows),'rows':out,'statuses':dict(Counter(x['status'] for x in out)),
        'qualification':'CONDITIONAL_HISTORICAL_EXECUTION_MODEL_NOT_LIVE_PROFIT_PROOF',
        'remaining':['hypothetical executor deployment costs','propagating entry impact into later historical state',
                     'inclusion/MEV/slippage tolerance and failed attempts','portfolio cash/concurrency and fresh unseen regime',
                     'execution venue/platform fee not yet specified; not assumed to be zero',
                     'unsupported mixed protocols and hook contexts retained in denominator']}
    save('smartmoney_settlement_result.json',result)
    print(json.dumps(result['statuses']))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['probe','all']);a=p.parse_args()
    {'probe':probe,'all':all_entries}[a.command]()
