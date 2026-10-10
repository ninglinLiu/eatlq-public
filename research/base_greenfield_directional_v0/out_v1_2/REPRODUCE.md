# Base Greenfield V1.2 Canonical Repair Replay

Label: **HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC**. This is not sealed confirmation.

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v1_2.canonical_repair_replay 2>&1 | tee out_v1_2/RUN.log
```

Inputs remain read-only under `out/`, `out_v1/`, and `out_v1_1/`. Archive RPC A is read from `/tmp/rpc_url.txt`; an independent RPC B is read from `/tmp/rpc_url_b.txt` or `/tmp/rpc_url2.txt`. Local-fork transactions require `/root/.foundry/bin/anvil`.

The 2,399-row historical panel already contains all 480 holdout rows, so `CANONICAL_METADATA_ALL.parquet` has 2,399 unique signal rows, not 2,879 duplicated rows. Every row performs direct `token0/token1/fee/factory` calls at its decision block followed by canonical factory `getPool`.

Frozen execution: H=3600s, TB_A=+50%/-35%, amountIn=16666666666666666 wei, wallet=0.5 WETH, max_concurrent=3, executor=0x4b7cd953871663474a0a25b81bdddf5c1f1195a7, router=0x2626664c2603336e57b271c5c0b26f421741e481.

Gas debits and infrastructure retry schedule are inherited from the frozen V1 execution implementation: 0.0001 WETH per attempted leg and retry blocks [0,5,30,150] only for infrastructure-unknown sell calls.

Code SHA256: `68f094b44124a6e272e438fe069be07f5da0b3eba2fbedcbb02dbb68558aaf0b`  
Final verdict: `SELL_TRUTH_INCOMPLETE`  
Execution truth: `EXECUTION_TRUTH_INCOMPLETE`
