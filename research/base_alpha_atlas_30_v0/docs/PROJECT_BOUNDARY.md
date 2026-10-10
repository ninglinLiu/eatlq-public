# Project Boundary

```text
project_root = /root/base_alpha_atlas_30_v0
protected_live = /opt/EATLQ (+ eatlq-* containers)
sibling_research = /root/lpi_base_production_safe_node (do not merge)
```

## Hard isolation

- Write only under `project_root`.
- Do not modify `/opt/EATLQ`.
- Do not use production venv, `.env`, Redis prefixes, checkpoints, or log dirs.
- Do not start dashboard/web/cron/systemd/pm2 for this project.
- Prefer one-shot CLI, bounded batch, checkpointable jobs, offline parquet.
- `max_heavy_workers` initial = 2; never > 3.
- Per heavy process: nice=19, ionice idle, threads=1, mem<=384MiB, timeout<=20m.
