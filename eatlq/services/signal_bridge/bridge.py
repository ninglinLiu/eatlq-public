#!/usr/bin/env python3
"""KEYSTONE Signal Bridge — live service that connects pool discovery to the executor.

Loop:
  1. Scan Redis pool:discovered:* for WETH V3 pools aged 10–15 min (entry window)
  2. Query ClickHouse for pre-entry features (only the first 10 min of swaps)
  3. Run the persisted ML model → if p(fatal) ≤ p_cut → emit BUY to Redis signals
  4. Track emitted pools to avoid duplicates

Non-blocking: pools outside the 10–15 min window are skipped. The bridge polls
every POLL_SEC (default 30s). A pool gets exactly one chance: if it misses the
window (e.g., bridge was down), it's gone. This is fine — we don't want stale entries.

Dependencies: _model.pkl and _params.json from train_model.py.
"""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
import signal
import sys
import time
from urllib.parse import urlparse

import clickhouse_connect
import numpy as np
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signal_bridge")

WETH = "0x4200000000000000000000000000000000000006".lower()
LN1P = math.log(1.0001)

MODEL_DIR = os.environ.get("BRIDGE_MODEL_DIR", os.path.dirname(os.path.abspath(__file__)))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SIGNALS_STREAM = os.environ.get("SIGNALS_STREAM", "signals")
POLL_SEC = int(os.environ.get("BRIDGE_POLL_SEC", "30"))
POSITION_SIZE_USD = float(os.environ.get("BRIDGE_POSITION_SIZE_USD", "100"))
DEDUPE_KEY = os.environ.get("BRIDGE_DEDUPE_KEY", "signal_bridge:published")
DEDUPE_TTL_SEC = int(os.environ.get("BRIDGE_DEDUPE_TTL_SEC", str(24 * 3600)))
P_CUT_MAX = float(os.environ.get("BRIDGE_P_CUT_MAX", "0.27"))
# Shared with executor cluster_blacklist — reject repeat-rug tickers pre-signal.
SYMBOL_BLACKLIST_KEY = os.environ.get("BRIDGE_SYMBOL_BLACKLIST_KEY", "executor:blacklist:symbol")

WINDOW_MARGIN = 300   # 5 min margin after W=600s → window is 10–15 min

_ch_client = None


def _get_ch():
    global _ch_client
    if _ch_client is not None:
        return _ch_client
    ch_url = os.environ.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_HTTP_URL") or "http://clickhouse:8123"
    parsed = urlparse(ch_url)
    _ch_client = clickhouse_connect.get_client(
        host=parsed.hostname or "clickhouse",
        port=parsed.port or (8443 if parsed.scheme == "https" else 8123),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "eatlq"),
        interface="http",
        secure=parsed.scheme == "https",
    )
    log.info("ClickHouse connected: %s", ch_url)
    return _ch_client


def load_model():
    model_path = os.path.join(MODEL_DIR, "_model.pkl")
    params_path = os.path.join(MODEL_DIR, "_params.json")
    if not os.path.exists(model_path) or not os.path.exists(params_path):
        log.error("Model not found at %s — run train_model.py first", MODEL_DIR)
        sys.exit(1)
    with open(model_path, "rb") as f:
        clf = pickle.load(f)
    params = json.load(open(params_path))
    log.info("Model loaded: p_cut=%.4f train_n=%d features=%d",
             params["p_cut"], params["train_n"], len(params["features"]))
    return clf, params


