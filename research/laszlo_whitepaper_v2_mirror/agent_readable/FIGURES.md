# Figure transcription index


Informational SVGs → Markdown tables/flows. Decorative PNGs → visual note only.


## Cover · Pulse Channel

- Asset: `v2/pulse-channel-whitepaper.png`
- Kind: `decorative`

> Decorative cover. No readable text/tables.
>
> Visual: dark tech scene; golden stylized "A"/pulse logo; floating dashboard (line/bar/donut charts); Bitcoin-node pedestals on a grid.


---


## Research Discipline

- Asset: `v2/research-discipline-tri-public.svg`
- Kind: `diagram`

**RESEARCH DISCIPLINE**

| # | Pillar | Meaning |
|---|--------|---------|
| 01 | Traceable Signals | Every decision auditable; Replay before scale |
| 02 | Model Restraint | Simulate when uncertain; No confidence without data |
| 03 | Risk First | Pre-trade checks; Automatic de-risking |


---


## LASZLO Iteration Loop (Phase 6 RL-style)

- Asset: `v2/rl-iteration-loop.svg`
- Kind: `flow`

**PHASE 6 — RL-STYLE ITERATION** (center gate: EDGE / 72% P)

```text
Ingest (soak / dashcam)
  → Label (tri / QC)
    → Train (holdout gate)
      → Paper (no BUY if below thr)
        → Ledger (replay / audit)
          → Diagnose (edge / worst)
            → (loop back)
```


---


## Retail Status Quo vs LASZLO

- Asset: `v2/retail-vs-institutional.svg`
- Kind: `comparison`

**RETAIL STATUS QUO vs LASZLO INSTITUTIONAL STANDARD**

| Dimension | RETAIL | LASZLO |
|-----------|--------|--------|
| Data filter | Noisy transfers · 15s polling | Blacklist + whale USD filter |
| Ingest latency | (tabs / polling) | WS ingest · Redis Streams · <20ms |
| Tooling | Nansen + TV + Uniswap tabs | Ingest → Strategy → Executor loop |
| MEV / gas | Public mempool · MEV sandwich | Base L2 gas · 3% slippage hard cap |
| Exits | Manual stop · forgot positions | Trailing · Time · Hard stop (chain) |
| Form factor | Fragmented tools · context loss | Single-tenant weapon system |


---


## Alpha vs Beta (conceptual)

- Asset: `v2/alpha-beta-split.png`
- Kind: `decorative`

> Decorative split panel. No readable text/tables.
>
> Left (gold): waveform → network/graph nodes (signal / model side).
> Right (teal): candlestick chart + icons (people / database / institution) — market & beta context.


---


## Hybrid Architecture

- Asset: `v2/hybrid-architecture.svg`
- Kind: `architecture`

**HYBRID ARCHITECTURE · RUST SHELL · PYTHON BRAIN · REDIS NERVE**

| Layer | Components | Role |
|-------|------------|------|
| Rust | Ingestor · Executor · Position Manager | IO-bound · WS · swap |
| Python | Strategy engine · Dashcam · label jobs | XGBoost · features |
| Redis | Streams bus · feat2 hash · state | halt · positions |

Footer: Docker Compose · laszlo-net · single-node vertical scale


---


## LASZLO Data Pipeline

- Asset: `v2/pipeline-flow.svg`
- Kind: `flow`

**LASZLO DATA PIPELINE**

```text
Base L2 (Alchemy WS/HTTP)
  → Ingestor
    → market_ticks (Redis Streams)
      → Strategy (XGBoost v3)
        → Executor (swap + ledger)
          → Ledger: trade_ledger.jsonl + dashcam.csv
```

- Phase 6: label → train → holdout · Pe ≥ 72%
- Risk gates: stale head · drawdown · SELL_ONLY · `fire_thr = max(model, 0.68)`


---


## Infrastructure Stack v2

- Asset: `v2/infra-stack-layers.svg`
- Kind: `layers`

**INFRASTRUCTURE STACK · V2**

| Layer | Name | Contents |
|-------|------|----------|
| 4 | Brain & Hand | Python Strategy · Rust Executor · PM · Streamlit; XGBoost · exactInputSingle · slot0() stops |
| 3 | Vault | Postgres wallet profiles · ClickHouse paused; Dashcam CSV for offline label / retrain |
| 2 | Synapse | Redis Streams · market_ticks · signals; feat2:{symbol} · positions:* · halt keys |
| 1 | Interceptor | Rust Ingestor · eth_subscribe logs; V3 Swap decode · MsgPack · WsPool RPC |


