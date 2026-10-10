# Hybrid Architecture

Source: `v2/hybrid-architecture.svg` · kind: `architecture`

**HYBRID ARCHITECTURE · RUST SHELL · PYTHON BRAIN · REDIS NERVE**

| Layer | Components | Role |
|-------|------------|------|
| Rust | Ingestor · Executor · Position Manager | IO-bound · WS · swap |
| Python | Strategy engine · Dashcam · label jobs | XGBoost · features |
| Redis | Streams bus · feat2 hash · state | halt · positions |

Footer: Docker Compose · laszlo-net · single-node vertical scale

