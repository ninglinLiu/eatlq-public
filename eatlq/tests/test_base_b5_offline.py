import importlib.util
import json
import unittest
from pathlib import Path

P=Path('/opt/EATLQ/scripts/research/base_b5/b5_offline_reverify.py')
S=importlib.util.spec_from_file_location('b5',P); B=importlib.util.module_from_spec(S); S.loader.exec_module(B)

class B5OfflineTest(unittest.TestCase):
    def test_regimes_do_not_overlap(self):
        spans=[(B.pd.Timestamp(x[0]),B.pd.Timestamp(x[1])) for x in B.REGIMES.values()]
        self.assertTrue(all(a[1] <= b[0] for a,b in zip(spans,spans[1:])))
    def test_direction_price_integer(self):
        s={'a0':10**15,'a1':-5*10**18}
        side,px,w,t=B.direction_price(s,-1)
        self.assertEqual((side,w,t),('buy',10**15,5*10**18)); self.assertEqual(px,2e-4)
    def test_missing_entry_is_adverse(self):
        class R: entry_block=10; orient=-1
        self.assertEqual(B.simulate(R(),[],{},2000)['ret'],-1.0375)
    def test_output_invariants_if_present(self):
        p=B.OUT
        if not p.exists(): self.skipTest('run analysis first')
        x=json.loads(p.read_text())
        self.assertEqual(x['rpc_calls'],0); self.assertTrue(x['research_only'])
        self.assertEqual(x['frozen']['gas_usd_per_attempted_leg'],.075)
        self.assertGreaterEqual(len(x['golden']),3)
        expected=json.loads(Path('/opt/EATLQ/tests/fixtures/base_b5_offline_golden_20260714.json').read_text())
        self.assertEqual(x['golden'],expected)
        for g in x['golden']:
            int(g['entry_amount0_raw']); int(g['entry_amount1_raw']); int(g['exit_amount0_raw']); int(g['exit_amount1_raw'])
        self.assertEqual(x['preregistered_right_tail']['evidence_status'],'FAIL')
        self.assertEqual(x['preregistered_right_tail']['go_decision'],'NOT_MADE')
        self.assertTrue(x['golden_archive_verification']['all_integer_equal'])
        lp=x['forward_lp_monitor_archive']['summary']
        self.assertEqual((lp['real_loss_n'],lp['prewarning_among_real_loss_n'],lp['rescue_proven_n']),(4,0,0))

if __name__=='__main__': unittest.main()
