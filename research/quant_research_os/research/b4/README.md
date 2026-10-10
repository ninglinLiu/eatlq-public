# B4 offline reducer

`analyze.py` consumes JSONL exported from pinned archive-state `eth_call` runs and computes the preregistered disagreement table and Wilson 95% intervals. It never contacts an RPC. `golden.jsonl` is a synthetic regression fixture (addresses/block are placeholders, not chain evidence); replace it only with recorded archive results including calldata/return hashes.

Required evidence fields: group, kind, token, pool, archive block, q/b revert flags, q_tax, b_tax, b_weth_out, buy_target_received, RPC identifier, calldata hash and return hash. No B4 rate or GO decision is claimed until the required pinned sample is supplied.
