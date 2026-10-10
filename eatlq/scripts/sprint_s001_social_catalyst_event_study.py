#!/usr/bin/env python3
"""Sprint S001 - Social Catalyst Event Study (offline research only).

Scope:
- Read-only fetch up to 20 recent original/quote posts per KOL from X API v2.
- Parse contract addresses and cashtags from post text.
- Map posts to Base WETH-leg pools via ClickHouse pool registry.
- Compute short-horizon funds-flow and tradable returns from swap prints:
  entry_0, entry_30s, entry_120s, exit_5m, exit_15m, exit_30m.
- Emit research artifacts only (no Redis signals, no executor wiring, no live).
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s001"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RAW_POSTS_CSV = OUT_DIR / "raw_x_posts.csv"
EVENTS_CSV = OUT_DIR / "social_token_events.csv"
RESOLUTION_CANDIDATES_CSV = OUT_DIR / "token_resolution_candidates.csv"
VENUE_INVENTORY_CSV = OUT_DIR / "social_token_venue_inventory.csv"
CASHTAG_ALIAS_TODO_CSV = OUT_DIR / "cashtag_alias_todo.csv"
LOCAL_POOL_VERIFICATION_CSV = OUT_DIR / "local_pool_verification.csv"
ALIASES_CSV = OUT_DIR / "cashtag_base_aliases.csv"
READOUT_MD = OUT_DIR / "event_study_readout.md"
FLAGS_JSON = OUT_DIR / "decision_flags.json"
TARGETED_BACKFILL_READOUT_MD = OUT_DIR / "targeted_registry_backfill_readout.md"

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID = 8453
WETH_BASE = "0x4200000000000000000000000000000000000006"

KOL_HANDLES = [
    "btcbabycow",
    "iruletrenches",
    "cutepanda",
    "0xLuo",
    "basezh",
]

MAX_POSTS_PER_KOL = 20
MAX_RAW_POSTS = 100
CONTROL_POOL_TARGET = 20
REUSE_EXISTING_RAW_POSTS = True

X_API_BASE = "https://api.x.com/2"
X_BEARER_ENV_KEYS = [
    "X_BEARER_TOKEN",
    "X_API_BEARER_TOKEN",
    "TWITTER_BEARER_TOKEN",
]

CA_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
CASHTAG_RE = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9_]{1,15})\b")


@dataclass
class PoolMeta:
    pool: str
    token0: str
    token1: str
    token0_symbol: str
    token1_symbol: str
    fee_bps: int


@dataclass
class ResolutionRecord:
    mention_id: str
    tweet_id: str
    handle: str
    created_at: str
    mention_type: str
    mention_value: str
    cashtag: str
    chain: str
    resolution_status: str
    resolution_source: str
    resolved_token_address: str
    resolved_pool_address: str
    candidate_pool_count: int
    skip_reason: str
    notes: str


@dataclass
class VenueInventoryRecord:
    mention_id: str
    tweet_id: str
    handle: str
    created_at: str
    mention_type: str
    mention_value: str
    chain_hint: str
    in_base_v3_registry: bool
    in_ethereum_v3_registry: bool
    base_non_v3_pool_count: int
    ethereum_non_v3_pool_count: int
    venue_assessment: str
    actionability: str
    reason: str


@dataclass
class CashtagTodoRecord:
    cashtag: str
    mention_count: int
    authors: str
    example_text: str
    suggested_lookup_status: str


@dataclass
class LocalPoolVerificationRecord:
    cashtag: str
    token_address: str
    chain: str
    base_v3_registry_pool_count: int
    base_v3_registry_weth_leg_pool_count: int
    base_discovered_pool_count: int
    base_discovered_v3_pool_count: int
    base_discovered_non_v3_pool_count: int
    base_discovered_weth_leg_pool_count: int
    base_v3_swaps_30d: int
    base_all_swaps_30d: int
    registry_missing_or_stale: bool
    recommended_actionability: str
    verification_note: str


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _to_dt(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).astimezone(UTC)


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _int256_from_hex(raw: str) -> int:
    s = raw[2:] if raw.startswith("0x") else raw
    if not s:
        return 0
    val = int(s, 16)
    if val >= 2**255:
        val -= 2**256
    return val


def _ch(sql: str, timeout: int = 45) -> list[list[str]]:
    cmd = [
        "docker",
        "exec",
        CH_CONTAINER,
        "clickhouse-client",
        "--format",
        "CSV",
        "--query",
        sql,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"clickhouse query failed: {proc.stderr.strip()[:240]}")
    rows: list[list[str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        rows.append(next(csv.reader([line])))
    return rows


def _load_bearer() -> str:
    for key in X_BEARER_ENV_KEYS:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def _x_get(path: str, params: dict[str, Any], bearer: str) -> dict[str, Any]:
    q = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{X_API_BASE}{path}"
    if q:
        url = f"{url}?{q}"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "EATLQ-S001-Social-Study/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"X API HTTP {e.code}: {body}") from e
    except URLError as e:
        raise RuntimeError(f"X API network error: {e}") from e


def fetch_recent_posts(handles: list[str], bearer: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for handle in handles:
        user_doc = _x_get(
            f"/users/by/username/{handle}",
            {"user.fields": "id,username,name"},
            bearer,
        )
        user = user_doc.get("data") or {}
        uid = user.get("id")
        if not uid:
            continue
        tweets_doc = _x_get(
            f"/users/{uid}/tweets",
            {
                "max_results": MAX_POSTS_PER_KOL,
                "exclude": "retweets",
                "tweet.fields": (
                    "created_at,text,lang,public_metrics,referenced_tweets,entities"
                ),
            },
            bearer,
        )
        for t in tweets_doc.get("data", []):
            refs = t.get("referenced_tweets") or []
            is_retweet = any(r.get("type") == "retweeted" for r in refs)
            is_reply = any(r.get("type") == "replied_to" for r in refs)
            if is_retweet or is_reply:
                continue
            txt = _clean_text(t.get("text", ""))
            out.append(
                {
                    "handle": f"@{handle}",
                    "tweet_id": t.get("id", ""),
                    "created_at": t.get("created_at", ""),
                    "is_quote": any(r.get("type") == "quoted" for r in refs),
                    "text": txt,
                    "lang": t.get("lang", ""),
                    "like_count": (t.get("public_metrics") or {}).get("like_count", 0),
                    "retweet_count": (t.get("public_metrics") or {}).get(
                        "retweet_count", 0
                    ),
                    "reply_count": (t.get("public_metrics") or {}).get("reply_count", 0),
                    "quote_count": (t.get("public_metrics") or {}).get("quote_count", 0),
                }
            )
            if len(out) >= MAX_RAW_POSTS:
                return out
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out[:MAX_RAW_POSTS]


def write_raw_posts(rows: list[dict[str, Any]]) -> None:
    cols = [
        "handle",
        "tweet_id",
        "created_at",
        "is_quote",
        "lang",
        "like_count",
        "retweet_count",
        "reply_count",
        "quote_count",
        "text",
    ]
    with RAW_POSTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def load_raw_posts() -> list[dict[str, Any]]:
    if not RAW_POSTS_CSV.exists():
        return []
    with RAW_POSTS_CSV.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def ensure_aliases_template() -> None:
    if ALIASES_CSV.exists():
        return
    cols = ["cashtag", "token_address", "chain", "source", "confidence", "notes"]
    with ALIASES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()


def load_aliases() -> dict[str, list[dict[str, str]]]:
    ensure_aliases_template()
    out: dict[str, list[dict[str, str]]] = {}
    with ALIASES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tag = (row.get("cashtag") or "").strip().upper()
            token = (row.get("token_address") or "").strip().lower()
            chain = (row.get("chain") or "").strip().lower()
            if not tag or not token:
                continue
            if chain and chain != "base":
                continue
            out.setdefault(tag, []).append(row)
    return out


def load_weth_pools() -> list[PoolMeta]:
    rows = _ch(
        f"""
SELECT
  lower(pool_address),
  lower(token0_address),
  lower(token1_address),
  token0_symbol,
  token1_symbol,
  toUInt32(fee_bps)
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={CHAIN_ID}
  AND (lower(token0_address)='{WETH_BASE}' OR lower(token1_address)='{WETH_BASE}')