---


## Base L2 Execution (isometric)

- Asset: `v2/base-l2-execution-isometric.png`
- Kind: `decorative`

> Decorative isometric diagram. No readable text/tables.
>
> Flow metaphor: input node → layered data stack (Uniswap/unicorn + AI/robot) → cluster cylinders → green shield gates → dispersed output.


---


## Tick Lifecycle Timeline

- Asset: `v2/tick-lifecycle-timeline.svg`
- Kind: `timeline`

**TICK LIFECYCLE · BASE L2 MEASURED PATH**

| Time | Event |
|------|--------|
| T+0 | Swap on-chain |
| +6ms | Redis XADD |
| +15ms | Signal out |
| +20ms | Tx submit |
| +2s | Sequencer confirm |
| after | open_position · slot0() monitor |


---


## ML Loop · Signal to Deployment

- Asset: `v2/ml-pipeline-loop-public.svg`
- Kind: `loop`

**ML LOOP · SIGNAL TO DEPLOYMENT**

| Step | Action |
|------|--------|
| Collect | live signals |
| Prepare | clean dataset |
| Label | outcome verify |
| Train | out-of-sample |
| Deploy | gated release |
| Monitor | review loop |

> Models graduate only after independent validation — never by lowering standards.


---


## BUY vs SELL Asymmetry

- Asset: `v2/buy-sell-asymmetry.svg`
- Kind: `comparison`

**EXECUTION ASYMMETRY · BUY CONSERVATIVE / SELL FAIL-OPEN**

| BUY path (fail-closed triggers) | SELL path (fail-open) |
|--------------------------------|------------------------|
| stale RPC head | intent_id dedup (SET NX) |
| daily drawdown breach | entry_token_amount cap |
| fire_thr < max(model, 0.68) | not blocked by stale head |
| balance RPC failure | pending_sell TTL self-heal |

Shared: single on-chain exit = `rust_executor` only · PM emits signal → executor swaps


---


## Triple Stop Loss

- Asset: `v2/triple-stop-loss.svg`
- Kind: `params`

**POSITION MANAGER · TRIPLE STOP**

| Stop | Param | Rule |
|------|-------|------|
| Trailing Stop | 5% | from peak · while in profit → auto SELL signal |
| Time Stop | 30m | flat ±2% range |
| Hard Stop | 15% | unconditional loss cap |


---


## Delivery Roadmap

- Asset: `v2/phase-roadmap-public.svg`
- Kind: `roadmap`

**DELIVERY ROADMAP**

| Phase | Name | Status signal |
|-------|------|---------------|
| P1 | Foundation | shipped path |
| P2 | Ingestion | shipped path |
| P3 | Strategy | shipped path |
| P4 | Execution | shipped path |
| P5 | Risk stack | shipped path |
| Now | Alpha calibration | in progress |

> Shipped: end-to-end pipeline on Base L2 · In progress: model calibration under real market data


---


## V1 Blueprint → V2 Battle-tested

- Asset: `v2/v1-v2-evolution.svg`
- Kind: `table`

**V1 BLUEPRINT → V2 BATTLE-TESTED**

| Dimension | V1.0 | V2.0 |
|-----------|------|------|
| Chain | Ethereum Mainnet | Base L2 (OP Stack) |
| MEV | Flashbots Bundle | EIP-1559 Gas + 3% slippage cap |
| Honeypot | Concept only | Quoter V2 eth_call simulate |
| Stops | *(row incomplete in SVG; v2 has)* | slot0() every 2s · triple stop |
| ML | Synthetic XGBoost | v1 synth → v2 labeled auto-switch |
| Ingest | pendingTransactions | eth_subscribe logs · V3 Swap ABI |
| Features | ClickHouse / Redis ZSET | feat2:{symbol} Redis Hash buckets |
| Drawdown | Manual kill switch | Auto 10% daily halt (UTC reset) |


---


## V1 Cover

- Asset: `v1/cover.png`
- Kind: `decorative`

> Decorative cover. No readable text/tables.
>
> Visual: neon "A"/pulse logo; abstract dashboard; circuit floor nodes.


---
