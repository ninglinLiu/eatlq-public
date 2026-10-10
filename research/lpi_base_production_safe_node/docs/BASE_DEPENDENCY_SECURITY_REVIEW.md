# Base Dependency Security Review

Status: `BLOCKED_NO_AUTHORITATIVE_DEPENDENCY_INPUT`

## Required pre-install fields

```text
dependency_count = UNKNOWN
download_size_estimate = UNKNOWN
disk_size_estimate = UNKNOWN
native_build_dependencies = UNKNOWN
post_install_hooks = UNKNOWN
network_hosts = UNKNOWN
risk_assessment = BLOCK
```

These fields cannot be inferred from the untracked prototype. No authoritative `pyproject.toml`, requirements lock, Poetry/uv/PDM lock, hashes, build backend, or source Manifest was received.

## Actual installation in this round

No project dependency was installed. The isolated venv contains only:

```text
pip==24.0
```

The venv was created by Python 3.12.3 using bundled/offline `venv` behavior. It has `include-system-site-packages=false`, user site disabled, occupies approximately 16 MiB, and resides entirely under `runtime/venv/`.

Current actual values:

```text
project_dependency_count = 0
network_download_bytes = 0
network_hosts_contacted_for_install = 0
project_install_hooks_executed = 0
native_project_builds = 0
system_packages_changed = 0
global_site_packages_changed = 0
```

## Future audit policy

Before installation, reject or stop on:

- unpinned direct URLs, VCS dependencies, editable installs, or local paths outside the candidate;
- dependency files not covered by the declared lock hash;
- setup/install hooks not reviewed as source;
- build isolation that downloads undeclared build requirements;
- native compilation or large wheels without explicit size/resource estimates;
- package-manager, Docker, systemd, cron, firewall, database-client, or system-library changes;
- default network, database, Redis, RPC, or production path access;
- Secret/environment discovery outside explicitly supplied non-secret config;
- install commands that can affect system or user site packages.

`scripts/audit_bootstrap_candidate.py` provides a bounded, standard-library, offline first-pass scan. It is not a substitute for reviewing the authoritative lock, build backend, package metadata, and exact wheel sources.

## Installation gate

Project dependency installation remains prohibited. A future approved install must use the isolated venv, `PIP_NO_INDEX` until approved artifacts/hosts are known, one process/thread, low CPU/IO priority, a 384 MiB memory cap, and a 10-minute timeout.
