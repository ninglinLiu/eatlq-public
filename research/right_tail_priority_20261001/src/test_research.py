"""Accounting, chronology, missingness and right-tail instrument regressions."""
import unittest
from unittest.mock import patch
from math import isclose
from eth_abi import encode
from eth_utils import keccak
from ledger import classify,WETH,USDC
from narrative import replay_group
from pilot import price_from_state
from v4_paths import decode_init,route_for,INIT,MANAGER
from amount_quotes import calldata,parse_quote,PARAM_TYPE,summarize
from eth_abi import decode
from smartmoney_routes import recover,pool_id
from smartmoney_settlement import run_data,mixed_data,STEP_TYPE,KEY_TYPE,B20_NAMESPACE,b20_balance_slot,needs_slot_repair
from smartmoney_expand_settlement import cash_account
from smartmoney_funding import attach_funding_leg,FUNDING_POOL
from smartmoney_gate_audit import source_authorization
from smartmoney_cash_schedule import cash_path
from pilot import TRANSFER,V4

class ResearchTests(unittest.TestCase):
    def test_cash_schedule_uses_chronology_not_positive_terminal_profit(self):
        events=[dict(timestamp=1,leg='buy',source_tx='a',cash_delta_raw='-100'),
                dict(timestamp=2,leg='buy',source_tx='b',cash_delta_raw='-100'),
                dict(timestamp=3,leg='sell',source_tx='a',cash_delta_raw='300')]
        result=cash_path(events)
        self.assertEqual(result['minimum_initial_quote_raw'],'200')
        self.assertEqual(result['ending_cash_delta_raw'],'100')
    def test_cash_schedule_same_timestamp_is_explicit_assumption(self):
        events=[dict(timestamp=1,leg='buy',source_tx='b',cash_delta_raw='-100'),
                dict(timestamp=1,leg='sell',source_tx='a',cash_delta_raw='100')]
        self.assertEqual(cash_path(events,True)['minimum_initial_quote_raw'],'100')
        self.assertEqual(cash_path(events,False)['minimum_initial_quote_raw'],'0')
    def test_future_exit_is_not_a_trade_failure(self):
        item={'buy':{'spent_raw':'100','received_raw':'1000'},'buy_fees':{'total_fee_model_raw':'2'}}
        with patch('smartmoney_expand_settlement.exit_not_observed',return_value=True), \
             patch('smartmoney_expand_settlement.read',return_value={'chain_timestamp':100}):
            out=cash_account(item,{'source_ts':10,'route':[{}]}, {},False)
        self.assertEqual(out['status'],'EXIT_HORIZON_NOT_YET_OBSERVED')
        self.assertEqual(out['scheduled_exit_timestamp'],86420)
        self.assertIsNone(out['modeled_net_eth_raw'])
        self.assertNotIn('sell',out)
    def test_source_authorization_is_not_copier_authorization(self):
        wallet='0x'+'12'*20;token='0x'+'13'*20;hook='0x'+'14'*20;pid=bytes.fromhex('15'*32)
        authorization=encode(['address','(address,bytes32,uint256,uint256,bytes)'],
            ['0x'+'00'*20,(wallet,pid,1000,123,b'\x01'*65)])
        selector=keccak(text='swapExactInput('+KEY_TYPE+',(bool,int256,uint160),bytes,uint256,uint256)')[:4]
        payload=selector+encode([KEY_TYPE,'(bool,int256,uint160)','bytes','uint256','uint256'],
            [('0x'+'00'*20,token,0,60,hook),(True,-123,4295128740),authorization,1,2000])
        out=source_authorization({'raw_input':'0x'+payload.hex()},wallet,'0x'+pid.hex())
        self.assertTrue(out['buyer_matches_source_wallet']);self.assertTrue(out['pool_id_matches_route'])
        self.assertEqual(out['signature_bytes'],65);self.assertFalse(out['copier_signature_available'])
    def test_funding_supplement_label_and_prior_metadata(self):
        path=[dict(from_token='target',to_token=USDC)]
        meta=dict(pool=FUNDING_POOL,fee=500,token0=WETH,token1=USDC,block_number=10)
        out=attach_funding_leg(path,meta,11)
        self.assertEqual(len(path),1)
        self.assertEqual(out[-1]['to_token'],WETH)
        self.assertIn('NOT_SOURCE_WITNESSED',out[-1]['route_origin'])
        with self.assertRaises(AssertionError):attach_funding_leg(path,meta,9)
    def test_intermediate_residual_is_not_full_net(self):
        item={'buy':{'spent_raw':'100','received_raw':'1000','timestamp':10,
                     'per_leg_unspent_input_raw':['0','5']},
              'sell':{'spent_raw':'1000','received_raw':'110','timestamp':86410,
                      'per_leg_unspent_input_raw':['0','0']},
              'buy_fees':{'total_fee_model_raw':'2'},'sell_fees':{'total_fee_model_raw':'3'}}
        out=cash_account(item,{'source_ts':0,'route':[{},{}]}, {},True)
        self.assertEqual(out['cash_recovered_after_modeled_chain_fees_less_entry_raw'],'5')
        self.assertEqual(out['status'],'INTERMEDIATE_INVENTORY_REMAINS')
        self.assertIsNone(out['modeled_net_eth_raw'])
    def test_partial_exit_cash_is_not_net_or_total_loss(self):
        item={'buy':{'spent_raw':'100','received_raw':'1000','timestamp':10},
              'sell':{'spent_raw':'200','received_raw':'15','timestamp':86410},
              'buy_fees':{'total_fee_model_raw':'2'},'sell_fees':{'total_fee_model_raw':'3'}}
        out=cash_account(item,{'source_ts':0,'route':[{}]}, {},False)
        self.assertEqual(out['target_inventory_remaining_raw'],'800')
        self.assertEqual(out['cash_recovered_after_modeled_chain_fees_less_entry_raw'],'-90')
        self.assertIsNone(out['modeled_net_eth_raw']);self.assertIsNone(out['residual_inventory_value_raw'])
        self.assertFalse(out['net_profit_certified'])
    def test_b20_namespace_is_official_erc7201_layout(self):
        self.assertEqual(hex(B20_NAMESPACE),'0xc78b71fee795ddd74aff64ea9b2474194c938c3196430e10bb5f01ed48434000')
        holder='0x'+'01'*20
        self.assertEqual(b20_balance_slot(holder),'0x'+keccak(encode(['address','uint256'],[holder,B20_NAMESPACE+4])).hex())
    def test_b20_unresolved_old_adapter_retried_not_failure(self):
        row={'status':'SELL_SIMULATION_UNRESOLVED','token':'0xb2000000000000000000008ee54d63459aa97f01','slot_evidence':{'slot':None}}
        self.assertTrue(needs_slot_repair(row))
        row['slot_evidence']['detector_version']=2
        self.assertFalse(needs_slot_repair(row))
    def test_mixed_calldata_reverses_all_legs_not_token_order(self):
        a='0x'+'01'*20;b='0x'+'02'*20;p='0x'+'03'*20
        k={'token0':a,'token1':b,'fee':3000,'tick_spacing':60,'hooks':'0x'+'00'*20}
        rr=[dict(id='key',kind='V4',from_token=a,to_token=b),
            dict(id=p,kind='V3',from_token=b,to_token=WETH,token0=b,token1=WETH)]
        data=mixed_data(rr,{'key':k},100,True)
        steps,amount,wrap=decode([STEP_TYPE+'[]','uint128','bool'],bytes.fromhex(data[10:]))
        self.assertEqual([s[0] for s in steps],[3,4]);self.assertEqual([s[3] for s in steps],[False,False])
        self.assertEqual(amount,100);self.assertTrue(wrap)
    def test_airdrop_is_not_purchase(self):
        self.assertNotEqual(classify({'target':100},0)['status'],'SINGLE_TARGET_OPPOSITE_ACCOUNT_CASH')
    def test_wrap_is_not_purchase(self):
        self.assertNotEqual(classify({WETH:10},-10)['status'],'SINGLE_TARGET_OPPOSITE_ACCOUNT_CASH')
    def test_native_buy_and_sell(self):
        self.assertEqual(classify({'target':100},-10)['side'],'buy')
        self.assertEqual(classify({'target':-100},11)['side'],'sell')
    def test_partial_refund_changes_cost(self):
        self.assertEqual(classify({'target':100},-10+2)['quote_net_raw'],'-8')
    def test_multiple_targets_unresolved(self):
        self.assertTrue(classify({'a':1,'b':2},-3)['status'].startswith('UNRESOLVED'))
    def test_usdc_does_not_become_eth(self):
        self.assertEqual(classify({'a':1,USDC:-25000000},0)['quote'],'USDC')
    def test_quote_orientation_and_decimal_cancellation(self):
        raw='0x'+(2*(2**96)).to_bytes(32,'big').hex()
        self.assertEqual(price_from_state('V3',raw,True),4)
        self.assertEqual(price_from_state('V3',raw,False),.25)
    def test_zero_reserves_unknown_not_total_loss(self):
        self.assertIsNone(price_from_state('V2','0x'+'00'*96,True))
    def test_v2_ratio(self):
        raw='0x'+encode(['uint112','uint112','uint32'],[10,30,0]).hex()
        self.assertEqual(price_from_state('V2',raw,True),3)
    def test_no_missing_competitor_zero_fill(self):
        rr=[{'contract_id':'a','chain':'bsc','pair_address':'pa','address':'a','pair_created_at_unix':'0'},
            {'contract_id':'b','chain':'bsc','pair_address':'pb','address':'b','pair_created_at_unix':'0'}]
        series={('bsc','pa','a'):{0:[0,1,1,1,1,1000]}}
        self.assertEqual(replay_group(rr,series)['signals'],[])
    def test_completed_hour_and_left_censor(self):
        rr=[{'contract_id':c,'chain':'bsc','pair_address':'p'+c,'address':c,'pair_created_at_unix':'0'} for c in ('a','b')]
        series={('bsc','p'+c,c):{0:[0,1,1,1,1,v],7200:[7200,1,1,1,1,500],10800:[10800,2,2,2,2,500]} for c,v in [('a',900),('b',100)]}
        r=replay_group(rr,series)['signals'][0]
        self.assertEqual(r['available_at'],3600)
        self.assertEqual(r['status'],'LEFT_CENSORED_OR_WINNER_SWITCH')
        self.assertEqual(r['returns']['1']['winner_gross'],1)
    def test_v4_key_verified(self):
        a='0x'+'01'*20;b='0x'+'02'*20;h='0x'+'00'*20
        pid='0x'+keccak(encode(['address','address','uint24','int24','address'],[a,b,3000,60,h])).hex()
        log={'address':MANAGER,'topics':[INIT,pid,'0x'+a[2:].rjust(64,'0'),'0x'+b[2:].rjust(64,'0')],
             'data':'0x'+encode(['uint24','int24','address','uint160','int24'],[3000,60,h,2**96,0]).hex(),
             'blockNumber':'0x1','transactionHash':'tx'}
        self.assertTrue(decode_init(log)['pool_key_hash_verified'])
    def test_multihop_route_preserves_currency(self):
        r={'token':'a','quote':'ETH','block_number':1,'swap_logs':[{'kind':'V4','topics':['sig','p1']},{'kind':'V4','topics':['sig','p2']}]}
        keys={'p1':{'token0':'a','token1':'b'},'p2':{'token0':WETH,'token1':'b'}}
        path=route_for(r,keys,{})
        self.assertEqual([e['token0_is_from'] for e in path],[True,False])
    def test_no_synthetic_route_through_unseen_pool(self):
        r={'token':'a','quote':'ETH','block_number':1,'swap_logs':[]}
        self.assertIsNone(route_for(r,{'p':{'token0':'a','token1':WETH}},{}))
    def test_tail_concentration_not_automatic_failure(self):
        returns=[-.9]*9+[10.0]
        self.assertLess(sum(v>0 for v in returns)/len(returns),.5)
        self.assertGreater(sum(returns),0)
    def test_amount_quote_directions_and_amount(self):
        a='0x'+'01'*20;b='0x'+'02'*20
        key={'token0':a,'token1':b,'fee':3000,'tick_spacing':60,'hooks':'0x'+'00'*20}
        for token,direction in [(a,True),(b,False)]:
            data=calldata(key,token,10**16)
            params=decode([PARAM_TYPE],bytes.fromhex(data[10:]))[0]
            self.assertEqual(params[1],direction)
            self.assertEqual(params[2],10**16)
            self.assertEqual(params[3],b'')
        with self.assertRaises(ValueError):calldata(key,'not-a-pool-token',1)
    def test_amount_quote_parse_zero_and_missing(self):
        self.assertIsNone(parse_quote('0x'))
        out=parse_quote('0x'+encode(['uint256','uint256'],[0,123]).hex())
        self.assertEqual(out['amount_out_raw'],'0')
        self.assertEqual(out['quoter_gas_units'],123)
    def test_amount_quote_summary_preserves_missing_and_is_not_net(self):
        rows=[{'input_quote_raw':str(10**16),'exits':{'24h':{'pool_quote_return':-.5}}},
              {'input_quote_raw':str(10**16),'exits':{}}]
        s=next(s for s in summarize(rows) if s['exit_policy']=='24h')
        self.assertEqual(s['n_frozen'],2)
        self.assertEqual(s['n_unresolved'],1)
        self.assertEqual(s['pool_quote_mean_return'],-.5)
        self.assertIsNone(s['net_pnl'])
    def test_pool_key_recovery_requires_full_hash(self):
        a='0x'+'01'*20;b='0x'+'02'*20;hook='0x'+'00'*20
        key={'token0':a,'token1':b,'fee':3000,'tick_spacing':60,'hooks':hook}
        pid=pool_id(key)
        log={'address':MANAGER,'topics':[V4,pid],
             'data':'0x'+encode(['int128','int128','uint160','uint128','int24','uint24'],[-10,20,2**96,100,0,3000]).hex()}
        rec={'logs':[log,{'address':a,'topics':[TRANSFER]},{'address':b,'topics':[TRANSFER]}],
             'blockNumber':'0x1','transactionHash':'tx'}
        result=recover(rec,{})
        self.assertEqual(pool_id(result[pid]),pid)
        log['address']='0x'+'99'*20
        self.assertEqual(recover(rec,{}),{})
    def test_settlement_buy_and_sell_directions(self):
        a='0x'+'01'*20;k={'token0':a,'token1':WETH,'fee':3000,'tick_spacing':60,'hooks':'0x'+'00'*20}
        route=[{'id':'p','kind':'V4','from_token':a,'to_token':WETH}]
        for buy,direction in [(True,False),(False,True)]:
            data=run_data(route,{'p':k},100,buy)
            values=decode([KEY_TYPE+'[]','bool[]','uint128','bool'],bytes.fromhex(data[10:]))
            self.assertEqual(values[1],(direction,))
            self.assertEqual(values[2],100)
            self.assertEqual(values[3],buy)

if __name__=='__main__':unittest.main(verbosity=2)