def get_candidate_pools(r: redis.Redis, W: int) -> list[dict]:
    """WETH V3 pools whose FIRST SWAP (t0) is in the entry window [W, W+WINDOW_MARGIN].

    The entry window is keyed on first-swap time t0 (matching training/backtest
    semantics), NOT pool deploy time. Memecoins are routinely deployed hours
    before liquidity/trading starts, so windowing on created_at_sec systematically
    misses every pool that launches late — which is most of them. t0 comes from
    the swaps table; pool metadata (orient/symbols) comes from the redis registry.
    """
    lo = W + WINDOW_MARGIN   # first swap at most this many seconds ago
    hi = W                   # first swap at least this many seconds ago
    try:
        ch = _get_ch()
        rows = ch.query(
            "SELECT pool_address, toUnixTimestamp(min(block_time)) AS t0 "
            "FROM eatlq.uniswap_v3_swaps_decoded "
            "WHERE chain_id=8453 "
            "GROUP BY pool_address "
            f"HAVING t0 >= toUnixTimestamp(now())-{lo} AND t0 <= toUnixTimestamp(now())-{hi}"
        ).result_rows
    except Exception as e:
        log.warning("candidate t0 query failed: %s", e)
        return []
    candidates = []
    for pool_addr, t0 in rows:
        try:
            raw = r.hgetall(f"pool:discovered:{pool_addr}")
        except Exception:
            continue
        if not raw:
            continue
        data = {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()}
        if data.get("has_weth_leg") != "1" or data.get("pool_type") != "v3":
            continue
        data["pool_address"] = pool_addr
        data["t0_sec"] = int(t0)
        candidates.append(data)
    return candidates


def _orient_from_meta(pool: dict) -> int:
    """Compute orient from pool Redis metadata: -1 if token0 is WETH, else +1."""
    return -1 if pool.get("token0_address", "").lower() == WETH else 1


def query_features(pool_addr: str, W: int, MIN_PRE: int) -> dict | None:
    """Query ClickHouse for pre-entry features of a single pool (orient injected externally)."""
    sql = f"""
    WITH fs AS (
      SELECT min(block_time) t0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND pool_address='{pool_addr}' AND block_time IS NOT NULL
    )
    SELECT
      countIf(dateDiff('second', fs.t0, s.block_time) <= {W}) ns_pre,
      uniqExactIf(s.recipient, dateDiff('second', fs.t0, s.block_time) <= {W}) buyers_pre,
      uniqExactIf(s.sender, dateDiff('second', fs.t0, s.block_time) <= {W}) senders_pre,
      argMinIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) first_tick,
      argMaxIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) last_tick_pre,
      minIf(s.tick, dateDiff('second', fs.t0, s.block_time) <= {W}) min_tick_pre,
      maxIf(s.tick, dateDiff('second', fs.t0, s.block_time) <= {W}) max_tick_pre,
      argMinIf(toFloat64(s.liquidity), s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) liq_first,
      argMaxIf(toFloat64(s.liquidity), s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) liq_last_pre,
      minIf(toFloat64(s.liquidity), dateDiff('second', fs.t0, s.block_time) <= {W}) liq_min_pre,
      countIf(dateDiff('second', fs.t0, s.block_time) <= {W//2}) ns_h1,
      countIf(dateDiff('second', fs.t0, s.block_time) > {W//2} AND dateDiff('second', fs.t0, s.block_time) <= {W}) ns_h2,
      argMaxIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W//2}) last_tick_h1,
      sumIf(abs(toFloat64(s.amount0)), dateDiff('second', fs.t0, s.block_time) <= {W}) wvol0,
      sumIf(abs(toFloat64(s.amount1)), dateDiff('second', fs.t0, s.block_time) <= {W}) wvol1
    FROM eatlq.uniswap_v3_swaps_decoded s, fs
    WHERE s.chain_id=8453 AND s.pool_address='{pool_addr}' AND s.block_time IS NOT NULL
      AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W}
    """
    try:
        ch = _get_ch()
        result = ch.query(sql)
        if not result.result_rows:
            return None
        row = result.result_rows[0]
        if len(row) < 15:
            return None
        names = ["ns_pre", "buyers_pre", "senders_pre", "first_tick",
                 "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first",
                 "liq_last_pre", "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1",
                 "wvol0", "wvol1"]
        d = {k: float(v) if v is not None else 0.0 for k, v in zip(names, row)}
        if d["ns_pre"] < MIN_PRE:
            return None
        return d
    except Exception as e:
        log.warning("CH query failed for %s: %s", pool_addr, e)
        return None


