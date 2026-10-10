# Base Required Authoritative Code Bundle Specification

Status: required before formal import

## Delivery options

Provide exactly one immutable delivery:

1. an approved repository URL/branch plus exact commit, with access explicitly authorized;
2. a `git bundle` containing the exact commit;
3. a `tar.zst` with embedded Manifest/checksums and an out-of-band expected SHA-256.

Do not deliver loose Python files.

## Required Manifest

The Manifest must contain non-empty, immutable values for:

```text
source_project
source_node_role
source_repository
source_branch
source_commit
source_tree_hash
created_at
schema_version
adapter_api_version
evidence_manifest_version
remote_bundle_version
python_version
dependency_lock_hash
test_count
test_result
bundle_sha256
```

`source_commit` must be a full commit hash. `source_tree_hash` must match that commit's root tree. All version fields must be exact, not `latest`. `test_result` must identify the command, offline/integration split, exit status, and source node environment. `bundle_sha256` must cover the delivered immutable artifact; its expected value must also arrive through a separate trusted message/channel.

## Minimum code payload

The delivery must include the master-node-compatible implementations and tests for:

- shared Schema/models and version declaration;
- Source Adapter protocol and capability contract;
- Registry and contract-version models;
- Evidence Manifest and Remote Bundle formats/exporter;
- Golden Validator with synthetic fixtures;
- Lifecycle state machine and UNKNOWN preservation;
- Base node extension point or documented adapter registration mechanism;
- access-default configuration proving database/RPC/business-table/backfill are disabled;
- dependency declaration plus fully resolved lock/hashes;
- license/build metadata and exact offline test command.

It must not include production data, logs, database volumes, `.env`, credentials, wallet material, order/position data, caches, venvs, build outputs, or unrelated project source.

## Archive safety

The artifact must contain no:

- absolute or parent-traversal paths;
- device, FIFO, socket, hard-link escape, or symlink escape;
- executable installer not explicitly declared and reviewed;
- Git submodule requiring unapproved network access;
- post-install hook that starts services or performs network/system mutation;
- Docker/systemd/cron/firewall/package-manager command;
- default production database/RPC/Redis endpoint or Secret read;
- fixed Robinhood-node filesystem path.

## Dependency evidence

Provide:

```text
dependency_count
download_size_estimate
installed_disk_size_estimate
native_build_dependencies
build_backend_and_hooks
approved_network_hosts
wheel_or_sdist_hashes
python_compatibility
```

Large native builds, system package requirements, Docker dependencies, unbounded installers, or global/user-site installation requirements are not acceptable for this node without separate approval.

## Receipt location and state transition

Place the artifact only under a new incoming evidence directory inside `/root/lpi_base_production_safe_node`. Receipt alone does not authorize execution. State becomes `ready_for_authoritative_code_import=true` only after artifact hash, Manifest, Git/tree, archive, dependency, path, Secret, offline tests, and pre/post live-health checks all pass.
