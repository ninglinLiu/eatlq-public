"""
Sprint049C — Copy-Follow Shadow Sidecar (Paper-Only)
=====================================================
Consumes the live ``market_ticks`` Redis stream under a dedicated
``copy_follow_group`` consumer group, monitoring 2 MEDIUM-concentration
FOLLOW_CANDIDATE_STRONG_AFTER_ROI wallets identified in Sprint049B.

ISOLATION CONTRACT
------------------
* Writes ONLY to ``logs/copy_follow_dashcam.csv`` and stdout (JSONL).
* Does NOT write to ``signals``, ``copy_follow_decisions`` (stream), or
  any Redis key outside the ``copy_follow:*`` namespace.
* NEVER calls execute_buy / execute_sell.
* NEVER reads or prints PRIVATE_KEY.
* All positions are paper/simulated only.
* Consumer group is separate from strategy_group / archiver_group.

LIVE_ALLOWED = false   TRAINING_ALLOWED = false   MODEL_PROMOTION_ALLOWED = false

Usage
-----
    python scripts/sprint049c_copy_follow_sidecar.py [--days 7] [--dry-run]

Env vars (same Redis env as strategy):
    REDIS_URL   default redis://localhost:6379/0
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import msgpack
import redis

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STREAM = "market_ticks"
CONSUMER_GROUP = "copy_follow_group"
CONSUMER_NAME = "copy_follow_sidecar_1"

# 2 MEDIUM-concentration FOLLOW_CANDIDATE_STRONG_AFTER_ROI wallets from Sprint049B.
# Primary pool info retained for reference; sidecar watches the WALLET, not the pool.
FOLLOW_CANDIDATES: dict[str, dict] = {
    "0xb0d18e658a2e5200c670572dbd978bb4b1971ea0": {
        "label": "MEDIUM_b0d18e",
        "own_roi_pct": 142.6,
        "matched_pairs": 20,
        "historical_tokens": [
            "0xa1f72459dfa10bad200ac160ecd78c6b77a747be",  # TOKEN_A
            "0xb102c4c8b673ecc29377ecf411685b29ecd42b07",  # TOKEN_B
        ],
        "historical_pools_dead": True,
        "warning": "Historical meme pools died Feb 2026; watching for wallet pivots to new tokens",
        "follow_mode": "DIRECTIONAL",  # copy BUY/SELL
    },
    "0xa94cf03ff22c0984fbc3e9af5f3a4001c1431e75": {
        "label": "MEDIUM_a94cf0",
        "own_roi_pct": 31.9,
        "matched_pairs": 16,
        "historical_tokens": [
            "0xa1f72459dfa10bad200ac160ecd78c6b77a747be",  # TOKEN_A (19 trades)
            "0xb102c4c8b673ecc29377ecf411685b29ecd42b07",  # TOKEN_B (12 trades)
        ],
        "historical_pools_dead": True,
        "warning": "Historical meme pools died Feb 2026; watching for wallet pivots to new tokens",
        "follow_mode": "DIRECTIONAL",  # copy BUY/SELL
    },
    # Sprint071: 0x6ff5 registered as SIGNAL_ONLY (PROGRAMMATIC_MARKET_MAKER).
    # Bidirectional market-maker — do NOT copy individual BUY/SELL orders.
    # Log every detected swap as WATCH_ONLY_SIGNAL; alpha_009 uses its presence
    # as a new-pool onset signal, not as a directional copy trigger.
    "0x6ff5693b99212da76ad316178a184ab56d299b43": {
        "label": "SPRINT071_0x6ff5_MM",
        "classification": "PROGRAMMATIC_MARKET_MAKER",
        "sprint069_verdict": "SENDER_SPECIFIC_ALPHA",
        "alpha_001_signal_share": 0.968,
        "follow_mode": "SIGNAL_ONLY",   # never open/close paper positions on this wallet
        "signal_logic": "presence_in_new_pool_within_60min = onset trigger for alpha_009",
        "max_safe_notional_u": 25,
        "note": "Sprint070: WATCH_ONLY_BOT. Individual swap copy not feasible (bidirectional, no mempool). "
                "Signal-window following (alpha_009 onset) remains valid.",
        "historical_pools_dead": False,
    },
}

# Known-dead meme pools (from Sprint049C token viability check).
# Trades in these pools are flagged DEAD_POOL — no follow action.
DEAD_POOLS_CONFIRMED: set[str] = {
    "0x07da9c5d35028f578dfac5be6e5aaa8a835704f6",  # died 2026-02-28
    "0x5c716b5ef03f1f5046ba30380c753529d3a10b35",  # died 2026-02-16
    "0x2828f20f064f59eede42c9fb5631a9bf9cee95de",  # died 2026-02-18
    "0xa1552e7cab4a7ff356dbdf1cc4ee3d3fe0750616",  # died 2026-02-10
    "0xaee2b8d4a154e36f479daece3fb3e6c3c03d396e",
}

GAS_COST_WETH = 0.001       # round-trip gas proxy
MAX_SLIPPAGE_BPS = 50       # 0.5% max assumed slippage
MAX_PAPER_NOTIONAL_WETH = 0.5  # paper position ceiling per trade
DASHCAM_PATH = Path("/opt/EATLQ/logs/copy_follow_dashcam.csv")
LATENCY_LOG_EVERY_N = 100   # log latency stats every N ticks (all wallets)

DASHCAM_COLUMNS = [
    "timestamp_utc",
    "stream_entry_id",
    "event_type",           # LATENCY_SAMPLE | CANDIDATE_DETECTED | WATCH_ONLY_SIGNAL | PAPER_FOLLOW_OPEN | PAPER_FOLLOW_CLOSE | SKIP
    "controller",
    "controller_label",
    "tx_hash",
    "pool_address",
    "token_address",
    "swap_direction",       # BUY / SELL / UNKNOWN
    "sold_token",
    "bought_token",
    "sold_amount",
    "bought_amount",
    "block_number",
    "block_time_utc",
    "stream_received_at_ms",
    "sidecar_observed_at_ms",
    "market_tick_latency_ms",   # sidecar_observed_at_ms - stream_received_at_ms
    "block_to_sidecar_latency_ms",  # sidecar_observed_at_ms - block_time_ms
    "pool_status",          # LIVE | DEAD_POOL_CONFIRMED | UNKNOWN
    "paper_action",         # OPEN | CLOSE | NONE
    "paper_entry_notional_weth",
    "paper_exit_notional_weth",
    "paper_pnl_weth",
    "paper_return_pct",
    "paper_hold_seconds",
    "cost_weth",
    "notes",
]

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

_PAYLOAD_FIELDS = (b"data", b"payload_msgpack", b"payload", b"tick",
                   "data", "payload_msgpack", "payload", "tick")


def parse_payload(entry_map: dict) -> Optional[dict]:
    """Decode msgpack payload from stream entry."""
    for key in _PAYLOAD_FIELDS:
        raw = entry_map.get(key)
        if not raw:
            continue
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        try:
            return msgpack.unpackb(raw, raw=False)
        except Exception:
            pass
    return None


def get_tx_from(entry_map: dict) -> str:
    raw = entry_map.get(b"tx_from") or entry_map.get("tx_from") or b""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return raw.decode("utf-8", errors="replace").lower()
    return str(raw).lower()


def stream_ts_to_ms(entry_id: bytes | str) -> int:
    """Extract millisecond timestamp from Redis stream ID (format: <ms>-<seq>)."""
    sid = entry_id.decode("utf-8") if isinstance(entry_id, bytes) else str(entry_id)
    return int(sid.split("-")[0])


# ---------------------------------------------------------------------------
# Dashcam writer
# ---------------------------------------------------------------------------

class DashcamWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        self._writer = None
        self._open()

    def _open(self) -> None:
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._file = open(self.path, "a", newline="", buffering=1)
        self._writer = csv.DictWriter(self._file, fieldnames=DASHCAM_COLUMNS, extrasaction="ignore")
        if new_file:
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict) -> None:
        self._writer.writerow({col: row.get(col, "") for col in DASHCAM_COLUMNS})
        self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()


# ---------------------------------------------------------------------------
# Paper position tracker
# ---------------------------------------------------------------------------

class PaperPositions:
    """Simple in-memory paper position tracker for the sidecar."""

    def __init__(self) -> None:
        # key: (controller, token) → open position dict
        self._open: dict[tuple, dict] = {}

    def open_position(self, controller: str, token: str, entry_notional_weth: float,
                      ts_utc: str, tx_hash: str) -> dict:
        pos = {
            "controller": controller,
            "token": token,
            "entry_notional_weth": entry_notional_weth,
            "entry_ts_utc": ts_utc,
            "entry_tx_hash": tx_hash,
            "status": "OPEN",
        }
        self._open[(controller, token)] = pos
        return pos

    def close_position(self, controller: str, token: str, exit_notional_weth: float,
                       ts_utc: str, cost_weth: float) -> Optional[dict]:
        key = (controller, token)
        if key not in self._open:
            return None
        pos = self._open.pop(key)
        entry = float(pos["entry_notional_weth"])
        pnl = exit_notional_weth - entry - cost_weth
        hold_sec = 0.0
        try:
            t0 = datetime.fromisoformat(pos["entry_ts_utc"])
            t1 = datetime.fromisoformat(ts_utc)
            hold_sec = (t1 - t0).total_seconds()
        except Exception:
            pass
        pos.update({
            "exit_notional_weth": exit_notional_weth,
            "exit_ts_utc": ts_utc,
            "paper_pnl_weth": pnl,
            "paper_return_pct": (pnl / entry * 100) if entry > 0 else 0.0,
            "cost_weth": cost_weth,
            "hold_seconds": hold_sec,
            "status": "CLOSED",
        })
        return pos

    def open_keys(self) -> list[tuple]:
        return list(self._open.keys())


# ---------------------------------------------------------------------------
# Swap direction helpers
# ---------------------------------------------------------------------------

WETH_ADDR = "0x4200000000000000000000000000000000000006"
USDC_ADDR = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
QUOTE_TOKENS = {WETH_ADDR, USDC_ADDR}


def classify_direction(tick: dict) -> tuple[str, str, str]:
    """Return (direction, sold_token, bought_token)."""
    sold = (tick.get("sold_token_address") or "").lower()
    bought = (tick.get("bought_token_address") or "").lower()
    if sold in QUOTE_TOKENS and bought not in QUOTE_TOKENS:
        return "BUY", sold, bought
    if bought in QUOTE_TOKENS and sold not in QUOTE_TOKENS:
        return "SELL", sold, bought
    return "UNKNOWN", sold, bought


def estimate_weth_notional(tick: dict, direction: str) -> float:
    """Best-effort WETH leg estimate from tick fields."""
    if direction == "BUY":
        amt = tick.get("sold_amount_normalized") or tick.get("amount0") or 0.0
    elif direction == "SELL":
        amt = tick.get("bought_amount_normalized") or tick.get("amount0") or 0.0
    else:
        amt = 0.0
    try:
        v = float(amt)
        return min(v, MAX_PAPER_NOTIONAL_WETH)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(days: float, dry_run: bool, redis_url: str) -> None:
    Path(DASHCAM_PATH.parent).mkdir(parents=True, exist_ok=True)
    dc = DashcamWriter(DASHCAM_PATH)

    client = redis.Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=5)

    # Create / join consumer group
    try:
        client.xgroup_create(STREAM, CONSUMER_GROUP, id="$", mkstream=False)
        print(json.dumps({"event": "consumer_group_created", "group": CONSUMER_GROUP,
                          "stream": STREAM, "start_id": "$"}))
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
        print(json.dumps({"event": "consumer_group_joined", "group": CONSUMER_GROUP,
                          "stream": STREAM}))

    print(json.dumps({
        "event": "sidecar_started",
        "stream": STREAM,
        "group": CONSUMER_GROUP,
        "consumer": CONSUMER_NAME,
        "follow_candidates": {
            addr: {"label": meta["label"], "follow_mode": meta.get("follow_mode", "DIRECTIONAL")}
            for addr, meta in FOLLOW_CANDIDATES.items()
        },
        "days": days,
        "dry_run": dry_run,
        "dashcam": str(DASHCAM_PATH),
        "ts": _now_utc_str(),
    }))

    positions = PaperPositions()
    deadline_ms = _now_ms() + int(days * 86_400_000)

    # --- Latency tracking ---
    latency_samples: list[int] = []  # market_tick_latency_ms
    block_latency_samples: list[int] = []
    total_ticks = 0
    candidate_hits = 0
    paper_opens = 0
    paper_closes = 0

    try:
        while _now_ms() < deadline_ms:
            replies = client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM: ">"},
                count=100,
                block=5000,
            )

            ack_ids: list[str] = []

            for _, entries in replies:
                for entry_id, entry_map in entries:
                    sidecar_observed_ms = _now_ms()
                    ts_utc = _now_utc_str()
                    sid_str = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                    ack_ids.append(sid_str)

                    stream_ts_ms = stream_ts_to_ms(entry_id)
                    market_tick_latency = sidecar_observed_ms - stream_ts_ms

                    tick = parse_payload(entry_map) or {}
                    tx_from = get_tx_from(entry_map) or tick.get("tx_from", "")

                    # --- block_time latency ---
                    block_time_ms: Optional[int] = None
                    raw_bt = tick.get("received_at_ms") or tick.get("block_time_ms")
                    if raw_bt:
                        try:
                            block_time_ms = int(raw_bt)
                        except (TypeError, ValueError):
                            pass
                    block_to_sidecar_latency = (sidecar_observed_ms - block_time_ms) if block_time_ms else None

                    # --- Latency bookkeeping ---
                    latency_samples.append(market_tick_latency)
                    if block_to_sidecar_latency is not None:
                        block_latency_samples.append(block_to_sidecar_latency)
                    total_ticks += 1

                    if total_ticks % LATENCY_LOG_EVERY_N == 0:
                        avg_lat = sum(latency_samples[-LATENCY_LOG_EVERY_N:]) / LATENCY_LOG_EVERY_N
                        avg_block = (
                            sum(block_latency_samples[-min(LATENCY_LOG_EVERY_N, len(block_latency_samples)):])
                            / min(LATENCY_LOG_EVERY_N, max(1, len(block_latency_samples)))
                        )
                        print(json.dumps({
                            "event": "latency_sample",
                            "total_ticks": total_ticks,
                            "avg_market_tick_latency_ms": round(avg_lat, 1),
                            "avg_block_to_sidecar_latency_ms": round(avg_block, 1),
                            "candidate_hits": candidate_hits,
                            "paper_opens": paper_opens,
                            "paper_closes": paper_closes,
                            "ts": ts_utc,
                        }))
                        dc.write({
                            "timestamp_utc": ts_utc,
                            "stream_entry_id": sid_str,
                            "event_type": "LATENCY_SAMPLE",
                            "market_tick_latency_ms": avg_lat,
                            "block_to_sidecar_latency_ms": avg_block,
                            "notes": f"total_ticks={total_ticks} hits={candidate_hits}",
                        })

                    # --- Candidate filter ---
                    if not tx_from or tx_from not in FOLLOW_CANDIDATES:
                        continue

                    candidate_hits += 1
                    cand = FOLLOW_CANDIDATES[tx_from]
                    pool_address = (tick.get("venue_address") or tick.get("pool_address") or "").lower()
                    direction, sold_tok, bought_tok = classify_direction(tick)
                    token_addr = bought_tok if direction == "BUY" else sold_tok

                    pool_status = "DEAD_POOL_CONFIRMED" if pool_address in DEAD_POOLS_CONFIRMED else (
                        "LIVE" if pool_address else "UNKNOWN"
                    )

                    block_number = tick.get("block_number", "")
                    block_time_utc = ""
                    if tick.get("block_time"):
                        block_time_utc = str(tick["block_time"])

                    base_row = {
                        "timestamp_utc": ts_utc,
                        "stream_entry_id": sid_str,
                        "controller": tx_from,
                        "controller_label": cand["label"],
                        "tx_hash": tick.get("tx_hash", ""),
                        "pool_address": pool_address,
                        "token_address": token_addr,
                        "swap_direction": direction,
                        "sold_token": sold_tok,
                        "bought_token": bought_tok,
                        "sold_amount": tick.get("sold_amount_normalized", ""),
                        "bought_amount": tick.get("bought_amount_normalized", ""),
                        "block_number": block_number,
                        "block_time_utc": block_time_utc,
                        "stream_received_at_ms": stream_ts_ms,
                        "sidecar_observed_at_ms": sidecar_observed_ms,
                        "market_tick_latency_ms": market_tick_latency,
                        "block_to_sidecar_latency_ms": block_to_sidecar_latency or "",
                        "pool_status": pool_status,
                    }

                    print(json.dumps({
                        "event": "CANDIDATE_DETECTED",
                        **{k: base_row[k] for k in ("controller", "controller_label",
                                                      "tx_hash", "pool_address", "token_address",
                                                      "swap_direction", "pool_status",
                                                      "market_tick_latency_ms")},
                        "ts": ts_utc,
                    }))

                    dc.write({"event_type": "CANDIDATE_DETECTED", "paper_action": "NONE", **base_row})

                    # SIGNAL_ONLY mode: market-maker / watch-only bot — log presence, no position.
                    if cand.get("follow_mode") == "SIGNAL_ONLY":
                        dc.write({
                            "event_type": "WATCH_ONLY_SIGNAL",
                            "paper_action": "NONE",
                            "notes": f"SIGNAL_ONLY:{cand.get('signal_logic','presence')}",
                            **base_row,
                        })
                        print(json.dumps({
                            "event": "WATCH_ONLY_SIGNAL",
                            "controller": tx_from,
                            "controller_label": cand["label"],
                            "pool_address": pool_address,
                            "swap_direction": direction,
                            "ts": ts_utc,
                        }))
                        continue

                    if dry_run or pool_status == "DEAD_POOL_CONFIRMED":
                        note = "DRY_RUN" if dry_run else "DEAD_POOL_SKIP"
                        dc.write({"event_type": "SKIP", "notes": note, **base_row})
                        continue

                    # --- Paper follow logic (DIRECTIONAL mode only) ---
                    key = (tx_from, token_addr)
                    weth_notional = estimate_weth_notional(tick, direction)

                    if direction == "BUY" and weth_notional > 0 and key not in positions.open_keys():
                        if not dry_run:
                            positions.open_position(tx_from, token_addr, weth_notional, ts_utc,
                                                    str(tick.get("tx_hash", "")))
                        paper_opens += 1
                        print(json.dumps({
                            "event": "PAPER_FOLLOW_OPEN",
                            "controller": tx_from,
                            "token": token_addr,
                            "entry_notional_weth": weth_notional,
                            "ts": ts_utc,
                        }))
                        dc.write({
                            "event_type": "PAPER_FOLLOW_OPEN",
                            "paper_action": "OPEN",
                            "paper_entry_notional_weth": weth_notional,
                            **base_row,
                        })

                    elif direction == "SELL" and key in positions.open_keys():
                        exit_notional = estimate_weth_notional(tick, direction)
                        if exit_notional > 0 and not dry_run:
                            closed = positions.close_position(tx_from, token_addr, exit_notional,
                                                              ts_utc, GAS_COST_WETH)
                        else:
                            closed = None
                        paper_closes += 1
                        pnl = closed["paper_pnl_weth"] if closed else 0.0
                        hold_sec = closed["hold_seconds"] if closed else 0.0
                        ret_pct = closed["paper_return_pct"] if closed else 0.0
                        print(json.dumps({
                            "event": "PAPER_FOLLOW_CLOSE",
                            "controller": tx_from,
                            "token": token_addr,
                            "exit_notional_weth": exit_notional,
                            "paper_pnl_weth": round(pnl, 6),
                            "paper_return_pct": round(ret_pct, 2),
                            "hold_seconds": round(hold_sec, 1),
                            "ts": ts_utc,
                        }))
                        dc.write({
                            "event_type": "PAPER_FOLLOW_CLOSE",
                            "paper_action": "CLOSE",
                            "paper_exit_notional_weth": exit_notional,
                            "paper_pnl_weth": pnl,
                            "paper_return_pct": ret_pct,
                            "paper_hold_seconds": hold_sec,
                            "cost_weth": GAS_COST_WETH,
                            **base_row,
                        })

            # XACK batch
            if ack_ids:
                client.xack(STREAM, CONSUMER_GROUP, *ack_ids)

    except KeyboardInterrupt:
        print(json.dumps({"event": "sidecar_interrupted", "ts": _now_utc_str()}))
    finally:
        dc.close()
        print(json.dumps({
            "event": "sidecar_stopped",
            "total_ticks": total_ticks,
            "candidate_hits": candidate_hits,
            "paper_opens": paper_opens,
            "paper_closes": paper_closes,
            "ts": _now_utc_str(),
        }))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint049C copy-follow shadow sidecar (paper-only)")
    ap.add_argument("--days", type=float, default=7.0, help="Run duration in days (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; skip paper position tracking")
    ap.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://172.18.0.10:6379/0"))
    args = ap.parse_args()

    print(json.dumps({
        "event": "sidecar_config",
        "LIVE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "paper_only": True,
        "follow_candidates": list(FOLLOW_CANDIDATES.keys()),
        "days": args.days,
        "dry_run": args.dry_run,
        "redis_url": args.redis_url.split("@")[-1],  # strip credentials if any
    }))
    run(days=args.days, dry_run=args.dry_run, redis_url=args.redis_url)


if __name__ == "__main__":
    main()
