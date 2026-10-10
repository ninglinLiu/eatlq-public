# Executor Language and Sizing Contract

Generated at: 2026-05-02  
Scope: documentation-only; static contract. No executor rewrite, no Rust migration, no Kelly implementation.  
MM reference: MM-07 (Executor Language), MM-08 (Dynamic Position Sizing)  
Code source of truth: `services/executor/Dockerfile`, `services/executor/src/main.py`  
Active guard: `mvp/audits/failed_no_7d_alpha_guard.md` (unchanged).

---

## MM-07 — Runtime language

**Current executor service is Python.** The process is built from `services/executor/Dockerfile` and implemented under `services/executor/src/`.

**"Rust Sniper"** (and similar wording in older architecture diagrams or the whitepaper) is **narrative / historical** naming. It **does not** describe the language of the runnable executor in this repository.

**Stable contract phrase:** **Rust Sniper is narrative/historical wording; the live executor code path is Python.**

### Operational gate

- **`EXECUTOR_DRY_RUN`** (`services/executor/src/main.py`) is the primary dry-run toggle; reviewers must cite env/config, not the whitepaper alone.

### `LIVE_TRADING_MODE` vs `EXECUTOR_DRY_RUN` (fail-safe)

Compose / operators may set **`LIVE_TRADING_MODE`** (`Operations.md`). The executor **refuses to start** if both `LIVE_TRADING_MODE=true` and **`EXECUTOR_DRY_RUN=1`** are set — the combination is ambiguous (live vs simulation). This does **not** auto-enable live trading; it only blocks a contradictory configuration.

### `pending_sell` / Operations narrative

Sprint004 reconciles the prior drift at the Python runtime layer: `services/executor/src/position.py` defines pool-keyed `pending_sell` / `pending_sell_at` fields on `positions:{pool}` hashes with a 60s TTL, and `services/executor/src/main.py` wraps only the existing `_monitor_once` SELL attempt path. A non-stale lock skips duplicate SELL attempts; a stale lock can be refreshed; successful SELL and zero-balance ghost cleanup close the position; explicit SELL failure clears the lock and keeps the position for the next monitor cycle.

Stable contract phrase: **Python executor pending-sell locking is pool-keyed (`positions:{pool}`), not token-keyed legacy LASZLO wording.**

---

## Signals stream — JSON payload contract

**Stream:** Redis stream name from `SIGNALS_STREAM` (default `signals`).  
**Hash fields:** each message must carry executable JSON in field **`payload`** (preferred, LASZLO) or legacy **`signal_json`**.  
**Dead-letter stream:** `SIGNALS_DEAD_LETTER_STREAM` (default `signals_dead_letter`). Entries include `entry_id`, truncated `error`, and `payload` preview. **Static field/env summary** for operators (no Redis): `signals_dead_letter_ops_summary()` in `services/executor/src/main.py`. **Broader observability** (stream lag, cross-links): `docs/observability-stream-contract.md`.

### Required semantics for `action`

- **`action` is compared case-sensitively.** Only the exact string **`BUY`** (ASCII uppercase) is treated as a buy command in `_handle_signal`.
- Values such as `buy`, `Buy`, or `SELL` are **not** malformed at ingress: they are logged and skipped (still **XACK**), so external publishers can multiplex non-buy rows on the same stream without filling the dead-letter queue.

### BUY payload — fields strategy and operators should set

| Field | Required | Notes |
|-------|----------|--------|
| `action` | yes | Must be exactly **`BUY`**. |
| `pool_address` **or** `token_address` | at least one | Both identify the **pool contract** for `registry_lookup`. Strategy `smart_money` sets **both** to the same pool address for backward compatibility. |
| `confidence` | recommended | Executor rejects below `SIGNAL_MIN_CONFIDENCE` / `STRATEGY_MIN_CONFIDENCE` (default `0.52`) with log + XACK (not dead-letter). |
| `chain_id` | recommended | Must match executor `CHAIN_ID`; mismatch skipped with log + XACK. |
| `signal_source` | optional | `pool_score` signals are **skipped** (shadow-only); `smart_money` is the executable path. |
| `pool_type`, `symbol`, `final_score`, `position_size_usd`, `timestamp` | optional | Used for logging / sizing / Telegram; not all are enforced at malformed gate. |

### Dead-letter `error` taxonomy (structural / parse only)

| `error` | When |
|---------|------|
| `parse_failed` | Missing `payload`/`signal_json` or JSON decode error. |
| `buy_missing_pool_or_token` | `action == BUY` but no non-empty `pool_address` or `token_address`. |
| `buy_invalid_pool_or_token_address` | Identifier present but not `0x` + 40 hex digits. |
| `handle_failed: …` | Unexpected exception inside `_handle_signal`. |

Executor **does not** add RPC calls at this gate; validation is string / JSON shape only.

Upstream **`market_ticks`** MsgPack stream (ingestor → strategy) is documented in **`docs/market-ticks-stream-contract.md`** (field aliases, XACK hygiene).

---

## MM-08 — Position sizing (bounded-fraction / capped, not Kelly)

Whitepaper prose may describe Kelly-style or “optimal” dynamic sizing. The **implemented** sizing model is **bounded-fraction / capped**:

- **Strategy** proposes a nominal size using **`BASE_CAPITAL * 0.6`** (`services/strategy/src/main.py`, env `BASE_CAPITAL`).
- **Executor** applies hard caps including **`EXECUTOR_MAX_POSITION_USD`**, **`EXECUTOR_MAX_BALANCE_FRACTION`**, **`EXECUTOR_MAX_CONCURRENT_POSITIONS`**, and per-signal **`position_size_usd`** / **`position_size`** (`services/executor/src/main.py`).

**Stable contract phrases:**

- **Current sizing is bounded-fraction / capped sizing, not full Kelly.**
- **Full Kelly criterion is not implemented** in strategy or executor static paths audited here.

---

## Forbidden

- Do not claim the executor is Rust without a separate Rust executor implementation.
- Do not implement or document full Kelly formulas as production behavior without new authorized design.
- Do not use corrected wording here to imply `FAILED_NO_7D_ALPHA` is lifted.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