FEATURE_NAMES = ["ns_pre","buyers_pre","senders_pre","swaps_per_buyer",
    "buyer_sender_ratio","early_ret","pre_vol","pre_pullback","pre_runup_from_lo",
    "liq_log","liq_trend","liq_drawdown","heat_accel","mom_h1","mom_h2"]


def compute_feature_vector(d: dict) -> np.ndarray:
    """From raw CH row dict → 15-element feature vector matching model training."""
    o = d["orient"]
    lp = lambda t: t * LN1P * o
    lp_first = lp(d["first_tick"])
    lp_last = lp(d["last_tick_pre"])
    lp_lo = min(lp(d["min_tick_pre"]), lp(d["max_tick_pre"]))
    lp_hi = max(lp(d["min_tick_pre"]), lp(d["max_tick_pre"]))
    lp_h1 = lp(d["last_tick_h1"]) if d.get("last_tick_h1") else lp_first

    ns_pre = max(d["ns_pre"], 1)
    buyers = max(d["buyers_pre"], 1)
    senders = max(d["senders_pre"], 1)
    liq_first = max(d["liq_first"], 1)
    ns_h1 = max(d["ns_h1"], 1)

    return np.array([
        d["ns_pre"],
        d["buyers_pre"],
        d["senders_pre"],
        ns_pre / buyers,                         # swaps_per_buyer
        d["buyers_pre"] / senders,                # buyer_sender_ratio
        lp_last - lp_first,                       # early_ret
        lp_hi - lp_lo,                            # pre_vol
        lp_hi - lp_last,                          # pre_pullback
        lp_last - lp_lo,                          # pre_runup_from_lo
        np.log1p(max(d["liq_last_pre"], 0)),      # liq_log
        d["liq_last_pre"] / liq_first,            # liq_trend
        d["liq_min_pre"] / liq_first,             # liq_drawdown
        d["ns_h2"] / ns_h1,                       # heat_accel
        lp_h1 - lp_first,                         # mom_h1
        lp_last - lp_h1,                          # mom_h2
    ], dtype=np.float64)


def _non_weth_token(pool: dict) -> str:
    t0 = pool.get("token0_address", "").lower()
    t1 = pool.get("token1_address", "").lower()
    if t0 == WETH:
        return t1
    return t0


def _non_weth_symbol(pool: dict) -> str:
    t0 = pool.get("token0_address", "").lower()
    if t0 == WETH:
        return (pool.get("token1_symbol") or "?").strip()
    return (pool.get("token0_symbol") or "?").strip()


def is_symbol_blacklisted(r: redis.Redis, pool: dict) -> bool:
    sym = _non_weth_symbol(pool).upper()
    if not sym or sym in ("?", "WETH", "ETH"):
        return False
    try:
        return bool(r.sismember(SYMBOL_BLACKLIST_KEY, sym))
    except Exception as e:
        log.warning("symbol blacklist check failed (fail-open) sym=%s: %s", sym, e)
        return False


def emit_signal(r: redis.Redis, pool: dict, p_fatal: float):
    """Push a BUY signal to the signals Redis stream."""
    payload = {
        "action": "BUY",
        "pool_address": pool["pool_address"],
        "token_address": _non_weth_token(pool),
        "chain_id": 8453,
        "signal_source": "keystone_bridge",
        "confidence": 0.90,
        "position_size_usd": POSITION_SIZE_USD,
        "p_fatal": round(p_fatal, 4),
        "pool_type": pool.get("pool_type", "v3"),
        "token0_symbol": pool.get("token0_symbol", "?"),
        "token1_symbol": pool.get("token1_symbol", "?"),
        "pre_volume_eth_wei": int(pool.get("pre_volume_eth_wei", 0)),
    }
    r.xadd(SIGNALS_STREAM, {"payload": json.dumps(payload)}, maxlen=10000, approximate=True)
    log.info("BUY signal emitted pool=%s p_fatal=%.3f %s/%s",
             pool["pool_address"], p_fatal,
             pool.get("token0_symbol", "?"), pool.get("token1_symbol", "?"))