"""
    )
    pools: list[PoolMeta] = []
    for r in rows:
        if len(r) < 6:
            continue
        pools.append(
            PoolMeta(
                pool=r[0],
                token0=r[1],
                token1=r[2],
                token0_symbol=(r[3] or "").upper(),
                token1_symbol=(r[4] or "").upper(),
                fee_bps=int(r[5]),
            )
        )
    return pools


def _token_in_registry(token_addr: str) -> bool:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={CHAIN_ID}
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return bool(rows and int(rows[0][0]) > 0)


def _token_in_swaps(token_addr: str, around: datetime) -> bool:
    dt0 = _fmt_dt(around - timedelta(days=14))
    dt1 = _fmt_dt(around + timedelta(days=1))
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_swaps s
INNER JOIN eatlq.uniswap_v3_pool_registry r
  ON lower(s.pool_address)=lower(r.pool_address) AND s.chain_id=r.chain_id
WHERE s.chain_id={CHAIN_ID}
  AND s.block_time >= toDateTime64('{dt0}', 3, 'UTC')
  AND s.block_time <  toDateTime64('{dt1}', 3, 'UTC')
  AND (
    lower(r.token0_address)='{token_addr}' OR lower(r.token1_address)='{token_addr}'
  )
"""
    )
    return bool(rows and int(rows[0][0]) > 0)


def parse_candidates(post_text: str) -> tuple[list[str], list[str]]:
    cas = sorted({m.lower() for m in CA_RE.findall(post_text)})
    cashtags = sorted({m.upper() for m in CASHTAG_RE.findall(post_text)})
    return cas, cashtags


def _pool_activity_score(pool: str, around: datetime) -> int:
    dt0 = _fmt_dt(around - timedelta(hours=12))
    dt1 = _fmt_dt(around + timedelta(hours=12))
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID}
  AND pool_address='{pool}'
  AND block_time >= toDateTime64('{dt0}', 3, 'UTC')
  AND block_time <  toDateTime64('{dt1}', 3, 'UTC')
""",
        timeout=40,
    )
    if not rows:
        return 0
    return int(rows[0][0])


def pick_pool_for_token(
    token_addr: str,
    event_ts: datetime,
    weth_pools_by_token: dict[str, list[PoolMeta]],
) -> PoolMeta | None:
    cands = weth_pools_by_token.get(token_addr.lower(), [])
    if not cands:
        return None
    scored = [(p, _pool_activity_score(p.pool, event_ts)) for p in cands]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0] if scored else None


def _cashtag_pool_candidates(cashtag: str, weth_pools: list[PoolMeta]) -> list[PoolMeta]:
    return [
        p
        for p in weth_pools
        if (p.token0 != WETH_BASE and p.token0_symbol == cashtag)
        or (p.token1 != WETH_BASE and p.token1_symbol == cashtag)
    ]


def _alias_token_candidates(cashtag: str, aliases: dict[str, list[dict[str, str]]]) -> list[str]:
    rows = aliases.get(cashtag, [])
    return sorted({(r.get("token_address") or "").strip().lower() for r in rows if r.get("token_address")})


def _token_side(pool: PoolMeta) -> tuple[str, str]:
    if pool.token0 == WETH_BASE:
        return pool.token1, pool.token1_symbol
    return pool.token0, pool.token0_symbol


def _swap_rows(pool: str, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    rows = _ch(
        f"""
SELECT
  toString(block_time),
  amount0_raw,
  amount1_raw
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID}
  AND pool_address='{pool}'
  AND block_time >= toDateTime64('{_fmt_dt(start_dt)}', 3, 'UTC')
  AND block_time <  toDateTime64('{_fmt_dt(end_dt)}', 3, 'UTC')
  AND block_time IS NOT NULL
ORDER BY block_time ASC
""",
        timeout=45,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if len(r) < 3:
            continue
        t = _to_dt(r[0].replace(" ", "T") + "Z")
        a0 = _int256_from_hex(r[1])
        a1 = _int256_from_hex(r[2])
        out.append({"t": t, "a0": a0, "a1": a1})
    return out


def _price_weth_per_token(pool: PoolMeta, a0: int, a1: int) -> float | None:
    if pool.token0 == WETH_BASE:
        if a1 == 0:
            return None
        return abs(a0) / abs(a1)
    if a0 == 0:
        return None
    return abs(a1) / abs(a0)


def _first_price_after(
    swaps: list[dict[str, Any]], pool: PoolMeta, target: datetime, grace_s: int = 600
) -> float | None:
    grace_end = target + timedelta(seconds=grace_s)
    for s in swaps:
        if target <= s["t"] <= grace_end:
            p = _price_weth_per_token(pool, s["a0"], s["a1"])
            if p and p > 0:
                return p
    return None


def _net_weth_flow(swaps: list[dict[str, Any]], pool: PoolMeta, t0: datetime, t1: datetime) -> float:
    wei = 0
    for s in swaps:
        if t0 <= s["t"] < t1:
            wei += s["a0"] if pool.token0 == WETH_BASE else s["a1"]
    return wei / 1e18


def compute_event_row(
    event_id: str,
    post: dict[str, Any],
    pool: PoolMeta,
    match_type: str,
    match_value: str,
    control_pools: list[PoolMeta],
    resolution_status: str = "resolved",
    skip_reason: str = "",
) -> dict[str, Any]:
    t0 = _to_dt(post["created_at"])
    swaps = _swap_rows(pool.pool, t0 - timedelta(minutes=6), t0 + timedelta(minutes=40))

    entry_0 = _first_price_after(swaps, pool, t0)
    entry_30 = _first_price_after(swaps, pool, t0 + timedelta(seconds=30))
    entry_120 = _first_price_after(swaps, pool, t0 + timedelta(seconds=120))
    exit_5m = _first_price_after(swaps, pool, t0 + timedelta(minutes=5))
    exit_15m = _first_price_after(swaps, pool, t0 + timedelta(minutes=15))
    exit_30m = _first_price_after(swaps, pool, t0 + timedelta(minutes=30))

    ret_5m = (exit_5m / entry_0 - 1.0) if entry_0 and exit_5m else None
    ret_15m = (exit_15m / entry_0 - 1.0) if entry_0 and exit_15m else None
    ret_30m = (exit_30m / entry_0 - 1.0) if entry_0 and exit_30m else None

    flow_pre_5m = _net_weth_flow(swaps, pool, t0 - timedelta(minutes=5), t0)
    flow_post_5m = _net_weth_flow(swaps, pool, t0, t0 + timedelta(minutes=5))
    flow_post_15m = _net_weth_flow(swaps, pool, t0, t0 + timedelta(minutes=15))
    flow_post_30m = _net_weth_flow(swaps, pool, t0, t0 + timedelta(minutes=30))

    control_returns_30m: list[float] = []
    for cp in control_pools:
        if cp.pool == pool.pool:
            continue
        c_swaps = _swap_rows(cp.pool, t0 - timedelta(minutes=1), t0 + timedelta(minutes=35))
        c_e0 = _first_price_after(c_swaps, cp, t0)
        c_e30 = _first_price_after(c_swaps, cp, t0 + timedelta(minutes=30))
        if c_e0 and c_e30:
            control_returns_30m.append(c_e30 / c_e0 - 1.0)
        if len(control_returns_30m) >= 10:
            break
    ctrl_30m = statistics.median(control_returns_30m) if control_returns_30m else None
    ctrl_adj_30m = (ret_30m - ctrl_30m) if (ret_30m is not None and ctrl_30m is not None) else None

    token_addr, token_symbol = _token_side(pool)
    return {
        "event_id": event_id,
        "handle": post["handle"],
        "tweet_id": post["tweet_id"],
        "created_at": post["created_at"],
        "is_quote": post["is_quote"],
        "match_type": match_type,
        "match_value": match_value,
        "resolution_status": resolution_status,
        "skip_reason": skip_reason,
        "token_address": token_addr,
        "token_symbol": token_symbol,
        "pool_address": pool.pool,
        "fee_bps": pool.fee_bps,
        "entry_0": entry_0,
        "entry_30s": entry_30,
        "entry_120s": entry_120,
        "exit_5m": exit_5m,
        "exit_15m": exit_15m,
        "exit_30m": exit_30m,
        "ret_5m": ret_5m,
        "ret_15m": ret_15m,
        "ret_30m": ret_30m,
        "flow_weth_pre_5m": flow_pre_5m,
        "flow_weth_post_5m": flow_post_5m,
        "flow_weth_post_15m": flow_post_15m,
        "flow_weth_post_30m": flow_post_30m,
        "flow_impulse_5m": flow_post_5m - flow_pre_5m,
        "control_ret_30m": ctrl_30m,
        "control_adj_ret_30m": ctrl_adj_30m,
    }


def write_events(rows: list[dict[str, Any]]) -> None:
    cols = [
        "event_id",
        "handle",
        "tweet_id",
        "created_at",
        "is_quote",
        "match_type",
        "match_value",
        "resolution_status",
        "skip_reason",
        "token_address",
        "token_symbol",
        "pool_address",
        "fee_bps",
        "entry_0",
        "entry_30s",
        "entry_120s",
        "exit_5m",
        "exit_15m",
        "exit_30m",
        "ret_5m",
        "ret_15m",
        "ret_30m",
        "flow_weth_pre_5m",
        "flow_weth_post_5m",
        "flow_weth_post_15m",
        "flow_weth_post_30m",
        "flow_impulse_5m",
        "control_ret_30m",
        "control_adj_ret_30m",
    ]
    with EVENTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def write_resolution_candidates(rows: list[ResolutionRecord]) -> None:
    cols = [
        "mention_id",
        "tweet_id",
        "handle",
        "created_at",
        "mention_type",
        "mention_value",
        "cashtag",
        "chain",
        "resolution_status",
        "resolution_source",
        "resolved_token_address",
        "resolved_pool_address",
        "candidate_pool_count",
        "skip_reason",
        "notes",
    ]
    with RESOLUTION_CANDIDATES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: getattr(row, k) for k in cols})


def write_venue_inventory(rows: list[VenueInventoryRecord]) -> None:
    cols = [
        "mention_id",
        "tweet_id",
        "handle",
        "created_at",
        "mention_type",
        "mention_value",
        "chain_hint",
        "in_base_v3_registry",
        "in_ethereum_v3_registry",
        "base_non_v3_pool_count",
        "ethereum_non_v3_pool_count",
        "venue_assessment",
        "actionability",
        "reason",
    ]
    with VENUE_INVENTORY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: getattr(row, k) for k in cols})


def write_cashtag_alias_todo(rows: list[CashtagTodoRecord]) -> None:
    cols = ["cashtag", "mention_count", "authors", "example_text", "suggested_lookup_status"]
    with CASHTAG_ALIAS_TODO_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: getattr(row, k) for k in cols})


def write_local_pool_verification(rows: list[LocalPoolVerificationRecord]) -> None:
    cols = [
        "cashtag",
        "token_address",
        "chain",
        "base_v3_registry_pool_count",
        "base_v3_registry_weth_leg_pool_count",
        "base_discovered_pool_count",
        "base_discovered_v3_pool_count",
        "base_discovered_non_v3_pool_count",
        "base_discovered_weth_leg_pool_count",
        "base_v3_swaps_30d",
        "base_all_swaps_30d",
        "registry_missing_or_stale",
        "recommended_actionability",
        "verification_note",
    ]
    with LOCAL_POOL_VERIFICATION_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: getattr(row, k) for k in cols})


def _token_in_v3_registry_chain(token_addr: str, chain_id: int) -> bool:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={chain_id}
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return bool(rows and int(rows[0][0]) > 0)


def _non_v3_pool_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.discovered_pools
WHERE chain_id={chain_id}
  AND lower(pool_type)!='v3'
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _v3_registry_pool_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={chain_id}
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _v3_registry_weth_leg_pool_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_pool_registry
WHERE chain_id={chain_id}
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
  AND (lower(token0_address)='{WETH_BASE}' OR lower(token1_address)='{WETH_BASE}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _discovered_v3_pool_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.discovered_pools
WHERE chain_id={chain_id}
  AND lower(pool_type)='v3'
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _discovered_pool_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.discovered_pools
WHERE chain_id={chain_id}
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _has_weth_leg_in_discovered(token_addr: str, chain_id: int) -> bool:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.discovered_pools
WHERE chain_id={chain_id}
  AND has_weth_leg=1
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return bool(rows and int(rows[0][0]) > 0)


def _weth_leg_discovered_count(token_addr: str, chain_id: int) -> int:
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.discovered_pools
WHERE chain_id={chain_id}
  AND has_weth_leg=1
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )
    return int(rows[0][0]) if rows else 0


def _swaps_for_token_30d(token_addr: str, chain_id: int, only_v3_registry: bool) -> int:
    if only_v3_registry:
        rows = _ch(
            f"""
