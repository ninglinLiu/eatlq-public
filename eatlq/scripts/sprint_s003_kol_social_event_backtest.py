#!/usr/bin/env python3
"""Sprint S003 — KOL Social Catalyst Large Event Backtest (research only).

Quant event study: post→token mentions vs delayed entry / holding windows using
OKX + DexScreener + GeckoTerminal (reuse S002 clients).

Forbidden: Redis/executor/live/training/PK/swap/broadcast/X web scrape.
Secrets: env only; never written to artifacts.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
S001_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s001"
OUT_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s003"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RAW_KOL_POSTS_CSV = OUT_DIR / "raw_kol_posts.csv"
RESOLVED_EVENTS_CSV = OUT_DIR / "resolved_social_events.csv"
EVENT_GRID_CSV = OUT_DIR / "event_return_grid.csv"
POLICY_AUTHOR_CSV = OUT_DIR / "policy_summary_by_author.csv"
POLICY_TOKEN_CSV = OUT_DIR / "policy_summary_by_token.csv"
POLICY_GRID_CSV = OUT_DIR / "policy_summary_by_grid.csv"
DATA_SOURCE_COVERAGE_CSV = OUT_DIR / "data_source_coverage.csv"
ALIAS_CANDIDATES_CSV = OUT_DIR / "alias_candidates.csv"
READOUT_MD = OUT_DIR / "kol_social_event_backtest_readout.md"
FLAGS_JSON = OUT_DIR / "decision_flags.json"

S001_RAW = S001_DIR / "raw_x_posts.csv"
S001_ALIASES = S001_DIR / "cashtag_base_aliases.csv"
S001_FLAGS = S001_DIR / "decision_flags.json"
S002_FLAGS = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s002" / "decision_flags.json"

PRIORITY_HANDLES = [
    "btcbabycow",
    "iruletrenches",
    "cutepanda",
    "0xLuo",
    "basezh",
    "0xWhy_26",
]

X_API_BASE = "https://api.x.com/2"
X_BEARER_ENV_KEYS = (
    "X_BEARER_TOKEN",
    "X_API_BEARER_TOKEN",
    "TWITTER_BEARER_TOKEN",
)

MAX_POSTS_PER_KOL_X = 200
MAX_RAW_POSTS_TOTAL = 1200
CAP_OKX_HARD_S003 = 5000
CAP_OKX_SOFT_S003 = 4800
CAP_DEX_SOFT_S003 = 2000
CAP_GECKO_SOFT_S003 = 800
CAP_PER_TOKEN_OKX_S003 = 80
CONSEC_SOURCE_STOP_S003 = 20
CONSEC_OKX_STOP_S003 = 20

ENTRY_DELAYS = [0, 1, 3, 5, 10, 15, 30, 60]
EXIT_AFTERS = [5, 15, 30, 60, 120, 240]

FIXED_POLICIES: dict[str, tuple[int, int]] = {
    "policy_fast": (1, 15),
    "policy_mid": (5, 30),
    "policy_slow": (15, 60),
    "policy_late": (30, 120),
}

CASHTAG_RE = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9_]{1,15})\b")
CA_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")

# Dedup DexScreener search per symbol per process (saves HTTP budget).
_DEX_SEARCH_CACHE: dict[str, tuple[str | None, str]] = {}


def _load_s002():
    p = REPO_ROOT / "scripts" / "sprint_s002_base_social_market_data_scout.py"
    name = "eatlq_s002_market"
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


S002 = _load_s002()

# --- Budget overrides for S003 ---
S002.CAP_OKX_HARD = CAP_OKX_HARD_S003
S002.CAP_OKX_SOFT = CAP_OKX_SOFT_S003
S002.CAP_DEX_SOFT = CAP_DEX_SOFT_S003
S002.CAP_GECKO_SOFT = CAP_GECKO_SOFT_S003
S002.CAP_PER_TOKEN_OKX = CAP_PER_TOKEN_OKX_S003
S002.CONSEC_ERRORS_STOP = CONSEC_OKX_STOP_S003

_orig_http = S002.http_json_public


def http_json_public_s003(
    budget: Any,
    which: str,
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    import json as json_lib

    if getattr(budget, "dex_blocked", False) and which == "dex":
        return None, "dex_blocked"
    if getattr(budget, "gecko_blocked", False) and which == "gecko":
        return None, "gecko_blocked"

    hdrs = {"User-Agent": "EATLQ-S003-KOLBacktest/1.0"}
    if headers:
        hdrs.update(headers)
    if which == "dex":
        if budget.dex >= S002.CAP_DEX_SOFT:
            return None, "cap"
        budget.dex += 1
    elif which == "gecko":
        if budget.gecko >= S002.CAP_GECKO_SOFT:
            return None, "cap"
        if budget.gecko_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
            budget.gecko_blocked = True
            return None, "gecko_errors"
        if budget.gecko > 0:
            time.sleep(0.65)
        budget.gecko += 1
    try:
        req = Request(url, headers=hdrs, method="GET")
        with urlopen(req, timeout=30) as resp:
            doc = json_lib.loads(resp.read().decode("utf-8", errors="ignore"))
        if which == "dex":
            budget.dex_consecutive_errors = 0
        else:
            budget.gecko_consecutive_errors = 0
        return doc, ""
    except HTTPError as e:
        if which == "dex":
            budget.dex_consecutive_errors += 1
            if budget.dex_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
                budget.dex_blocked = True
        else:
            budget.gecko_consecutive_errors += 1
            if budget.gecko_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
                budget.gecko_blocked = True
        if e.code == 429:
            return None, "rate"
        return None, f"http_{e.code}"
    except URLError:
        if which == "dex":
            budget.dex_consecutive_errors += 1
            if budget.dex_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
                budget.dex_blocked = True
        else:
            budget.gecko_consecutive_errors += 1
            if budget.gecko_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
                budget.gecko_blocked = True
        return None, "network"
    except json_lib.JSONDecodeError:
        if which == "gecko":
            budget.gecko_consecutive_errors += 1
            if budget.gecko_consecutive_errors >= CONSEC_SOURCE_STOP_S003:
                budget.gecko_blocked = True
        return None, "parse"


S002.http_json_public = http_json_public_s003


def attach_budget_flags(budget: S002.CallBudget) -> None:
    budget.dex_blocked = False
    budget.gecko_blocked = False


def _norm_handle(h: str) -> str:
    return (h or "").strip().lstrip("@").lower()


def load_bearer() -> str:
    for k in X_BEARER_ENV_KEYS:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def x_get(path: str, params: dict[str, Any], bearer: str) -> dict[str, Any]:
    q = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{X_API_BASE}{path}"
    if q:
        url = f"{url}?{q}"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "EATLQ-S003-KOLBacktest/1.0",
        },
        method="GET",
    )
    with urlopen(req, timeout=28) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_kol_posts_x(handles: list[str], bearer: str) -> tuple[list[dict[str, Any]], str]:
    """Up to MAX_POSTS_PER_KOL_X per handle (paginated); hard stop on first X API failure."""
    rows: list[dict[str, Any]] = []
    note = ""
    try:
        for handle in handles:
            if len(rows) >= MAX_RAW_POSTS_TOTAL:
                break
            h = _norm_handle(handle)
            user_doc = x_get(f"/users/by/username/{h}", {"user.fields": "id,username"}, bearer)
            user = user_doc.get("data") or {}
            uid = user.get("id")
            if not uid:
                continue
            remaining_bucket = min(
                MAX_POSTS_PER_KOL_X, MAX_RAW_POSTS_TOTAL - len(rows)
            )
            collected = 0
            next_token: str | None = None
            while collected < remaining_bucket:
                # X API requires max_results in [5, 100]; skip the tail if < 5
                batch = min(100, remaining_bucket - collected)
                if batch < 5:
                    break
                params: dict[str, Any] = {
                    "max_results": batch,
                    "exclude": "retweets,replies",
                    "tweet.fields": "created_at,text,lang,public_metrics,referenced_tweets",
                }
                if next_token:
                    params["pagination_token"] = next_token
                tweets_doc = x_get(f"/users/{uid}/tweets", params, bearer)
                for t in tweets_doc.get("data") or []:
                    refs = t.get("referenced_tweets") or []
                    if any(r.get("type") == "retweeted" for r in refs):
                        continue
                    txt = re.sub(r"\s+", " ", (t.get("text") or "")).strip()
                    rows.append(
                        {
                            "handle": f"@{h}",
                            "tweet_id": str(t.get("id", "")),
                            "created_at": t.get("created_at", ""),
                            "is_quote": any(r.get("type") == "quoted" for r in refs),
                            "lang": t.get("lang", ""),
                            "like_count": (t.get("public_metrics") or {}).get(
                                "like_count", ""
                            ),
                            "retweet_count": (t.get("public_metrics") or {}).get(
                                "retweet_count", ""
                            ),
                            "reply_count": (t.get("public_metrics") or {}).get(
                                "reply_count", ""
                            ),
                            "quote_count": (t.get("public_metrics") or {}).get(
                                "quote_count", ""
                            ),
                            "text": txt,
                            "extension_reason": "",
                        }
                    )
                    collected += 1
                    if len(rows) >= MAX_RAW_POSTS_TOTAL:
                        break
                    if collected >= remaining_bucket:
                        break
                meta = tweets_doc.get("meta") or {}
                next_token = meta.get("next_token")
                if not next_token:
                    break
                if not tweets_doc.get("data"):
                    break
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[:MAX_RAW_POSTS_TOTAL], note
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:240]
        note = f"x_http_{e.code}:{body[:120]}"
        # Return whatever was collected before the error rather than discarding all
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[:MAX_RAW_POSTS_TOTAL], note
    except (URLError, OSError, ValueError, KeyError, TypeError) as e:
        note = f"x_error:{type(e).__name__}"
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[:MAX_RAW_POSTS_TOTAL], note


def load_s001_raw() -> list[dict[str, str]]:
    if not S001_RAW.exists():
        return []
    with S001_RAW.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def discover_extension_handles(
    s001_rows: list[dict[str, str]],
    priority_lower: set[str],
) -> dict[str, str]:
    """handle_lower -> reason."""
    by_h: dict[str, list[str]] = defaultdict(list)
    for r in s001_rows:
        hl = _norm_handle(r.get("handle") or "")
        if not hl or hl in priority_lower:
            continue
        by_h[hl].append(r.get("text") or "")
    out: dict[str, str] = {}
    for hl, texts in by_h.items():
        if len(texts) < 3:
            continue
        ntok = sum(1 for t in texts if CASHTAG_RE.search(t) or CA_RE.search(t))
        if ntok / max(len(texts), 1) >= 0.25:
            out[hl] = "s001_raw_high_token_mention_rate"
    return out


def merge_posts(
    s001_rows: list[dict[str, str]],
    x_rows: list[dict[str, Any]],
    extension_handles: dict[str, str],
) -> list[dict[str, Any]]:
    """Dedupe tweet_id; newest wins for duplicates."""
    pool: dict[str, dict[str, Any]] = {}

    def ingest(row: dict[str, Any]) -> None:
        tid = str(row.get("tweet_id") or "").strip()
        if not tid:
            return
        prev = pool.get(tid)
        if prev is None:
            pool[tid] = dict(row)
            return
        t_new = row.get("created_at") or ""
        t_old = prev.get("created_at") or ""
        if t_new >= t_old:
            pool[tid] = dict(row)

    pri = {_norm_handle(h) for h in PRIORITY_HANDLES}
    for r in s001_rows:
        hl = _norm_handle(r.get("handle") or "")
        ext = extension_handles.get(hl, "")
        ingest(
            {
                "handle": r.get("handle") or f"@{hl}",
                "tweet_id": r.get("tweet_id", ""),
                "created_at": r.get("created_at", ""),
                "is_quote": r.get("is_quote", ""),
                "lang": r.get("lang", ""),
                "like_count": r.get("like_count", ""),
                "retweet_count": r.get("retweet_count", ""),
                "reply_count": r.get("reply_count", ""),
                "quote_count": r.get("quote_count", ""),
                "text": r.get("text", ""),
                "extension_reason": ext,
            }
        )
    for r in x_rows:
        hl = _norm_handle(r.get("handle") or "")
        if hl not in pri:
            continue
        ingest({**r, "extension_reason": ""})

    out = list(pool.values())
    out.sort(key=lambda z: z.get("created_at", ""), reverse=True)
    return out[:MAX_RAW_POSTS_TOTAL]


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
        "extension_reason",
    ]
    with RAW_KOL_POSTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def load_aliases_flat() -> dict[str, str]:
    out: dict[str, str] = {}
    if not S001_ALIASES.exists():
        return out
    with S001_ALIASES.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tag = (row.get("cashtag") or "").strip().upper()
            tok = (row.get("token_address") or "").strip().lower()
            chain = (row.get("chain") or "").strip().lower()
            if not tag or not tok:
                continue
            if chain and chain != "base":
                continue
            out[tag] = tok
    return out


def reverse_alias(addr: str, aliases: dict[str, str]) -> str | None:
    for sym, a in aliases.items():
        if a == addr:
            return sym
    return None


def dexscreener_search_token(
    budget: S002.CallBudget, symbol_upper: str
) -> tuple[str | None, str]:
    """Resolve cashtag via DexScreener search; '' token + reason."""
    k = symbol_upper.strip().upper()
    if k in _DEX_SEARCH_CACHE:
        return _DEX_SEARCH_CACHE[k]

    q = quote(k)
    url = f"https://api.dexscreener.com/latest/dex/search?q={q}"
    doc, err = S002.http_json_public(budget, "dex", url)
    if err or not doc:
        _DEX_SEARCH_CACHE[k] = (None, f"dex_search_{err}")
        return _DEX_SEARCH_CACHE[k]
    pairs = doc.get("pairs") or []
    base_rows: list[tuple[str, float]] = []
    weth = S002.WETH_BASE.lower()
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if str(p.get("chainId", "")).lower() != "base":
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        bt = (p.get("baseToken") or {}) if isinstance(p.get("baseToken"), dict) else {}
        qt = (p.get("quoteToken") or {}) if isinstance(p.get("quoteToken"), dict) else {}
        a1 = str(bt.get("address") or "").lower()
        a2 = str(qt.get("address") or "").lower()
        for addr in (a1, a2):
            if S002._valid_evm_address(addr) and addr != weth:
                base_rows.append((addr, liq))
                break
    if not base_rows:
        _DEX_SEARCH_CACHE[k] = (None, "dex_search_no_base_pair")
        return _DEX_SEARCH_CACHE[k]
    base_rows.sort(key=lambda x: x[1], reverse=True)
    top_addr, top_liq = base_rows[0]
    if len(base_rows) > 1:
        addr2, liq2 = base_rows[1]
        if addr2 != top_addr and top_liq > 0 and liq2 / top_liq >= 0.85:
            _DEX_SEARCH_CACHE[k] = (None, "AMBIGUOUS_SKIP")
            return _DEX_SEARCH_CACHE[k]
    _DEX_SEARCH_CACHE[k] = (top_addr, "dex_search")
    return _DEX_SEARCH_CACHE[k]


def venue_from_row(tweet_id: str, mention_key: str, vmap: dict[tuple[str, str], str]) -> str:
    for k in (mention_key.upper(), mention_key):
        v = vmap.get((tweet_id, k), "")
        if v:
            return v
    return ""


def load_venue_map() -> dict[tuple[str, str], str]:
    vcsv = S001_DIR / "social_token_venue_inventory.csv"
    m: dict[tuple[str, str], str] = {}
    if not vcsv.exists():
        return m
    with vcsv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = (row.get("tweet_id") or "").strip()
            mv = (row.get("mention_value") or "").strip()
            va = (row.get("venue_assessment") or "").strip()
            if tid:
                m[(tid, mv.upper())] = va
    return m


def build_resolved_events(
    posts: list[dict[str, Any]],
    aliases: dict[str, str],
    vmap: dict[tuple[str, str], str],
    budget: S002.CallBudget,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Emit resolved events + alias_candidates rows."""
    events: dict[tuple[str, str], dict[str, Any]] = {}
    alias_cands: list[dict[str, str]] = []

    for row in posts:
        tid = str(row.get("tweet_id") or "").strip()
        handle = str(row.get("handle") or "").strip()
        created = str(row.get("created_at") or "").strip()
        text = row.get("text") or ""
        ext_r = str(row.get("extension_reason") or "").strip()
        if not tid:
            continue
        thash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

        found: dict[str, tuple[str | None, str, str]] = {}

        for raw_tag in CASHTAG_RE.findall(text):
            sym = raw_tag.upper()
            addr = aliases.get(sym)
            if addr:
                found[sym] = (addr, "alias_csv", "high")
            else:
                tok, src = dexscreener_search_token(budget, sym)
                if tok:
                    found[sym] = (tok, src, "medium")
                elif src == "AMBIGUOUS_SKIP":
                    alias_cands.append(
                        {
                            "cashtag": sym,
                            "tweet_id": tid,
                            "author": handle,
                            "reason": "AMBIGUOUS_SKIP",
                            "notes": "dex_search_multiple_candidates",
                        }
                    )
                else:
                    alias_cands.append(
                        {
                            "cashtag": sym,
                            "tweet_id": tid,
                            "author": handle,
                            "reason": src,
                            "notes": "",
                        }
                    )

        for ca in CA_RE.findall(text):
            low = ca.lower()
            sym = reverse_alias(low, aliases)
            if sym:
                found.setdefault(sym, (low, "alias_csv", "high"))
            else:
                ghost = f"CA:{low[:10]}"
                found.setdefault(ghost, (low, "contract_only", "high"))

        for sym, (addr, src, conf) in found.items():
            if not addr:
                continue
            vm = venue_from_row(tid, sym, vmap)
            key = (tid, addr.lower())
            prev = events.get(key)
            if prev and prev["resolution_confidence"] == "high" and conf != "high":
                continue
            events[key] = {
                "author": handle,
                "post_id": tid,
                "post_time_utc": created,
                "token_symbol": sym if not sym.startswith("CA:") else "",
                "token_address": addr.lower(),
                "chain": "base",
                "primary_venue": vm or "",
                "resolution_source": src,
                "resolution_confidence": conf,
                "extension_reason": ext_r,
                "text_hash_short": thash,
            }

    ev_list = sorted(events.values(), key=lambda x: (x["post_time_utc"], x["post_id"]))
    return ev_list, alias_cands


