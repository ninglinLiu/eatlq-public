# Reproduce Base Greenfield V2 Phase 0

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v2.phase0_census 2>&1 | tee out_v2/RUN.log
```

Phase 0 trains no alpha model. Closed KEYSTONE/F10/G2/C3/P2 artifacts are not read. The only imported V1.2 code is generic RPC/state-override execution plumbing.

Freeze SHA256: `b0fa7b1b823fe28f1168e552e5b6ac6d5a87c880f1fc1edb4fda5bda807ca3e7`  
Code SHA256: `3ae296cc67ab0f9a9ef505fac74bf0b090269df815ecb5dbac5763fc4fe96762`  
Verdict: `NO_VIABLE_BASE_NEW_POOL_UNIVERSE`