SELECT count()
FROM eatlq.uniswap_v3_swaps s
INNER JOIN eatlq.uniswap_v3_pool_registry r
  ON lower(s.pool_address)=lower(r.pool_address) AND s.chain_id=r.chain_id
WHERE s.chain_id={chain_id}
  AND s.block_time >= now() - INTERVAL 30 DAY
  AND (lower(r.token0_address)='{token_addr}' OR lower(r.token1_address)='{token_addr}')
"""
        )
    else:
        rows = _ch(
            f"""
SELECT count()
FROM eatlq.uniswap_v3_swaps s
INNER JOIN eatlq.discovered_pools d
  ON lower(s.pool_address)=lower(d.pool_address) AND s.chain_id=d.chain_id
WHERE s.chain_id={chain_id}
  AND s.block_time >= now() - INTERVAL 30 DAY
  AND (lower(d.token0_address)='{token_addr}' OR lower(d.token1_address)='{token_addr}')
"""
        )
    return int(rows[0][0]) if rows else 0


def _chain_hint_from_text(text: str, token_addr: str) -> str:
    t = text.lower()
    a = token_addr.lower()
    if f"ethereum:{a}" in t or f"eth:{a}" in t:
        return "ethereum"
    if f"base:{a}" in t:
        return "base"
    return "unknown"


def build_local_pool_verification(
    aliases: dict[str, list[dict[str, str]]],
) -> tuple[list[LocalPoolVerificationRecord], dict[str, LocalPoolVerificationRecord]]:
    rows: list[LocalPoolVerificationRecord] = []
    by_token: dict[str, LocalPoolVerificationRecord] = {}
    for cashtag, entries in sorted(aliases.items()):
        for entry in entries:
            token = (entry.get("token_address") or "").strip().lower()
            chain = (entry.get("chain") or "base").strip().lower()
            if not token:
                continue
            if chain != "base":
                rec = LocalPoolVerificationRecord(
                    cashtag=cashtag,
                    token_address=token,
                    chain=chain or "unknown",
                    base_v3_registry_pool_count=0,
                    base_v3_registry_weth_leg_pool_count=0,
                    base_discovered_pool_count=0,
                    base_discovered_v3_pool_count=0,
                    base_discovered_non_v3_pool_count=0,
                    base_discovered_weth_leg_pool_count=0,
                    base_v3_swaps_30d=0,
                    base_all_swaps_30d=0,
                    registry_missing_or_stale=False,
                    recommended_actionability="ETHEREUM_RESEARCH_ONLY",
                    verification_note="alias chain is non-base for current universe",
                )
                rows.append(rec)
                by_token[token] = rec
                continue

            v3_pool_count = _v3_registry_pool_count(token, CHAIN_ID)
            v3_weth_count = _v3_registry_weth_leg_pool_count(token, CHAIN_ID)
            disc_pool_count = _discovered_pool_count(token, CHAIN_ID)
            disc_v3_count = _discovered_v3_pool_count(token, CHAIN_ID)
            disc_non_v3_count = _non_v3_pool_count(token, CHAIN_ID)
            disc_weth_count = _weth_leg_discovered_count(token, CHAIN_ID)
            swaps_v3_30d = _swaps_for_token_30d(token, CHAIN_ID, only_v3_registry=True)
            swaps_all_30d = _swaps_for_token_30d(token, CHAIN_ID, only_v3_registry=False)

            missing_or_stale = v3_pool_count == 0 and disc_v3_count > 0
            if v3_weth_count > 0 and swaps_v3_30d > 0:
                action = "ACTIONABLE_CURRENT_V3"
                note = "Base V3 WETH-leg pool and recent swaps both present"
            elif v3_pool_count > 0 and v3_weth_count == 0:
                action = "BASE_V3_NON_WETH_LEG"
                note = "Base V3 pool exists but none are WETH-leg"
            elif missing_or_stale:
                action = "BASE_V3_REGISTRY_MISSING_OR_STALE"
                note = "discovered_pools has Base V3 but uniswap_v3_pool_registry missing token"
            elif disc_non_v3_count > 0:
                action = "BASE_NON_V3_RESEARCH_ONLY"
                note = "only Base non-V3 pools are available locally"
            elif disc_pool_count > 0:
                action = "NEEDS_LOCAL_POOL_VERIFICATION"
                note = "Base pools exist but venue classification remains unclear"
            else:
                action = "NEEDS_LOCAL_POOL_VERIFICATION"
                note = "no local Base pool evidence for alias token"

            rec = LocalPoolVerificationRecord(
                cashtag=cashtag,
                token_address=token,
                chain="base",
                base_v3_registry_pool_count=v3_pool_count,
                base_v3_registry_weth_leg_pool_count=v3_weth_count,
                base_discovered_pool_count=disc_pool_count,
                base_discovered_v3_pool_count=disc_v3_count,
                base_discovered_non_v3_pool_count=disc_non_v3_count,
                base_discovered_weth_leg_pool_count=disc_weth_count,
                base_v3_swaps_30d=swaps_v3_30d,
                base_all_swaps_30d=swaps_all_30d,
                registry_missing_or_stale=missing_or_stale,
                recommended_actionability=action,
                verification_note=note,
            )
            rows.append(rec)
            by_token[token] = rec
    return rows, by_token


def _target_pool_rows_from_discovered(token_addr: str) -> list[list[str]]:
    return _ch(
        f"""
