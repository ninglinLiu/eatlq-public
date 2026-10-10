import json, unittest
from pathlib import Path

ROOT=Path('/opt/EATLQ'); D=ROOT/'mvp/diagnostics'

class RepairEvidenceTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.e=json.loads((D/'base_b7_execution_evidence_20260714.json').read_text())
  cls.w=json.loads((D/'base_b6_weather_repair_20260714.json').read_text())
  cls.c=json.loads((D/'base_b6_keystone_regime_gate_corrected_20260714.json').read_text())
  cls.h=json.loads((D/'base_b7_hybrid_reanalysis_20260714.json').read_text())
 def test_read_only_methods(self):
  self.assertEqual(set(self.e['rpc']['methods']),{'eth_call'})
  self.assertLessEqual(set(self.w['rpc']['methods']),{'eth_blockNumber','eth_getBlockByNumber','eth_getLogs','eth_call'})
 def test_credit_caps(self):
  self.assertLessEqual(self.e['rpc']['http_attempts'],self.e['rpc']['hard_cap_attempts'])
  self.assertLessEqual(self.w['rpc']['http_attempts'],self.w['rpc']['hard_cap_attempts'])
  self.assertEqual(self.e['rpc']['api_credits'],20*self.e['rpc']['http_attempts'])
  self.assertEqual(self.w['rpc']['api_credits'],20*self.w['rpc']['http_attempts'])
 def test_execution_cohorts(self):
  self.assertEqual(self.e['summary']['rows'],63)
  self.assertEqual(self.e['summary']['gap'],43)
  self.assertEqual(self.e['summary']['control'],20)
  self.assertEqual(self.e['summary']['gap_sellable_by_point']['entry'],43)
 def test_weather_continuity(self):
  self.assertEqual(self.w['summary']['days_with_events'],137)
  self.assertEqual(len(self.w['summary']['per_day']),137)
  self.assertGreater(self.w['summary']['factory_weth_events'],100000)
 def test_corrected_gate_is_mechanical_fail(self):
  self.assertEqual(len(self.c['weather_by_day']),102)
  self.assertEqual(sum(v['eligible'] for v in self.c['weather_by_day'].values()),94)
  self.assertEqual(sum(v['open'] for v in self.c['weather_by_day'].values()),0)
  self.assertEqual(self.c['preregistered_right_tail']['evidence_status'],'FAIL')
  self.assertTrue(all(v is False for v in self.c['preregistered_right_tail']['gates'].values()))
 def test_hybrid_is_exploratory_and_keeps_weak_w3_tail(self):
  self.assertEqual(self.h['rpc_calls'],0)
  self.assertEqual(self.h['status'],'EXPLORATORY_NOT_PREREGISTERED_CONFIRMATION')
  self.assertEqual(self.h['performance']['hybrid_trigger_proxy']['pooled']['n'],116)
  self.assertGreater(self.h['performance']['hybrid_trigger_proxy']['pooled']['mean'],0)
  self.assertLess(self.h['performance']['hybrid_trigger_proxy']['W3']['top1_removed_mean'],0)

if __name__=='__main__':unittest.main()
