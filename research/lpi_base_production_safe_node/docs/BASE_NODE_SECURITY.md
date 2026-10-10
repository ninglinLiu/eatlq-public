# Base Node Security

Status: `PAUSED_LIVE_PRIORITY`

## Observed network surface

- Public listeners: TCP 22, 80, 443; UDP 443.
- Loopback listeners: 127.0.0.1:2019, 3000, 8090, and two Node-related ephemeral listeners; local DNS listeners.
- ClickHouse 8123/9000/9009, Postgres 5432, and Redis 6379 are visible only inside Docker metadata and are not host-published.
- No new port, firewall rule, route, network, or cross-VPS connection was created.

## Secret-preserving audit controls

- Did not read `/proc/*/environ`, `.env` contents, wallet files, keystores, SSH keys, shell history, API credentials, RPC URLs, database passwords, cookies, JWTs, or Authorization headers.
- Process command-line arguments were not collected. Only executable/comm names, PIDs, resource metadata, cwd from Docker metadata, and targeted Docker fields were used.
- Docker environment and full config were not inspected. Targeted fields excluded environment variables and command arguments.
- Crontab command bodies were not printed or stored; only active-entry counts were recorded.
- Existing log bodies were not read. Only file size and mtime metadata was sampled.
- No database, Redis, HTTP, RPC, WebSocket, exchange, wallet, or signing endpoint was contacted.

## Mutations performed

Only these new audit directories and Markdown reports were created under `/root/lpi_base_production_safe_node`. No file under `/opt/EATLQ`, `/var/lib/docker`, production volumes, production logs, or production checkpoints was intentionally written.

No package installation, Git checkout, code copy, Docker start/stop, service mutation, signal, debug/trace, schema change, cache operation, cron change, firewall change, or transaction operation occurred.

## Verifiable non-mutation evidence

- Production HEAD after audit: `90077ef020315e2490833c328d46941d6a24d996`.
- Production tracked-status hash after audit: `8d1870eba8d153a970722b9185c5da8ff48ee5ea171129e5ea048a3281fe3415` using `git status --porcelain=v1 --untracked-files=no | sha256sum`.
- All observed production container IDs remained identical and `Up` in the post-audit Docker listing.
- No production service restart time changed during the observation window.
- The command record in the phase report contains no write target outside the new audit root. Git status was an explicitly authorized Phase 0 read-only audit; Docker operations were metadata reads only.

Limitation: no filesystem-level pre-audit hash exists for every production file or volume, so the evidence supports the scoped commands actually executed; it is not a global proof against unrelated concurrent actors.