SELECT
  lower(pool_address),
  lower(token0_address),
  lower(token1_address),
  toUInt32(fee_bps),
  lower(pool_type),
  has_weth_leg
FROM eatlq.discovered_pools
WHERE chain_id={CHAIN_ID}
  AND lower(pool_type)='v3'
  AND (lower(token0_address)='{token_addr}' OR lower(token1_address)='{token_addr}')
"""
    )


def _pool_swaps_count(pool_addr: str, days: int | None = None) -> int:
    window_clause = f" AND block_time >= now() - INTERVAL {days} DAY" if days else ""
    rows = _ch(
        f"""
SELECT count()
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID}
  AND lower(pool_address)='{pool_addr.lower()}'
  {window_clause}
"""
    )
    return int(rows[0][0]) if rows else 0


def build_targeted_backfill_assessment(
    local_verify_rows: list[LocalPoolVerificationRecord],
    before_flags: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    targets = [
        r for r in local_verify_rows if r.recommended_actionability == "BASE_V3_REGISTRY_MISSING_OR_STALE"
    ]
    findings: list[dict[str, Any]] = []
    for r in targets:
        pools = _target_pool_rows_from_discovered(r.token_address)
        base_v3_pool_count = len(pools)
        weth_leg_v3_count = sum(1 for p in pools if len(p) >= 6 and str(p[5]) in ("1", "true", "True"))
        complete_fields_count = sum(
            1
            for p in pools
            if len(p) >= 6 and p[0] and p[1] and p[2] and p[3] not in ("", "0")
        )
        swaps_all = sum(_pool_swaps_count(p[0], days=None) for p in pools if p and p[0])
        swaps_30d = sum(_pool_swaps_count(p[0], days=30) for p in pools if p and p[0])
        insertion_necessary = base_v3_pool_count > 0 and weth_leg_v3_count > 0 and complete_fields_count > 0
        finding = {
            "cashtag": r.cashtag,
            "token_address": r.token_address,
            "discovered_pools_hits": r.base_discovered_pool_count,
            "base_v3_pool_count": base_v3_pool_count,
            "weth_leg_v3_pool_count": weth_leg_v3_count,
            "swaps_count_all": swaps_all,
            "swaps_count_30d": swaps_30d,
            "registry_backfill_necessary": insertion_necessary,
            "registry_backfill_inconclusive": complete_fields_count == 0,
            "base_v3_non_actionable_for_current_return": base_v3_pool_count == 0 or weth_leg_v3_count == 0 or swaps_all == 0,
        }
        findings.append(finding)

    before_current = (
        (((before_flags.get("metrics") or {}).get("venue_inventory_counts") or {}).get("current_v3_actionable_count"))
        if before_flags
        else None
    )
    before_events = before_flags.get("token_events_total") if before_flags else None

    summary_flags = {
        "targeted_tokens": [f.get("cashtag") for f in findings],
        "before_current_v3_actionable_count": before_current,
        "before_token_events_total": before_events,
        "TARGETED_BACKFILL_UNLOCKED_SOCIAL_V3_EVENTS": False,
        "BASE_V3_NON_ACTIONABLE_FOR_CURRENT_RETURN": any(
            f["base_v3_non_actionable_for_current_return"] for f in findings
        ),
        "REGISTRY_BACKFILL_INCONCLUSIVE": any(
            f["registry_backfill_inconclusive"] for f in findings
        ),
    }

    md_lines = [
        "# S001F Targeted Registry Backfill Readout",
        "",
        "- Scope: `BASE_V3_REGISTRY_MISSING_OR_STALE` tokens only",
        "- Mode: read-only verification; no full backfill executed",
        "",
        "## Token Findings",
        "",
    ]
    if not findings:
        md_lines.append("- No target tokens found in current local pool verification.")
    else:
        for f in findings:
            md_lines.extend(
                [
                    f"- `{f['cashtag']}` `{f['token_address']}`",
                    f"  - discovered_pools hits: `{f['discovered_pools_hits']}`",
                    f"  - Base V3 pool count: `{f['base_v3_pool_count']}`",
                    f"  - WETH-leg V3 pool count: `{f['weth_leg_v3_pool_count']}`",
                    f"  - swaps count (all / 30d): `{f['swaps_count_all']}` / `{f['swaps_count_30d']}`",
                    f"  - registry backfill necessary: `{f['registry_backfill_necessary']}`",
                    f"  - Base V3 non-actionable for current return: `{f['base_v3_non_actionable_for_current_return']}`",
                ]
            )
    return findings, summary_flags, "\n".join(md_lines)


def build_venue_inventory(
    resolution_rows: list[ResolutionRecord],
    raw_posts: list[dict[str, Any]],
    aliases: dict[str, list[dict[str, str]]],
    local_verify: dict[str, LocalPoolVerificationRecord],
) -> list[VenueInventoryRecord]:
    by_tweet = {r.get("tweet_id", ""): r for r in raw_posts}
    out: list[VenueInventoryRecord] = []
    for rr in resolution_rows:
        token = rr.mention_value.lower()
        alias_provided = False
        if rr.mention_type == "cashtag":
            alias_tokens = _alias_token_candidates(rr.cashtag, aliases)
            if len(alias_tokens) > 1:
                out.append(
                    VenueInventoryRecord(
                        mention_id=rr.mention_id,
                        tweet_id=rr.tweet_id,
                        handle=rr.handle,
                        created_at=rr.created_at,
                        mention_type=rr.mention_type,
                        mention_value=rr.mention_value,
                        chain_hint="base",
                        in_base_v3_registry=False,
                        in_ethereum_v3_registry=False,
                        base_non_v3_pool_count=0,
                        ethereum_non_v3_pool_count=0,
                        venue_assessment="ALIAS_AMBIGUOUS",
                        actionability="UNRESOLVED_ALIAS_NEEDED",
                        reason="multiple alias token addresses provided",
                    )
                )
                continue
            if len(alias_tokens) == 0:
                out.append(
                    VenueInventoryRecord(
                        mention_id=rr.mention_id,
                        tweet_id=rr.tweet_id,
                        handle=rr.handle,
                        created_at=rr.created_at,
                        mention_type=rr.mention_type,
                        mention_value=rr.mention_value,
                        chain_hint="base",
                        in_base_v3_registry=False,
                        in_ethereum_v3_registry=False,
                        base_non_v3_pool_count=0,
                        ethereum_non_v3_pool_count=0,
                        venue_assessment="NEEDS_MANUAL_ALIAS",
                        actionability="UNRESOLVED_ALIAS_NEEDED",
                        reason="cashtag mention requires vetted alias mapping",
                    )
                )
                continue
            token = alias_tokens[0]
            alias_provided = True

        post = by_tweet.get(rr.tweet_id, {})
        hint = _chain_hint_from_text(post.get("text", ""), token)
        base_v3 = _token_in_v3_registry_chain(token, 8453)
        eth_v3 = _token_in_v3_registry_chain(token, 1)
        base_discovered = _discovered_pool_count(token, 8453)
        eth_discovered = _discovered_pool_count(token, 1)
        base_non_v3 = _non_v3_pool_count(token, 8453)
        eth_non_v3 = _non_v3_pool_count(token, 1)
        base_weth_leg = _has_weth_leg_in_discovered(token, 8453)

        venue_assessment = "UNRESOLVED"
        actionability = "UNRESOLVED_ALIAS_NEEDED"
        reason = "token not found in current indexed venue universe"

        eth_signal = hint == "ethereum" or eth_v3 or eth_discovered > 0 or eth_non_v3 > 0
        base_signal = base_v3 or base_discovered > 0 or base_non_v3 > 0 or hint == "base"

        if alias_provided:
            vr = local_verify.get(token)
            if vr:
                actionability = vr.recommended_actionability
                if actionability == "ACTIONABLE_CURRENT_V3":
                    venue_assessment = "BASE_UNISWAP_V3"
                elif actionability == "BASE_NON_V3_RESEARCH_ONLY":
                    venue_assessment = "BASE_NON_V3"
                elif actionability == "BASE_V3_REGISTRY_MISSING_OR_STALE":
                    venue_assessment = "BASE_V3_REGISTRY_MISSING_OR_STALE"
                elif actionability == "BASE_V3_NON_WETH_LEG":
                    venue_assessment = "BASE_V3_NON_WETH_LEG"
                elif actionability == "ETHEREUM_RESEARCH_ONLY":
                    venue_assessment = "ETHEREUM_OR_OTHER_CHAIN"
                else:
                    venue_assessment = "ALIAS_NEEDS_POOL_VERIFICATION"
                reason = vr.verification_note
            else:
                venue_assessment = "ALIAS_NEEDS_POOL_VERIFICATION"
                actionability = "NEEDS_LOCAL_POOL_VERIFICATION"
                reason = "alias token not found in local verification map"
        elif base_v3:
            venue_assessment = "BASE_UNISWAP_V3"
            actionability = "ACTIONABLE_CURRENT_V3"
            reason = "token exists in Base Uniswap V3 registry"
        elif base_non_v3 > 0 and not eth_signal:
            venue_assessment = "BASE_NON_V3"
            actionability = "BASE_NON_V3_RESEARCH_ONLY"
            reason = "token appears on Base non-V3 pools (with WETH leg)" if base_weth_leg else "token appears on Base non-V3 pools (no WETH leg)"
        elif eth_signal and not base_signal:
            venue_assessment = "ETHEREUM_OR_OTHER_CHAIN"
            actionability = "ETHEREUM_RESEARCH_ONLY"
            reason = "token appears Ethereum-scoped and not in Base V3 registry"
        elif eth_signal and base_signal and not base_v3:
            venue_assessment = "CROSS_CHAIN_AMBIGUOUS"
            actionability = "UNRESOLVED_ALIAS_NEEDED"
            reason = "cross-chain or mixed venue hints without Base V3 confirmation"
        elif alias_provided:
            venue_assessment = "ALIAS_NEEDS_POOL_VERIFICATION"
            actionability = "NEEDS_LOCAL_POOL_VERIFICATION"
            reason = "alias token provided but venue evidence is insufficient locally"

        out.append(
            VenueInventoryRecord(
                mention_id=rr.mention_id,
                tweet_id=rr.tweet_id,
                handle=rr.handle,
                created_at=rr.created_at,
                mention_type=rr.mention_type,
                mention_value=token,
                chain_hint=hint,
                in_base_v3_registry=base_v3,
                in_ethereum_v3_registry=eth_v3,
                base_non_v3_pool_count=base_non_v3,
                ethereum_non_v3_pool_count=eth_non_v3,
                venue_assessment=venue_assessment,
                actionability=actionability,
                reason=reason,
            )
        )
    return out


def build_cashtag_alias_todo(
    resolution_rows: list[ResolutionRecord], raw_posts: list[dict[str, Any]]
) -> list[CashtagTodoRecord]:
    by_tweet = {r.get("tweet_id", ""): r for r in raw_posts}
    bucket: dict[str, dict[str, Any]] = {}
    for rr in resolution_rows:
        if rr.mention_type != "cashtag":
            continue
        tag = rr.cashtag or rr.mention_value
        b = bucket.setdefault(
            tag,
            {
                "count": 0,
                "authors": set(),
                "example_text": "",
                "status": "NEEDS_MANUAL_ALIAS",
            },
        )
        b["count"] += 1
        b["authors"].add(rr.handle)
        if not b["example_text"]:
            b["example_text"] = _clean_text((by_tweet.get(rr.tweet_id) or {}).get("text", ""))[:200]
        if rr.skip_reason == "ALIAS_CONFLICT_MULTIPLE_TOKENS":
            b["status"] = "ALIAS_CONFLICT_REVIEW"
        elif rr.resolution_status == "resolved":
            b["status"] = "ALIAS_PROVIDED"
    rows: list[CashtagTodoRecord] = []
    for tag, v in sorted(bucket.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        rows.append(
            CashtagTodoRecord(
                cashtag=tag,
                mention_count=int(v["count"]),
                authors=";".join(sorted(v["authors"])),
                example_text=v["example_text"],
                suggested_lookup_status=v["status"],
            )
        )
    return rows

def _resolve_ca(
    mention_id: str,
    post: dict[str, Any],
    token_addr: str,
    event_ts: datetime,
    weth_by_token: dict[str, list[PoolMeta]],
) -> tuple[ResolutionRecord, PoolMeta | None]:
    pool = pick_pool_for_token(token_addr, event_ts, weth_by_token)
    if pool:
        rec = ResolutionRecord(
            mention_id=mention_id,
            tweet_id=post["tweet_id"],
            handle=post["handle"],
            created_at=post["created_at"],
            mention_type="ca",
            mention_value=token_addr,
            cashtag="",
            chain="base",
            resolution_status="resolved",
            resolution_source="contract_address",
            resolved_token_address=token_addr,
            resolved_pool_address=pool.pool,
            candidate_pool_count=len(weth_by_token.get(token_addr, [])),
            skip_reason="",
            notes="address-first resolved to active WETH-leg pool",
        )
        return rec, pool

    in_registry = _token_in_registry(token_addr)
    in_swaps = _token_in_swaps(token_addr, event_ts)
    if not in_registry:
        skip_reason = "CA_NOT_IN_BASE_POOL_REGISTRY"
        note = "token address absent from pool registry"
    elif not weth_by_token.get(token_addr):
        skip_reason = "CA_NO_WETH_LEG_POOL"
        note = "token seen in registry but no WETH-leg tradable pool"
    elif not in_swaps:
        skip_reason = "CA_NO_SWAP_ACTIVITY_NEAR_EVENT"
        note = "token has no swap activity around event window"
    else:
        skip_reason = "CA_POOL_SELECTION_FAILED"
        note = "unexpected resolution failure after address lookup"
    rec = ResolutionRecord(
        mention_id=mention_id,
        tweet_id=post["tweet_id"],
        handle=post["handle"],
        created_at=post["created_at"],
        mention_type="ca",
        mention_value=token_addr,
        cashtag="",
        chain="base",
        resolution_status="unresolved",
        resolution_source="contract_address",
        resolved_token_address="",
        resolved_pool_address="",
        candidate_pool_count=len(weth_by_token.get(token_addr, [])),
        skip_reason=skip_reason,
        notes=note,
    )
    return rec, None


def _resolve_cashtag(
    mention_id: str,
    post: dict[str, Any],
    cashtag: str,
    event_ts: datetime,
    aliases: dict[str, list[dict[str, str]]],
    weth_pools: list[PoolMeta],
    weth_by_token: dict[str, list[PoolMeta]],
) -> tuple[ResolutionRecord, PoolMeta | None]:
    alias_tokens = _alias_token_candidates(cashtag, aliases)
    if len(alias_tokens) > 1:
        rec = ResolutionRecord(
            mention_id=mention_id,
            tweet_id=post["tweet_id"],
            handle=post["handle"],
            created_at=post["created_at"],
            mention_type="cashtag",
            mention_value=cashtag,
            cashtag=cashtag,
            chain="base",
            resolution_status="unresolved",
            resolution_source="alias_file",
            resolved_token_address="",
            resolved_pool_address="",
            candidate_pool_count=0,
            skip_reason="ALIAS_CONFLICT_MULTIPLE_TOKENS",
            notes="multiple alias entries for cashtag",
        )
        return rec, None
    if len(alias_tokens) == 1:
        token = alias_tokens[0]
        pool = pick_pool_for_token(token, event_ts, weth_by_token)
        if pool:
            rec = ResolutionRecord(
                mention_id=mention_id,
                tweet_id=post["tweet_id"],
                handle=post["handle"],
                created_at=post["created_at"],
                mention_type="cashtag",
                mention_value=cashtag,
                cashtag=cashtag,
                chain="base",
                resolution_status="resolved",
                resolution_source="alias_file",
                resolved_token_address=token,
                resolved_pool_address=pool.pool,
                candidate_pool_count=len(weth_by_token.get(token, [])),
                skip_reason="",
                notes="resolved by cashtag alias file",
            )
            return rec, pool
        rec = ResolutionRecord(
            mention_id=mention_id,
            tweet_id=post["tweet_id"],
            handle=post["handle"],
            created_at=post["created_at"],
            mention_type="cashtag",
            mention_value=cashtag,
            cashtag=cashtag,
            chain="base",
            resolution_status="unresolved",
            resolution_source="alias_file",
            resolved_token_address=token,
            resolved_pool_address="",
            candidate_pool_count=len(weth_by_token.get(token, [])),
            skip_reason="ALIAS_TOKEN_NO_WETH_POOL",
            notes="alias token not tradable on WETH leg",
        )
        return rec, None

    registry_candidates = _cashtag_pool_candidates(cashtag, weth_pools)
    token_set = sorted({_token_side(p)[0] for p in registry_candidates})
    if not registry_candidates:
        rec = ResolutionRecord(
            mention_id=mention_id,
            tweet_id=post["tweet_id"],
            handle=post["handle"],
            created_at=post["created_at"],
            mention_type="cashtag",
            mention_value=cashtag,
            cashtag=cashtag,
            chain="base",
            resolution_status="unresolved",
            resolution_source="registry_symbol",
            resolved_token_address="",
            resolved_pool_address="",
            candidate_pool_count=0,
            skip_reason="CASHTAG_NOT_FOUND_NEEDS_ALIAS",
            notes="no registry symbol match; alias whitelist required",
        )
        return rec, None
    if len(token_set) > 1:
        rec = ResolutionRecord(
            mention_id=mention_id,
            tweet_id=post["tweet_id"],
            handle=post["handle"],
            created_at=post["created_at"],
            mention_type="cashtag",
            mention_value=cashtag,
            cashtag=cashtag,
            chain="base",
            resolution_status="unresolved",
            resolution_source="registry_symbol",
            resolved_token_address="",
            resolved_pool_address="",
            candidate_pool_count=len(registry_candidates),
            skip_reason="CASHTAG_AMBIGUOUS_NEEDS_ALIAS",
            notes="multiple token addresses share same symbol",
        )
        return rec, None
    scored = [(p, _pool_activity_score(p.pool, event_ts)) for p in registry_candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    pool = scored[0][0]
    token, _ = _token_side(pool)
    rec = ResolutionRecord(
        mention_id=mention_id,
        tweet_id=post["tweet_id"],
        handle=post["handle"],
        created_at=post["created_at"],
        mention_type="cashtag",
        mention_value=cashtag,
        cashtag=cashtag,
        chain="base",
        resolution_status="resolved",
        resolution_source="registry_symbol",
        resolved_token_address=token,
        resolved_pool_address=pool.pool,
        candidate_pool_count=len(registry_candidates),
        skip_reason="",
        notes="resolved via unique symbol -> WETH pool",
    )
    return rec, pool

def _profit_factor(values: list[float]) -> float | None:
    pos = [v for v in values if v > 0]
    neg = [-v for v in values if v <= 0]
    if not values:
        return None
    if not neg:
        return None
    return sum(pos) / sum(neg) if sum(neg) > 0 else None


def _max_drawdown(values: list[float]) -> float:
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for v in values:
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return mdd


def _safe_mean(vals: list[float]) -> float | None:
    return statistics.fmean(vals) if vals else None


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "NA"
    return f"{v*100:.3f}%"


def summarize(
    rows: list[dict[str, Any]],
    resolution_rows: list[ResolutionRecord],
    venue_rows: list[VenueInventoryRecord],
    aliases: dict[str, list[dict[str, str]]],
    cashtag_todo_rows: list[CashtagTodoRecord],
) -> tuple[dict[str, Any], str]:
    resolved_rows = [r for r in rows if r.get("resolution_status") == "resolved"]
    valid = [r for r in resolved_rows if r.get("ret_30m") is not None]
    n = len(valid)

    ret5 = [float(r["ret_5m"]) for r in valid if r.get("ret_5m") is not None]
    ret15 = [float(r["ret_15m"]) for r in valid if r.get("ret_15m") is not None]
    ret30 = [float(r["ret_30m"]) for r in valid if r.get("ret_30m") is not None]
    ctrl_adj = [
        float(r["control_adj_ret_30m"])
        for r in valid
        if r.get("control_adj_ret_30m") is not None
    ]

    by_handle: dict[str, int] = {}
    by_token: dict[str, int] = {}
    for r in valid:
        by_handle[r["handle"]] = by_handle.get(r["handle"], 0) + 1
        by_token[r["token_address"]] = by_token.get(r["token_address"], 0) + 1

    top_handle_share = (max(by_handle.values()) / n) if n and by_handle else 0.0
    top_token_share = (max(by_token.values()) / n) if n and by_token else 0.0
    concentration_risk = top_handle_share > 0.60 or top_token_share > 0.60

    unresolved_reason_counts: dict[str, int] = {}
    ca_total = 0
    ca_resolved = 0
    cashtag_total = 0
    cashtag_resolved = 0
    needs_alias = False
    for rr in resolution_rows:
        if rr.mention_type == "ca":
            ca_total += 1
            if rr.resolution_status == "resolved":
                ca_resolved += 1
        elif rr.mention_type == "cashtag":
            cashtag_total += 1
            if rr.resolution_status == "resolved":
                cashtag_resolved += 1
        if rr.resolution_status != "resolved":
            unresolved_reason_counts[rr.skip_reason] = unresolved_reason_counts.get(rr.skip_reason, 0) + 1
            if "ALIAS" in rr.skip_reason:
                needs_alias = True

    metrics = {
        "token_event_rows_total": len(rows),
        "token_events_total": len(resolved_rows),
        "token_events_valid": n,
        "ret_5m": {
            "pf": _profit_factor(ret5),
            "hit_rate": (sum(1 for v in ret5 if v > 0) / len(ret5)) if ret5 else None,
            "mean_return": _safe_mean(ret5),
            "max_drawdown": _max_drawdown(ret5) if ret5 else None,
        },
        "ret_15m": {
            "pf": _profit_factor(ret15),
            "hit_rate": (sum(1 for v in ret15 if v > 0) / len(ret15)) if ret15 else None,
            "mean_return": _safe_mean(ret15),
            "max_drawdown": _max_drawdown(ret15) if ret15 else None,
        },
        "ret_30m": {
            "pf": _profit_factor(ret30),
            "hit_rate": (sum(1 for v in ret30 if v > 0) / len(ret30)) if ret30 else None,
            "mean_return": _safe_mean(ret30),
            "max_drawdown": _max_drawdown(ret30) if ret30 else None,
        },
        "control_adjusted_return_30m_mean": _safe_mean(ctrl_adj),
        "kpi_kol_concentration": top_handle_share,
        "kpi_token_concentration": top_token_share,
        "concentration_risk": concentration_risk,
        "mentions": {
            "ca_total": ca_total,
            "ca_resolved": ca_resolved,
            "cashtag_total": cashtag_total,
            "cashtag_resolved": cashtag_resolved,
        },
        "unresolved_reasons": dict(
            sorted(unresolved_reason_counts.items(), key=lambda x: x[1], reverse=True)
        ),
    }
    action_counter: dict[str, int] = {}
    for vr in venue_rows:
        action_counter[vr.actionability] = action_counter.get(vr.actionability, 0) + 1
    venue_counts = {
        "current_v3_actionable_count": action_counter.get("ACTIONABLE_CURRENT_V3", 0),
        "base_non_v3_count": action_counter.get("BASE_NON_V3_RESEARCH_ONLY", 0),
        "base_v3_registry_missing_or_stale_count": action_counter.get("BASE_V3_REGISTRY_MISSING_OR_STALE", 0),
        "base_v3_non_weth_leg_count": action_counter.get("BASE_V3_NON_WETH_LEG", 0),
        "ethereum_or_other_chain_count": action_counter.get("ETHEREUM_RESEARCH_ONLY", 0),
        "needs_local_pool_verification_count": action_counter.get("NEEDS_LOCAL_POOL_VERIFICATION", 0),
        "unresolved_alias_needed_count": action_counter.get("UNRESOLVED_ALIAS_NEEDED", 0),
    }
    metrics["venue_inventory_counts"] = venue_counts
    unique_cashtags = len(cashtag_todo_rows)
    aliases_provided = len(aliases)
    alias_mentions_covered = sum(
        1
        for vr in venue_rows
        if vr.mention_type == "cashtag" and vr.actionability != "UNRESOLVED_ALIAS_NEEDED"
    )
    metrics["coverage_counts"] = {
        "unique_cashtags": unique_cashtags,
        "aliases_provided": aliases_provided,
        "alias_mentions_covered": alias_mentions_covered,
    }

    if n < 12:
        verdict = "INCONCLUSIVE_SAMPLE_TOO_SMALL"
    elif n < 20:
        pf30 = metrics["ret_30m"]["pf"] or 0.0
        m30 = metrics["ret_30m"]["mean_return"] or 0.0
        verdict = "WEAK_PASS" if pf30 > 1.0 and m30 > 0 else "INCONCLUSIVE"
    else:
        pf30 = metrics["ret_30m"]["pf"] or 0.0
        hit30 = metrics["ret_30m"]["hit_rate"] or 0.0
        m30 = metrics["ret_30m"]["mean_return"] or 0.0
        if pf30 > 1.2 and hit30 >= 0.52 and m30 > 0:
            verdict = "PASS"
        else:
            verdict = "FAIL"

    flags = {
        "sprint": "S001",
        "generated_at_utc": _now_iso(),
        "raw_posts_cap_respected": True,
        "token_events_total": len(resolved_rows),
        "token_events_valid": n,
        "verdict": verdict,
        "concentration_risk": concentration_risk,
        "concentration_reason": {
            "top_handle_share": top_handle_share,
            "top_token_share": top_token_share,
        },
        "gates": {
            "gte_20_required_for_pass_fail": n >= 20,
            "between_12_and_19": 12 <= n < 20,
            "lt_12_force_inconclusive": n < 12,
        },
        "metrics": metrics,
        "NEEDS_ALIAS_WHITELIST": needs_alias,
        "VENUE_UNIVERSE_MISMATCH": len(resolved_rows) == 0 and len(venue_rows) > 0,
        "CURRENT_V3_SOCIAL_OVERLAP_ZERO": venue_counts["current_v3_actionable_count"] == 0,
        "SOCIAL_UNIVERSE_WIDER_THAN_CURRENT_V3": venue_counts["base_non_v3_count"] > venue_counts["current_v3_actionable_count"],
        "REGISTRY_BACKFILL_CANDIDATE": venue_counts["base_v3_registry_missing_or_stale_count"] > 0,
    }

    table = (
        "| Metric | 5m | 15m | 30m |\n"
        "|---|---:|---:|---:|\n"
        f"| PF | {metrics['ret_5m']['pf'] or 'NA'} | {metrics['ret_15m']['pf'] or 'NA'} | {metrics['ret_30m']['pf'] or 'NA'} |\n"
        f"| Hit Rate | {_fmt_pct(metrics['ret_5m']['hit_rate'])} | {_fmt_pct(metrics['ret_15m']['hit_rate'])} | {_fmt_pct(metrics['ret_30m']['hit_rate'])} |\n"
        f"| Mean Return | {_fmt_pct(metrics['ret_5m']['mean_return'])} | {_fmt_pct(metrics['ret_15m']['mean_return'])} | {_fmt_pct(metrics['ret_30m']['mean_return'])} |\n"
        f"| Max Drawdown | {_fmt_pct(metrics['ret_5m']['max_drawdown'])} | {_fmt_pct(metrics['ret_15m']['max_drawdown'])} | {_fmt_pct(metrics['ret_30m']['max_drawdown'])} |\n"
    )
    return flags, table


def write_readout(
    raw_posts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    resolution_rows: list[ResolutionRecord],
    venue_rows: list[VenueInventoryRecord],
    flags: dict[str, Any],
    metric_table: str,
) -> None:
    mentions = (flags.get("metrics") or {}).get("mentions") or {}
    unresolved = (flags.get("metrics") or {}).get("unresolved_reasons") or {}
    venue_counts = (flags.get("metrics") or {}).get("venue_inventory_counts") or {}
    coverage_counts = (flags.get("metrics") or {}).get("coverage_counts") or {}
    top_unresolved = list(unresolved.items())[:5]
    lines = [
        "# Sprint S001 - Social Catalyst Event Study",
        "",
        f"- Generated at: `{flags['generated_at_utc']}`",
        f"- Raw posts: `{len(raw_posts)}` (cap `{MAX_RAW_POSTS}`)",
        f"- Token events (total/valid): `{flags['token_events_total']}/{flags['token_events_valid']}`",
        f"- Verdict: `{flags['verdict']}`",
        f"- Concentration risk: `{flags['concentration_risk']}`",
        f"- NEEDS_ALIAS_WHITELIST: `{flags.get('NEEDS_ALIAS_WHITELIST', False)}`",
        f"- VENUE_UNIVERSE_MISMATCH: `{flags.get('VENUE_UNIVERSE_MISMATCH', False)}`",
        f"- CURRENT_V3_SOCIAL_OVERLAP_ZERO: `{flags.get('CURRENT_V3_SOCIAL_OVERLAP_ZERO', False)}`",
        f"- SOCIAL_UNIVERSE_WIDER_THAN_CURRENT_V3: `{flags.get('SOCIAL_UNIVERSE_WIDER_THAN_CURRENT_V3', False)}`",
        f"- REGISTRY_BACKFILL_CANDIDATE: `{flags.get('REGISTRY_BACKFILL_CANDIDATE', False)}`",
        "",
        "## Metrics",
        "",
        metric_table,
        "",
        "## Concentration",
        "",
        f"- KOL top1 share: `{flags['concentration_reason']['top_handle_share']:.3f}`",
        f"- Token top1 share: `{flags['concentration_reason']['top_token_share']:.3f}`",
        "",
        "## Notes",
        "",
        "- Research-only workflow; no Redis signal writes; no executor/live integration.",
        "- Returns are swap-print executable proxies from WETH-leg pools on Base.",
        "- Control-adjusted return uses median 30m return across contemporaneous control pools.",
        "",
        "## Resolver Diagnostics",
        "",
        f"- CA resolved: `{mentions.get('ca_resolved', 0)}/{mentions.get('ca_total', 0)}`",
        f"- Cashtag resolved: `{mentions.get('cashtag_resolved', 0)}/{mentions.get('cashtag_total', 0)}`",
        f"- current_v3_actionable_count: `{venue_counts.get('current_v3_actionable_count', 0)}`",
        f"- base_non_v3_count: `{venue_counts.get('base_non_v3_count', 0)}`",
        f"- base_v3_registry_missing_or_stale_count: `{venue_counts.get('base_v3_registry_missing_or_stale_count', 0)}`",
        f"- base_v3_non_weth_leg_count: `{venue_counts.get('base_v3_non_weth_leg_count', 0)}`",
        f"- ethereum_or_other_chain_count: `{venue_counts.get('ethereum_or_other_chain_count', 0)}`",
        f"- needs_local_pool_verification_count: `{venue_counts.get('needs_local_pool_verification_count', 0)}`",
        f"- unresolved_alias_needed_count: `{venue_counts.get('unresolved_alias_needed_count', 0)}`",
        f"- unique_cashtags: `{coverage_counts.get('unique_cashtags', 0)}`",
        f"- aliases_provided: `{coverage_counts.get('aliases_provided', 0)}`",
        f"- alias_mentions_covered: `{coverage_counts.get('alias_mentions_covered', 0)}`",
    ]
    if top_unresolved:
        lines.extend(["", "### Top Unresolved Reasons", ""])
        for k, v in top_unresolved:
            lines.append(f"- `{k}`: `{v}`")
    ca_unresolved = [r for r in resolution_rows if r.mention_type == "ca" and r.resolution_status != "resolved"]
    if ca_unresolved:
        lines.extend(["", "### CA Mention Failures", ""])
        for r in ca_unresolved[:5]:
            lines.append(
                f"- tweet `{r.tweet_id}` CA `{r.mention_value}` -> `{r.skip_reason}` ({r.notes})"
            )
    ca_venue = [r for r in venue_rows if r.mention_type == "ca"]
    if ca_venue:
        lines.extend(["", "### CA Venue Findings", ""])
        for r in ca_venue:
            lines.append(
                f"- tweet `{r.tweet_id}` CA `{r.mention_value}` -> `{r.actionability}` ({r.reason})"
            )
    READOUT_MD.write_text("\n".join(lines), encoding="utf-8")


def top_control_pools(weth_pools: list[PoolMeta]) -> list[PoolMeta]:
    if not weth_pools:
        return []
    pools = [p.pool for p in weth_pools]
    sql_list = ",".join(f"'{p}'" for p in pools[:1500])
    rows = _ch(
        f"""
