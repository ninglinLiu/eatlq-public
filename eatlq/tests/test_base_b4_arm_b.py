import json, pathlib, unittest

ROOT=pathlib.Path('/opt/EATLQ'); SCRIPT=ROOT/'scripts/research/base_b4/b4_arm_b.py'; OUT=ROOT/'mvp/diagnostics/base_b4_arm_b_20260714.json'
class B4ArmBTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls): cls.d=json.loads(OUT.read_text())
 def test_census_exact(self):
  self.assertEqual(self.d['estimand']['sample_n'],1265);self.assertTrue(self.d['estimand']['full_census'])
  self.assertEqual(self.d['summary']['pooled']['instant_hp'],306)
  self.assertEqual(self.d['summary']['pooled']['unknown_adverse'],224)
 def test_h0a_conservative(self):
  self.assertEqual(self.d['H0a_status'],'INCONCLUSIVE_UNKNOWN_ADVERSE')
  self.assertEqual(self.d['summary']['pooled']['preregistered_disagreements'],0)
  self.assertGreater(self.d['summary']['pooled']['H0a_disagreement_identification_interval'][1],.05)
 def test_golden_integer_equal(self):
  self.assertGreaterEqual(len(self.d['golden'])+1,3)
  for x in self.d['golden']:
   self.assertTrue(x['raw_response_equal']);self.assertEqual(x['router_buy_out'],x['received_target']);self.assertEqual(x['router_sell_out'],x['recovered_weth'])
 def test_flip_exact(self):
  cases={x['name']:x for x in self.d['flip_before_after']}
  lp=cases['lp_pull_PARADAENGI_1']['points'];self.assertEqual(lp[0]['pool_weth_raw'],4061724635084328876);self.assertEqual(lp[1]['pool_weth_raw'],16)
  tt=cases['transfer_toggle_known_missed']['points'];self.assertTrue(tt[0]['qv2_reasonable']);self.assertEqual(tt[1]['recovered_weth'],0)
 def test_no_send_sign_broadcast_code(self):
  s=SCRIPT.read_text().lower()
  for bad in ('send_raw_transaction','sign_transaction','eth_sendrawtransaction','eth_sendtransaction','private_key'):
   self.assertNotIn(bad,s)
if __name__=='__main__':unittest.main()
