import TableOfContents from "@/components/TableOfContents";

export default function Home() {
  return (
    <div className="min-h-screen bg-paper">
      {/* Top nav bar */}
      <header className="sticky top-0 z-50 bg-paper/95 backdrop-blur-sm border-b border-border">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span
              className="w-6 h-6 rounded-sm flex items-center justify-center text-white text-xs font-bold"
              style={{ background: "#C85A00" }}
            >
              E
            </span>
            <span className="font-semibold text-sm text-ink tracking-tight">
              EATLQ
            </span>
            <span className="text-border text-sm">|</span>
            <span className="text-muted text-sm hidden sm:block">
              Engineering Portfolio
            </span>
          </div>
          <div className="flex items-center gap-4 text-xs text-muted font-mono">
            <span>v2.0</span>
            <span>May 2026</span>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-12 lg:flex lg:gap-16">
        {/* Sidebar TOC — desktop only */}
        <aside className="hidden lg:block w-52 shrink-0">
          <div className="sticky top-20">
            <TableOfContents />
          </div>
        </aside>

        {/* Main content */}
        <main className="min-w-0 max-w-3xl flex-1">
          {/* Hero */}
          <div className="mb-14">
            <div className="flex flex-wrap items-center gap-2 mb-5">
              <span className="phase-badge phase-done">Engineering Report</span>
              <span className="phase-badge phase-active">Web3 / DeFi</span>
              <span className="phase-badge phase-active">ML Systems</span>
              <span className="phase-badge phase-planned">Base L2</span>
            </div>
            <h1
              className="text-4xl sm:text-5xl font-semibold tracking-tight leading-tight mb-4"
              style={{ fontFamily: "Inter, system-ui, sans-serif", color: "#1A1A1A" }}
            >
              EATLQ: On-Chain DeFi
              <br />
              Intelligence System
            </h1>
            <p className="text-lg text-muted leading-relaxed max-w-2xl" style={{ fontFamily: "Georgia, serif" }}>
              A transparent engineering report on designing and operating a
              production-grade, ML-driven on-chain trading intelligence system
              targeting Base L2 DeFi markets. Written for technical review and
              portfolio reference.
            </p>
            <div
              className="mt-6 flex flex-wrap gap-6 text-sm text-muted border-t border-border pt-5"
              style={{ fontFamily: "Inter, sans-serif" }}
            >
              <div>
                <span className="font-medium text-ink">Author</span>
                <br />
                EATLQ Engineering Team
              </div>
              <div>
                <span className="font-medium text-ink">Base System</span>
                <br />
                LASZLO v2.0 (Franklin Nexus)
              </div>
              <div>
                <span className="font-medium text-ink">Chain</span>
                <br />
                Base L2 (OP Stack)
              </div>
              <div>
                <span className="font-medium text-ink">Date</span>
                <br />
                May 2026
              </div>
            </div>
          </div>

          <hr className="section-rule" />

          {/* ABSTRACT */}
          <section id="abstract" className="prose-paper scroll-mt-24">
            <h2>Abstract</h2>
            <p>
              This document provides a transparent account of building and
              extending a multi-service, real-time on-chain trading intelligence
              system. The system ingests Ethereum-compatible on-chain swap events
              via a Rust-based ingestor, computes rolling market features through
              a Python strategy engine, scores trade opportunities with an
              XGBoost ML model, and executes swaps on Uniswap V3 (Base L2) with
              integrated pre-trade risk controls.
            </p>
            <p>
              The base architecture — known internally as LASZLO — was designed
              by Franklin Nexus. The EATLQ team extended it with a meme token
              intelligence layer, ClickHouse analytics views, enhanced wallet
              profiling, an Alpha Research SOP framework, and a multi-agent
              AI-assisted development workflow. This report is honest about both
              what worked and what failed.
            </p>
            <div className="callout">
              <strong>Scope note:</strong> This document covers architecture,
              engineering methodology, and empirical system performance. It
              intentionally omits specific profitable signal parameters and
              proprietary Alpha strategies. The goal is accurate technical
              disclosure, not marketing.
            </div>

            {/* Key metrics */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 my-8">
              {[
                { value: "~25ms", label: "Target ingest latency" },
                { value: "5", label: "Microservices" },
                { value: "4+1", label: "Feature dimensions" },
                { value: "~2 mo", label: "Development cycle" },
              ].map((m) => (
                <div key={m.label} className="metric-card">
                  <div className="metric-value">{m.value}</div>
                  <div className="metric-label">{m.label}</div>
                </div>
              ))}
            </div>
          </section>

          <hr className="section-rule" />

          {/* SYSTEM ARCHITECTURE */}
          <section id="architecture" className="prose-paper scroll-mt-24">
            <h2>1. System Architecture</h2>
            <p>
              The system follows a heterogeneous, service-oriented design
              principle:{" "}
              <strong>Rust for latency-critical I/O boundaries</strong>,{" "}
              <strong>Python for ML inference and strategy logic</strong>, and{" "}
              <strong>Redis as the real-time nervous system</strong> connecting
              all services. The entire stack runs on a single Docker Compose
              host — a deliberate "MVP-first" choice that prioritises operational
              simplicity over horizontal scalability while the system is still in
              active research.
            </p>

            <h3>1.1 Service Topology</h3>

            <div className="arch-diagram my-6">
              <pre className="bg-transparent p-0 m-0 text-inherit font-mono text-xs sm:text-sm leading-relaxed overflow-x-auto">{`  ┌─────────────────────────────────────────────────────┐
  │                   Base L2 (OP Stack)                │
  │              Alchemy WebSocket / HTTP RPC           │
  └──────────────────────┬──────────────────────────────┘
                         │ eth_subscribe("logs")
                         ▼
          ┌──────────────────────────┐
          │   ingestor  [Rust]       │  ← Swap ABI decode
          │   PoolCache / Registry   │    USD estimation
          └──────────┬───────────────┘    MsgPack encode
                     │  XADD  market_ticks
                     ▼
          ┌──────────────────────────┐
          │   Redis Streams          │  ← message_ticks
          │   + Feature Store ZSET   │    signals
          └──────┬───────────────────┘    signals_dead_letter
                 │
     ┌───────────┴───────────┐
     ▼                       ▼
┌─────────────┐    ┌──────────────────────┐
│  strategy   │    │  pool_discovery      │
│  [Python]   │    │  [Python]            │
│             │    │  new pool backfill   │
│ FeatureStore│    └──────────────────────┘
│ XGBoost v2  │
│ dashcam.csv │
└──────┬──────┘
       │ XADD signals
       ▼
┌─────────────────────────┐    ┌─────────────────┐
│  executor  [Python/Rust]│───▶│  anvil  [Paper] │
│  RiskGuardian           │    │  (Anvil fork)   │
│  Uniswap V3 swap        │    └─────────────────┘
│  position_manager       │    ┌─────────────────┐
│  pending_sell lock      │───▶│  Base L2 [Live] │
└─────────────────────────┘    └─────────────────┘
       │
       ▼
┌──────────────────┐   ┌─────────────────────┐
│  PostgreSQL       │   │  ClickHouse         │
│  wallet_profiles  │   │  analytics views    │
└──────────────────┘   └─────────────────────┘`}</pre>
            </div>

            <h3>1.2 Service Responsibilities</h3>

            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Language</th>
                  <th>Responsibility</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["redis-stack", "—", "Streams (market_ticks, signals), ZSET feature store, execution state"],
                  ["ingestor", "Rust", "WebSocket log subscription, Swap decode, MsgPack → Redis XADD"],
                  ["strategy_engine", "Python", "Feature computation, XGBoost scoring, dashcam CSV writer"],
                  ["executor", "Python / Rust", "Risk guards, Uniswap V3 exactInputSingle, pending_sell lock"],
                  ["pool_discovery", "Python", "New pool detection, feature backfill"],
                  ["position_manager", "Python", "Trailing/time/hard stop-loss, slot0() price polling"],
                  ["dashboard", "Python (stdlib)", "Real-time Redis read → HTTP status board"],
                  ["postgres", "—", "Wallet profiles OLTP, strategy state"],
                  ["clickhouse", "—", "Analytics, meme token views (EATLQ extension)"],
                ].map(([svc, lang, desc]) => (
                  <tr key={svc}>
                    <td><code>{svc}</code></td>
                    <td><span className="text-muted text-xs">{lang}</span></td>
                    <td>{desc}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h3>1.3 Design Philosophy</h3>
            <ul>
              <li>
                <strong>Signal-to-noise over data volume</strong> — data
                without decision value is filtered at the ingestor boundary,
                not deferred to downstream services.
              </li>
              <li>
                <strong>Slow is fast</strong> — foundational contracts
                (Rust/Redis stream schemas, executor language contract,
                feature store prefix conventions) were frozen before any live
                capital was deployed.
              </li>
              <li>
                <strong>Reality-first architecture</strong> — single-machine
                Docker Compose; ClickHouse and the archiver are optional
                in-phase toggles, not hard dependencies.
              </li>
            </ul>
          </section>

          <hr className="section-rule" />

          {/* DATA PIPELINE */}
          <section id="data-pipeline" className="prose-paper scroll-mt-24">
            <h2>2. On-Chain Data Pipeline</h2>
            <p>
              The data pipeline begins at the RPC boundary and ends at a
              Redis Stream consumer group. Every design decision along this path
              optimises for latency determinism and back-pressure resilience.
            </p>

            <h3>2.1 Ingestor (Rust)</h3>
            <p>
              The Rust ingestor subscribes to{" "}
              <code>eth_subscribe(&quot;logs&quot;)</code> via an Alchemy
              WebSocket connection. For each confirmed block it decodes
              Uniswap V3 Swap events from raw EVM logs, derives USD-equivalent
              value and trade direction, and serialises the resulting{" "}
              <code>MarketTick</code> structs using{" "}
              <code>rmp_serde::to_vec_named</code> (MsgPack). The encoded
              payload is written to the <code>market_ticks</code> Redis Stream
              under field name <code>data</code>.
            </p>
            <p>
              Key ingestor invariants:
            </p>
            <ul>
              <li>
                <code>PoolCache</code> and <code>TokenRegistry</code> are
                in-process hot caches — repeated RPC lookups for known pool
                metadata are avoided entirely.
              </li>
              <li>
                Whale thresholds (<code>WHALE_USD_THRESH</code> ≈ $100k) are
                applied at ingest time, not in the strategy layer, to reduce
                downstream payload volume.
              </li>
              <li>
                An optional primary/fallback RPC pair provides continuity
                against Alchemy rate-limit events (HTTP 429).
              </li>
            </ul>

            <h3>2.2 Redis Streams & Consumer Groups</h3>
            <p>
              Redis Streams act as the durable, ordered message bus between
              services. Consumer groups are configured for each downstream
              consumer — <code>strategy_group</code>,{" "}
              <code>execution_group</code>, and optionally{" "}
              <code>archiver_group</code>. Explicit <code>XACK</code> after
              successful processing, combined with <code>XAUTOCLAIM</code>-based
              stale-entry recovery, means no tick is silently dropped under
              restarts or partial failures.
            </p>

            <div className="callout">
              <strong>Stream field contract:</strong> The ingestor writes
              MsgPack under field <code>data</code>. Strategy and archiver
              both apply the four-field fallback chain{" "}
              <code>data → payload_msgpack → payload → tick</code> for
              forward-compat decoding. Bad or missing payloads are{" "}
              <code>XACK</code>-ed and skipped to prevent PEL accumulation —
              the system prefers dropping a malformed tick over blocking the
              consumer group.
            </div>

            <h3>2.3 Pool Discovery</h3>
            <p>
              A separate <code>pool_discovery</code> service listens on{" "}
              <code>market_ticks</code> for pools not yet tracked in the feature
              store and triggers a backfill of their historical tick data.
              This allows the strategy engine to accumulate a meaningful feature
              window before scoring a newly observed pool.
            </p>
          </section>

          <hr className="section-rule" />

          {/* ML ENGINE */}
          <section id="ml-engine" className="prose-paper scroll-mt-24">
            <h2>3. ML Signal Engine</h2>
            <p>
              The strategy engine is a Python service that continuously consumes{" "}
              <code>market_ticks</code>, computes rolling features from a
              Redis-backed feature store, scores each tick with an XGBoost
              classifier, and emits a <code>signals</code> entry when the score
              exceeds a configured threshold.
            </p>

            <h3>3.1 Feature Store</h3>
            <p>
              Features are computed over a 5-minute sliding window stored as
              Redis Sorted Sets (keyed by timestamp, periodically pruned). The
              canonical four features from the base LASZLO architecture are:
            </p>

            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Formula / Description</th>
                  <th>Range</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["price_velocity_5m", "Net capital flow / total absolute flow", "[-1, 1]"],
                  ["buy_pressure_ratio", "Buy tick count / total tick count", "[0, 1]"],
                  ["token_volatility_5m", "Coefficient of variation of trade sizes (capped at 2.0)", "[0, 2]"],
                  ["activity_heat", "Raw tick count in 5-min window", "ℤ≥0"],
                ].map(([f, desc, range]) => (
                  <tr key={f}>
                    <td><code>{f}</code></td>
                    <td>{desc}</td>
                    <td><code className="text-muted">{range}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>

            <p>
              A fifth feature — <code>unique_buyer_entropy_5m</code> — was
              introduced by the EATLQ team as an extension to capture buyer
              concentration, which the original four-feature set did not cover.
            </p>

            <div className="callout-warning callout">
              <strong>Design note on small-token price distortion:</strong>{" "}
              DEX-listed micro-cap tokens frequently lack reliable USD price
              oracles. The <code>price_velocity_5m</code> feature deliberately
              uses <em>capital flow direction</em> as a momentum proxy rather
              than raw price movement, which degrades gracefully under illiquid
              conditions.
            </div>

            <h3>3.2 Scoring Modes</h3>
            <p>Two scoring modes are supported and are automatically selected at runtime:</p>
            <ul>
              <li>
                <strong>Mode A (v1, rule-based):</strong>{" "}
                <code>
                  Final_Score = 0.40×Whale + 0.30×Momentum + 0.20×Activity −
                  0.10×Risk
                </code>
                . Used when no trained v2 model file is present; provides a
                deterministic, interpretable baseline.
              </li>
              <li>
                <strong>Mode B (v2, XGBoost):</strong> A trained XGBoost
                classifier outputs a probability-like score directly. Activated
                when <code>signal_model_realtime_v2.json</code> is present and
                passes integrity checks.
              </li>
            </ul>

            <h3>3.3 Offline Training Loop</h3>
            <p>
              High-scoring ticks (above <code>DASHCAM_THRESHOLD</code>) are
              written to <code>logs/dashcam.csv</code> during live/paper runs.
              This CSV forms the input to a weekly offline training pipeline:
            </p>

            <pre>{`dashcam.csv
  → scripts/prepare_dataset.py   # clean, deduplicate
  → logs/dashcam_cleaned.csv

  → scripts/label_dashcam.py     # executable labels via RPC price-lookup
  → logs/labeled_dataset.csv     # time-split: train / holdout

  → scripts/retrain_model.py     # XGBoost, val accuracy > 55% gate
  → models/signal_model_v2.json  # promoted only if gate passes

  → scripts/phase6_weekly_cycle.sh  (cron scheduler)`}</pre>

            <p>
              The training gate enforces both a validation set accuracy
              threshold (&gt;55%) and a PR-AUC improvement criterion over
              the incumbent model before any promotion is allowed. Model
              promotion is always a human-approved action — no automated
              hot-swap.
            </p>

            <h3>3.4 Wallet Profiler</h3>
            <p>
              Wallet behaviour is classified into five archetypes stored in
              PostgreSQL and joined at feature-compute time:{" "}
              <strong>Smart Money</strong>, <strong>Sniper</strong>,{" "}
              <strong>Diamond Hand</strong>, <strong>Paper Hand</strong>,{" "}
              <strong>Whale</strong>. In the Phase 5/6 transition period,
              wallet-profile enrichment falls back to the{" "}
              <code>WHALE_USD_THRESH</code> heuristic when DB data is
              unavailable.
            </p>
          </section>

          <hr className="section-rule" />

          {/* EXECUTION */}
          <section id="execution" className="prose-paper scroll-mt-24">
            <h2>4. Execution Engine & Risk Management</h2>

            <h3>4.1 Execution Architecture</h3>
            <p>
              The executor is the only service that issues on-chain
              transactions. It operates in one of two modes controlled by
              environment variable: <strong>Paper mode</strong> (against a
              local Anvil fork of Base L2) and{" "}
              <strong>Live mode</strong> (against a production RPC endpoint).
              Chain ID validation (<code>chain_id == 8453</code>) is enforced
              at startup to prevent accidental mainnet execution.
            </p>
            <p>
              On receiving a <code>signals</code> entry, the executor follows
              this decision path:
            </p>
            <pre>{`signals XREADGROUP
  → daily drawdown check    (10% of UTC-day start balance)
  → liquidity pre-check     (pool WETH reserve heuristic)
  → honeypot detection      (Quoter V2 quoteExactInputSingle eth_call)
  → balance check           (fail-safe: reject BUY on RPC failure)
  → exactInputSingle swap   (amountOutMinimum = Quoter estimate × slippage)
  → open_position           (write Redis position hash)
  → position_manager        (slot0() poll every 2s for stop-loss triggers)`}</pre>

            <h3>4.2 Pre-Trade Risk Controls</h3>
            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Control</th>
                  <th>Mechanism</th>
                  <th>Failure Mode</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Honeypot detection", "Quoter V2 simulates sell before buy is executed", "Reject BUY (conservative)"],
                  ["Liquidity floor", "Pool WETH reserve vs. position size heuristic", "Reject BUY"],
                  ["Daily drawdown", "10% of UTC-day opening balance", "SYSTEM_PANIC, halt all execution"],
                  ["Gas circuit breaker", "baseFee > threshold → reject", "Reject BUY"],
                  ["Balance check", "eth_call balanceOf before BUY", "Live: reject; Paper: warn and continue"],
                ].map(([ctrl, mech, fail]) => (
                  <tr key={ctrl}>
                    <td>{ctrl}</td>
                    <td>{mech}</td>
                    <td className="text-muted text-xs">{fail}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h3>4.3 Position Manager & Stop-Loss Logic</h3>
            <p>
              After a BUY is executed, a position entry is written to Redis.
              The position manager polls <code>slot0()</code> on the Uniswap
              V3 pool every ~2 seconds to derive a live mark-to-market price.
              Three independent stop-loss triggers operate simultaneously:
            </p>
            <ul>
              <li>
                <strong>Trailing stop</strong> — triggered when price retraces
                ≥5% from peak, provided the position is still in profit at the
                peak.
              </li>
              <li>
                <strong>Time stop</strong> — triggered when price has been flat
                (±2%) for ≥30 minutes.
              </li>
              <li>
                <strong>Hard stop</strong> — triggered unconditionally at −15%
                from entry price.
              </li>
            </ul>
            <p>
              All stop-loss signals pass through the same executor path as
              normal SELL signals. A <code>pending_sell</code> lock with a 60s
              TTL prevents duplicate SELL storms during RPC outages.
            </p>

            <h3>4.4 Kelly-variant Position Sizing</h3>
            <p>
              Position size is computed as:
            </p>
            <pre>{`Position_Size = Base_Capital × Final_Score × Volatility_Adjustment`}</pre>
            <p>
              <code>Base_Capital</code> is calibrated to actual wallet balance,
              not a theoretical target. This prevents grossly oversized
              positions during early-stage small-balance runs.
            </p>
          </section>

          <hr className="section-rule" />

          {/* EATLQ EXTENSIONS */}
          <section id="eatlq-extensions" className="prose-paper scroll-mt-24">
            <h2>5. EATLQ Team Extensions</h2>
            <p>
              The EATLQ team made several significant extensions to the base
              LASZLO architecture. These are clearly distinguished from the
              original design to maintain attribution accuracy.
            </p>

            <h3>5.1 Meme Token Intelligence Layer</h3>
            <p>
              The original LASZLO wallet profiler treats &quot;Sniper&quot; as a
              general category (fast entry, quick exit). EATLQ introduced a
              dedicated meme token sub-system:
            </p>
            <ul>
              <li>
                <code>meme_token_registry_v1</code> — ClickHouse table tracking
                tokens identified as meme-style assets by heuristics (age,
                liquidity profile, holder concentration).
              </li>
              <li>
                <code>sender_meme_profile_v1</code> — per-wallet meme trading
                history aggregated in ClickHouse.
              </li>
              <li>
                <code>compute_meme_sniper_scores.py</code> — offline scoring
                script that enriches wallet profiles with a meme-specific
                sniper probability, separate from the general Sniper label.
              </li>
              <li>
                ClickHouse view set <code>meme_*</code> — a family of
                materialized views enabling sub-second analytical queries over
                meme token activity.
              </li>
            </ul>

            <div className="callout">
              <strong>Attribution note:</strong> The{" "}
              <code>meme_sniper</code> label, <code>meme_share ≥ 0.7</code>{" "}
              filter, and all <code>meme_*</code> ClickHouse views are EATLQ
              additions. They do not exist in the original LASZLO design and
              are not represented in the base whitepaper.
            </div>

            <h3>5.2 Analytics Infrastructure</h3>
            <p>
              ClickHouse was an optional component in the base architecture
              (Phase 4.5 toggle). EATLQ activated it as a persistent analytics
              layer and built a structured view hierarchy for:
            </p>
            <ul>
              <li>Hourly and daily swap aggregates per pool and token</li>
              <li>Wallet behaviour timelines across multiple pools</li>
              <li>Model performance tracking (dashcam score distributions)</li>
              <li>Pool lifecycle stages (discovery → mature → dying)</li>
            </ul>

            <h3>5.3 Agent-Driven Decision Documentation</h3>
            <p>
              All material architectural decisions made by the EATLQ team are
              recorded in <code>docs/decisions/</code> as structured ADR
              (Architecture Decision Record) files. This includes the executor
              language contract, feature store prefix conventions, model feature
              gap analysis, and compose profile specifications. This decision
              log serves as ground truth for resolving narrative drift between
              the whitepaper and the live codebase.
            </p>

            <h3>5.4 Research Harness</h3>
            <p>
              Phase 6 required running dozens of labelling and model
              experiments in a controlled, reproducible manner. The EATLQ team
              consolidated 14 one-off runner scripts into a structured research
              harness:
            </p>
            <ul>
              <li>
                <code>run004_research_utils.py</code> — shared data loading,
                metric computation, and artefact serialisation
              </li>
              <li>
                <code>run004_research_harness.py</code> — experiment runner
                driven by YAML config files in <code>configs/run004/</code>
              </li>
              <li>
                Standardised diagnostic JSON output format for all experiment
                runs (stored in <code>mvp/diagnostics/</code>)
              </li>
            </ul>
          </section>

          <hr className="section-rule" />

          {/* ALPHA SOP */}
          <section id="alpha-sop" className="prose-paper scroll-mt-24">
            <h2>6. Alpha Research Standard Operating Procedure</h2>
            <p>
              Over the course of Phase 6, we developed a formal SOP for
              evaluating, validating, and gating new Alpha signal hypotheses.
              This framework is described at the structural level; specific
              signal parameters and profitable strategies are omitted.
            </p>

            <h3>6.1 Research Pipeline Gates</h3>
            <p>
              Every new signal hypothesis must pass through a sequence of gates
              before any live capital exposure:
            </p>

            <div className="arch-diagram my-6">
              <pre className="bg-transparent p-0 m-0 text-inherit font-mono text-xs sm:text-sm leading-relaxed">{`  HYPOTHESIS
       │
       ▼
  ┌──────────────────────────────────────────────────────┐
  │  GATE 0: Static Analysis                             │
  │  - Feature coverage check (are features computable?) │
  │  - Data availability check (≥N labellable samples?)  │
  │  - Pool diversity (top-5 pool share < 80%?)          │
  └──────────────────────┬───────────────────────────────┘
                         │ PASS
                         ▼
  ┌──────────────────────────────────────────────────────┐
  │  GATE 1: Offline Research                            │
  │  - LOO cross-validation (ROC-AUC > 0.60 average)    │
  │  - Temporal stability (70/30 time-split)             │
  │  - PR-AUC lift vs. positive-rate baseline            │
  └──────────────────────┬───────────────────────────────┘
                         │ PASS
                         ▼
  ┌──────────────────────────────────────────────────────┐
  │  GATE 2: Paper Shadow Run (≥7 days adaptive)         │
  │  - Net return > 0%                                   │
  │  - Win rate satisfactory for strategy type           │
  │  - No systematic execution errors                    │
  │  - Human review of closed position log               │
  └──────────────────────┬───────────────────────────────┘
                         │ PASS + HUMAN APPROVAL
                         ▼
  ┌──────────────────────────────────────────────────────┐
  │  GATE 3: Tiny Live (≤ $X, ≤ N days)                 │
  │  - Live execution mechanics validated                │
  │  - Slippage, gas cost, and latency confirmed          │
  │  - Human approval required before scaling            │
  └──────────────────────┬───────────────────────────────┘
                         │ PASS + HUMAN APPROVAL
                         ▼
            SCALE (incremental, monitored)`}</pre>
            </div>

            <h3>6.2 Signal Family Classification</h3>
            <p>
              Candidate signals are organised into families, each with distinct
              feature requirements and evaluation criteria:
            </p>
            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Family</th>
                  <th>Signal Type</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Momentum", "Price velocity + buy pressure divergence", "Active research"],
                  ["Smart Money", "Wallet archetype co-occurrence patterns", "Active, secondary priority"],
                  ["Microstructure", "Pool age × liquidity × price impact", "Proposed (H9+)"],
                  ["Lifecycle", "Token pool stage classification", "Proposed (H9+)"],
                  ["Multi-window", "Feature correlation across 1m / 5m / 15m windows", "Proposed"],
                ].map(([fam, sig, status]) => (
                  <tr key={fam}>
                    <td><strong>{fam}</strong></td>
                    <td>{sig}</td>
                    <td>
                      <span
                        className={`phase-badge ${
                          status.startsWith("Active")
                            ? "phase-active"
                            : "phase-planned"
                        }`}
                      >
                        {status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h3>6.3 Decision Flag System</h3>
            <p>
              Shared across all agent and human sessions, a set of boolean
              decision flags prevents accidental cross-gate actions. Key flags:
            </p>
            <pre>{`LIVE_ALLOWED            = false  # until Gate 3 human approval
PRODUCTION_SIGNAL_WRITE = false  # never auto-promoted
MODEL_PROMOTION_ALLOWED = false  # requires separate human gate
TRAINING_ALLOWED        = true   # research training only
PRIVATE_KEY_IN_ARTIFACT = false  # absolute prohibition`}</pre>
          </section>

          <hr className="section-rule" />

          {/* DEV METHODOLOGY */}
          <section id="dev-methodology" className="prose-paper scroll-mt-24">
            <h2>7. Development Methodology</h2>
            <p>
              The EATLQ project served as a testbed for a new development
              workflow centred on structured AI-agent collaboration. This section
              describes that workflow honestly — including where it worked well
              and where it introduced new challenges.
            </p>

            <h3>7.1 AI-Assisted Parallel Development</h3>
            <p>
              Development made extensive use of AI coding assistants (primarily
              Claude via Cursor IDE) for:
            </p>
            <ul>
              <li>
                Generating boilerplate for Rust stream contracts, Python
                dataclass schemas, and Docker Compose service definitions
              </li>
              <li>
                Rapid iteration on research scripts — exploratory data analysis,
                feature ablation, and metric computation — where correctness
                can be immediately verified against known outputs
              </li>
              <li>
                Documentation synthesis: converting informal design discussions
                into structured ADR files and contract documents
              </li>
              <li>
                Cross-service integration debugging — tracing field name mismatches
                (e.g., <code>data</code> vs. <code>payload_msgpack</code>) across
                Rust and Python service boundaries
              </li>
            </ul>

            <div className="callout-success callout">
              <strong>What worked well:</strong> Using AI to draft and
              validate inter-service contracts (stream field schemas, executor
              language contracts, feature store prefix conventions) significantly
              reduced integration bugs. The AI assistant excelled at holding
              cross-file consistency.
            </div>

            <div className="callout-warning callout">
              <strong>Where human judgment was essential:</strong> Signal
              hypothesis generation, model performance interpretation,
              capital-risk decisions, and gate authorisations all required
              unassisted human judgment. AI-generated code occasionally
              introduced subtle bugs in the ML evaluation pipeline (e.g.,
              data leakage in time-series splits) that required careful review.
            </div>

            <h3>7.2 Agent Flow Design</h3>
            <p>
              For longer-horizon tasks — particularly Phase 6 experiment
              orchestration — we developed a structured agent task decomposition
              pattern:
            </p>
            <ul>
              <li>
                <strong>Stateful handoff documents</strong> (<code>AGENTS.md</code>,
                session summaries) allow agents to resume context across sessions
                without re-reading the entire codebase.
              </li>
              <li>
                <strong>Guard clauses</strong> embedded in architecture docs
                prevent agents from making authorised-seeming changes that
                violate system contracts (e.g., promoting a model without
                passing the validation gate).
              </li>
              <li>
                <strong>Decision flag checks</strong> — agents are instructed
                to read and honour the decision flag system before any
                state-mutating action.
              </li>
              <li>
                <strong>Artefact tiering</strong> — experiment outputs are
                classified as lightweight (JSON diagnostics only), medium
                (diagnostics + cleaned data), or heavy (full labelled datasets +
                model files). This prevents directory explosion from routine
                research runs.
              </li>
            </ul>

            <h3>7.3 Code Review & Contract Enforcement</h3>
            <p>
              The project maintains a <code>Code-Review.md</code> document
              as a living code review guide. It classifies risks into P0 (live
              capital correctness), P1 (performance and scalability), and P2
              (maintainability). Static contract checks (<code>make check-static</code>)
              can be run without Docker or RPC access and form the pre-commit
              baseline.
            </p>

            <h3>7.4 On-Chain Data Analysis</h3>
            <p>
              Working directly with on-chain data at this level required
              developing competence across several Web3-specific domains:
            </p>
            <ul>
              <li>
                EVM log decoding (ABI-based Swap event parsing, topic filtering)
              </li>
              <li>
                Uniswap V3 mechanics (fee tiers, <code>slot0()</code> price
                derivation, <code>exactInputSingle</code>, Quoter V2 simulation)
              </li>
              <li>
                Base L2 specifics (single sequencer, <code>maxPriorityFeePerGas ≈ 1 wei</code>,
                absence of Flashbots-style MEV protection)
              </li>
              <li>
                RPC reliability patterns (rate limiting, failover, 429 back-off)
              </li>
              <li>
                Token market microstructure (liquidity thinness, honeypot patterns,
                wash trading signatures)
              </li>
            </ul>
          </section>

          <hr className="section-rule" />

          {/* EMPIRICAL RESULTS */}
          <section id="empirical-results" className="prose-paper scroll-mt-24">
            <h2>8. Empirical Results</h2>
            <p>
              This section reports honestly on what the system produced during
              Phase 6 operation. The engineering pipeline functioned as
              designed; the model and signal quality did not meet thresholds for
              live deployment during this period.
            </p>

            <h3>8.1 Paper Run Results</h3>
            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Duration</th>
                  <th>Closed Trades</th>
                  <th>Net Return</th>
                  <th>Win Rate</th>
                  <th>Gate Decision</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["H6 paper", "1.2 days", "176", "−6.45%", "22.7%", "HOLD"],
                  ["H7 paper", "4 days", "200", "−1.36%", "46.5%", "HOLD"],
                ].map(([run, dur, trades, ret, wr, gate]) => (
                  <tr key={run}>
                    <td><code>{run}</code></td>
                    <td>{dur}</td>
                    <td>{trades}</td>
                    <td className={ret.startsWith("−") ? "text-red-600 font-medium" : "text-green-700 font-medium"}>{ret}</td>
                    <td>{wr}</td>
                    <td><span className="phase-badge phase-planned">{gate}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h3>8.2 Model Performance (Phase 6, Run-004)</h3>
            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Value</th>
                  <th>Threshold</th>
                  <th>Pass?</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["ROC-AUC (best run)", "0.72", "> 0.60", "Conditional"],
                  ["ROC-AUC (LOO avg)", "0.575", "> 0.60", "Fail"],
                  ["ROC-AUC (LOO worst)", "0.488", "> 0.55", "Fail"],
                  ["PR-AUC lift vs baseline", "−0.05 to +0.03", "> 0", "Marginal"],
                  ["Temporal stability", "UNSTABLE", "STABLE required", "Fail"],
                  ["Top-5 pool share", "90%+", "< 80%", "Fail"],
                  ["Approx. vs exec. label agreement", "31%–53%", "> 75%", "Fail"],
                ].map(([metric, val, thresh, pass]) => (
                  <tr key={metric}>
                    <td>{metric}</td>
                    <td className="font-mono text-sm">{val}</td>
                    <td className="text-muted text-xs">{thresh}</td>
                    <td>
                      <span
                        className={`phase-badge ${
                          pass === "Fail"
                            ? "phase-planned"
                            : pass === "Conditional" || pass === "Marginal"
                            ? "phase-active"
                            : "phase-done"
                        }`}
                      >
                        {pass}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="callout">
              <strong>Important context:</strong> The model failures above
              are research findings, not system failures. The engineering
              infrastructure (ingestion, execution path, risk controls) operated
              correctly throughout. The active production model{" "}
              <code>signal_model_realtime_v2.json</code> was never replaced or
              promoted; all experimental models remained in research isolation.
            </div>

            <h3>8.3 Infrastructure Performance</h3>
            <p>Observed during extended paper runs:</p>
            <ul>
              <li>
                Redis PEL (Pending Entry List) accumulation: resolved by
                combining <code>XAUTOCLAIM</code> on startup with explicit
                <code>XACK</code>-on-bad-message policy.
              </li>
              <li>
                Alchemy WebSocket disconnections: handled by ingestor reconnect
                loop; no tick loss observed beyond the reconnect window (~2–5s).
              </li>
              <li>
                Anvil fork drift: local Anvil fork can lag the live chain
                significantly under heavy load; paper mode metrics should be
                treated as directional, not precise.
              </li>
            </ul>
          </section>

          <hr className="section-rule" />

          {/* LESSONS LEARNED */}
          <section id="lessons" className="prose-paper scroll-mt-24">
            <h2>9. Lessons Learned</h2>

            <h3>9.1 Label Quality Dominates Model Quality</h3>
            <p>
              The most important finding from Phase 6 is that the labelling
              method determines the ceiling of model performance, not the choice
              of ML algorithm. Approximate price-change labels (computed from
              a static price at label time) showed{" "}
              <strong>negative correlation with actual executable return</strong>{" "}
              across three sample size regimes (n=60, n=300, n=752). Switching to
              RPC-bounded executable labels (simulated sell via Quoter V2)
              resolved label validity but introduced a sample-size constraint.
            </p>

            <h3>9.2 Single-Pool Data Concentration is a Hidden Risk</h3>
            <p>
              When one pool accounts for &gt;90% of training samples, the model
              learns pool-specific microstructure rather than generalizable
              signal. Leave-one-pool-out validation exposed this clearly:{" "}
              <em>removing the dominant pool dropped average ROC-AUC below
              random</em>. The fix requires a longer data collection window with
              enforced pool diversity, not a better model architecture.
            </p>

            <h3>9.3 Feature Dimensionality Was Insufficient</h3>
            <p>
              The original four-feature set covers price momentum and activity
              volume, but misses critical microstructure dimensions: buyer
              concentration, pool age, reserve depth, price impact at position
              size, and multi-window momentum divergence. The EATLQ team
              identified this gap through ablation studies and documented it in{" "}
              <code>docs/model-feature-gap.md</code>. Expanding the feature set
              is the primary prerequisite for Phase H9+.
            </p>

            <h3>9.4 Engineering Infrastructure Compounds Value</h3>
            <p>
              Despite the model not passing deployment gates, the engineering
              work was not wasted. Each iteration produced durable infrastructure:
              the RPC-bounded executable label pipeline, the LOO + temporal
              stability test harness, the dead-letter and pending-sell executor
              safety mechanisms, and the structured research harness. These form
              a solid foundation for the next research cycle.
            </p>

            <h3>9.5 Document Contracts Early and Enforce Them</h3>
            <p>
              The project accumulated "narrative drift" — discrepancies between
              whitepaper descriptions and runtime implementation — faster than
              anticipated. The solution that worked was embedding guard clauses
              directly into canonical architecture documents, making them
              machine-readable by AI assistants. This pattern — treating
              documentation as executable contracts — proved more robust than
              ad-hoc code comments.
            </p>

            <h3>9.6 MsgPack Serialisation Schema Pinning</h3>
            <p>
              Early in Phase 1, a Rust ingestor using{" "}
              <code>rmp_serde::to_vec</code> (list encoding) was incompatible
              with a Python decoder expecting dict-style MsgPack. Switching to{" "}
              <code>to_vec_named</code> resolved the incompatibility. This
              cross-language serialisation contract was then formalised in{" "}
              <code>docs/market-ticks-stream-contract.md</code> and is now
              tested in static contract checks.
            </p>
          </section>

          <hr className="section-rule" />

          {/* ROADMAP */}
          <section id="roadmap" className="prose-paper scroll-mt-24">
            <h2>10. Roadmap</h2>

            <table className="data-table my-4">
              <thead>
                <tr>
                  <th>Phase</th>
                  <th>Scope</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Phase 1–4.5", "Core infrastructure: ingestor, strategy, executor, dashcam, paper runs", "done"],
                  ["Phase 5", "Position manager, honeypot detection, drawdown circuit breaker, retrain pipeline", "done"],
                  ["Phase 6 (H0–H8.5)", "Feature engineering, executable labelling, XGBoost research harness, Run-004 ablations", "done"],
                  ["Phase H9", "Feature revision plan: buyer concentration, pool age, price impact, reserve depth", "active"],
                  ["Phase H10", "Feature recompute harness with enriched RPC data", "active"],
                  ["Phase H11–H13", "Enriched executable dataset → ablation → isolated candidate dry-run", "planned"],
                  ["Future", "ClickHouse full activation, multi-chain (BNB/Monad), automated weekly training cycle", "planned"],
                ].map(([phase, scope, status]) => (
                  <tr key={phase}>
                    <td><strong>{phase}</strong></td>
                    <td>{scope}</td>
                    <td>
                      <span
                        className={`phase-badge ${
                          status === "done"
                            ? "phase-done"
                            : status === "active"
                            ? "phase-active"
                            : "phase-planned"
                        }`}
                      >
                        {status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="callout">
              <strong>Next milestone:</strong> H9 feature revision requires
              designing a new feature vector that addresses the microstructure
              gap identified in Run-004. The target is a feature set that
              produces LOO ROC-AUC &gt;0.60 on a pool-diverse dataset of
              ≥1,500 executable-labelled samples before any paper run is
              authorised.
            </div>
          </section>

          <hr className="section-rule" />

          {/* Footer */}
          <footer className="py-8 text-sm text-muted" style={{ fontFamily: "Inter, sans-serif" }}>
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
              <div>
                <p className="font-medium text-ink mb-1">EATLQ Engineering Portfolio</p>
                <p>
                  Base architecture: LASZLO v2.0 by Franklin Nexus.
                  Extensions and operation: EATLQ Engineering Team.
                </p>
              </div>
              <div className="text-right text-xs">
                <p>May 2026</p>
                <p>Base L2 (OP Stack)</p>
              </div>
            </div>
            <p className="mt-4 text-xs border-t border-border pt-4">
              This document is provided for portfolio and technical review
              purposes. It does not constitute investment advice. Specific
              signal parameters, wallet keys, and API credentials are not
              disclosed in this document.
            </p>
          </footer>
        </main>
      </div>
    </div>
  );
}