SELECT pool_address, count() AS c
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID}
  AND pool_address IN ({sql_list})
  AND block_time >= now() - INTERVAL 7 DAY
GROUP BY pool_address
ORDER BY c DESC
LIMIT {CONTROL_POOL_TARGET}
""",
        timeout=55,
    )
    wanted = {r[0] for r in rows if r}
    return [p for p in weth_pools if p.pool in wanted]


def run() -> int:
    before_flags: dict[str, Any] = {}
    if FLAGS_JSON.exists():
        try:
            before_flags = json.loads(FLAGS_JSON.read_text(encoding="utf-8"))
        except Exception:
            before_flags = {}

    raw_posts = load_raw_posts() if REUSE_EXISTING_RAW_POSTS else []
    if not raw_posts:
        bearer = _load_bearer()
        if not bearer:
            write_raw_posts([])
            write_events([])
            write_resolution_candidates([])
            write_venue_inventory([])
            write_cashtag_alias_todo([])
            write_local_pool_verification([])
            flags, metric_table = summarize([], [], [], {}, [])
            flags["notes"] = ["Missing X bearer token and no local raw_x_posts.csv."]
            FLAGS_JSON.write_text(json.dumps(flags, indent=2), encoding="utf-8")
            write_readout([], [], [], [], flags, metric_table)
            print("Missing X bearer token in environment.", file=sys.stderr)
            return 0
        try:
            raw_posts = fetch_recent_posts(KOL_HANDLES, bearer)
            write_raw_posts(raw_posts)
        except Exception as exc:
            write_raw_posts([])
            write_events([])
            write_resolution_candidates([])
            write_venue_inventory([])
            write_cashtag_alias_todo([])
            write_local_pool_verification([])
            flags, metric_table = summarize([], [], [], {}, [])
            flags["notes"] = [f"X API fetch blocked: {str(exc)[:220]}"]
            FLAGS_JSON.write_text(json.dumps(flags, indent=2), encoding="utf-8")
            write_readout([], [], [], [], flags, metric_table)
            print(f"X API fetch blocked: {exc}", file=sys.stderr)
            return 0

    weth_pools = load_weth_pools()
    weth_by_token: dict[str, list[PoolMeta]] = {}
    for p in weth_pools:
        tok, _ = _token_side(p)
        weth_by_token.setdefault(tok, []).append(p)
    controls = top_control_pools(weth_pools)
    aliases = load_aliases()
    local_verify_rows, local_verify_map = build_local_pool_verification(aliases)
    targeted_findings, targeted_flags, targeted_md = build_targeted_backfill_assessment(
        local_verify_rows, before_flags
    )

    events: list[dict[str, Any]] = []
    resolution_rows: list[ResolutionRecord] = []
    seen = set()
    seq = 1
    mention_seq = 1
    for post in raw_posts:
        event_ts = _to_dt(post["created_at"])
        cas, cashtags = parse_candidates(post["text"])

        for ca in cas:
            mention_id = f"M{mention_seq:04d}"
            mention_seq += 1
            rec, pool = _resolve_ca(mention_id, post, ca, event_ts, weth_by_token)
            resolution_rows.append(rec)
            if not pool:
                events.append(
                    {
                        "event_id": "",
                        "handle": post["handle"],
                        "tweet_id": post["tweet_id"],
                        "created_at": post["created_at"],
                        "is_quote": post["is_quote"],
                        "match_type": "ca",
                        "match_value": ca,
                        "resolution_status": "unresolved",
                        "skip_reason": rec.skip_reason,
                        "token_address": "",
                        "token_symbol": "",
                        "pool_address": "",
                        "fee_bps": "",
                    }
                )
                continue
            k = (post["tweet_id"], pool.pool)
            if k in seen:
                events.append(
                    {
                        "event_id": "",
                        "handle": post["handle"],
                        "tweet_id": post["tweet_id"],
                        "created_at": post["created_at"],
                        "is_quote": post["is_quote"],
                        "match_type": "ca",
                        "match_value": ca,
                        "resolution_status": "unresolved",
                        "skip_reason": "DUPLICATE_TWEET_POOL",
                        "token_address": rec.resolved_token_address,
                        "token_symbol": "",
                        "pool_address": rec.resolved_pool_address,
                        "fee_bps": "",
                    }
                )
                continue
            seen.add(k)
            events.append(
                compute_event_row(
                    event_id=f"S001E{seq:04d}",
                    post=post,
                    pool=pool,
                    match_type="ca",
                    match_value=ca,
                    control_pools=controls,
                    resolution_status="resolved",
                    skip_reason="",
                )
            )
            seq += 1

        for tag in cashtags:
            mention_id = f"M{mention_seq:04d}"
            mention_seq += 1
            rec, pool = _resolve_cashtag(
                mention_id=mention_id,
                post=post,
                cashtag=tag,
                event_ts=event_ts,
                aliases=aliases,
                weth_pools=weth_pools,
                weth_by_token=weth_by_token,
            )
            resolution_rows.append(rec)
            if not pool:
                events.append(
                    {
                        "event_id": "",
                        "handle": post["handle"],
                        "tweet_id": post["tweet_id"],
                        "created_at": post["created_at"],
                        "is_quote": post["is_quote"],
                        "match_type": "cashtag",
                        "match_value": tag,
                        "resolution_status": "unresolved",
                        "skip_reason": rec.skip_reason,
                        "token_address": rec.resolved_token_address,
                        "token_symbol": "",
                        "pool_address": "",
                        "fee_bps": "",
                    }
                )
                continue
            k = (post["tweet_id"], pool.pool)
            if k in seen:
                events.append(
                    {
                        "event_id": "",
                        "handle": post["handle"],
                        "tweet_id": post["tweet_id"],
                        "created_at": post["created_at"],
                        "is_quote": post["is_quote"],
                        "match_type": "cashtag",
                        "match_value": tag,
                        "resolution_status": "unresolved",
                        "skip_reason": "DUPLICATE_TWEET_POOL",
                        "token_address": rec.resolved_token_address,
                        "token_symbol": "",
                        "pool_address": rec.resolved_pool_address,
                        "fee_bps": "",
                    }
                )
                continue
            seen.add(k)
            events.append(
                compute_event_row(
                    event_id=f"S001E{seq:04d}",
                    post=post,
                    pool=pool,
                    match_type="cashtag",
                    match_value=tag,
                    control_pools=controls,
                    resolution_status="resolved",
                    skip_reason="",
                )
            )
            seq += 1

    write_events(events)
    write_resolution_candidates(resolution_rows)
    venue_rows = build_venue_inventory(resolution_rows, raw_posts, aliases, local_verify_map)
    cashtag_todo_rows = build_cashtag_alias_todo(resolution_rows, raw_posts)
    write_venue_inventory(venue_rows)
    write_cashtag_alias_todo(cashtag_todo_rows)
    write_local_pool_verification(local_verify_rows)
    flags, metric_table = summarize(events, resolution_rows, venue_rows, aliases, cashtag_todo_rows)
    after_current = (
        (((flags.get("metrics") or {}).get("venue_inventory_counts") or {}).get("current_v3_actionable_count"))
    )
    after_events = flags.get("token_events_total")
    targeted_flags["after_current_v3_actionable_count"] = after_current
    targeted_flags["after_token_events_total"] = after_events
    targeted_flags["TARGETED_BACKFILL_UNLOCKED_SOCIAL_V3_EVENTS"] = (
        isinstance(targeted_flags.get("before_current_v3_actionable_count"), int)
        and isinstance(after_current, int)
        and after_current > targeted_flags["before_current_v3_actionable_count"]
        and after_current > 0
    )
    flags.update(targeted_flags)
    flags["targeted_backfill_findings"] = targeted_findings
    TARGETED_BACKFILL_READOUT_MD.write_text(targeted_md, encoding="utf-8")
    FLAGS_JSON.write_text(json.dumps(flags, indent=2), encoding="utf-8")
    write_readout(raw_posts, events, resolution_rows, venue_rows, flags, metric_table)
    return 0


if __name__ == "__main__":
    sys.exit(run())
