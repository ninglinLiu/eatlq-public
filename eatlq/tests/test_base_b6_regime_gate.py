import importlib.util
import json
import unittest
from pathlib import Path

ROOT=Path('/opt/EATLQ')
OUT=ROOT/'mvp/diagnostics/base_b6_keystone_regime_gate_20260714.json'
GOLD=ROOT/'tests/fixtures/base_b6_golden_20260714.json'
P=ROOT/'scripts/research/base_b6/b6_regime_gate.py'
S=importlib.util.spec_from_file_location('b6',P); B=importlib.util.module_from_spec(S); S.loader.exec_module(B)


class B6GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x=json.loads(OUT.read_text())

    def test_research_only_zero_rpc(self):
        self.assertTrue(self.x['research_only'])
        self.assertEqual(self.x['rpc_ledger']['logical_calls'],0)
        self.assertEqual(self.x['rpc_ledger']['http_attempts'],0)
        self.assertEqual(self.x['go_decision'],'NOT_MADE')

    def test_preregistration_hash(self):
        self.assertEqual(self.x['frozen_preregistration_sha256'],B.sha(B.PREREG))
        self.assertEqual(self.x['frozen_preregistration_sha256'],'c0c913de56ca47c911c412d487e34edd75501458246099cfd8e7817c005217a0')

    def test_gate_does_not_use_regime_labels(self):
        self.assertFalse(self.x['causality']['gate_uses_W_labels'])
        self.assertIn('strictly before',self.x['causality']['weather_timestamp'])
        self.assertTrue(all(not r['used_by_gate'] for r in self.x['performance']['regime_acceptance_strata_not_gate_inputs']))

    def test_b5_always_on_exact_reproduction(self):
        got=self.x['performance']['overall']['always_on']
        b5=json.loads(B.B5.read_text())['performance']['keystone_full']['pooled']
        self.assertEqual(got['n'],b5['n'])
        self.assertAlmostEqual(got['mean'],b5['mean'],places=15)
        self.assertAlmostEqual(got['top1_removed_mean'],b5['top1_removed_mean'],places=15)

    def test_proxy_association_measured_only(self):
        a=self.x['proxy_validation']
        self.assertEqual(a['n'],1041)
        self.assertEqual(a['unknown_excluded_from_association'],224)
        self.assertEqual(a['confusion_matrix'],{'tp':302,'fp':419,'fn':4,'tn':316})
        self.assertAlmostEqual(a['phi'],0.41157791812270844)

    def test_zero_open_is_fail_not_flat_pass(self):
        self.assertEqual(self.x['capacity']['open_calendar_days'],0)
        self.assertEqual(self.x['capacity']['selected_opportunities'],0)
        self.assertIsNone(self.x['performance']['overall']['true_gate']['mean'])
        self.assertEqual(self.x['random_gate']['finite_draws'],0)
        rt=self.x['preregistered_right_tail']
        self.assertEqual(rt['evidence_status'],'FAIL')
        self.assertFalse(any(rt['gates'].values()))

    def test_rolling_and_w3_partial(self):
        self.assertEqual(len(self.x['performance']['rolling_14d_step_7d']),15)
        w3=[r for r in self.x['performance']['regime_acceptance_strata_not_gate_inputs'] if r['regime_id']=='W3'][0]
        self.assertTrue(w3['w3_partial'])

    def test_golden_exact(self):
        self.assertEqual(self.x['golden'],json.loads(GOLD.read_text()))


if __name__=='__main__': unittest.main()