def write_alias_candidates(rows: list[dict[str, str]]) -> None:
    cols = ["cashtag", "tweet_id", "author", "reason", "notes"]
    with ALIAS_CANDIDATES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _okx_candles_paginated(
    client: S002.OKXClient,
    addr: str,
    bar: str,
    token_okx_used: dict[str, int],
    t_min: int,
    t_max: int,
    max_pages: int = 4,
) -> tuple[list[tuple[int, float, float]], str]:
    """Fetch up to max_pages * 300 candle bars for the window [t_min, t_max].

    OKX DEX candles API semantics (note: counter-intuitive naming):
      before=X  →  return bars with ts > X  (newer than X)
      after=X   →  return bars with ts < X  (older than X)

    So to get bars in [t_min, t_max] we pass before=t_min, after=t_max.
    Pagination walks the after cursor backward (older) on each page.
    """
    all_rows: list[list[str]] = []
    after_ms: int | None = t_max
    for _ in range(max_pages):
        if token_okx_used.get(addr, 0) >= S002.CAP_PER_TOKEN_OKX:
            break
        if not client.budget.okx_ok():
            break
        rows, err = S002.okx_candles(
            client, addr, bar=bar, limit="300", before_ms=t_min, after_ms=after_ms
        )
        token_okx_used[addr] = token_okx_used.get(addr, 0) + 1
        if err or not rows:
            break
        all_rows.extend(rows)
        # oldest ts in this page; on next page get bars older than that
        oldest_ts = min(int(r[0]) for r in rows)
        if oldest_ts <= t_min:
            break
        after_ms = oldest_ts
    series = S002.candles_to_series(all_rows)
    return series, ""


