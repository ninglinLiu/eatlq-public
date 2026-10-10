import json, unittest
from analyze import disagreement, wilson

class B4Test(unittest.TestCase):
  def test_fee_on_transfer_is_disagreement(self):
    r=json.loads(open('golden.jsonl').readline()); self.assertEqual(disagreement(r), True)

  def test_good_is_not_disagreement(self):
    r=json.loads(open('golden.jsonl').read().splitlines()[1]); self.assertEqual(disagreement(r), False)

  def test_wilson_integer_exact_sample(self):
    lo,hi=wilson(0,1); self.assertEqual(lo, 0.0); self.assertGreater(hi, 0.0)

if __name__ == '__main__': unittest.main()
