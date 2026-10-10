# Deferred until SAFE_TO_BEGIN_OFFLINE_RESEARCH = true

Per plan §21 and auto-pause rules, the following remain **not started**:

1. Stage 0 — 30 precise strategy cards  
2. Orthogonality first-pass matrix  
3. Shared panel **build** (`base_alpha_atlas_shared_panel.parquet`)  
4. Any Stage 1–5 compute  

Allowed now:

- Keep reading Phase -1–0 report  
- Optionally draft non-executing markdown notes that do not touch production  
- Re-run passive baseline when host is quieter  

Not allowed now:

- ClickHouse / Postgres research queries against production  
- Copying large production datasets (I/O risk) while CPU headroom is unstable  
- Starting workers / screen / tmux research daemons  
