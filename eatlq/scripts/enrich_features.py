"""Run-003B-prep: enrich a labeled_dataset_*.csv with 3 new feature families.

Reads an existing labeled dataset that already has the 4 base features
(price_velocity_5m, buy_pressure_ratio, token_volatility_5m, activity_heat)
and joins in:

  A. Pool-age features
     - pool_age_sec, pool_age_min
     - pool_age_bucket (categorical: "<5m", "5-15m", "15-60m", "1-6h", "6-24h", ">24h")
     - is_new_pool_5m / 15m / 60m

  B. Smart-money activity features (5-min window ending at signal_ts)
     - sm_buy_count_5m, sm_sell_count_5m, sm_unique_buyers_5m
     - sm_net_flow_5m (WETH-equivalent, + = buying, - = selling)
     - sm_buy_pressure_ratio_5m (buys / (buys+sells) if any, else 0.5)
     - sm_first_seen_in_pool (1 if ANY smart wallet's first swap in this pool
       happened inside the 5-min window, else 0)
     - sm_wallet_tier_max, sm_wallet_tier_mean (ZSCORE from Redis zsets)

  C. Buyer-concentration features (5-min window ending at signal_ts)
     - unique_buyers_5m
     - buyer_gini_5m
     - top1_buyer_share_5m, top3_buyer_share_5m, top5_buyer_share_5m
     - new_buyer_ratio_5m (buyers who never bought this pool before this window)
     - repeat_buyer_ratio_5m
     - unique_buyer_entropy_5m  (properly recomputed; overrides dead CSV column)

Source data:
  ClickHouse.uniswap_swaps_clean_v1  — (pool_address, ts_sec, wallet, is_buy, a0_in, a1_in, a0_out, a1_out)
  ClickHouse.discovered_pools        — pool_type, created_block (for pool age fallback)
  Redis zsets                        — wallet:smart_money / diamond_hand / whale with tier score

Usage:
  python scripts/enrich_features.py \\
      --input  logs/labeled_dataset_run003a_v2_only.csv \\
      --output logs/labeled_dataset_run003a_v2_only_enriched.csv

Idempotent: running twice overwrites output.
"""
from __future__ import annotations
import argparse
import base64
import csv
import json
import logging
import math
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("enrich")


WINDOW_SEC = 300  # 5-min feature window


# ---------- ClickHouse helper ---------------------------------------------

def _ch_env() -> Tuple[str, str, str, str]:
    return (
        os.environ.get("CLICKHOUSE_URL", "http://laszlo-clickhouse:8123"),
        os.environ.get("CLICKHOUSE_USER", "default"),
        os.environ.get("CLICKHOUSE_PASSWORD", ""),
        os.environ.get("CLICKHOUSE_DB", "laszlo"),
    )


def ch_query(sql: str, fmt: str = "JSONEachRow") -> str:
    url, user, pw, db = _ch_env()
    req_url = f"{url}/?database={db}&default_format={fmt}"
    req = urllib.request.Request(req_url, data=sql.encode(), method="POST")
    if user:
        auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    return urllib.request.urlopen(req, timeout=120).read().decode()


def ch_rows(sql: str) -> List[dict]:
    body = ch_query(sql).strip()
    return [json.loads(line) for line in body.splitlines() if line]


# ---------- Redis smart-money tiers ---------------------------------------