def was_published(r: redis.Redis, addr: str) -> bool:
    try:
        return bool(r.sismember(DEDUPE_KEY, addr.lower()))
    except Exception:
        return False


def mark_published(r: redis.Redis, addr: str) -> None:
    try:
        r.sadd(DEDUPE_KEY, addr.lower())
        r.expire(DEDUPE_KEY, DEDUPE_TTL_SEC)
    except Exception as e:
        log.warning("dedupe mark failed for %s: %s", addr, e)


def main():
    clf, params = load_model()
    p_cut = min(float(params["p_cut"]), P_CUT_MAX)
    W = params["W"]
    MIN_PRE = params["MIN_PRE"]
    r = redis.from_url(REDIS_URL, decode_responses=False)
    emitted: set[str] = set()
    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    log.info("Signal bridge started: poll=%ds W=%ds p_cut=%.4f size=$%.0f",
             POLL_SEC, W, p_cut, POSITION_SIZE_USD)

    while not stop:
        try:
            candidates = get_candidate_pools(r, W)
            skipped_dedupe = 0
            skipped_minpre = 0
            skipped_symbol = 0
            scored = 0
            rejected = 0
            emitted_count = 0
            for pool in candidates:
                addr = pool["pool_address"]
                if addr in emitted or was_published(r, addr):
                    skipped_dedupe += 1
                    continue
                if is_symbol_blacklisted(r, pool):
                    sym = _non_weth_symbol(pool)
                    log.info(
                        "REJECT %s symbol_blacklist=%s %s/%s",
                        addr, sym,
                        pool.get("token0_symbol", "?"), pool.get("token1_symbol", "?"),
                    )
                    emitted.add(addr)
                    mark_published(r, addr)
                    skipped_symbol += 1
                    continue
                raw = query_features(addr, W, MIN_PRE)
                if raw is None:
                    # Do NOT mark as published here. ClickHouse decoding can lag
                    # the chain by a few blocks; a pool may have its fifth
                    # first-window swap on-chain while the decoded table is still
                    # at 1-4 rows. If we dedupe now, we permanently skip an
                    # otherwise valid trade once the data catches up.
                    log.debug("skip %s: <%d pre-swaps in first %ds", addr, MIN_PRE, W)
                    skipped_minpre += 1
                    continue
                raw["orient"] = _orient_from_meta(pool)
                # Pre-entry WETH volume (wei) for the executor's volume-share cap.
                # WETH leg = token0 when orient==-1 (token0 is WETH), else token1.
                pool["pre_volume_eth_wei"] = int(
                    raw.get("wvol0", 0.0) if raw["orient"] == -1 else raw.get("wvol1", 0.0)
                )
                import pandas as pd
                fv = pd.DataFrame([compute_feature_vector(raw)], columns=FEATURE_NAMES)
                p_fatal = clf.predict_proba(fv)[0, 1]
                scored += 1
                if p_fatal > p_cut:
                    log.info("REJECT %s p_fatal=%.3f > p_cut=%.3f %s/%s",
                             addr, p_fatal, p_cut,
                             pool.get("token0_symbol", "?"), pool.get("token1_symbol", "?"))
                    emitted.add(addr)
                    mark_published(r, addr)
                    rejected += 1
                    continue
                emit_signal(r, pool, p_fatal)
                emitted.add(addr)
                mark_published(r, addr)
                emitted_count += 1
            if candidates:
                log.info(
                    "entry-window summary candidates=%d deduped=%d symbol_skip=%d minpre_skip=%d "
                    "scored=%d rejected=%d emitted=%d",
                    len(candidates), skipped_dedupe, skipped_symbol, skipped_minpre, scored,
                    rejected, emitted_count,
                )
        except Exception:
            log.exception("bridge cycle error")

        # housekeeping: cap emitted set
        if len(emitted) > 50000:
            emitted = set(list(emitted)[-10000:])

        time.sleep(POLL_SEC)

    log.info("Signal bridge stopped")


if __name__ == "__main__":
    main()
