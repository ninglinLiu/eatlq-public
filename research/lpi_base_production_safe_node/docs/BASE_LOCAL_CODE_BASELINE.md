# Base Local Authoritative Code Baseline

```text
baseline_status = LOCAL_AUTHORITATIVE_GENERATED
authority_mode = local_requirement_derived
ready_for_authoritative_local_baseline = true
```

## Authority statement

This baseline was generated locally inside `/root/lpi_base_production_safe_node` from the Launchpad Intelligence Base Production-Safe Node requirements and data contracts.

It is **not** imported from a master-node Bundle. Compatibility with a future master-node Bundle must be re-validated when one arrives.

## Package layout

```text
launchpad_intelligence/
├── versions.py              # schema/adapter/manifest/bundle versions
├── config.py + config/defaults.json
├── schema/unknown.py
├── provenance/
├── envelopes/standard.py
├── registry/models.py
├── sources/base_source.py
├── query/preflight.py
├── lifecycle/machine.py
├── golden/validator.py
├── evidence/{manifest,bundle}.py
├── health/guard.py
├── security/{paths,secrets,network}.py
└── nodes/base/
    ├── adapters/{null_source,clickhouse_readonly}.py
    └── normalization/normalize.py
```

## Immutable versions

```text
schema_version = lpi.schema.v0.1.0
adapter_api_version = lpi.adapter_api.v0.1.0
manifest_version = lpi.evidence_manifest.v0.1.0
remote_bundle_version = lpi.remote_bundle.v0.1.0
```

`source_commit` / `source_tree_hash` are filled by the local Git commit created for this baseline (see `reports/BASE_LOCAL_BOOTSTRAP_RESULT.md`).

## Fail-closed defaults

```text
database_access_enabled = false
rpc_access_enabled = false
http_access_enabled = false
backfill_enabled = false
business_table_query_enabled = false
```

## Prototype handling

The 19 untracked files under `/root/quant_research_os/web/launchpad_intelligence/` were audited read-only for interface shape only.

```text
inventory = evidence/untrusted_prototype_inventory.tsv
inventory_sha256 = 90d66d5b4c1f2fcde67d5c7a497f5328e265d8598c3d22ffcafa25abf9571beb
```

Adopted concepts (re-implemented, not copied):

* Registry / ContractVersion separation;
* Lifecycle as an explicit state machine;
* Offline-first adapter purity.

Rejected / overridden vs prototype:

* Pump-style graduation-centric lifecycle as default for Clanker;
* Hardcoded network/RPC assumptions;
* Treating missing fields as empty strings instead of UNKNOWN;
* Any executable path that could open DB/RPC by default.

Conflict resolution: current task data contract and production safety boundary win.

## Dependencies

```text
runtime = CPython 3.12 stdlib only
third_party_dependencies = none
venv = runtime/venv (empty of project packages)
```
