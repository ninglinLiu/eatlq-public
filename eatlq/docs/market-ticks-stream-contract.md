# `market_ticks` Redis stream contract

**File:** `docs/market-ticks-stream-contract.md`

Generated at: 2026-05-03  
Scope: **static contract** between **ingestor** / optional producers, **strategy**
(`services/strategy/src/main.py`, `feature_store.py`), **archiver**, and **pool_discovery**.
No DEX topic or decode-semantics changes are implied here — only **stream field names**
and **MsgPack blob** handling.

Active guard: `mvp/audits/failed_no_7d_alpha_guard.md` (unchanged).  
This document does **not** authorize training, labeling, live trading, or Phase 6.

---

## Stream and consumer

- **Stream name:** `MARKET_TICKS_STREAM` env (default `market_ticks`).
- **Strategy** reads via `XREADGROUP` with `decode_responses=False` (bytes keys/values).
- **Batch XACK:** Each polled entry ID is acknowledged at end of loop iteration —
  including entries skipped for missing payload or MsgPack/decode failures — so the
  consumer group **PEL does not grow** on bad rows (same hygiene pattern as archiver).

---

## Payload field priority (single-field MsgPack blob)

Producers should use **one** of the following Redis hash fields per XADD message.
**Strategy** resolves them in this **fixed order** (`parse_stream_payload`):

1. **`data`** — **canonical** for `services/ingestor` (`redis_streams.rs`: XADD field `data`).
2. **`payload_msgpack`** — legacy / archiver compatibility (`services/archiver/src/redis_streams.rs`).
3. **`payload`** — alternate tools / historical rows.
4. **`tick`** — alternate tools / historical rows.

Rules:

- The **first non-empty** blob in that order wins. An empty `data` (`b""`) is ignored
  so a fallback field can still be used.
- Field names may appear as **bytes** or **str** keys (strategy accepts both).
- **`tx_from`** (and similar) may appear as **additional** stream fields; strategy
  merges `tx_from` into the decoded dict when msgpack unpacking succeeds.

---

## MsgPack contents

- **Encoding:** ingestor uses **`rmp_serde::to_vec_named`** → Python `msgpack.unpackb(..., raw=False)`
  should yield a **`dict`** (or legacy list-shaped key/value encodings handled in
  `FeatureStore._normalize_msgpack_payload`).
- **Required for useful feature updates:** decoded dict should include pool/log fields
  consumed by `FeatureStore._extract_tick_fields` (e.g. `address` or `token_address`,
  `topic0`, `data`, block/tx indices — see `feature_store.py`). Rows that decode but
  lack `address` / `token` alias produce an empty `token_address` and are **skipped**
  in the strategy hot loop **without** failing the consumer.

---

## Malformed rows (strategy behavior)

| Case | Strategy action |
|------|-----------------|
| No `data` / `payload_msgpack` / `payload` / `tick`, or all empty | Skip processing; **XACK** with batch |
| MsgPack unpack failure or unsupported shape | `except` in main loop → skip; **XACK** |
| Decode OK but no resolvable pool key in tick | Skip filters; **XACK** |

---

## References in repo

- Ingestor write path: `services/ingestor/src/redis_streams.rs`
- Archiver read: `services/archiver/src/redis_streams.rs` — same **four-field** MsgPack blob priority as strategy (`data` → `payload_msgpack` → `payload` → `tick`)
- Strategy parse: `services/strategy/src/main.py` — `parse_stream_payload`, `_MARKET_TICK_PAYLOAD_FIELD_ORDER`
- Dashboard decode: `services/dashboard/server.py` — `_MSGPACK_PAYLOAD_FIELDS` matches the same order
- Pool discovery tick publish: `services/pool_discovery/src/swap_poller.py` (uses `data` + optional `tx_from`)
- Ops / lag / dead-letter aggregation (no extra metrics backend): `docs/observability-stream-contract.md`

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
