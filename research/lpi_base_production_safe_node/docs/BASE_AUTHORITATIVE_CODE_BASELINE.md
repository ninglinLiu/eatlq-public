# Base Authoritative Code Baseline

```text
baseline_status = LOCAL_AUTHORITATIVE_GENERATED
source_directory_status = PACKAGE_AT_REPO_ROOT
```

See `docs/BASE_LOCAL_CODE_BASELINE.md` for the authoritative local package description.

## Authority form used this round

Local requirement-derived generation inside the isolated workspace, sealed by a local Git commit.

Accepted future upgrade forms remain:

1. repository/branch with an explicit immutable Git commit from a master node;
2. `git bundle` containing the declared commit;
3. `tar.zst` with an out-of-band SHA-256 and embedded Manifest/checksums.

## Versions

```text
schema_version = lpi.schema.v0.1.0
adapter_api_version = lpi.adapter_api.v0.1.0
evidence_manifest_version = lpi.evidence_manifest.v0.1.0
remote_bundle_version = lpi.remote_bundle.v0.1.0
python_version = 3.12.3
```

Commit/tree hashes are recorded in `reports/BASE_LOCAL_BOOTSTRAP_RESULT.md` after the sealing commit.

## Untrusted prototype inventory

```text
/root/lpi_base_production_safe_node/evidence/untrusted_prototype_inventory.tsv
inventory_sha256 = 90d66d5b4c1f2fcde67d5c7a497f5328e265d8598c3d22ffcafa25abf9571beb
entries = 19
```

Prototypes were read-only audited; designs were re-implemented. See `docs/BASE_PROTOTYPE_ADOPTION_LOG.md`.

## Import / readiness gates

```text
ready_for_authoritative_local_baseline = true
ready_for_offline_tests = true
ready_for_base_adapter_scaffolding = true
ready_for_authoritative_code_import = false   # master-node Bundle still absent
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
```
