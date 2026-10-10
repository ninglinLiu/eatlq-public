import hashlib,json,unittest
from pathlib import Path

ROOT=Path("/opt/EATLQ");D=ROOT/"mvp/diagnostics"
def load(p):return json.loads(Path(p).read_text())

class Reaudit10Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.final=load(D/"base_reaudit10_final_20260715.json");cls.core=load(D/"base_reaudit10_seconds_rpc_20260715.json");cls.b4=load(D/"base_reaudit10_b4_health_rpc_20260715.json");cls.rug=load(D/"base_reaudit10_rug_five_evidence_rpc_20260715.json");cls.toggle=load(D/"base_reaudit10_transfer_toggle_probe_20260715.json");cls.gold=load(ROOT/"tests/fixtures/base_reaudit10_golden_20260715.json")
 def test_preregistration_hash_and_posture(self):
  p=ROOT/"configs/research/base_reaudit10_seconds_preregistration_20260715.json";h=hashlib.sha256(p.read_bytes()).hexdigest();self.assertEqual(h,"c942e7f286f9dda5cc384f0c91f4e98172d2e6307fd80e66869817580ca2534b");self.assertEqual(self.final["preregistration_sha256"],h);self.assertEqual(self.final["go_decision"],"NOT_MADE")
 def test_locked_universes_and_offsets(self):
  self.assertEqual(self.core["summary"]["rows"],431);self.assertEqual(self.core["summary"]["selected"],116);self.assertEqual(self.core["offset_blocks"],[1,3,5,10,15]);self.assertEqual(self.b4["summary"]["rows"],1265);self.assertEqual(len(self.rug["rows"]),262)
 def test_rpc_methods_are_read_only(self):
  allowed={"eth_blockNumber","eth_call","eth_getBlockByNumber","eth_getLogs","eth_getTransactionReceipt","debug_traceTransaction"};self.assertLessEqual(set(self.final["rpc_ledger"]["rpc"]["methods"]),allowed)
 def test_latency_frontier_and_unknown_policy(self):
  self.assertEqual(self.final["keystone_state"],"活-待真钱延迟")
  for x in self.final["exit_latency_budget_frontier"]:
   self.assertGreater(x["pooled"]["mean"],0);self.assertGreater(x["pooled"]["top1_removed_mean"],0);self.assertIn(x["status"],("PASS","INCONCLUSIVE"));self.assertNotIn("UNRESOLVED_EXIT",("CONFIRMED_LP_RUG","TRANSFER_RESTRICTION_RUG"))
 def test_w2_and_h0a(self):
  a=self.final["b4_health"]["ages"];self.assertEqual(a["2m"]["windows"]["W2"]["instant_hp"],304);self.assertEqual(a["2m"]["windows"]["W2"]["n"],408);self.assertEqual(a["10m"]["windows"]["W2"]["instant_hp"],311);self.assertEqual(a["2m"]["H0a_measured_disease_n"],306);self.assertEqual(a["2m"]["H0a_unknown_disease_n"],2);self.assertEqual(a["2m"]["qv2_reasonable_atomic_severe_disagreement"],0)
 def test_d5_weather_b3(self):
  self.assertEqual(self.final["d5_marginal"]["1:1"]["blocked"],0);self.assertEqual(self.final["d5_marginal"]["1:1"]["mean_delta"],0);self.assertEqual(self.final["weather_gate"]["1"]["derived_gate"]["n"],0);self.assertFalse(self.final["weather_gate"]["1"]["beats_random_p95"]);self.assertEqual(self.final["b3_frozen"]["groups"]["real_signal"]["W2"]["n"],0);self.assertEqual(self.final["b3_frozen"]["groups"]["real_signal"]["W3"]["n"],0)
 def test_rug_classes(self):
  c=self.final["rug_adjudication"]["counts"];self.assertEqual(sum(c.values()),262);self.assertEqual(c["CONFIRMED_LP_RUG"],235);self.assertGreaterEqual(c["UNRESOLVED"],1)
 def test_integer_goldens(self):
  g=self.gold["healthy_roundtrip"];row=next(r for r in self.b4["rows"] if r["pool_address"]==g["pool"]);p=next(x for x in row["points"] if x["block"]==g["block"]);self.assertEqual(p["block_evidence"]["hash"],g["block_hash"]);self.assertEqual(p["atomic"]["received_target"],g["received_target"]);self.assertEqual(p["atomic"]["recovered_weth"],g["recovered_weth"]);self.assertTrue(p["atomic"]["buy_integer_equal"]);self.assertTrue(p["atomic"]["sell_integer_equal"])
  t=self.gold["transfer_toggle_boundary"];row=next(r for r in self.toggle["rows"] if r["pool"]==t["pool"]);self.assertEqual(row["first_bad_block"],t["first_bad_block"]);self.assertTrue(row["points"][0]["atomic"]["sell_ok"]);self.assertFalse(row["points"][1]["atomic"]["buy_ok"])

if __name__=="__main__":unittest.main()