def build_series_cache_extended(
    events: list[dict[str, Any]],
    coverage_by_addr: dict[str, dict[str, Any]],
    client: S002.OKXClient | None,
    budget: S002.CallBudget,
    token_okx_used: dict[str, int],
) -> dict[str, tuple[list[tuple[int, float, float]], str]]:
    """Multi-resolution OHLCV cache.

    Resolution cascade per token:
    1. OKX 1m (paginated up to 4 pages) — for events within ~3 days
    2. OKX 1H (paginated up to 4 pages) — for events 3-30 days old; marks as
       "okx_candles_1h" so grid_row_for skips 5m/15m/30m windows
    3. Gecko minute (current depth ~3 days)
    4. Gecko hourly (current depth ~weeks) — marks as "gecko_terminal_1h"

    Series keyed by token_address.lower().
    """
    by_addr: dict[str, list[int]] = {}
    for ev in events:
        addr = (ev.get("token_address") or "").strip().lower()
        if not addr:
            continue
        ts_str = ev.get("post_time_utc") or ""
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0_ms = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            continue
        by_addr.setdefault(addr, []).append(t0_ms)

    # For 1m: window = event ± 2h before, 320 min after (max exit 240m + 60m + 20m pad)
    # For 1H: same window but tolerance is ±1 bar (60 min)
    PAD_BEFORE = 130 * 60 * 1000   # 2h10m
    PAD_AFTER  = 330 * 60 * 1000   # 5h30m (covers max 240m exit + 60m entry + 30m pad)
    BAR_1H_MS  = 3_600_000

    cache: dict[str, tuple[list[tuple[int, float, float]], str]] = {}

    for addr, times in by_addr.items():
        cov = coverage_by_addr.get(addr, {})
        t_min = min(times) - PAD_BEFORE
        t_max = max(times) + PAD_AFTER

        # --- 1m OKX ---
        if client and budget.okx_ok() and token_okx_used.get(addr, 0) < S002.CAP_PER_TOKEN_OKX:
            series_1m, _ = _okx_candles_paginated(
                client, addr, "1m", token_okx_used, t_min, t_max, max_pages=4
            )
            if series_1m and len(series_1m) >= 5 and S002.series_covers_window(series_1m, t_min, t_max):
                cache[addr] = (series_1m, "okx_market_candles")
                continue

        # --- 1H OKX ---
        if client and budget.okx_ok() and token_okx_used.get(addr, 0) < S002.CAP_PER_TOKEN_OKX:
            series_1h, _ = _okx_candles_paginated(
                client, addr, "1H", token_okx_used, t_min, t_max, max_pages=4
            )
            if series_1h and len(series_1h) >= 3:
                # Use looser cover check for hourly
                ts0 = series_1h[0][0]
                ts1 = series_1h[-1][0]
                if ts0 <= t_min + 2 * BAR_1H_MS and ts1 >= t_max - 2 * BAR_1H_MS:
                    cache[addr] = (series_1h, "okx_candles_1h")
                    continue

        # --- Gecko minute ---
        gecko_pool = str(cov.get("gecko_terminal_pool_id") or "")
        if not gecko_pool:
            _gk, p2 = S002.gecko_ohlcv_available(budget, addr)
            gecko_pool = str(p2 or "")
        if gecko_pool:
            gser = S002.gecko_fetch_window(
                budget, gecko_pool, before_ts_sec=int(t_max // 1000) + 180, limit=500
            )
            if gser and len(gser) >= 5 and S002.series_covers_window(gser, t_min, t_max):
                cache[addr] = (gser, "gecko_terminal")
                continue

            # --- Gecko hourly fallback ---
            if not getattr(budget, "gecko_blocked", False) and budget.gecko < S002.CAP_GECKO_SOFT:
                h_url = (
                    f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
                    f"{gecko_pool}/ohlcv/hour?aggregate=1&limit=500&currency=usd"
                    f"&before_timestamp={int(t_max // 1000) + 7200}"
                )
                h_doc, h_err = S002.http_json_public(budget, "gecko", h_url)
                if not h_err and h_doc:
                    h_ohlcv = (h_doc.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
                    h_ser: list[tuple[int, float, float]] = []
                    for row in h_ohlcv:
                        if isinstance(row, list) and len(row) >= 5:
                            h_ser.append((int(row[0]) * 1000, float(row[4]), float(row[5]) if len(row) > 5 else 0.0))
                    h_ser.sort(key=lambda x: x[0])
                    if h_ser and len(h_ser) >= 3:
                        ts0h = h_ser[0][0]
                        ts1h = h_ser[-1][0]
                        if ts0h <= t_min + 2 * BAR_1H_MS and ts1h >= t_max - 2 * BAR_1H_MS:
                            cache[addr] = (h_ser, "gecko_terminal_1h")

    return cache


def max_runup(closes: list[float], entry_px: float) -> float | None:
    if not closes or entry_px <= 0:
        return None
    peak = entry_px
    run = 0.0
    for c in closes:
        peak = max(peak, c)
        run = max(run, (peak / entry_px) - 1.0)
    return run


def grid_row_for(
    ev: dict[str, Any],
    series: list[tuple[int, float, float]],
    source: str,
    trades: list[dict[str, Any]],
    cov: dict[str, Any],
    entry_delay: int,
    exit_after: int,
) -> dict[str, Any]:
    ts_str = ev.get("post_time_utc") or ""
    skip = ""
    try:
        ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
        t0_ms = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
    except ValueError:
        skip = "BAD_TIMESTAMP"

    # Hourly-resolution sources cannot score sub-60m exit windows reliably
    HOURLY_SOURCES = ("okx_candles_1h", "gecko_terminal_1h")
    if not skip and source in HOURLY_SOURCES and exit_after < 60:
        skip = "HOURLY_RESOLUTION_SHORT_WINDOW_SKIP"

    row_base = {
        "author": ev.get("author", ""),
        "post_id": ev.get("post_id", ""),
        "post_time_utc": ts_str,
        "token_symbol": ev.get("token_symbol", ""),
        "token_address": ev.get("token_address", ""),
        "chain": ev.get("chain", ""),
        "primary_venue": ev.get("primary_venue", ""),
        "data_source_used": source,
        "entry_delay_min": entry_delay,
        "exit_after_min": exit_after,
        "entry_price": "",
        "exit_price": "",
        "return_pct": "",
        "max_drawdown_before_exit": "",
        "max_runup_before_exit": "",
        "volume_change_window": "",
        "tx_count_change_window": "",
        "quote_buy_available": str(bool(cov.get("okx_quote_buy_available"))).lower(),
        "quote_sell_available": str(bool(cov.get("okx_quote_sell_available"))).lower(),
        "skip_reason": skip,
    }
    if skip:
        return row_base

    t_entry_ms = t0_ms + entry_delay * 60 * 1000
    t_exit_ms = t_entry_ms + exit_after * 60 * 1000
    entry_px = S002.price_at_or_before(series, t_entry_ms)
    exit_px = S002.price_at_or_before(series, t_exit_ms)
    if entry_px is None or entry_px <= 0:
        row_base["skip_reason"] = "NO_ENTRY_PRICE"
        return row_base
    if exit_px is None or exit_px <= 0:
        row_base["skip_reason"] = "NO_EXIT_PRICE"
        return row_base

    fwd = S002.slice_forward(series, t_entry_ms, exit_after * 60 * 1000)
    closes = [c for _, c, _ in fwd]
    if len(closes) < 2:
        row_base["skip_reason"] = "GRID_INSUFFICIENT_BARS"
        return row_base

    ret = (exit_px / entry_px) - 1.0
    mdd = S002.max_drawdown_from_closes(closes)
    mru = max_runup(closes, entry_px)

    vol_fwd = sum(v for ts, _, v in series if t_entry_ms <= ts <= t_exit_ms)
    vol_back = sum(
        v for ts, _, v in series if t_entry_ms - exit_after * 60 * 1000 <= ts < t_entry_ms
    )
    if vol_back > 0:
        vc = (vol_fwd / vol_back) - 1.0
    elif vol_fwd > 0:
        vc = float("inf")
    else:
        vc = float("nan")

    tx_ch = ""
    if trades:
        wsec = exit_after * 60
        cnt = S002.compute_trade_counts(trades, t_entry_ms, [wsec])
        after, before = cnt.get(wsec, (0, 0))
        if before > 0:
            tx_ch = f"{(after / before) - 1.0:.6f}"
        elif after > 0:
            tx_ch = "inf"

    row_base.update(
        {
            "entry_price": f"{entry_px:.10g}",
            "exit_price": f"{exit_px:.10g}",
            "return_pct": f"{ret:.6f}",
            "max_drawdown_before_exit": f"{mdd:.6f}" if mdd is not None else "",
            "max_runup_before_exit": f"{mru:.6f}" if mru is not None else "",
            "volume_change_window": f"{vc:.6f}" if math.isfinite(vc) else ("inf" if vc > 0 else ""),
            "tx_count_change_window": tx_ch,
            "skip_reason": "",
        }
    )
    return row_base


def profit_factor(returns: list[float]) -> float | None:
    pos = sum(r for r in returns if r > 0)
    neg = sum(r for r in returns if r < 0)
    if neg == 0:
        return None if pos == 0 else float("inf")
    return pos / abs(neg)


def agg_block(name: str, rows: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    rets: list[float] = []
    mdds: list[float] = []
    for r in rows:
        if (r.get("skip_reason") or "").strip():
            continue
        try:
            rets.append(float(r["return_pct"]))
            if (r.get("max_drawdown_before_exit") or "").strip():
                mdds.append(float(r["max_drawdown_before_exit"]))
        except ValueError:
            continue
    n = len(rets)
    authors = [r.get("author") or "" for r in rows if not (r.get("skip_reason") or "").strip()]
    tokens = [
        (r.get("token_address") or "").lower()
        for r in rows
        if not (r.get("skip_reason") or "").strip()
    ]
    src = [r.get("data_source_used") or "unknown" for r in rows if not (r.get("skip_reason") or "").strip()]
    ac = Counter(authors)
    tc = Counter(tokens)
    sc = Counter(src)
    top_a = max(ac.values()) / n if n else 0.0
    top_t = max(tc.values()) / n if n else 0.0
    pf = profit_factor(rets)
    out: dict[str, Any] = {
        "slice_name": name,
        "group_keys": json.dumps(keys),
        "n": n,
        "hit_rate": (sum(1 for x in rets if x > 0) / n) if n else 0.0,
        "mean_return": statistics.mean(rets) if n else 0.0,
        "median_return": statistics.median(rets) if n else 0.0,
        "profit_factor": pf if pf is not None and math.isfinite(float(pf)) else "",
        "max_drawdown_avg": statistics.mean(mdds) if mdds else "",
        "best_return": max(rets) if n else "",
        "worst_return": min(rets) if n else "",
        "concentration_top_author_share": round(top_a, 4),
        "concentration_top_token_share": round(top_t, 4),
        "data_source_distribution": dict(sc),
    }
    return out


def subset_exclude_top_author(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    authors = [r.get("author") for r in rows if not (r.get("skip_reason") or "").strip()]
    if not authors:
        return []
    top = Counter(authors).most_common(1)[0][0]
    return [r for r in rows if (r.get("author") or "") != top]


def subset_exclude_top_token(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    toks = [
        (r.get("token_address") or "").lower()
        for r in rows
        if not (r.get("skip_reason") or "").strip()
    ]
    if not toks:
        return []
    top = Counter(toks).most_common(1)[0][0]
    return [r for r in rows if (r.get("token_address") or "").lower() != top]


def dedupe_event_rows(valid_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One representative row per (post_id, token_address) for concentration stats."""
    m: dict[tuple[str, str], dict[str, Any]] = {}
    for r in valid_rows:
        k = (str(r.get("post_id")), str(r.get("token_address") or "").lower())
        m.setdefault(k, r)
    return list(m.values())


def economic_verdict(valid_n: int, conc40: bool, conc60: bool) -> str:
    if valid_n < 12:
        return "INCONCLUSIVE_SAMPLE_TOO_SMALL"
    if valid_n < 30:
        if valid_n >= 12 and conc60:
            return "FAIL_WEAK_SAMPLE"
        return "WEAK_PASS_OR_INCONCLUSIVE"
    if conc60:
        return "FAIL_NO_GENERALIZATION_GATE"
    if conc40:
        return "DIAGNOSTIC_PASS_WITH_CONCENTRATION_RISK"
    return "DIAGNOSTIC_PASS_BAND"


def write_coverage_csv(coverage_rows: list[dict[str, Any]]) -> None:
    if not coverage_rows:
        return
    cols = list(coverage_rows[0].keys())
    with DATA_SOURCE_COVERAGE_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in coverage_rows:
            w.writerow({k: r.get(k, "") for k in cols})


def main() -> int:
    budget = S002.CallBudget()
    attach_budget_flags(budget)

    key_ok, sec_ok, pw_ok, _pid = S002.env_credentials()
    auth_ok = key_ok and sec_ok and pw_ok
    client = S002.OKXClient(budget) if auth_ok else None

    s001_posts = load_s001_raw()
    ext_map = discover_extension_handles(s001_posts, {_norm_handle(h) for h in PRIORITY_HANDLES})

    bearer = load_bearer()
    x_note = ""
    x_rows: list[dict[str, Any]] = []
    if bearer:
        x_rows, x_note = fetch_kol_posts_x(PRIORITY_HANDLES, bearer)

    merged = merge_posts(s001_posts, x_rows, ext_map)
    write_raw_posts(merged)

    aliases = load_aliases_flat()
    vmap = load_venue_map()
    resolved, alias_cands = build_resolved_events(merged, aliases, vmap, budget)
    write_alias_candidates(alias_cands)

    chain_ok = False
    if client and budget.okx_ok():
        chain_ok, _ = S002.okx_chain_liquidity_sources(client)

    token_okx_used: dict[str, int] = {}
    coverage_by_addr: dict[str, dict[str, Any]] = {}
    symbols_by_addr: dict[str, str] = {}

    for ev in resolved:
        addr = (ev.get("token_address") or "").strip().lower()
        sym = (ev.get("token_symbol") or addr[:8]).upper()
        if addr:
            symbols_by_addr.setdefault(addr, sym)

    for addr in sorted(symbols_by_addr.keys()):
        if not budget.okx_ok():
            break
        sym = symbols_by_addr[addr]
        row = S002.token_coverage_row(
            client, budget, sym, addr, chain_ok, token_okx_used
        )
        coverage_by_addr[addr] = row

    write_coverage_csv(list(coverage_by_addr.values()))

    with RESOLVED_EVENTS_CSV.open("w", newline="", encoding="utf-8") as f:
        cols = [
            "author",
            "post_id",
            "post_time_utc",
            "token_symbol",
            "token_address",
            "chain",
            "primary_venue",
            "resolution_source",
            "resolution_confidence",
            "extension_reason",
            "text_hash_short",
        ]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for e in resolved:
            w.writerow({k: e.get(k, "") for k in cols})

    series_cache = build_series_cache_extended(
        resolved, coverage_by_addr, client, budget, token_okx_used
    )
    trades_cache = S002.fetch_trades_cache(
        client, budget, coverage_by_addr, token_okx_used
    )

    grid_rows: list[dict[str, Any]] = []
    for ev in resolved:
        addr = (ev.get("token_address") or "").strip().lower()
        cov = coverage_by_addr.get(addr, {})
        cached = series_cache.get(addr)
        trades = trades_cache.get(addr, [])
        if not cached:
            for ed in ENTRY_DELAYS:
                for xa in EXIT_AFTERS:
                    grid_rows.append(
                        {
                            "author": ev.get("author", ""),
                            "post_id": ev.get("post_id", ""),
                            "post_time_utc": ev.get("post_time_utc", ""),
                            "token_symbol": ev.get("token_symbol", ""),
                            "token_address": addr,
                            "chain": ev.get("chain", ""),
                            "primary_venue": ev.get("primary_venue", ""),
                            "data_source_used": "",
                            "entry_delay_min": ed,
                            "exit_after_min": xa,
                            "entry_price": "",
                            "exit_price": "",
                            "return_pct": "",
                            "max_drawdown_before_exit": "",
                            "max_runup_before_exit": "",
                            "volume_change_window": "",
                            "tx_count_change_window": "",
                            "quote_buy_available": str(
                                bool(cov.get("okx_quote_buy_available"))
                            ).lower(),
                            "quote_sell_available": str(
                                bool(cov.get("okx_quote_sell_available"))
                            ).lower(),
                            "skip_reason": "NO_PRICE_SERIES",
                        }
                    )
            continue
        ser, src = cached
        for ed in ENTRY_DELAYS:
            for xa in EXIT_AFTERS:
                grid_rows.append(grid_row_for(ev, ser, src, trades, cov, ed, xa))

    gcols = [
        "author",
        "post_id",
        "post_time_utc",
        "token_symbol",
        "token_address",
        "chain",
        "primary_venue",
        "data_source_used",
        "entry_delay_min",
        "exit_after_min",
        "entry_price",
        "exit_price",
        "return_pct",
        "max_drawdown_before_exit",
        "max_runup_before_exit",
        "volume_change_window",
        "tx_count_change_window",
        "quote_buy_available",
        "quote_sell_available",
        "skip_reason",
    ]
    with EVENT_GRID_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=gcols)
        w.writeheader()
        for r in grid_rows:
            w.writerow({k: r.get(k, "") for k in gcols})

    valid_rows = [r for r in grid_rows if not (r.get("skip_reason") or "").strip()]
    unique_ev = dedupe_event_rows(valid_rows)
    valid_price_events = len(unique_ev)

    # Aggregates (returns/distributions over grid rows; concentration uses unique events)
    overall = agg_block("overall", valid_rows, [])
    if unique_ev:
        ac_u = Counter(r.get("author") or "" for r in unique_ev)
        tc_u = Counter((r.get("token_address") or "").lower() for r in unique_ev)
        nu = len(unique_ev)
        overall["concentration_top_author_share"] = round(max(ac_u.values()) / nu, 4)
        overall["concentration_top_token_share"] = round(max(tc_u.values()) / nu, 4)
    by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_venue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_ed: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_xa: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_grid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)

    for r in valid_rows:
        by_author[r.get("author") or ""].append(r)
        by_token[(r.get("token_address") or "").lower()].append(r)
        by_venue[r.get("primary_venue") or "unknown"].append(r)
        by_src[r.get("data_source_used") or "unknown"].append(r)
        try:
            ed = int(r["entry_delay_min"])
            xa = int(r["exit_after_min"])
        except ValueError:
            continue
        by_ed[ed].append(r)
        by_xa[xa].append(r)
        by_grid[(ed, xa)].append(r)

    robust_no_token = agg_block("drop_top_token", subset_exclude_top_token(valid_rows), [])
    robust_no_author = agg_block("drop_top_author", subset_exclude_top_author(valid_rows), [])

    grid_summaries: list[dict[str, Any]] = []
    for ed in ENTRY_DELAYS:
        for xa in EXIT_AFTERS:
            blk = agg_block(f"grid_{ed}_{xa}", by_grid.get((ed, xa), []), [str(ed), str(xa)])
            blk["policy"] = ""
            blk["entry_delay_min"] = ed
            blk["exit_after_min"] = xa
            grid_summaries.append(blk)

    grid_summaries.sort(
        key=lambda z: float(z["mean_return"]) if z["n"] else -1e9, reverse=True
    )
    best_grid = None
    for g in grid_summaries:
        if g["n"] >= 8:
            best_grid = {
                "entry_delay_min": int(g["entry_delay_min"]),
                "exit_after_min": int(g["exit_after_min"]),
                "n": g["n"],
                "mean_return": round(float(g["mean_return"]), 6),
                "hit_rate": round(float(g["hit_rate"]), 4),
            }
            break
    if best_grid is None and grid_summaries:
        g = grid_summaries[0]
        best_grid = {
            "entry_delay_min": int(g.get("entry_delay_min", 0)),
            "exit_after_min": int(g.get("exit_after_min", 0)),
            "n": g["n"],
            "mean_return": round(float(g["mean_return"]), 6) if g["n"] else 0.0,
            "hit_rate": round(float(g["hit_rate"]), 4) if g["n"] else 0.0,
            "note": "low_n_best_mean_diagnostic",
        }

    fixed_policy_rows: list[dict[str, Any]] = []
    for pname, (ed, xa) in FIXED_POLICIES.items():
        sub = by_grid.get((ed, xa), [])
        blk = agg_block(pname, sub, [str(ed), str(xa)])
        blk["policy"] = pname
        blk["entry_delay_min"] = ed
        blk["exit_after_min"] = xa
        fixed_policy_rows.append(blk)

    # policy CSV by author / token / grid — only fixed policies for author/token slices
    pol_author_out: list[dict[str, Any]] = []
    authors_u = sorted({r.get("author") for r in valid_rows if r.get("author")})
    for auth in authors_u:
        for pname, (ed, xa) in FIXED_POLICIES.items():
            sub = [
                r
                for r in valid_rows
                if r.get("author") == auth
                and int(r["entry_delay_min"]) == ed
                and int(r["exit_after_min"]) == xa
            ]
            row = agg_block(f"{auth}:{pname}", sub, [auth, pname])
            row["author"] = auth
            row["policy"] = pname
            pol_author_out.append(row)

    pol_token_out: list[dict[str, Any]] = []
    tokens_u = sorted({(r.get("token_address") or "").lower() for r in valid_rows})
    for tok in tokens_u:
        if not tok:
            continue
        sym = next(
            (r.get("token_symbol") for r in valid_rows if (r.get("token_address") or "").lower() == tok),
            "",
        )
        for pname, (ed, xa) in FIXED_POLICIES.items():
            sub = [
                r
                for r in valid_rows
                if (r.get("token_address") or "").lower() == tok
                and int(r["entry_delay_min"]) == ed
                and int(r["exit_after_min"]) == xa
            ]
            row = agg_block(f"{tok}:{pname}", sub, [tok, pname])
            row["token_address"] = tok
            row["token_symbol"] = sym or ""
            row["policy"] = pname
            pol_token_out.append(row)

    def dump_policy_csv(path: Path, rows_out: list[dict[str, Any]], extras: list[str]) -> None:
        base_cols = [
            "slice_name",
            "group_keys",
            "n",
            "hit_rate",
            "mean_return",
            "median_return",
            "profit_factor",
            "max_drawdown_avg",
            "best_return",
            "worst_return",
            "concentration_top_author_share",
            "concentration_top_token_share",
            "data_source_distribution",
        ]
        cols = extras + [c for c in base_cols if c not in extras]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows_out:
                rr = dict(r)
                if isinstance(rr.get("data_source_distribution"), dict):
                    rr["data_source_distribution"] = json.dumps(
                        rr["data_source_distribution"], sort_keys=True
                    )
                w.writerow({k: rr.get(k, "") for k in cols})

    dump_policy_csv(
        POLICY_GRID_CSV,
        grid_summaries + fixed_policy_rows,
        ["policy", "entry_delay_min", "exit_after_min"],
    )
    dump_policy_csv(POLICY_AUTHOR_CSV, pol_author_out, ["author", "policy"])
    dump_policy_csv(POLICY_TOKEN_CSV, pol_token_out, ["token_address", "token_symbol", "policy"])

    src_ctr = Counter(r.get("data_source_used") or "unknown" for r in valid_rows)
    n_sr = len(valid_rows)
    okx_share_grid = (src_ctr.get("okx_market_candles", 0) / n_sr) if n_sr else 0.0

    conc_auth = float(overall["concentration_top_author_share"])
    conc_tok = float(overall["concentration_top_token_share"])
    concentration_risk = conc_auth > 0.4 or conc_tok > 0.4
    no_general_alpha = conc_auth > 0.6 or conc_tok > 0.6

    qb_any = sum(
        1 for r in coverage_by_addr.values() if r.get("okx_quote_buy_available")
    )
    qs_any = sum(
        1 for r in coverage_by_addr.values() if r.get("okx_quote_sell_available")
    )
    quote_limited = qb_any == 0 or qs_any == 0

    src_u = Counter(r.get("data_source_used") or "unknown" for r in unique_ev)
    nu = len(unique_ev)
    okx_share_unique = (src_u.get("okx_market_candles", 0) / nu) if nu else 0.0
    data_source_risk = nu > 0 and okx_share_unique < 0.5

    verdict = economic_verdict(valid_price_events, concentration_risk, no_general_alpha)

    top_author = Counter(r.get("author") for r in unique_ev).most_common(1)
    top_token = Counter((r.get("token_address") or "").lower() for r in unique_ev).most_common(
        1
    )

    best_fixed_row: dict[str, Any] | None = None
    best_mr = -1e18
    for r in fixed_policy_rows:
        try:
            n = int(r["n"])
            mr = float(r["mean_return"]) if n else -1e18
        except (TypeError, ValueError):
            mr = -1e18
            n = 0
        if n <= 0:
            continue
        if mr > best_mr:
            best_mr = mr
            best_fixed_row = r

    best_fixed_policy_payload: dict[str, Any] = {}
    if best_fixed_row:
        best_fixed_policy_payload = {
            "policy": best_fixed_row.get("policy"),
            "entry_delay_min": best_fixed_row.get("entry_delay_min"),
            "exit_after_min": best_fixed_row.get("exit_after_min"),
            "n": best_fixed_row.get("n"),
            "mean_return": best_fixed_row.get("mean_return"),
            "hit_rate": best_fixed_row.get("hit_rate"),
        }

    data_limited = (
        len(merged) < 300 or len(resolved) < 50 or valid_price_events < 30
    )

    flags = {
        "sprint": "S003",
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "constraints_respected": {
            "no_redis_signals": True,
            "no_executor": True,
            "no_live_trading": True,
            "no_model_training": True,
            "no_private_keys": True,
            "no_swap_broadcast": True,
            "no_x_web_scrape": True,
        },
        "sample_targets": {
            "raw_posts": len(merged),
            "raw_posts_target": 300,
            "resolved_token_events": len(resolved),
            "resolved_token_events_target": 50,
            "valid_price_events": valid_price_events,
            "valid_price_events_target": 30,
            "DATA_LIMITED": data_limited,
        },
        "budget_counters": {
            "OKX_CALLS_USED": budget.okx,
            "DEX_CALLS_USED": budget.dex,
            "GECKO_CALLS_USED": budget.gecko,
            "dex_blocked": getattr(budget, "dex_blocked", False),
            "gecko_blocked": getattr(budget, "gecko_blocked", False),
        },
        "x_fetch": {
            "attempted": bool(bearer),
            "note": x_note or ("ok" if x_rows else "skipped_no_bearer"),
            "posts_fetched_from_api": len(x_rows),
        },
        "economic_gate": {
            "S003_VERDICT": verdict,
            "VALID_EVENTS": valid_price_events,
            "CONCENTRATION_RISK": concentration_risk,
            "NO_GENERAL_ALPHA_CLAIM": no_general_alpha,
            "DATA_SOURCE_RISK": data_source_risk,
            "QUOTE_LIMITED": quote_limited,
            "QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS": quote_limited,
            "okx_share_among_valid_grid_rows": round(okx_share_grid, 4),
            "okx_share_among_unique_events": round(okx_share_unique, 4),
        },
        "diagnostics": {
            "BEST_DIAGNOSTIC_GRID": best_grid or {},
            "BEST_FIXED_POLICY": best_fixed_policy_payload,
            "TOP_AUTHOR": top_author[0][0] if top_author else "",
            "TOP_TOKEN": top_token[0][0] if top_token else "",
            "TOP_AUTHOR_SHARE": round(conc_auth, 4),
            "TOP_TOKEN_SHARE": round(conc_tok, 4),
            "aggregate_overall": {k: overall[k] for k in overall if k != "data_source_distribution"}
            | {"data_source_distribution": dict(src_ctr)},
            "robustness_drop_top_token": {k: robust_no_token[k] for k in robust_no_token if k != "data_source_distribution"},
            "robustness_drop_top_author": {
                k: robust_no_author[k]
                for k in robust_no_author
                if k != "data_source_distribution"
            },
        },
        "inherits": {
            "S001_verdict_research_only": True,
            "S002_data_smoke_not_alpha": True,
        },
        "S003_VERDICT": verdict,
        "BEST_DIAGNOSTIC_GRID": best_grid or {},
        "BEST_FIXED_POLICY": best_fixed_policy_payload,
        "TOP_AUTHOR": top_author[0][0] if top_author else "",
        "TOP_TOKEN": top_token[0][0] if top_token else "",
        "VALID_EVENTS": valid_price_events,
        "CONCENTRATION_RISK": concentration_risk,
        "DATA_SOURCE_RISK": data_source_risk,
        "QUOTE_LIMITED": quote_limited,
        "NO_GENERAL_ALPHA_CLAIM": no_general_alpha,
        "NEXT_ACTION": (
            "Collect more historical posts (offline expansion) or widen alias coverage; "
            "rerun when VALID_EVENTS clears 30 without concentration breach."
            if data_limited or verdict.startswith("INCONCLUSIVE")
            else "Treat grid winners as diagnostics only; optionally freeze fixed policies for paper simulation."
        ),
    }
    FLAGS_JSON.write_text(json.dumps(flags, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# S003 — KOL Social Catalyst Large Event Backtest",
        "",
        f"- Generated UTC: `{flags['generated_at_utc']}`",
        "",
        "## Sample counts",
        "",
        f"- raw_posts: **{len(merged)}** (target 300+)",
        f"- resolved_token_events: **{len(resolved)}** (target 50+)",
        f"- valid_price_events (unique post×token with ≥1 filled grid cell): **{valid_price_events}** (target 30+)",
        f"- DATA_LIMITED: **{data_limited}**",
        "",
        "## Budget",
        "",
        f"- OKX calls: **{budget.okx}** / {CAP_OKX_HARD_S003}",
        f"- DexScreener calls: **{budget.dex}** / {CAP_DEX_SOFT_S003}",
        f"- GeckoTerminal calls: **{budget.gecko}** / {CAP_GECKO_SOFT_S003}",
        "",
        "## Decision flags (see `decision_flags.json`)",
        "",
        f"- S003_VERDICT: **{verdict}**",
        f"- QUOTE_LIMITED: **{quote_limited}**",
        f"- DATA_SOURCE_RISK: **{data_source_risk}**",
        f"- CONCENTRATION_RISK: **{concentration_risk}**",
        f"- NO_GENERAL_ALPHA_CLAIM: **{no_general_alpha}**",
        "",
        "### Diagnostics (not alpha claims)",
        "",
        f"- BEST_DIAGNOSTIC_GRID: `{json.dumps(best_grid, sort_keys=True)}`",
        f"- BEST_FIXED_POLICY: `{json.dumps(best_fixed_policy_payload, sort_keys=True)}`",
        "",
        "## Artifacts",
        "",
        f"- `{RAW_KOL_POSTS_CSV.relative_to(REPO_ROOT)}`",
        f"- `{EVENT_GRID_CSV.relative_to(REPO_ROOT)}`",
        f"- `{FLAGS_JSON.relative_to(REPO_ROOT)}`",
        "",
    ]
    READOUT_MD.write_text("\n".join(lines), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
