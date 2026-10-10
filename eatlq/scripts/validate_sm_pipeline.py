"""
Validate Smart Money Pipeline -- full chain audit of the SM signal path.

Checks each link in the chain:
  1. market_ticks → tx_from populated?
  2. tx_from → matches SM wallet?
  3. pool address → is in pool:discovered:*?
  4. pool → is_new_pool (created < 24h)?
  5. SM buy count → reaches threshold?

Usage:
    REDIS_URL=redis://redis:6379/0 python scripts/validate_sm_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

import msgpack
import redis


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis.from_url(redis_url, decode_responses=False)

    stream = os.environ.get("MARKET_TICKS_STREAM", "market_ticks")
    sample_size = int(os.environ.get("SM_VALIDATE_SAMPLE", "2000"))

    entries = r.xrevrange(stream, count=sample_size)
    if not entries:
        print("ERROR: No entries in market_ticks stream")
        sys.exit(1)

    print(f"Sampled {len(entries)} entries from {stream}\n")

    sm_redis = redis.from_url(redis_url, decode_responses=True)
    sm_count = sm_redis.zcard("wallet:smart_money")
    sniper_count = sm_redis.zcard("wallet:sniper")

    pool_discovered_keys = sm_redis.keys("pool:discovered:*")
    total_pools = len(pool_discovered_keys)

    sm_threshold = int(os.environ.get("SMART_MONEY_BUY_THRESHOLD", "2"))
    max_age_h = int(os.environ.get("SMART_MONEY_POOL_MAX_AGE_H", "24"))
    now = time.time()

    print(f"Smart Money wallets: {sm_count}")
    print(f"Sniper wallets:      {sniper_count}")
    print(f"Discovered pools:    {total_pools}")
    print(f"SM threshold:        >= {sm_threshold}")
    print(f"New pool window:     < {max_age_h}h\n")

    # Count pools by age
    new_pools_24h = 0
    new_pools_48h = 0
    for pk in pool_discovered_keys:
        key_str = pk if isinstance(pk, str) else pk.decode()
        created = sm_redis.hget(key_str, "created_at_sec")
        disc = sm_redis.hget(key_str, "discovered_at_ms")
        if created and int(created) > 0:
            age_h = (now - int(created)) / 3600
        elif disc:
            age_h = (now * 1000 - int(disc)) / 3600000
        else:
            age_h = 9999
        if age_h < 24:
            new_pools_24h += 1
        if age_h < 48:
            new_pools_48h += 1

    print(f"Pools age < 24h:     {new_pools_24h}")
    print(f"Pools age < 48h:     {new_pools_48h}\n")

    # Analyze market_ticks
    total = 0
    has_tx_from = 0
    tx_from_is_sm = 0
    sm_on_discovered = 0
    sm_on_new_pool = 0
    sm_pools_new: Counter = Counter()
    sm_pools_old: Counter = Counter()
    unique_sm_senders: set = set()

    sm_buy_count_keys = sm_redis.keys("sm:buy_count:*")
    active_sm_counts = {}
    for k in sm_buy_count_keys:
        key_str = k if isinstance(k, str) else k.decode()
        pool_addr = key_str.replace("sm:buy_count:", "")
        count = sm_redis.scard(key_str)
        if count and int(count) > 0:
            active_sm_counts[pool_addr] = int(count)

    for entry_id, fields in entries:
        total += 1

        raw_tx_from = fields.get(b"tx_from", b"")
        tx_from = raw_tx_from.decode().lower().strip() if raw_tx_from else ""

        if not (tx_from and tx_from.startswith("0x") and len(tx_from) == 42):
            tx_from = ""
        else:
            has_tx_from += 1

        payload_raw = (
            fields.get(b"data")
            or fields.get(b"payload_msgpack")
            or fields.get(b"payload")
            or fields.get(b"tick")
        )
        pool_addr = ""
        if payload_raw:
            try:
                payload = msgpack.unpackb(payload_raw, raw=False)
                if isinstance(payload, list) and len(payload) >= 13:
                    pool_addr = str(payload[11]).lower()
                elif isinstance(payload, dict):
                    pool_addr = str(payload.get("address", "")).lower()
            except Exception:
                pass

        if not tx_from:
            continue

        is_sm = False
        for tier in ("wallet:smart_money", "wallet:sniper"):
            score = sm_redis.zscore(tier, tx_from)
            if score is not None:
                is_sm = True
                break

        if not is_sm:
            continue

        tx_from_is_sm += 1
        unique_sm_senders.add(tx_from)

        if not pool_addr:
            continue

        disc_key = f"pool:discovered:{pool_addr}"
        disc_at = sm_redis.hget(disc_key, "discovered_at_ms")
        created = sm_redis.hget(disc_key, "created_at_sec")
        is_discovered = disc_at is not None or (created is not None and int(created) > 0)

        if is_discovered:
            sm_on_discovered += 1

            if created and int(created) > 0:
                age_h = (now - int(created)) / 3600
            elif disc_at:
                age_h = (now * 1000 - int(disc_at)) / 3600000
            else:
                age_h = 9999

            if age_h < max_age_h:
                sm_on_new_pool += 1
                sm_pools_new[pool_addr] += 1
            else:
                sm_pools_old[pool_addr] += 1
        else:
            sm_pools_old[pool_addr] += 1

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    print(f"{'='*66}")
    print(f"  FULL PIPELINE CHAIN AUDIT ({total} ticks)")
    print(f"{'='*66}")
    print()
    print(f"  Link 1: tx_from populated?")
    print(f"    YES: {has_tx_from} ({pct(has_tx_from, total)})")
    if has_tx_from == 0:
        print(f"    !! CRITICAL: swap-poller not resolving senders")
    print()
    print(f"  Link 2: tx_from in SM wallet set?")
    print(f"    YES: {tx_from_is_sm} ({pct(tx_from_is_sm, has_tx_from)})")
    print(f"    Unique SM senders seen: {len(unique_sm_senders)}")
    if tx_from_is_sm == 0:
        print(f"    !! WARNING: No SM wallet overlap with swap activity")
    print()
    print(f"  Link 3: SM trade on discovered pool?")
    print(f"    YES: {sm_on_discovered} ({pct(sm_on_discovered, tx_from_is_sm)})")
    print()
    print(f"  Link 4: SM trade on NEW pool (< {max_age_h}h)?")
    print(f"    YES: {sm_on_new_pool} ({pct(sm_on_new_pool, tx_from_is_sm)})")
    if sm_on_new_pool == 0 and tx_from_is_sm > 0:
        print(f"    !! BOTTLENECK: SM wallets active but NOT on new pools")
    print()
    print(f"  Link 5: sm:buy_count reaching threshold ({sm_threshold})?")
    reached = {k: v for k, v in active_sm_counts.items() if v >= sm_threshold}
    print(f"    Pools with count >= {sm_threshold}: {len(reached)}")
    if reached:
        for p, c in sorted(reached.items(), key=lambda x: -x[1])[:10]:
            print(f"      {p[:16]}... count={c}")
    else:
        print(f"    !! BOTTLENECK: No pool reached SM threshold")
    print()

    print(f"  Top SM-active NEW pools:")
    if sm_pools_new:
        for p, c in sm_pools_new.most_common(10):
            buy_count = active_sm_counts.get(p, 0)
            print(f"    {p[:16]}... ticks={c}  sm_buy_count={buy_count}")
    else:
        print(f"    (none)")

    print(f"\n  Top SM-active OLD pools (not eligible):")
    if sm_pools_old:
        for p, c in sm_pools_old.most_common(5):
            print(f"    {p[:16]}... ticks={c}")
    else:
        print(f"    (none)")

    print(f"\n{'='*66}")

    # --- Signals stream audit: check for any recent pool_score entries ---
    print(f"\n  SIGNALS STREAM AUDIT (pool_score freeze check):")
    signals_stream = os.environ.get("SIGNALS_STREAM", "signals")
    audit_window_sec = 3600
    cutoff_ms_signals = int((now - audit_window_sec) * 1000)
    try:
        recent_signals = r.xrevrange(signals_stream.encode(), count=500)
        ps_found = 0
        ps_examples = []
        sm_found = 0
        for _, fields in recent_signals:
            raw = fields.get(b"payload") or fields.get(b"signal_json", b"")
            if not raw:
                continue
            try:
                sig = json.loads(raw.decode())
            except Exception:
                continue
            ts = int(sig.get("timestamp", 0))
            if ts < cutoff_ms_signals:
                continue
            src = sig.get("signal_source", "")
            if src == "pool_score":
                ps_found += 1
                if len(ps_examples) < 3:
                    ps_examples.append(sig.get("pool_address", "?")[:16] + "...")
            elif src == "smart_money":
                sm_found += 1
        print(f"    Signals in last {audit_window_sec//60}min — smart_money: {sm_found}  pool_score: {ps_found}")
        if ps_found > 0:
            print(f"    !! ALERT: {ps_found} pool_score signal(s) found in stream!")
            print(f"       Pools: {ps_examples}")
            print(f"       Rebuild and redeploy strategy + executor containers.")
        else:
            print(f"    OK: no pool_score signals in stream (freeze confirmed).")
    except Exception as e:
        print(f"    WARN: could not audit signals stream: {e}")
    print()

    # Diagnosis
    print(f"\n  DIAGNOSIS:")
    if has_tx_from == 0:
        print(f"  [BROKEN] swap-poller does not extract tx_from. Fix swap-poller first.")
    elif tx_from_is_sm == 0:
        print(f"  [WEAK] SM wallets not seen in swap activity.")
        print(f"         Check if Dune seed wallets are actually active on Base new tokens.")
    elif sm_on_new_pool == 0:
        print(f"  [BOTTLENECK] SM wallets trade only OLD pools.")
        print(f"         Possible causes:")
        print(f"         1. swap-poller only monitors established pools (top 20)")
        print(f"         2. Pool discovery bootstrap doesn't cover SM activity")
        print(f"         3. is_new_pool window ({max_age_h}h) too narrow")
        print(f"         FIX: Make swap-poller watch newly discovered pools dynamically")
    elif len(reached) == 0:
        print(f"  [ALMOST] SM seen on new pools but count < {sm_threshold}")
        print(f"         Single-SM events: {sm_on_new_pool}")
        print(f"         Consider: shadow-test >=1 threshold alongside >=2")
    else:
        print(f"  [OK] Pipeline functional. {len(reached)} pools reached SM threshold.")


if __name__ == "__main__":
    main()
