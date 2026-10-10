# Reproduce V1.1 Execution Truth Audit

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v1_1.execution_truth_audit
```

Requires archive RPC in `/tmp/rpc_url.txt`. Optional second archive: `/tmp/rpc_url_b.txt`.

**Immutability:** does not write to `out/` or `out_v1/`.

**Outputs:** `out_v1_1/` (see EXECUTION_TRUTH_MANIFEST.json for input SHA256).

**Label:** `HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC` — not a new sealed holdout confirmation.

**Verdict:** `MIXED_FAILURE`  
**G2:** DISCOVERY_LEAD retained; `kill_g2=False`; deploy forbidden.

Canonical reuse: if `out_v1_1/CANONICAL_POOL_AUDIT.parquet` exists it is reused; delete to force re-audit.
