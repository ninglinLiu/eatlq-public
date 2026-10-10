# Observability stream contract (static ops)

**File:** `docs/observability-stream-contract.md`

Generated at: 2026-05-03  
Scope: **read-only runbook** for operators and static tests. No metrics backend, no Redis/Docker/RPC requirements for this document alone.  
Authoritative payload/alias rules for `market_ticks`: **`docs/market-ticks-stream-contract.md`**.  
Dead-letter `error` taxonomy: **`docs/executor-language-contract.md`**.

Active guard: `mvp/audits/failed_no_7d_alpha_guard.md` (unchanged).  
This document does **not** authorize training, labeling, live trading, or Phase 6.

---

## Goals

When the system sees **malformed `market_ticks`**, **malformed `signals`**, **stream lag**, or **dead-letter growth**, operators should know **where to look** without relying on a custom metrics stack.

---

## `market_ticks` — malformed rows (no Redis counter)

| Layer | Observable behavior |
| ----- | ------------------- |
| **strategy** | Missing MsgPack blob / bad unpack / empty pool key → skip hot path; entry still **XACK**’d (PEL hygiene). No per-reason Redis counter. |
| **archiver** | Missing `data`/`payload_msgpack`/`payload`/`tick`, non-bytes, or MsgPack deserialize error → `warn` + **XACK**. |
| **dashboard** | Decode uses the same **field order** as strategy (`data` → `payload_msgpack` → `payload` → `tick`); failure → tick omitted for that row (HTTP read path). |

**Implication:** “How many bad ticks?” is answered by **logs** + **sampling** (`XRANGE` / replay), not a first-class counter in this repo.

---

## `signals` / `signals_dead_letter`

- **Stream names:** `SIGNALS_STREAM` (default `signals`), `SIGNALS_DEAD_LETTER_STREAM` (default `signals_dead_letter`).
- **Dead-letter fields** (executor `XADD`): `entry_id`, `error`, `payload` (preview capped).
- **Static summary helper (no I/O):** `signals_dead_letter_ops_summary()` in `services/executor/src/main.py` — env names, field tuple, doc pointers.

**Suggested checks (live Redis shell):**

- `XLEN signals_dead_letter` — backlog depth (trimmed with `MAXLEN ~ 5000` on write).
- `XRANGE signals_dead_letter - + COUNT 5` — inspect recent `error` values.
- Executor logs: `dead_letter` / `parse_failed` / `handle_failed`.

---

## Stream lag / consumer health

Redis **consumer groups** expose pending and lag (version-dependent):

- `XINFO GROUPS <stream>` — group name, `lag`, `pending`, last-delivered ID.
- `XPENDING <stream> <group>` — summary of PEL entries.

Rising **pending** on `market_ticks` or `signals` usually means a stuck/slow consumer or upstream flood — correlate with service logs and CPU.

**Note:** `XLEN` is the total stream length (history + undelivered); interpret next to `last-delivered-id`, not as “queue depth” alone.

---

## Drawdown / emergency halt

- Executor daily loss guard sets **`SYSTEM_PANIC`** (override with `SYSTEM_PANIC_KEY`). Strategy honors the same key via `should_block_signal`.
- **Inspect:** `GET SYSTEM_PANIC` (or custom key) for the textual reason; see `Operations.md` §「回撤与紧急停机」.

---

## Long-window paper observation (Sprint013 S1-T3, read-only)

For the Sprint012 "30–60 min observation" next step, use the read-only tool:

```bash
# Safe dry run (no Redis contact; prints resolved config as JSON):
python3 scripts/paper_observation_window.py --dry

# Live 30-minute observation (requires REDIS_URL in the environment and
# an already-running paper/dry-run stack; script will NOT start any service):
REDIS_URL=redis://127.0.0.1:6379/0 \
python3 scripts/paper_observation_window.py --window-sec 1800 --interval-sec 30
```

Guarantees (statically tested in `tests/test_paper_observation_window.py`):

- **No writes** — the script only calls `XLEN`, `XINFO GROUPS`, `XRANGE`-style read primitives. It does not `XADD`, `XDEL`, `XTRIM`, `XCLAIM`, `SET`, `DEL`, or `PUBLISH`.
- **Credential redaction** — `REDIS_URL`, provider URLs, and 64-char hex private keys are redacted before being written to stdout or to the artifact JSON/MD.
- **Decision flags unchanged** — the artifact embeds `IMPLEMENTATION_ALLOWED / TRAINING_ALLOWED / LABELING_ALLOWED / LIVE_ALLOWED / PHASE6_ALLOWED / WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED / REAL_TRADING_ALLOWED` all `false` and cites `mvp/runs/run-004-v2-7d-alpha-readout.md` + `mvp/audits/failed_no_7d_alpha_guard.md` as controlling. It cannot supersede `FAILED_NO_7D_ALPHA`.
- **Stop rules** — stop the run (Ctrl-C is safe; the script holds no locks) if `signals_dead_letter` XLEN spikes, ingestor restart storm is observed, archiver PEL grows unboundedly, or any `LIVE_TRADING_MODE` drift is detected.

Artifacts land under `mvp/observations/obs_<UTC>.json` + `obs_<UTC>.md`. Those files are `.gitignore`-d by default; force-add a specific one only when it is evidence for a Step 2 unlock.

---

## References

- `docs/market-ticks-stream-contract.md`
- `docs/executor-language-contract.md`
- `Operations.md` — dead-letter, lag, panic, static `make check-static`
- `Architecture.md` — stream field summary + cross-links

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | REAL_RPC_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