def load_smart_money(
    smart_money_key: str = "wallet:smart_money",
    diamond_key: str = "wallet:diamond_hand",
    whale_key: str = "wallet:whale",
) -> Dict[str, Dict[str, float]]:
    """Return {tier: {wallet_lower: score}} for each tier zset.

    Uses the `redis` python client against REDIS_URL (preferred) or
    REDIS_HOST/PORT. Falls back to empty dicts if unreachable.
    """
    import redis  # type: ignore
    tier_keys = {
        "smart_money": smart_money_key,
        "diamond_hand": diamond_key,
        "whale": whale_key,
    }
    out: Dict[str, Dict[str, float]] = {t: {} for t in tier_keys}
    url = os.environ.get("REDIS_URL")
    try:
        if url:
            r = redis.Redis.from_url(url)
        else:
            r = redis.Redis(
                host=os.environ.get("REDIS_HOST", "redis"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
            )
        r.ping()
    except Exception as e:
        log.warning("redis connection failed: %s", e)
        return out

    for tier, redis_key in tier_keys.items():
        try:
            z = r.zrange(redis_key, 0, -1, withscores=True)
            for w, s in z:
                wl = (w.decode() if isinstance(w, bytes) else str(w)).lower()
                out[tier][wl] = float(s)
        except Exception as e:
            log.warning("redis zrange %s (%s) failed: %s", tier, redis_key, e)
    sizes = {t: len(out[t]) for t in tier_keys}
    log.info("smart-money tiers loaded from %s: %s", smart_money_key, sizes)
    return out


# ---------- Pool age helper -----------------------------------------------

def load_pool_first_swap(pools: List[str]) -> Dict[str, int]:
    """Return {pool: first_swap_ts_sec}. Pool addresses are normalised to lower."""
    if not pools:
        return {}
    pools_lc = [p.lower() for p in pools]
    chunks = [pools_lc[i:i + 1000] for i in range(0, len(pools_lc), 1000)]
    out: Dict[str, int] = {}
    for chunk in chunks:
        ph = ",".join(f"'{p}'" for p in chunk)
        sql = f"""
        SELECT lower(pool_address) AS p, toUInt32(min(ts_sec)) AS first_ts
        FROM uniswap_swaps_clean_v1
        WHERE lower(pool_address) IN ({ph})
        GROUP BY p
        """
        for row in ch_rows(sql):
            out[row["p"]] = int(row["first_ts"])
    return out


def load_pool_created_fallback(pools: List[str]) -> Dict[str, int]:
    """Return {pool: discovered_at_ts}.

    Used as a fallback for pools that have no swaps in our ingested window.
    `discovered_at` is when our indexer first saw it; not perfect, but gives
    a consistent lower bound for age.
    """
    if not pools:
        return {}
    pools_lc = [p.lower() for p in pools]
    chunks = [pools_lc[i:i + 1000] for i in range(0, len(pools_lc), 1000)]
    out: Dict[str, int] = {}
    for chunk in chunks:
        ph = ",".join(f"'{p}'" for p in chunk)
        sql = f"""
        SELECT lower(pool_address) AS p, toUInt32(min(toUnixTimestamp(discovered_at))) AS ts
        FROM discovered_pools
        WHERE lower(pool_address) IN ({ph})
        GROUP BY p
        """
        for row in ch_rows(sql):
            out[row["p"]] = int(row["ts"])
    return out


def pool_age_bucket(age_sec: int) -> str:
    if age_sec < 300:
        return "lt_5m"
    if age_sec < 900:
        return "5_15m"
    if age_sec < 3600:
        return "15_60m"
    if age_sec < 21600:
        return "1_6h"
    if age_sec < 86400:
        return "6_24h"
    return "gt_24h"


# ---------- Window aggregation --------------------------------------------

def load_window_swaps(
    pool_windows: List[Tuple[str, int]],
) -> Dict[Tuple[str, int], List[dict]]:
    """For each (pool, signal_ts) pair, fetch all swaps in [signal_ts-300, signal_ts).

    Executes in batches grouped by distinct pool. Returns dict keyed by
    the original (pool_lc, signal_ts) with each value being a list of swap
    rows:  {wallet, is_buy, ts_sec, a0_in, a1_in, a0_out, a1_out, weth_side}.

    weth_side = 0 if WETH is token0, 1 if WETH is token1 (determined per pool
    by looking at which side has non-zero amount_in during a buy).
    """
    if not pool_windows:
        return {}
    by_pool: Dict[str, List[int]] = defaultdict(list)
    for p, ts in pool_windows:
        by_pool[p].append(ts)

    pools = list(by_pool.keys())
    chunks = [pools[i:i + 300] for i in range(0, len(pools), 300)]
    out: Dict[Tuple[str, int], List[dict]] = defaultdict(list)
    min_global = min(ts - WINDOW_SEC for ts in pool_windows_flat_ts(pool_windows))
    max_global = max(ts for ts in pool_windows_flat_ts(pool_windows))
    for idx, chunk in enumerate(chunks):
        ph = ",".join(f"'{p}'" for p in chunk)
        sql = f"""
        SELECT
          lower(pool_address) AS p,
          toUInt32(ts_sec) AS ts,
          wallet,
          is_buy,
          a0_in, a1_in, a0_out, a1_out
        FROM uniswap_swaps_clean_v1
        WHERE lower(pool_address) IN ({ph})
          AND ts_sec BETWEEN {min_global} AND {max_global}
        """
        rows = ch_rows(sql)
        log.info("chunk %d/%d: %d swaps", idx + 1, len(chunks), len(rows))
        # bucket rows into their matching (pool, signal_ts) windows
        by_pool_swaps: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            by_pool_swaps[r["p"]].append(r)
        for p, tslist in by_pool.items():
            ps = by_pool_swaps.get(p, [])
            if not ps:
                continue
            ps.sort(key=lambda r: r["ts"])
            for ts in tslist:
                lo = ts - WINDOW_SEC
                hi = ts
                window = [r for r in ps if lo <= r["ts"] < hi]
                if window:
                    out[(p, ts)] = window
    return out


def pool_windows_flat_ts(pool_windows: List[Tuple[str, int]]) -> List[int]:
    return [ts for _, ts in pool_windows]


def weth_side_for_pool(swaps: List[dict]) -> int | None:
    """Infer which side is WETH by looking at any buy: for a buy, `in` side is WETH.

    Returns 0 (WETH=token0) or 1 (WETH=token1), or None if ambiguous.
    """
    for r in swaps:
        try:
            is_buy = int(r.get("is_buy", 0))
        except Exception:
            continue
        if not is_buy:
            continue
        a0 = float(r.get("a0_in", 0) or 0)
        a1 = float(r.get("a1_in", 0) or 0)
        if a0 > 0 and a1 == 0:
            return 0
        if a1 > 0 and a0 == 0:
            return 1
    # fallback via sells: for a sell, `in` side is TOKEN (WETH is `out`)
    for r in swaps:
        try:
            is_buy = int(r.get("is_buy", 0))
        except Exception:
            continue
        if is_buy:
            continue
        a0o = float(r.get("a0_out", 0) or 0)
        a1o = float(r.get("a1_out", 0) or 0)
        if a0o > 0 and a1o == 0:
            return 0
        if a1o > 0 and a0o == 0:
            return 1
    return None


# ---------- Feature computation -------------------------------------------

def gini(values: List[float]) -> float:
    if not values:
        return 0.0
    arr = sorted(v for v in values if v > 0)
    n = len(arr)
    if n == 0:
        return 0.0
    cum = 0.0
    s = sum(arr)
    if s <= 0:
        return 0.0
    for i, v in enumerate(arr, 1):
        cum += i * v
    return (2.0 * cum) / (n * s) - (n + 1) / n


def shannon_entropy(weights: List[float]) -> float:
    total = sum(w for w in weights if w > 0)
    if total <= 0:
        return 0.0
    ent = 0.0
    for w in weights:
        if w <= 0:
            continue
        p = w / total
        ent -= p * math.log(p + 1e-12)
    return ent


def compute_features_for_row(
    pool: str,
    signal_ts: int,
    swaps: List[dict],
    tiers: Dict[str, Dict[str, float]],
    first_swap_by_pool: Dict[str, int],
    prior_buyers_by_pool: Dict[str, set],
    sm_first_seen_in_pool_cache: Dict[str, int],
    weth_side_cache: Dict[str, int | None],
) -> Dict[str, float]:
    """Compute all enriched features for one labeled row."""
    pool_lc = pool.lower()

    # --- pool age ---
    first_ts = first_swap_by_pool.get(pool_lc, 0)
    age_sec = max(0, signal_ts - first_ts) if first_ts else 0
    feat: Dict[str, float | str] = {
        "pool_age_sec": age_sec,
        "pool_age_min": round(age_sec / 60.0, 2),
        "pool_age_bucket": pool_age_bucket(age_sec),
        "is_new_pool_5m": int(age_sec < 300),
        "is_new_pool_15m": int(age_sec < 900),
        "is_new_pool_60m": int(age_sec < 3600),
    }

    # --- smart-money + concentration + entropy features (need window swaps) ---
    sm_set = set(tiers.get("smart_money", {}).keys())
    dh_set = set(tiers.get("diamond_hand", {}).keys())
    wh_set = set(tiers.get("whale", {}).keys())
    sm_score = tiers.get("smart_money", {})
    dh_score = tiers.get("diamond_hand", {})
    wh_score = tiers.get("whale", {})

    wside = weth_side_cache.get(pool_lc)
    if wside is None:
        wside = weth_side_for_pool(swaps)
        weth_side_cache[pool_lc] = wside

    sm_buy_count = 0
    sm_sell_count = 0
    sm_net_flow = 0.0
    sm_abs_volume = 0.0
    sm_buyers: set = set()
    sm_tier_scores: List[float] = []

    per_buyer_weth: Dict[str, float] = defaultdict(float)

    for r in swaps:
        try:
            is_buy = int(r.get("is_buy", 0))
        except Exception:
            continue
        wallet = (r.get("wallet") or "").lower()
        try:
            a0_in = float(r.get("a0_in", 0) or 0)
            a1_in = float(r.get("a1_in", 0) or 0)
            a0_out = float(r.get("a0_out", 0) or 0)
            a1_out = float(r.get("a1_out", 0) or 0)
        except Exception:
            continue

        if wside == 0:
            weth_in = a0_in
            weth_out = a0_out
        elif wside == 1:
            weth_in = a1_in
            weth_out = a1_out
        else:
            weth_in = max(a0_in, a1_in)
            weth_out = max(a0_out, a1_out)

        if is_buy:
            # buyer spent WETH, received TOKEN → WETH flow IN to pool (buy pressure)
            per_buyer_weth[wallet] += float(weth_in)

        # smart-money aggregation
        in_sm = wallet in sm_set or wallet in dh_set or wallet in wh_set
        if in_sm:
            # pick highest available tier score
            s_opts = []
            if wallet in sm_score:
                s_opts.append(sm_score[wallet])
            if wallet in dh_score:
                s_opts.append(dh_score[wallet])
            if wallet in wh_score:
                s_opts.append(wh_score[wallet])
            if s_opts:
                sm_tier_scores.append(max(s_opts))
            if is_buy:
                sm_buy_count += 1
                sm_buyers.add(wallet)
                sm_net_flow += float(weth_in)
                sm_abs_volume += float(weth_in)
            else:
                sm_sell_count += 1
                sm_net_flow -= float(weth_out)
                sm_abs_volume += float(weth_out)

    # sm_first_seen_in_pool: was any sm wallet's FIRST interaction in this pool
    # during the 5-min window? This is expensive to compute per-row, so the
    # cache is pool-scoped ("forever first-seen"): if a SM wallet first ever
    # appears in this pool at ts_X, then sm_first_seen_in_pool=1 for rows
    # whose window contains ts_X.
    cache_key = pool_lc
    if cache_key not in sm_first_seen_in_pool_cache:
        sm_first_seen_in_pool_cache[cache_key] = -1  # not-yet-computed flag
    # We defer the cache build: caller computes first-seen map once per pool
    # (see main()). Here we just read from prior_buyers_by_pool, which is the
    # "wallets seen in pool up to but excluding the current window" set.
    prior = prior_buyers_by_pool.get(pool_lc, set())
    sm_new_in_window = 0
    for w in sm_buyers:
        if w not in prior:
            sm_new_in_window = 1
            break
    feat["sm_first_seen_in_pool"] = sm_new_in_window

    sm_total = sm_buy_count + sm_sell_count
    feat["sm_buy_count_5m"] = sm_buy_count
    feat["sm_sell_count_5m"] = sm_sell_count
    feat["sm_unique_buyers_5m"] = len(sm_buyers)
    feat["sm_net_flow_5m"] = round(sm_net_flow, 6)
    feat["sm_buy_pressure_ratio_5m"] = (
        round(sm_buy_count / sm_total, 4) if sm_total > 0 else 0.5
    )
    feat["sm_wallet_tier_max"] = round(max(sm_tier_scores), 6) if sm_tier_scores else 0.0
    feat["sm_wallet_tier_mean"] = (
        round(sum(sm_tier_scores) / len(sm_tier_scores), 6) if sm_tier_scores else 0.0
    )

    # --- buyer concentration on per_buyer_weth ---
    buyers = list(per_buyer_weth.keys())
    weights = list(per_buyer_weth.values())
    feat["unique_buyers_5m"] = len(buyers)
    feat["buyer_gini_5m"] = round(gini(weights), 6) if buyers else 0.0
    total_w = sum(weights) if weights else 0.0
    sorted_w = sorted(weights, reverse=True)
    for k in (1, 3, 5):
        top = sum(sorted_w[:k])
        feat[f"top{k}_buyer_share_5m"] = round(top / total_w, 6) if total_w > 0 else 0.0

    feat["unique_buyer_entropy_5m"] = round(shannon_entropy(weights), 6)
    feat["_buyer_weth_total"] = round(total_w, 6)
    feat["_sm_abs_volume_total"] = round(sm_abs_volume, 6)
    feat["_sm_feature_non_zero"] = int(
        sm_buy_count > 0
        or sm_sell_count > 0
        or len(sm_buyers) > 0
        or abs(sm_net_flow) > 0
        or len(sm_tier_scores) > 0
    )
    feat["_sm_volume_share_5m"] = round(sm_abs_volume / total_w, 6) if total_w > 0 else 0.0

    new_buyers = sum(1 for b in buyers if b not in prior)
    feat["new_buyer_ratio_5m"] = (
        round(new_buyers / len(buyers), 4) if buyers else 0.0
    )
    feat["repeat_buyer_ratio_5m"] = (
        round(1 - (new_buyers / len(buyers)), 4) if buyers else 0.0
    )

    return feat


def build_prior_buyers_index(pool_list: List[str]) -> Dict[str, Dict[int, set]]:
    """For each pool, build a sorted list of (ts, buyer) events so the caller
    can figure out the "prior buyers" set for any signal_ts cheaply.

    Returns {pool: [(ts_sec, buyer), ...]} in ascending ts order.

    We implement "prior" lookup per-row by bisect on the ts list.
    """
    if not pool_list:
        return {}
    pools_lc = [p.lower() for p in pool_list]
    chunks = [pools_lc[i:i + 300] for i in range(0, len(pools_lc), 300)]
    idx: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for ci, chunk in enumerate(chunks):
        ph = ",".join(f"'{p}'" for p in chunk)
        sql = f"""
        SELECT lower(pool_address) AS p, toUInt32(ts_sec) AS ts, wallet
        FROM uniswap_swaps_clean_v1
        WHERE lower(pool_address) IN ({ph}) AND is_buy=1
        """
        rows = ch_rows(sql)
        log.info("prior-buyers chunk %d/%d: %d buy events", ci + 1, len(chunks), len(rows))
        for r in rows:
            idx[r["p"]].append((int(r["ts"]), (r.get("wallet") or "").lower()))
    for p in idx:
        idx[p].sort(key=lambda x: x[0])
    return idx


def prior_buyers_before(index: List[Tuple[int, str]], cutoff_ts: int) -> set:
    """Return set of buyers who bought before `cutoff_ts` (exclusive of window)."""
    if not index:
        return set()
    import bisect
    keys = [t for t, _ in index]
    i = bisect.bisect_left(keys, cutoff_ts)
    return {w for _, w in index[:i]}


# ---------- Main ----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="input labeled CSV")
    ap.add_argument("--output", required=True, help="output enriched CSV")
    ap.add_argument(
        "--smart-money-key",
        default="wallet:smart_money",
        help="redis zset key used as the primary smart-money source",
    )
    ap.add_argument(
        "--summary-json",
        default="",
        help="optional path to write coverage summary json",
    )
    args = ap.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        log.error("input not found: %s", in_path)
        sys.exit(1)

    rows: List[dict] = []
    with in_path.open() as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            if not r.get("timestamp") or not r.get("token_address"):
                continue
            rows.append(r)
    log.info("loaded %d rows from %s", len(rows), in_path)

    pools = sorted({r["token_address"].lower() for r in rows})
    log.info("unique pools: %d", len(pools))

    # 1) pool age
    log.info("loading first-swap per pool ...")
    first_by_pool = load_pool_first_swap(pools)
    log.info("first-swap resolved: %d/%d pools", len(first_by_pool), len(pools))
    missing = [p for p in pools if p not in first_by_pool]
    if missing:
        log.info("fallback to discovered_at for %d missing pools", len(missing))
        fb = load_pool_created_fallback(missing)
        first_by_pool.update(fb)
        log.info("after fallback: %d/%d", len(first_by_pool), len(pools))

    # 2) smart-money tiers
    tiers = load_smart_money(smart_money_key=args.smart_money_key)

    # 3) per-pool prior buyer index (for new_buyer_ratio + sm_first_seen)
    log.info("loading prior-buyers index ...")
    prior_idx = build_prior_buyers_index(pools)
    log.info("prior-buyers index built for %d pools", len(prior_idx))

    # 4) per-row window swap data
    pool_windows = [(r["token_address"].lower(), int(r["timestamp"]) // 1000) for r in rows]
    log.info("loading %d window swap sets ...", len(pool_windows))
    window_cache = load_window_swaps(pool_windows)
    log.info("windows with swaps: %d/%d",
             sum(1 for k in pool_windows if k in window_cache), len(pool_windows))

    # 5) compute features row by row
    weth_side_cache: Dict[str, int | None] = {}
    sm_first_seen_cache: Dict[str, int] = {}

    enriched_cols = [
        "pool_age_sec", "pool_age_min", "pool_age_bucket",
        "is_new_pool_5m", "is_new_pool_15m", "is_new_pool_60m",
        "sm_buy_count_5m", "sm_sell_count_5m", "sm_unique_buyers_5m",
        "sm_net_flow_5m", "sm_buy_pressure_ratio_5m",
        "sm_first_seen_in_pool", "sm_wallet_tier_max", "sm_wallet_tier_mean",
        "unique_buyers_5m", "buyer_gini_5m",
        "top1_buyer_share_5m", "top3_buyer_share_5m", "top5_buyer_share_5m",
        "new_buyer_ratio_5m", "repeat_buyer_ratio_5m",
        "unique_buyer_entropy_5m",  # overrides any pre-existing dead column
    ]

    with in_path.open() as fin, out_path.open("w", newline="") as fout:
        rdr = csv.DictReader(fin)
        base_fields = [f for f in rdr.fieldnames if f != "unique_buyer_entropy_5m"]
        fieldnames = base_fields + enriched_cols
        w = csv.DictWriter(fout, fieldnames=fieldnames)
        w.writeheader()

        non_empty_windows = 0
        sm_feature_non_zero_rows = 0
        sm_buyer_hit_rows = 0
        sm_volume_num = 0.0
        sm_volume_den = 0.0
        for r in rows:
            pool_lc = r["token_address"].lower()
            ts_ms = int(r["timestamp"])
            ts_sec = ts_ms // 1000

            swaps = window_cache.get((pool_lc, ts_sec), [])
            if swaps:
                non_empty_windows += 1
            prior = prior_buyers_before(prior_idx.get(pool_lc, []), ts_sec - WINDOW_SEC)
            prior_buyers_map = {pool_lc: prior}

            feats = compute_features_for_row(
                pool_lc, ts_sec, swaps, tiers,
                first_by_pool, prior_buyers_map,
                sm_first_seen_cache, weth_side_cache,
            )
            row = {k: v for k, v in r.items() if k != "unique_buyer_entropy_5m"}
            row.update({k: feats.get(k, 0) for k in enriched_cols})
            w.writerow(row)
            sm_feature_non_zero_rows += int(feats.get("_sm_feature_non_zero", 0))
            sm_buyer_hit_rows += int(float(feats.get("sm_unique_buyers_5m", 0) or 0) > 0)
            sm_volume_num += float(feats.get("_sm_abs_volume_total", 0.0) or 0.0)
            sm_volume_den += float(feats.get("_buyer_weth_total", 0.0) or 0.0)

        log.info("non-empty 5-min windows: %d/%d", non_empty_windows, len(rows))

    log.info("output=%s", out_path)
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "input": str(in_path),
            "output": str(out_path),
            "smart_money_key": args.smart_money_key,
            "rows": len(rows),
            "non_empty_windows": non_empty_windows,
            "sm_feature_non_zero_rows": sm_feature_non_zero_rows,
            "sm_feature_non_zero_rate": round(sm_feature_non_zero_rows / max(len(rows), 1), 6),
            "sm_buyer_hit_rows": sm_buyer_hit_rows,
            "sm_buyer_hit_rate": round(sm_buyer_hit_rows / max(len(rows), 1), 6),
            "sm_volume_share": round(sm_volume_num / max(sm_volume_den, 1e-12), 6),
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("summary=%s", summary_path)


if __name__ == "__main__":
    main()
